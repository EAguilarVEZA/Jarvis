"""
smtp_mailer — minimal SMTP sender for scheduled-research / brief delivery.

Configuration comes entirely from the environment so no secrets live in code:
    SMTP_HOST   (required)     e.g. smtp.gmail.com
    SMTP_FROM   (required)     e.g. jarvis@yourdomain.com
    SMTP_PORT   (default 587)  465 → implicit SSL; else STARTTLS unless SMTP_TLS=false
    SMTP_USER / SMTP_PASS      (optional) for authenticated relays
    SMTP_TLS    (default true)

Until SMTP_HOST + SMTP_FROM are set, is_configured() is False and callers skip
sending — so this is safe to wire in now and it simply activates once configured.
"""
from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage

log = logging.getLogger("jarvis.mailer")


def _cfg() -> dict:
    return {
        "host": os.getenv("SMTP_HOST") or os.getenv("EMAIL_HOST"),
        "port": int(os.getenv("SMTP_PORT") or os.getenv("EMAIL_PORT") or 587),
        "user": os.getenv("SMTP_USER") or os.getenv("EMAIL_USER"),
        "pw": os.getenv("SMTP_PASS") or os.getenv("SMTP_PASSWORD") or os.getenv("EMAIL_PASS"),
        "sender": os.getenv("SMTP_FROM") or os.getenv("EMAIL_FROM"),
        "tls": (os.getenv("SMTP_TLS", "true").lower() != "false"),
    }


def is_configured() -> bool:
    c = _cfg()
    return bool(c["host"] and c["sender"])


def _recipients(to) -> list:
    if isinstance(to, (list, tuple)):
        items = list(to)
    else:
        items = str(to or "").replace(";", ",").split(",")
    return [t.strip() for t in items if t and t.strip()]


def send(to, subject: str, body_text: str, body_html: str | None = None,
         attachments: list | None = None) -> list:
    """Send one email (blocking — call via a thread executor from async code).

    attachments: optional list of (filename, data_bytes, mime_type) tuples.
    mime_type like "application/pdf" is split into maintype/subtype.
    """
    c = _cfg()
    if not (c["host"] and c["sender"]):
        raise RuntimeError("SMTP not configured — set SMTP_HOST and SMTP_FROM.")
    rcpts = _recipients(to)
    if not rcpts:
        raise RuntimeError("No recipients.")
    msg = EmailMessage()
    msg["From"] = c["sender"]
    msg["To"] = ", ".join(rcpts)
    msg["Subject"] = subject
    msg.set_content(body_text or "")
    if body_html:
        msg.add_alternative(body_html, subtype="html")
    for att in (attachments or []):
        try:
            fname, data, mime = att
            maintype, _, subtype = (mime or "application/octet-stream").partition("/")
            msg.add_attachment(data, maintype=maintype or "application",
                               subtype=subtype or "octet-stream", filename=fname)
        except Exception as e:
            log.warning("attachment skipped: %s", e)
    ctx = ssl.create_default_context()
    if c["port"] == 465:
        with smtplib.SMTP_SSL(c["host"], c["port"], context=ctx, timeout=20) as s:
            if c["user"]:
                s.login(c["user"], c["pw"] or "")
            s.send_message(msg)
    else:
        with smtplib.SMTP(c["host"], c["port"], timeout=20) as s:
            if c["tls"]:
                s.starttls(context=ctx)
            if c["user"]:
                s.login(c["user"], c["pw"] or "")
            s.send_message(msg)
    log.info("email sent to %s: %s", rcpts, subject)
    return rcpts


def default_from() -> str:
    return _cfg()["sender"] or ""
