import os, random, smtplib
from email.message import EmailMessage
from functools import wraps
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

load_dotenv()
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret")
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///local.db").replace("postgres://", "postgresql://", 1)
db = SQLAlchemy(app)

PEOPLE = ["leela", "Chinni", "mahi", "Charan", "Other"]
EXT = (".mp4", ".webm", ".mov")

# ---------- Neon / Postgres (structured) ----------
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    email = db.Column(db.String(160), unique=True, nullable=False)
    pw_hash = db.Column(db.String(255))  # empty for Google users

class Question(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    text = db.Column(db.String(300), nullable=False)
    popup = db.Column(db.String(300), nullable=False)

SEED_Q = [
    ("Most beautiful girl is you — Yes or No?", "Correct answer. Nuvvu cheppakapoyina naaku telusu 😌"),
    ("Cake lo first piece naake ivvali — Yes or No?", "Approved ✅ Knife ikkada ivvu."),
    ("Nuvvu inka 18 ne ga? Yes or No?", "Aadhaar card chupinchu 🤨 ...fine, 18 ne."),
    ("Ee roju diet cancel chesesam — Yes or No?", "Birthday calories count avvavu 🍰"),
    ("Nenu cheppina jokes ki nuvvu navvav — Yes or No?", "Navvakapoyina fine, nenu navvanu le 😂"),
    ("Treat ivvadam ippudu start chesthava — Yes or No?", "Zomato open chesa already 🛵"),
    ("Nuvvu ee website ni screenshot teesukuntav — Yes or No?", "Bad girl 📸 Pettuko, free."),
    ("Mee friends lo nuvve most fun person — Yes or No?", "Chinni, mahi, Charan: 'avunu' 🙌"),
    ("Birthday roju alarm pettukoru — Yes or No?", "Sleep mode: ON 😴"),
    ("Nenu ee website ni free ga chesanu, thanks cheppava — Yes or No?", "Thanks accept chesa 🙏 Treat inka due!"),
    ("Next year kuda ee website kavala — Yes or No?", "Subscription: ₹0, but treat compulsory 😎"),
    ("Last question: Happy birthday ani cheppina vallaki hug — Yes or No?", "Hug loading... 🤗 Happy Birthday!"),
]

def seed():
    db.create_all()
    if not Question.query.first():
        db.session.add_all(Question(text=t, popup=p) for t, p in SEED_Q)
        db.session.commit()

with app.app_context():
    seed()

# ---------- MongoDB (unstructured) ----------
_mongo = None
def mongo():
    global _mongo
    uri = os.getenv("MONGO_URI")
    if not uri:
        return None
    if _mongo is None:
        from pymongo import MongoClient
        _mongo = MongoClient(uri, serverSelectionTimeoutMS=3000).get_default_database(default="birthday")
    return _mongo

def mongo_list(coll, query=None):
    try:
        m = mongo()
        return list(m[coll].find(query or {}, {"_id": 0})) if m is not None else []
    except Exception as e:
        app.logger.warning("Mongo unavailable: %s", e)
        return []

def static_files(folder):
    path = os.path.join(app.static_folder, folder)
    if not os.path.isdir(path):
        return []
    return [f"/static/{folder}/{f}" for f in sorted(os.listdir(path)) if not f.startswith(".")]

# ---------- Auth ----------
def login_required(fn):
    @wraps(fn)
    def wrap(*a, **k):
        if "uid" not in session:
            return redirect(url_for("home"))
        return fn(*a, **k)
    return wrap

def start_session(user):
    session["uid"], session["name"], session["email"] = user.id, user.name, user.email

@app.get("/")
def home():
    return render_template("home.html", google=bool(os.getenv("GOOGLE_CLIENT_ID")), tab=request.args.get("tab", "login"))

@app.post("/signup")
def signup():
    f = request.form
    name, email, pw = f.get("name", "").strip(), f.get("email", "").strip().lower(), f.get("password", "")
    if f.get("key", "") != os.getenv("SITE_KEY", "radhakrishna") or not (name and email and pw):
        return redirect(url_for("oops"))
    user = User.query.filter_by(email=email).first()
    if user:
        return redirect(url_for("oops"))
    user = User(name=name, email=email, pw_hash=generate_password_hash(pw))
    db.session.add(user); db.session.commit()
    start_session(user)
    return redirect(url_for("wish"))

@app.post("/login")
def login():
    email, pw = request.form.get("email", "").strip().lower(), request.form.get("password", "")
    user = User.query.filter_by(email=email).first()
    if user and user.pw_hash and check_password_hash(user.pw_hash, pw):
        start_session(user)
        return redirect(url_for("wish"))
    return redirect(url_for("oops"))

@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))

# Google OAuth (enabled only when env vars are set)
oauth = None
if os.getenv("GOOGLE_CLIENT_ID"):
    from authlib.integrations.flask_client import OAuth
    oauth = OAuth(app)
    oauth.register("google", client_id=os.getenv("GOOGLE_CLIENT_ID"), client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
                   server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
                   client_kwargs={"scope": "openid email profile"})

@app.get("/auth/google")
def google_login():
    if not oauth:
        return redirect(url_for("oops"))
    return oauth.google.authorize_redirect(url_for("google_callback", _external=True))

@app.get("/auth/google/callback")
def google_callback():
    try:
        info = oauth.google.authorize_access_token()["userinfo"]
    except Exception:
        return redirect(url_for("oops"))
    email = info["email"].lower()
    allowed = [e.strip().lower() for e in os.getenv("ALLOWED_EMAILS", "").split(",") if e.strip()]
    if allowed and email not in allowed:
        return redirect(url_for("oops"))
    user = User.query.filter_by(email=email).first()
    if not user:
        user = User(name=info.get("given_name") or email.split("@")[0], email=email)
        db.session.add(user); db.session.commit()
    start_session(user)
    return redirect(url_for("wish"))

# ---------- Pages ----------
@app.get("/oops")
def oops():
    return render_template("error.html", memes=mongo_list("memes") or [{"url": u} for u in static_files("memes")])

@app.get("/wish")
@login_required
def wish():
    return render_template("wish.html", people=PEOPLE)

@app.get("/questions")
@login_required
def questions():
    return render_template("questions.html", qs=[{"text": q.text, "popup": q.popup} for q in Question.query.order_by(Question.id)])

@app.get("/mail")
@login_required
def mail():
    action, person = request.args.get("action", "send"), request.args.get("person", "Other")
    sent = send_thanks(session["name"], session["email"], action, person)
    return render_template("mail.html", action=action, sent=sent)

def send_thanks(name, to, action, person):
    subject = f"Thank you, {name}! 🎂 ({person}'s video)"
    body = (f"Hi {name},\n\nThanks for {'liking' if action == 'like' else 'sending love on'} {person}'s birthday video "
            f"and for being part of today. 💛\n\nWith love,\nThe Birthday Website 🦚")
    host, user, pw = os.getenv("SMTP_HOST"), os.getenv("SMTP_USER"), os.getenv("SMTP_PASS")
    if not (host and user and pw):
        app.logger.info("SMTP not configured. Would send to %s: %s", to, subject)
        return False
    try:
        msg = EmailMessage(); msg["Subject"], msg["From"], msg["To"] = subject, user, to; msg.set_content(body)
        with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", 587))) as s:
            s.starttls(); s.login(user, pw); s.send_message(msg)
        return True
    except Exception as e:
        app.logger.error("Mail failed: %s", e)
        return False

# ---------- APIs ----------
@app.get("/api/videos")
@login_required
def api_videos():
    person = request.args.get("person", "")
    urls = [v["url"] for v in mongo_list("videos", {"person": person})] or static_files(f"videos/{person.lower()}")
    return jsonify([u for u in urls if u.lower().endswith(EXT) or u.startswith("http")])

@app.get("/api/wallpapers")
def api_wallpapers():
    return jsonify([v["url"] for v in mongo_list("wallpapers")] or static_files("wallpapers"))

DEFAULT_REPLIES = [
    "ఎందుకు రా చదువు కున్నావ్ నా బాబు అంతా అంతా పాఠి చదివేస్తే ఎవరు నువ్వు నేర్చుకుంటందీ…",
    "ఒక పని చెయ్యి ఇక్కడ నువ్వు.",
    "Ame Ra bala raju emina pani chesuko ra 😭",
    "Password gurthu pettukoleva? Birthday gurthu pettukunnav kada 🤦",
]
@app.post("/api/chat")
def api_chat():
    replies = [r["text"] for r in mongo_list("chat_responses")] or DEFAULT_REPLIES
    return jsonify({"reply": random.choice(replies)})

if __name__ == "__main__":
    app.run(debug=True)
