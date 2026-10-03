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
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from functools import wraps
from pathlib import Path

try:
    from authlib.integrations.flask_client import OAuth
except ModuleNotFoundError:  # Keeps password login usable until dependencies are installed.
    OAuth = None
from flask import Flask, current_app, jsonify, redirect, render_template, request, session, url_for

BASE_DIR = Path(__file__).resolve().parent
VIDEO_EXTENSIONS = {".mp4", ".webm", ".ogg", ".mov"}
RECIPIENTS_FILE = BASE_DIR / "data" / "approved_recipients.json"
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
oauth = OAuth() if OAuth is not None else None


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
        SITE_PASSWORD=os.getenv("SITE_PASSWORD", ""),
        SIGNUP_KEY=os.getenv("SIGNUP_KEY", os.getenv("SITE_PASSWORD", "")),
        GOOGLE_CLIENT_ID=os.getenv("GOOGLE_CLIENT_ID", ""),
        GOOGLE_CLIENT_SECRET=os.getenv("GOOGLE_CLIENT_SECRET", ""),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    )
    if oauth is not None:
        oauth.init_app(app)
    if oauth is not None and app.config["GOOGLE_CLIENT_ID"] and app.config["GOOGLE_CLIENT_SECRET"]:
        oauth.register(
            name="google",
            client_id=app.config["GOOGLE_CLIENT_ID"],
            client_secret=app.config["GOOGLE_CLIENT_SECRET"],
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile"},
        )

    @app.get("/")
    def home():
        if is_authenticated():
            return redirect(url_for("celebrate"))
        return render_template(
            "login.html",
            google_enabled=bool(oauth is not None and app.config["GOOGLE_CLIENT_ID"] and app.config["GOOGLE_CLIENT_SECRET"]),
        )

    @app.post("/login/password")
    def password_login():
        submitted_password = str(request.form.get("password", ""))
        configured_password = app.config["SITE_PASSWORD"]
        if configured_password and secure_compare(submitted_password, configured_password):
            create_login("Birthday Guest")
            return redirect(url_for("celebrate"))
        return redirect(url_for("error_page", reason="Wrong password ra babu — try once more."))

    @app.get("/signup")
    def signup():
        if is_authenticated():
            return redirect(url_for("celebrate"))
        return render_template("signup.html")

    @app.post("/signup")
    def complete_signup():
        email = str(request.form.get("email", "")).strip().lower()
        invite_key = str(request.form.get("invite_key", ""))
        recipient = next((entry for entry in approved_recipients() if entry["email"] == email), None)
        if recipient and app.config["SIGNUP_KEY"] and secure_compare(invite_key, app.config["SIGNUP_KEY"]):
            create_login(recipient["name"])
            persist_event(
                app,
                {
                    "name": recipient["name"],
                    "action": "signup",
                    "source": request.remote_addr or "unknown",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            return redirect(url_for("celebrate"))
        return redirect(url_for("error_page", reason="Invite email or secret key is not correct. Meme class ki welcome! 🤭"))

    @app.get("/login/google")
    def google_login():
        google = oauth.create_client("google") if oauth is not None else None
        if google is None:
            return redirect(url_for("error_page", reason="Google sign-in is not configured yet."))
        return google.authorize_redirect(url_for("google_callback", _external=True))

    @app.get("/auth/google/callback")
    def google_callback():
        google = oauth.create_client("google") if oauth is not None else None
        if google is None:
            return redirect(url_for("error_page", reason="Google sign-in is taking a chai break."))
        try:
            token = google.authorize_access_token()
            user_info = token.get("userinfo") or google.get("userinfo").json()
            email = str(user_info.get("email", "")).strip().lower()
        except Exception as exc:
            app.logger.warning("Google login failed: %s", exc)
            return redirect(url_for("error_page", reason="Google login did not finish. Try again, babu."))

        recipient = next((entry for entry in approved_recipients() if entry["email"] == email), None)
        if recipient is None:
            return redirect(url_for("error_page", reason="This Gmail is not in the birthday guest list."))
        create_login(recipient["name"])
        return redirect(url_for("celebrate"))

    @app.get("/logout")
    def logout():
        session.clear()
        return redirect(url_for("home"))

    @app.get("/celebrate")
    @login_required
    def celebrate():
        return render_template("home.html")

    @app.get("/error")
    def error_page():
        if is_authenticated():
            return redirect(url_for("celebrate"))
        return render_template("error.html", reason=request.args.get("reason", "")), 200

    @app.get("/wish")
    @login_required
    def wish():
        media_dir = BASE_DIR / "static" / "media"
        videos = [
            f"media/{path.name}"
            for path in sorted(media_dir.iterdir())
            if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS
        ]
        return render_template("wish.html", videos=videos, recipients=approved_recipients())

    @app.get("/questions")
    @login_required
    def questions():
        return render_template("questions.html")

    @app.get("/mail")
    @login_required
    def mail():
        name = clean_name(request.args.get("name", "Friend"))
        if name != "Friend" and recipient_by_name(name) is None:
            name = "Friend"
        delivery = request.args.get("delivery", "queued")
        return render_template("mail.html", name=name, delivery=delivery)

    @app.post("/api/send-thanks")
    @login_required
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
        if is_authenticated():
            return redirect(url_for("celebrate"))
        return render_template("error.html"), 404

    return app


def clean_name(value: object) -> str:
    return str(value).strip().title()[:40]


def is_authenticated() -> bool:
    return session.get("authenticated") is True


def create_login(display_name: str) -> None:
    session.clear()
    session.permanent = True
    session["authenticated"] = True
    session["display_name"] = display_name


def secure_compare(first: str, second: str) -> bool:
    import hmac

    return hmac.compare_digest(first.encode("utf-8"), second.encode("utf-8"))


def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if not is_authenticated():
            return redirect(url_for("error_page", reason="First login avvu ra babu, then surprise open avuthundi!"))
        return view(*args, **kwargs)

    return wrapped_view


def approved_recipients() -> list[dict[str, str]]:
    """Return only validated entries from the single editable recipient file."""
    try:
        raw_entries = json.loads(RECIPIENTS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        current_app.logger.warning("Approved recipient list could not be read: %s", exc)
        return []

    if not isinstance(raw_entries, dict):
        current_app.logger.warning("Approved recipient list must contain a JSON object.")
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
