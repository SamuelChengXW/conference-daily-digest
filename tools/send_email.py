"""Send the rendered digest email.

Primary path: Resend's HTTPS API (send-to-self sandbox mode — no domain
verification needed since this only ever emails the user's own address).
Reason this beats Gmail SMTP for unattended sending from GitHub Actions:
SMTP logins from CI's rotating datacenter IPs are a known source of
intermittent Google security holds, which nobody is present to clear.

`send_via_gmail_smtp()` is kept as a documented, working fallback — not
wired into run_pipeline.py by default. Switch to it in run_pipeline.py if
Resend ever becomes unsuitable (e.g. you want to send to more than one
recipient, which requires Resend domain verification anyway).

Requires RESEND_API_KEY (and EMAIL_TO) as env vars — see .env.example.
"""
from __future__ import annotations

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

import requests
from dotenv import load_dotenv

from common import PROJECT_ROOT, load_config, today

load_dotenv(PROJECT_ROOT / ".env")

RESEND_API_URL = "https://api.resend.com/emails"


def send_via_resend(html_body: str, subject: str, to_email: str, api_key: str) -> bool:
    resp = requests.post(
        RESEND_API_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            # Resend's shared sandbox sender — fine for send-to-self use;
            # switch to a verified domain address if you ever add recipients.
            "from": "Conference Digest <onboarding@resend.dev>",
            "to": [to_email],
            "subject": subject,
            "html": html_body,
        },
        timeout=30,
    )
    if resp.status_code >= 300:
        print(f"Resend send failed: {resp.status_code} {resp.text}")
        return False
    return True


def send_via_gmail_smtp(html_body: str, subject: str, to_email: str,
                         gmail_address: str, app_password: str) -> bool:
    """Documented fallback, not called by run_pipeline.py by default."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = gmail_address
    msg["To"] = to_email
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(gmail_address, app_password)
            server.sendmail(gmail_address, [to_email], msg.as_string())
        return True
    except smtplib.SMTPException as e:
        print(f"Gmail SMTP send failed: {e}")
        return False


def run(html_body: str, config: Optional[dict] = None) -> bool:
    config = config or load_config()
    subject = config["email"]["subject_template"].format(date=today().isoformat())

    to_email = os.environ.get("EMAIL_TO")
    if not to_email:
        print("EMAIL_TO not set — skipping send (set it in .env for local runs "
              "or as a GitHub Actions secret). Deliberately not committed to "
              "config/filters.yaml since this repo is public.")
        return False

    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        print("RESEND_API_KEY not set — skipping send (set it in .env for local runs "
              "or as a GitHub Actions secret).")
        return False

    return send_via_resend(html_body, subject, to_email, api_key)


if __name__ == "__main__":
    from common import DOCS_DIR
    html = (DOCS_DIR / "index.html").read_text(encoding="utf-8")
    ok = run(html)
    print("Email sent." if ok else "Email NOT sent.")
