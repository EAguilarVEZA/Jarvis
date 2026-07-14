"""
server.py — Phase 3 dispatch integration snippet

This file is NOT meant to be run. It shows the exact changes to make in
~/jarvis/server.py to integrate BuilderSession into the voice dispatch.

Three changes, all safe and additive:

  CHANGE A: Add an import near the top of server.py (around line 54)
  CHANGE B: Add session storage when the WebSocket opens
  CHANGE C: Add the dispatch branch in the user_text handler (around line 2400)

Each change is shown as a "before"/"after" block. Apply by hand or
have your editor's find-and-replace do it. None of these change existing
behavior — they only add a new code path.
"""

# ═══════════════════════════════════════════════════════════════════════
# CHANGE A — Import the new module
# ═══════════════════════════════════════════════════════════════════════
#
# WHERE: Near line 54, alongside the other reporting imports
#
# BEFORE:
#
#     from reporting_module import router as reporting_router, detect_report_trigger, build_voice_summary
#
# AFTER:
#
#     from reporting_module import router as reporting_router, detect_report_trigger, build_voice_summary
#     # Phase 3 — interactive report builder
#     from reporting.catalog import WarehouseCatalog
#     from reporting.bq_client import BQClient as _BuilderBQClient
#     from reporting.builder_session import BuilderSession
#     from reporting.intent_extractor import is_report_trigger as is_builder_trigger
#
#     # Singleton catalog — loaded once at import. ~30MB inventory.json + curated.yaml
#     _builder_catalog = WarehouseCatalog.load()
#     _builder_bq = _BuilderBQClient()


# ═══════════════════════════════════════════════════════════════════════
# CHANGE B — Create a BuilderSession when the WebSocket connects
# ═══════════════════════════════════════════════════════════════════════
#
# WHERE: Inside the WebSocket handler, near where other per-connection
#        state objects are created (planner, work_session, voice_state, etc.)
#        Look around line 2200-2300 in server.py
#
# ADD A LINE LIKE THIS where you can see `planner = ...` or `work_session = ...`:
#
#     # Phase 3 — one builder per WebSocket connection
#     def _builder_runner(sql, params):
#         """Adapter: ReportBuilder wants list[dict]; BQClient returns QueryResult."""
#         qr = _builder_bq.query(sql, params)
#         return qr.rows
#
#     builder_session = BuilderSession(_builder_catalog, query_runner=_builder_runner)


# ═══════════════════════════════════════════════════════════════════════
# CHANGE C — Add the dispatch branch
# ═══════════════════════════════════════════════════════════════════════
#
# WHERE: In the user_text handler, AFTER the planner check (line 2355)
#        and AFTER the "quit work mode" check (line 2398), but BEFORE
#        `elif work_session.active:` (line 2399) and BEFORE
#        `_fast_action = detect_action_fast(user_text)` (line 2408).
#
# This is the critical ordering. We want:
#   1. Planner intercepts first (existing — don't touch)
#   2. "Quit work mode" intercepts second (existing — don't touch)
#   3. >>> NEW: Builder mode intercepts third <<<
#   4. Work mode (existing)
#   5. Fast actions including show_report (existing)
#   6. LLM fallback (existing)
#
# This ordering means:
#   - "let's build a report" → enters builder (path 3)
#   - "show me the dashboard" → falls through to fast action (path 5) — existing path
#   - "what's our CPA for cardiology" → falls through to LLM/fast action (existing)
#
# CODE TO INSERT — paste right BEFORE `elif work_session.active:`:

# ─── BUILDER MODE: interactive report construction ─────────────
elif builder_session.in_builder_mode or is_builder_trigger(user_text):
    try:
        if builder_session.in_builder_mode:
            ws_msgs = builder_session.handle_input(user_text)
        else:
            ws_msgs = builder_session.start(user_text)

        # Send all WS messages. The LAST one's _spoken key tells us what Jarvis says aloud.
        spoken = None
        for m in ws_msgs:
            spoken = m.pop("_spoken", spoken) or spoken  # carry forward last _spoken
            await ws.send_json(m)

        if spoken:
            # Reuse existing TTS pipeline — synthesize_speech is already in server.py
            audio = await synthesize_speech(spoken)
            if audio:
                await ws.send_json({"type": "status", "state": "speaking"})
                await ws.send_json({
                    "type": "audio",
                    "data": base64.b64encode(audio).decode(),
                    "text": spoken,
                })

        # Set response_text so the downstream "response done" logging works
        response_text = spoken or "(builder action)"
    except Exception as e:
        log.exception("Builder error")
        await ws.send_json({"type": "builder:error", "message": str(e)})
        await ws.send_json({"type": "builder:done", "reason": "error"})
        response_text = f"Builder error: {e}"
# ────────────────────────────────────────────────────────────────


# ═══════════════════════════════════════════════════════════════════════
# Verification after applying
# ═══════════════════════════════════════════════════════════════════════
#
# 1. Restart Jarvis:    cd ~/jarvis && ./jarvis restart
#
# 2. Tail the backend log in another terminal:
#    tail -f ~/jarvis/logs/backend.log
#
# 3. From the browser at http://localhost:5174 say:
#    "Jarvis, let's build a report"
#
#    You should see in the log:
#      User: let's build a report
#      Builder session START: "let's build a report"
#
#    And via WebSocket the frontend receives:
#      builder:state, builder:question
#
#    The frontend doesn't render anything YET (that's Session 2).
#    But the browser DevTools console will show the messages arriving.
#
# 4. To verify existing paths still work, say:
#    "Show me the marketing dashboard"
#
#    The existing path should fire — builder_session.in_builder_mode is False,
#    is_builder_trigger("show me the marketing dashboard") is False (verified
#    in smoke test), so the dispatch falls through to its old behavior.
