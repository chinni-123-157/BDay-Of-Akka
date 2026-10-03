"""Birthday surprise website – Flask entry point.

The app runs fully without services in local development.  Add Neon, MongoDB and
SMTP environment variables in Render to turn on event persistence and mail.
"""
from __future__ import annotations

import json
import logging
import os
import re
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from flask import Flask, jsonify, render_template, request

BASE_DIR = Path(__file__).resolve().parent
VIDEO_EXTENSIONS = {".mp4", ".webm", ".ogg", ".mov"}
RECIPIENTS_FILE = BASE_DIR / "data" / "approved_recipients.json"
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def create_app() -> Flask:
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=os.getenv("SECRET_KEY", "replace-this-before-production"),
        DATABASE_URL=os.getenv("DATABASE_URL", ""),
        MONGODB_URI=os.getenv("MONGODB_URI", ""),
        MONGODB_DATABASE=os.getenv("MONGODB_DATABASE", "birthday_site"),
        SMTP_HOST=os.getenv("SMTP_HOST", ""),
        SMTP_PORT=int(os.getenv("SMTP_PORT", "587")),
        SMTP_USERNAME=os.getenv("SMTP_USERNAME", ""),
        SMTP_PASSWORD=os.getenv("SMTP_PASSWORD", ""),
        SMTP_FROM=os.getenv("SMTP_FROM", ""),
    )

    @app.get("/")
    def home():
        return render_template("home.html")

    @app.get("/error")
    def error_page():
        return render_template("error.html"), 200

    @app.get("/wish")
    def wish():
        media_dir = BASE_DIR / "static" / "media"
        videos = [
            f"media/{path.name}"
            for path in sorted(media_dir.iterdir())
            if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS
        ]
        return render_template("wish.html", videos=videos, recipients=approved_recipients())

    @app.get("/questions")
    def questions():
        return render_template("questions.html")

    @app.get("/mail")
    def mail():
        name = clean_name(request.args.get("name", "Friend"))
        if name != "Friend" and recipient_by_name(name) is None:
            name = "Friend"
        delivery = request.args.get("delivery", "queued")
        return render_template("mail.html", name=name, delivery=delivery)

    @app.post("/api/send-thanks")
    def send_thanks():
        payload = request.get_json(silent=True) or {}
        name = clean_name(payload.get("name", ""))
        action = payload.get("action", "send")
        recipient = recipient_by_name(name)
        if recipient is None:
            return jsonify(ok=False, message="Only an approved recipient from the email list can receive this message."), 400
        if action not in {"send", "like"}:
            return jsonify(ok=False, message="That button is having a comedy break."), 400

        event = {
            "name": recipient["name"],
            "action": action,
            "source": request.remote_addr or "unknown",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        persistence = persist_event(app, event)
        delivery = send_thank_you_email(app, recipient)
        return jsonify(ok=True, name=recipient["name"], delivery=delivery, persistence=persistence)

    @app.errorhandler(404)
    def not_found(_error):
        return render_template("error.html"), 404

    return app


def clean_name(value: object) -> str:
    return str(value).strip().title()[:40]


def approved_recipients() -> list[dict[str, str]]:
    """Return only validated entries from the single editable recipient file."""
    try:
        raw_entries = json.loads(RECIPIENTS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        app.logger.warning("Approved recipient list could not be read: %s", exc)
        return []

    if not isinstance(raw_entries, dict):
        app.logger.warning("Approved recipient list must contain a JSON object.")
        return []

    recipients: list[dict[str, str]] = []
    seen_emails: set[str] = set()
    for entry in raw_entries.get("recipients", []):
        name = clean_name(entry.get("name", ""))
        email = str(entry.get("email", "")).strip().lower()
        if name and EMAIL_PATTERN.fullmatch(email) and email not in seen_emails:
            recipients.append({"name": name, "email": email})
            seen_emails.add(email)
    return recipients


def recipient_by_name(name: str) -> dict[str, str] | None:
    return next((entry for entry in approved_recipients() if entry["name"] == name), None)


def persist_event(app: Flask, event: dict) -> dict[str, str]:
    """Store the interaction in both configured databases; local mode stays usable."""
    result = {"neon": "not-configured", "mongo": "not-configured"}

    if app.config["DATABASE_URL"]:
        try:
            import psycopg

            with psycopg.connect(app.config["DATABASE_URL"], connect_timeout=5) as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS birthday_events (
                            id BIGSERIAL PRIMARY KEY,
                            name TEXT NOT NULL,
                            action TEXT NOT NULL,
                            source TEXT,
                            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        )
                        """
                    )
                    cursor.execute(
                        "INSERT INTO birthday_events (name, action, source, created_at) VALUES (%s, %s, %s, %s)",
                        (event["name"], event["action"], event["source"], event["created_at"]),
                    )
            result["neon"] = "saved"
        except Exception as exc:  # Service failures must never break a birthday button.
            app.logger.warning("Neon event save failed: %s", exc)
            result["neon"] = "unavailable"

    if app.config["MONGODB_URI"]:
        try:
            from pymongo import MongoClient

            with MongoClient(app.config["MONGODB_URI"], serverSelectionTimeoutMS=5000) as client:
                client[app.config["MONGODB_DATABASE"]]["birthday_events"].insert_one(event)
            result["mongo"] = "saved"
        except Exception as exc:
            app.logger.warning("Mongo event save failed: %s", exc)
            result["mongo"] = "unavailable"
    return result


def send_thank_you_email(app: Flask, recipient: dict[str, str]) -> str:
    """Send automatically, but only to an entry in approved_recipients.json."""
    if not (app.config["SMTP_HOST"] and app.config["SMTP_FROM"]):
        return "mail-not-configured"
    try:
        name = recipient["name"]

        message = EmailMessage()
        message["Subject"] = f"Thank you, {name}! 🎉"
        message["From"] = app.config["SMTP_FROM"]
        message["To"] = recipient["email"]
        message.set_content(
            f"Hi {name},\n\nThank you for being part of this birthday surprise! 🎂\n\nWith love and confetti,\nBirthday Crew"
        )
        with smtplib.SMTP(app.config["SMTP_HOST"], app.config["SMTP_PORT"], timeout=10) as smtp:
            smtp.starttls()
            if app.config["SMTP_USERNAME"]:
                smtp.login(app.config["SMTP_USERNAME"], app.config["SMTP_PASSWORD"])
            smtp.send_message(message)
        return "sent"
    except Exception as exc:
        app.logger.warning("Thank-you email failed: %s", exc)
        return "mail-unavailable"


app = create_app()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app.run(debug=True, host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
