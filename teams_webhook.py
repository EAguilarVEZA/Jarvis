"""
teams_webhook.py — post a Martin brief to a Microsoft Teams channel via an
Incoming Webhook. Set TEAMS_WEBHOOK_URL in .env (the URL Teams gives you when
you add a channel's "Incoming Webhook" connector). No credentials are stored
here — just the webhook URL.

post(title, markdown_text) -> True on HTTP 2xx, else False (never raises).
"""
from __future__ import annotations
import os
import json
import logging
import urllib.request
import urllib.error

log = logging.getLogger("teams_webhook")


def _url():
    return (os.environ.get("TEAMS_WEBHOOK_URL") or "").strip()


def enabled():
    return _url().startswith("http")


def post(title, markdown_text, theme="78D4FF"):
    url = _url()
    if not url.startswith("http"):
        return False
    # Legacy Office 365 Connector "MessageCard" — what Incoming Webhooks accept.
    card = {
        "@type": "MessageCard",
        "@context": "http://schema.org/extensions",
        "summary": title or "Martin brief",
        "themeColor": theme,
        "title": title or "Martin brief",
        # render_text already spaces items with blank lines — send as-is.
        "text": markdown_text or "",
    }
    data = json.dumps(card).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return 200 <= r.status < 300
    except urllib.error.URLError as e:
        log.warning("teams webhook post failed: %s", e)
        return False
    except Exception as e:  # noqa
        log.warning("teams webhook post error: %s", e)
        return False
