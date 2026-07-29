"""
utils/email.py
───────────────
Minimal transactional email sending — currently used for password reset codes.

WHY STDLIB smtplib INSTEAD OF A THIRD-PARTY LIBRARY:
  • No new dependency to install — works with any SMTP provider (Gmail app
    password, SendGrid, Mailgun SMTP relay, AWS SES SMTP, etc.) by just
    setting SMTP_* values in .env.
  • Keeps this pharmacy install self-contained, consistent with the rest
    of the project's "no extra moving parts" approach.

DEV/LOCAL FALLBACK:
  If SMTP_HOST is not configured, the email is logged instead of sent,
  so password reset can be tested without setting up real email —
  the code shows up in the server console/log.
"""

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


def send_email(to_email: str, subject: str, body_text: str, body_html: str | None = None) -> bool:
    """
    Send an email. Returns True if sent (or logged in dev-fallback mode),
    False if a real SMTP send was attempted and failed.

    Never raises — callers (e.g. forgot-password) must not fail or leak
    whether an address exists just because email delivery had a hiccup.
    """
    if not settings.SMTP_HOST:
        # Dev/local fallback: no SMTP configured — log instead of sending.
        logger.warning(
            "SMTP not configured — logging email instead of sending.\n"
            "  To: %s\n  Subject: %s\n  Body:\n%s",
            to_email, subject, body_text,
        )
        return True

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"{settings.SMTP_FROM_NAME} <{settings.SMTP_FROM_EMAIL}>"
        msg["To"] = to_email
        msg.attach(MIMEText(body_text, "plain"))
        if body_html:
            msg.attach(MIMEText(body_html, "html"))

        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=10) as server:
            if settings.SMTP_USE_TLS:
                server.starttls()
            if settings.SMTP_USER:
                server.login(settings.SMTP_USER, settings.SMTP_PASSWORD.get_secret_value())
            server.sendmail(settings.SMTP_FROM_EMAIL, [to_email], msg.as_string())
        return True

    except Exception as exc:
        logger.error("Failed to send email to %s: %s", to_email, exc)
        return False


def send_password_reset_code(to_email: str, full_name: str, code: str, expire_minutes: int) -> bool:
    """Send a password reset code email using a simple, clear template."""
    subject = "Your password reset code"
    text = (
        f"Hi {full_name},\n\n"
        f"Your password reset code is: {code}\n\n"
        f"This code expires in {expire_minutes} minutes. "
        f"If you didn't request this, you can safely ignore this email.\n"
    )
    html = f"""
    <div style="font-family:sans-serif;max-width:420px;margin:0 auto">
      <p>Hi {full_name},</p>
      <p>Your password reset code is:</p>
      <p style="font-size:28px;font-weight:700;letter-spacing:6px;background:#f3f4f6;
                padding:14px 20px;border-radius:8px;text-align:center">{code}</p>
      <p style="color:#6b7280;font-size:13px">
        This code expires in {expire_minutes} minutes. If you didn't request this,
        you can safely ignore this email.
      </p>
    </div>
    """
    return send_email(to_email, subject, text, html)
