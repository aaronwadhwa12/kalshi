"""
Multi-channel notification dispatcher.

Channels (configured via .env / GitHub Secrets):
  ntfy.sh   — free push notifications to iOS/Android (recommended)
  Email     — SMTP via Gmail or any provider
  Discord   — webhook POST
  iMessage  — macOS only, via osascript (local use)
"""
import smtplib
import subprocess
import sys
from email.mime.text import MIMEText
from typing import Optional

import requests

import config


# ---------------------------------------------------------------------------
# ntfy.sh  (https://ntfy.sh — install the app, subscribe to your topic)
# ---------------------------------------------------------------------------

def send_ntfy(body: str, title: str = "NBA Kalshi Alert",
              priority: str = "high", topic: str = None) -> bool:
    topic = topic or config.NTFY_TOPIC
    if not topic:
        return False
    try:
        resp = requests.post(
            f"https://ntfy.sh/{topic}",
            data=body.encode("utf-8"),
            headers={
                "Title":    title,
                "Priority": priority,
                "Tags":     "basketball,money_with_wings",
            },
            timeout=10,
        )
        return resp.status_code == 200
    except Exception as e:
        print(f"[ntfy] {e}")
        return False


# ---------------------------------------------------------------------------
# Email via SMTP
# ---------------------------------------------------------------------------

def send_email(body: str, subject: str = "NBA Kalshi Alert") -> bool:
    if not config.SMTP_USER or not config.SMTP_PASSWORD or not config.EMAIL_TO:
        return False
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"]    = config.SMTP_USER
        msg["To"]      = config.EMAIL_TO
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as smtp:
            smtp.login(config.SMTP_USER, config.SMTP_PASSWORD)
            smtp.send_message(msg)
        return True
    except Exception as e:
        print(f"[email] {e}")
        return False


# ---------------------------------------------------------------------------
# Telegram Bot API
# ---------------------------------------------------------------------------

def send_telegram(body: str, title: str = "NBA Kalshi Alert") -> bool:
    """
    Send a Telegram message via Bot API.

    Setup (one-time, 2 minutes):
      1. Message @BotFather on Telegram → /newbot → copy the token
      2. Start a chat with your new bot (send it /start)
      3. Get your chat_id: https://api.telegram.org/bot<TOKEN>/getUpdates
      4. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env / GitHub Secrets
    """
    token   = config.TELEGRAM_BOT_TOKEN
    chat_id = config.TELEGRAM_CHAT_ID
    if not token or not chat_id:
        return False
    try:
        text = f"*{title}*\n\n```\n{body}\n```"
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id":    chat_id,
                "text":       text,
                "parse_mode": "Markdown",
            },
            timeout=10,
        )
        return resp.status_code == 200
    except Exception as e:
        print(f"[telegram] {e}")
        return False


# ---------------------------------------------------------------------------
# Discord webhook
# ---------------------------------------------------------------------------

def send_discord(body: str, title: str = "NBA Kalshi Alert") -> bool:
    if not config.DISCORD_WEBHOOK_URL:
        return False
    try:
        # Wrap in a code block so formatting survives Discord's markdown
        payload = {"content": f"**{title}**\n```\n{body}\n```"}
        resp = requests.post(config.DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        return resp.status_code in (200, 204)
    except Exception as e:
        print(f"[discord] {e}")
        return False


# ---------------------------------------------------------------------------
# iMessage (macOS only)
# ---------------------------------------------------------------------------

def send_imessage(body: str, to: str = None) -> bool:
    recipient = to or config.IMESSAGE_TO
    if not recipient:
        return False
    if sys.platform != "darwin":
        print("[iMessage] Skipped — not macOS")
        return False
    try:
        escaped = body.replace("\\", "\\\\").replace('"', '\\"')
        parts   = escaped.split("\n")
        escaped = '" & return & "'.join(parts)
        script  = f'''
tell application "Messages"
    set targetService to 1st service whose service type = iMessage
    set targetBuddy to buddy "{recipient}" of targetService
    send "{escaped}" to targetBuddy
end tell
'''
        result = subprocess.run(["osascript", "-e", script],
                                capture_output=True, text=True)
        return result.returncode == 0
    except Exception as e:
        print(f"[iMessage] {e}")
        return False


# ---------------------------------------------------------------------------
# Dispatcher — tries all configured channels
# ---------------------------------------------------------------------------

def send_notification(body: str, title: str = "NBA Kalshi Alert") -> dict:
    """
    Send via every configured channel. Returns a dict of channel→success.
    Always prints to stdout regardless.
    """
    print(f"\n{'='*50}")
    print(f"{title}")
    print(f"{'='*50}")
    print(body)
    print(f"{'='*50}\n")

    results = {}

    if config.NTFY_TOPIC:
        results["ntfy"] = send_ntfy(body, title)
        if results["ntfy"]:
            print(f"[ntfy] Sent to topic '{config.NTFY_TOPIC}'")

    if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID:
        results["telegram"] = send_telegram(body, title)
        if results["telegram"]:
            print(f"[telegram] Sent to chat {config.TELEGRAM_CHAT_ID}")

    if config.SMTP_USER and config.EMAIL_TO:
        results["email"] = send_email(body, subject=title)
        if results["email"]:
            print(f"[email] Sent to {config.EMAIL_TO}")

    if config.DISCORD_WEBHOOK_URL:
        results["discord"] = send_discord(body, title)
        if results["discord"]:
            print("[discord] Sent")

    if config.IMESSAGE_TO and sys.platform == "darwin":
        results["imessage"] = send_imessage(body)
        if results["imessage"]:
            print(f"[iMessage] Sent to {config.IMESSAGE_TO}")

    if not any(results.values()):
        print("[notify] No channels configured — printed to console only.")
        print("[notify] Set NTFY_TOPIC in .env or GitHub Secrets to get push alerts.")

    return results
