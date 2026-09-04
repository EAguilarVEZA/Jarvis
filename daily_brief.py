"""
daily_brief.py
──────────────
Deterministic renderer + rules for Martin's Daily Brief (critical emails +
remaining meetings). ZERO LLM tokens in this module — the scheduled routine
reads Outlook on-screen, hands us structured rows, and we tag + rank + render.

Design decisions (per Edgar):
  * Internal vs External is taken from Outlook's own "External" sender badge
    (Orlando Health stamps external mail), so it needs no domain guessing.
    If a domain IS supplied we can also classify against INTERNAL_DOMAINS.
  * "Critical" is decided by RULES only — deadline/action words, approvals,
    contracts/deals, direct-to-you. No model call.
  * ALKU is a legitimate staffing vendor (time approvals). It is treated as a
    normal External sender — never flagged as phishing.

Public API:
  classify_scope(sender_email=None, external_badge=None) -> "Internal"|"External"
  is_critical(subject, sender=None, direct_to_me=None) -> (bool, reason)
  render(brief) -> str   # self-contained dark HTML fragment for the pill

`brief` shape:
  {
    "slot": "morning"|"midday"|"evening",
    "date_label": "Monday, July 27",
    "as_of": "8:00 AM",
    "include_calendar": True/False,
    "meetings": [ {"time","title","platform","note"(opt),"now"(bool,opt)} ],
    "emails":   [ {"sender","subject","time","scope":"Internal"|"External",
                   "critical":bool,"reason"(opt)} ],
  }
"""
from __future__ import annotations
import os
import re
import html as _html

# Org domains counted as Internal when a real sender address is available.
INTERNAL_DOMAINS = [
    d.strip().lower()
    for d in os.environ.get("BRIEF_INTERNAL_DOMAINS", "orlandohealth.com").split(",")
    if d.strip()
]

# Rules for the deterministic "critical" flag.
_ACTION_WORDS = [
    "action", "final reminder", "reminder", "urgent", "asap", "deadline",
    "response needed", "reply needed", "please respond", "approval", "approve",
    "sign", "signature", "contract", "agreement", "renewal", "invoice",
    "past due", "overdue", "rsvp", "time approval", "expires", "today",
]
# Sender/subject fragments that mark a live deal or exec thread as important.
_DEAL_HINTS = ["mastercard", "adobe", "workfront", "contract", "agreement", "renewal"]


def _esc(s):
    return _html.escape(str(s if s is not None else ""))


def classify_scope(sender_email=None, external_badge=None):
    """External badge from Outlook wins; else fall back to domain check."""
    if external_badge is True:
        return "External"
    if external_badge is False:
        return "Internal"
    if sender_email and "@" in sender_email:
        dom = sender_email.split("@", 1)[1].strip().lower()
        return "Internal" if any(dom == d or dom.endswith("." + d) for d in INTERNAL_DOMAINS) else "External"
    return "External"  # safe default: treat unknown as external


def is_critical(subject, sender=None, direct_to_me=None):
    s = (subject or "").lower()
    who = (sender or "").lower()
    for w in _ACTION_WORDS:
        if w in s:
            return True, f"action/deadline: “{w}”"
    for h in _DEAL_HINTS:
        if h in s or h in who:
            return True, f"active deal thread: {h}"
    if direct_to_me is True:
        return True, "sent directly to you"
    return False, ""


# ────────────────────────── rendering (dark, pill-native) ──────────────────────────
_C = {
    "navy": "#ffffff", "card": "#ffffff", "line": "#e3e8ef",
    "cyan": "#1f7fd0", "ink": "#1f2b38", "mut": "#64748b",
    "extbg": "#fdece2", "extfg": "#b45309",
    "intbg": "#dcfce7", "intfg": "#15803d",
    "critbg": "#fee2e2", "critfg": "#b91c1c",
}

# Small labelled pill button style shared by Open / Mute / Hide.
_PILL = ("flex-shrink:0;border:0;cursor:pointer;font-size:10.5px;font-weight:600;"
         "line-height:1;padding:4px 8px;border-radius:6px;")


# Outlook-on-the-web deep links. Screen-reading can't capture per-item IDs, so
# these open the mailbox / calendar (the connector would allow exact-item links).
_OWA_MAIL = os.environ.get("OWA_MAIL_URL", "https://outlook.office.com/mail/")
_OWA_CAL = os.environ.get("OWA_CAL_URL", "https://outlook.office.com/calendar/view/day")

_MUTE_STORE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "brief_mute.json")


def _load_mute():
    try:
        import json
        with open(_MUTE_STORE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_mute(d):
    import json
    try:
        with open(_MUTE_STORE, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
    except Exception:
        pass


def load_muted():
    return [str(s) for s in _load_mute().get("senders", [])]


def load_muted_meetings():
    return [str(s) for s in _load_mute().get("meetings", [])]


def _add(kind, val):
    v = (val or "").strip()
    if not v:
        return
    d = _load_mute(); arr = d.get(kind, [])
    if not any(x.lower() == v.lower() for x in arr):
        arr.append(v); d[kind] = arr[-300:]; _save_mute(d)


def _remove(kind, val):
    v = (val or "").strip().lower()
    d = _load_mute(); d[kind] = [x for x in d.get(kind, []) if x.lower() != v]
    _save_mute(d)


def add_muted(sender): _add("senders", sender)
def remove_muted(sender): _remove("senders", sender)
def add_muted_meeting(title): _add("meetings", title)
def remove_muted_meeting(title): _remove("meetings", title)


def _matches(val, muted):
    s = (val or "").lower()
    return any(mv and mv.lower() in s for mv in muted)


def filter_muted(emails):
    muted = load_muted()
    return list(emails or []) if not muted else [e for e in (emails or []) if not _matches(e.get("sender", ""), muted)]


def filter_muted_meetings(meetings):
    muted = load_muted_meetings()
    return list(meetings or []) if not muted else [m for m in (meetings or []) if not _matches(m.get("title", ""), muted)]


def _parse_time(s):
    """'9:00 AM' -> minutes since midnight, else None."""
    m = re.match(r"\s*(\d{1,2}):(\d{2})\s*([AaPp])[Mm]", s or "")
    if not m:
        return None
    h = int(m.group(1)) % 12
    if m.group(3).lower() == "p":
        h += 12
    return h * 60 + int(m.group(2))


def _fmt_min(t):
    """minutes-since-midnight -> ('10:00 AM', '10:00')."""
    h, mn = divmod(int(t) % (24 * 60), 60)
    ap = "AM" if h < 12 else "PM"
    hh = h % 12 or 12
    return f"{hh}:{mn:02d} {ap}", f"{h:02d}:{mn:02d}"


def _dur_label(d):
    d = int(d); h, mn = divmod(d, 60)
    return (f"{h}h" if h else "") + (f"{mn}m" if mn else "") or f"{d}m"


def _tag(text, bg, fg):
    return (f'<span style="flex-shrink:0;background:{bg};color:{fg};font-size:10.5px;'
            f'font-weight:600;letter-spacing:.02em;padding:2px 7px;border-radius:6px;">{_esc(text)}</span>')


def _email_row(e):
    scope = e.get("scope") or "External"
    is_ext = scope.lower().startswith("ext")
    scope_tag = _tag(scope.upper(), _C["extbg"] if is_ext else _C["intbg"],
                     _C["extfg"] if is_ext else _C["intfg"])
    crit_tag = _tag("CRITICAL", _C["critbg"], _C["critfg"]) if e.get("critical") else ""
    sub = _esc(e.get("subject", ""))
    sender_raw = e.get("sender", "")
    sender = _esc(sender_raw)
    time = _esc(e.get("time", ""))
    reason = _esc(e.get("reason", "")) if e.get("reason") else ""
    meta = " · ".join([x for x in [time, reason] if x])
    # Labelled pills so it's obvious what they do: "Open ↗" and "Mute ✕".
    openb = (f'<button class="brief-open" data-open="{_OWA_MAIL}" title="Open your Outlook mailbox" '
             f'style="{_PILL}background:#e7f0fb;color:#1f7fd0;">Open ↗</button>')
    mute = (f'<button class="brief-mute" data-mute-sender="{sender}" title="Mute this sender — hide their emails from future briefs" '
            f'style="{_PILL}background:#eef1f5;color:#64748b;">Mute ✕</button>')
    actions = f'<span style="margin-left:auto;display:flex;gap:4px;align-items:center;">{openb}{mute}</span>'
    return (
        f'<div class="brief-email" data-sender="{sender}" style="background:{_C["card"]};'
        f'border:1px solid {_C["line"]};border-radius:11px;padding:9px 11px;margin-bottom:7px;">'
        f'<div style="display:flex;gap:7px;align-items:center;flex-wrap:wrap;margin-bottom:3px;">'
        f'{crit_tag}{scope_tag}'
        f'<span style="font-size:13px;font-weight:600;color:{_C["ink"]};">{sender}</span>{actions}</div>'
        f'<div style="font-size:12.5px;color:{_C["ink"]};">{sub}</div>'
        f'<div style="font-size:11px;color:{_C["mut"]};margin-top:2px;">{meta}</div>'
        f'</div>'
    )


def _meeting_row(m):
    now = m.get("now")
    edge = _C["cyan"] if now else _C["line"]
    start = m.get("time", "")
    time = _esc(start)
    title = _esc(m.get("title", ""))
    plat = m.get("platform", "") or ""
    dur = m.get("duration_min")
    # End time (start + duration) drives the "drop finished meetings" filter and
    # is shown so a 1h 9:00 meeting reads as ending at 10:00.
    data_end = ""
    meta_bits = [plat] if plat else []
    sm = _parse_time(start)
    if sm is not None and dur:
        end_disp, data_end = _fmt_min(sm + int(dur))
        meta_bits.append(f"{_dur_label(dur)} · ends {end_disp}")
    elif dur:
        meta_bits.append(_dur_label(dur))
    meta = _esc(" · ".join(meta_bits))
    title_raw = _esc(m.get("title", ""))
    now_tag = _tag("NOW", "#dbeafe", _C["cyan"]) if now else ""
    openb = (f'<button class="brief-open" data-open="{_OWA_CAL}" title="Open your Outlook calendar" '
             f'style="{_PILL}background:#e7f0fb;color:#1f7fd0;">Open ↗</button>')
    hideb = (f'<button class="brief-hide" data-hide-title="{title_raw}" title="Hide this meeting from future briefs" '
             f'style="{_PILL}background:#eef1f5;color:#64748b;">Hide ✕</button>')
    actions = f'<span style="flex-shrink:0;display:flex;gap:4px;align-items:center;">{now_tag}{openb}{hideb}</span>'
    return (
        f'<div class="brief-meeting" data-end="{data_end}" data-title="{title_raw}" style="background:{_C["card"]};border:1px solid {edge};'
        f'border-radius:11px;padding:8px 11px;margin-bottom:7px;display:flex;gap:12px;align-items:center;">'
        f'<div style="flex-shrink:0;width:66px;font-size:12.5px;font-weight:600;color:{_C["cyan"]};">{time}</div>'
        f'<div style="flex:1;min-width:0;">'
        f'<div style="font-size:12.5px;font-weight:600;color:{_C["ink"]};">{title}</div>'
        f'<div style="font-size:11px;color:{_C["mut"]};">{meta}</div></div>{actions}</div>'
    )


def _stat(label, value, accent=None):
    color = accent or _C["ink"]
    return (f'<div style="flex:1;background:{_C["card"]};border:1px solid {_C["line"]};'
            f'border-radius:10px;padding:8px 10px;text-align:center;">'
            f'<div style="font-size:20px;font-weight:700;color:{color};line-height:1;">{value}</div>'
            f'<div style="font-size:10.5px;color:{_C["mut"]};margin-top:3px;">{_esc(label)}</div></div>')


_SLOT_TITLE = {"morning": "Morning brief", "midday": "Midday brief", "evening": "Evening brief"}


def render(brief):
    slot = brief.get("slot", "morning")
    emails = filter_muted(brief.get("emails", []) or [])
    meetings = filter_muted_meetings(brief.get("meetings", []) or [])
    include_cal = brief.get("include_calendar", True)
    crit = [e for e in emails if e.get("critical")]
    n_ext = sum(1 for e in emails if (e.get("scope", "").lower().startswith("ext")))
    n_int = len(emails) - n_ext

    title = _SLOT_TITLE.get(slot, "Daily brief")
    head = (
        f'<div style="display:flex;align-items:baseline;justify-content:space-between;gap:8px;margin-bottom:9px;">'
        f'<div><div style="font-size:15px;font-weight:700;color:{_C["ink"]};">{_esc(title)}</div>'
        f'<div style="font-size:11.5px;color:{_C["mut"]};">{_esc(brief.get("date_label",""))}</div></div>'
        f'<div style="font-size:11px;color:{_C["mut"]};">as of {_esc(brief.get("as_of",""))}</div></div>'
    )

    stats = ['<div style="display:flex;gap:7px;margin-bottom:11px;">',
             _stat("Critical", len(crit), _C["critfg"]),
             _stat("External", n_ext, _C["extfg"]),
             _stat("Internal", n_int, _C["intfg"])]
    if include_cal:
        stats.insert(2, _stat("Meetings left", len(meetings), _C["cyan"]))
    stats.append('</div>')

    parts = [f'<div style="font-family:Inter,system-ui,sans-serif;color:{_C["ink"]};">', head, "".join(stats)]

    # Emails: critical first, then the rest.
    ordered = crit + [e for e in emails if not e.get("critical")]
    parts.append(f'<div style="font-size:12px;font-weight:600;color:{_C["cyan"]};margin:2px 0 7px;">Emails</div>')
    if ordered:
        parts.append("".join(_email_row(e) for e in ordered))
    else:
        parts.append(f'<div style="font-size:12px;color:{_C["mut"]};margin-bottom:8px;">No new mail.</div>')

    if include_cal:
        parts.append(f'<div style="font-size:12px;font-weight:600;color:{_C["cyan"]};margin:10px 0 7px;">Meetings — rest of today</div>')
        if meetings:
            parts.append("".join(_meeting_row(m) for m in meetings))
        else:
            parts.append(f'<div style="font-size:12px;color:{_C["mut"]};">Nothing left on the calendar.</div>')

    parts.append("</div>")
    return "".join(parts)


def render_text(brief):
    """Human-friendly summary for Teams / email — one item per line, a blank line
    between every item, clear section headers. Each element of `blocks` becomes
    its own paragraph (joined with a blank line), which is what makes Teams show
    them spaced out one-per-line instead of run together."""
    slot = brief.get("slot", "morning")
    title = _SLOT_TITLE.get(slot, "Daily brief")
    emails = filter_muted(brief.get("emails", []) or [])
    meetings = brief.get("meetings", []) or []
    crit = [e for e in emails if e.get("critical")]
    others = [e for e in emails if not e.get("critical")]

    def _email_line(e):
        bits = [b for b in [e.get("scope", ""), e.get("time", "")] if b]
        tail = ("  ·  " + " · ".join(bits)) if bits else ""
        return f"•  **{e.get('sender','')}** — {e.get('subject','')}{tail}"

    blocks = [f"**{title}**  —  {brief.get('date_label','')}   (as of {brief.get('as_of','')})"]

    blocks.append("**Critical emails**")
    if crit:
        blocks.extend(_email_line(e) for e in crit)
    else:
        blocks.append("_None flagged critical._")

    if others:
        blocks.append("**Other emails**")
        blocks.extend(_email_line(e) for e in others)

    if brief.get("include_calendar", True):
        blocks.append("**Meetings left today**")
        if meetings:
            for m in meetings:
                plat = f"   ·   {m.get('platform')}" if m.get("platform") else ""
                blocks.append(f"•  **{m.get('time','')}**  —  {m.get('title','')}{plat}")
        else:
            blocks.append("_Nothing left on the calendar._")

    return "\n\n".join(blocks)


if __name__ == "__main__":
    demo = {
        "slot": "morning", "date_label": "Monday, July 27", "as_of": "8:00 AM",
        "include_calendar": True,
        "emails": [
            {"sender": "Nadine Hamad", "subject": "FINAL REMINDER - action required", "time": "9:15 AM",
             "scope": "Internal", "critical": True, "reason": "action/deadline"},
            {"sender": "Daniel B. Ruyter", "subject": "MasterCard agreement schedule", "time": "10:38 AM",
             "scope": "External", "critical": True, "reason": "active deal thread"},
            {"sender": "ALKU", "subject": "Time Approval Request", "time": "11:21 AM",
             "scope": "External", "critical": True, "reason": "approval"},
            {"sender": "Jennifer Foley", "subject": "Touch base: contract renewal", "time": "10:08 AM",
             "scope": "External", "critical": True, "reason": "renewal"},
            {"sender": "Maria Tribble", "subject": "Tentative: Orlando Health", "time": "10:51 AM",
             "scope": "External", "critical": False},
        ],
        "meetings": [
            {"time": "11:30 AM", "title": "Craig Nelson intro", "platform": "Microsoft Teams", "now": True},
            {"time": "1:00 PM", "title": "OH QA tool demo", "platform": "Microsoft Teams"},
            {"time": "3:00 PM", "title": "Adobe Workfront AI assistant", "platform": "Microsoft Teams"},
            {"time": "4:00 PM", "title": "MarTech leaders weekly", "platform": "Microsoft Teams"},
        ],
    }
    out = render(demo)
    print("render OK, %d chars" % len(out))
    print("classify ALKU:", classify_scope(external_badge=True))
    print("critical(FINAL REMINDER):", is_critical("FINAL REMINDER - action required"))
    open("/tmp/brief_demo.html", "w").write(
        '<body style="background:#070b10;padding:20px;max-width:440px;margin:auto;">' + out + "</body>")
