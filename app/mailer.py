"""Outgoing email: Resend API, or any SMTP server. With neither configured the message is logged instead."""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

import httpx

from app import config

log = logging.getLogger("infoharness.mail")


def send_email(to: str, subject: str, text: str) -> bool:
    s = config.settings
    try:
        if s.resend_api_key:
            r = httpx.post("https://api.resend.com/emails", timeout=15,
                           headers={"Authorization": f"Bearer {s.resend_api_key}"},
                           json={"from": s.mail_from, "to": [to], "subject": subject, "text": text})
            if r.status_code >= 300:
                log.error("resend rejected mail to %s: %s %s", to, r.status_code, r.text[:200])
                return False
            return True
        if s.smtp_host:
            msg = EmailMessage()
            msg["From"], msg["To"], msg["Subject"] = s.mail_from, to, subject
            msg.set_content(text)
            with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=20) as smtp:
                smtp.starttls()
                if s.smtp_user:
                    smtp.login(s.smtp_user, s.smtp_password)
                smtp.send_message(msg)
            return True
    except Exception:
        log.exception("sending mail to %s failed", to)
        return False
    log.warning("EMAIL not configured (set RESEND_API_KEY or SMTP_HOST). Would send to %s: %s\n%s", to, subject, text)
    return False
