"""
push_brief.py — CLI used by the scheduled Daily-Brief routine.

The scheduled run reads Outlook on-screen, writes the structured rows to a JSON
file, then calls this to render (deterministically, zero tokens) and drop the
result into brief_store.json — the SAME file the Martin backend serves at
/api/brief/latest, so the pill pops it. Writing the file directly avoids needing
network access to the Mac's localhost from the sandbox.

Usage:
    python3 push_brief.py <slot> <data.json>
      slot     = morning | midday | evening
      data.json= { "date_label","as_of","include_calendar",
                   "emails":[{sender,subject,time,scope,critical,reason}],
                   "meetings":[{time,title,platform,now}] }

You may omit "critical"/"scope" on emails and let this apply the rules:
  scope    -> from each email's "external": true/false  (Outlook External badge)
  critical -> daily_brief.is_critical(subject, sender)
"""
import sys
import os
import json
import time

import daily_brief

STORE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "brief_store.json")
MAX = 20


def _apply_rules(data):
    for e in data.get("emails", []):
        if "scope" not in e:
            e["scope"] = daily_brief.classify_scope(
                sender_email=e.get("sender_email"),
                external_badge=e.get("external"))
        if "critical" not in e:
            crit, reason = daily_brief.is_critical(e.get("subject", ""), e.get("sender", ""))
            e["critical"] = crit
            if crit and not e.get("reason"):
                e["reason"] = reason
    return data


def main():
    if len(sys.argv) < 3:
        print("usage: push_brief.py <morning|midday|evening> <data.json>")
        sys.exit(2)
    slot = sys.argv[1].strip().lower()
    with open(sys.argv[2], encoding="utf-8") as f:
        data = json.load(f)
    data = _apply_rules(data)
    brief = {
        "slot": slot,
        "date_label": data.get("date_label", ""),
        "as_of": data.get("as_of", ""),
        "include_calendar": data.get("include_calendar", slot != "evening"),
        "emails": data.get("emails", []),
        "meetings": data.get("meetings", []) if slot != "evening" else [],
    }
    html = daily_brief.render(brief)
    text = daily_brief.render_text(brief)

    try:
        with open(STORE, encoding="utf-8") as f:
            store = json.load(f)
    except Exception:
        store = {"briefs": [], "seen": []}
    entry = {"id": f"{slot}-{int(time.time())}", "slot": slot, "html": html, "text": text, "ts": time.time()}
    store["briefs"] = ([entry] + store.get("briefs", []))[:MAX]

    # Best-effort Teams post from here; if the network is blocked, the Martin
    # backend posts it when the pill next polls (see brief_api). Guard so it
    # only goes out once either way.
    try:
        import teams_webhook
        if teams_webhook.enabled():
            if teams_webhook.post(f"Martin — {slot} brief", text):
                store.setdefault("teams_posted", []).append(entry["id"])
                print("also posted to Teams channel")
    except Exception as _e:
        pass

    with open(STORE, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=2)
    print(f"pushed {entry['id']} ({len(brief['emails'])} emails, {len(brief['meetings'])} meetings)")


if __name__ == "__main__":
    main()
