import os, io, re, hmac, random, smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from functools import wraps
try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = lambda: None
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, abort, Response
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import generate_password_hash, check_password_hash
from data import QUESTIONS, WISHES, GREETINGS

load_dotenv()

def env(name, default=""):
    """Read an env var, tolerating pasted {braces} or quotes."""
    return os.getenv(name, default).strip().strip("{}'\" ")

app = Flask(__name__)
app.secret_key = env("SECRET_KEY", "dev-secret")
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)   # correct https URLs behind Render
app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=bool(os.getenv("RENDER")),
                  MAX_CONTENT_LENGTH=30 * 1024 * 1024)

def db_url():
    url = env("DATABASE_URL", "sqlite:///local.db")
    for prefix in ("postgres://", "postgresql://"):   # psycopg 3 driver
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url
app.config["SQLALCHEMY_DATABASE_URI"] = db_url()
db = SQLAlchemy(app)

PEOPLE = ["leela", "Chinni", "mahi", "Charan", "Other"]
EXT = (".mp4", ".webm", ".mov")
MSG_LIMIT = 10      # birthday girl may send each user at most 10 in-app messages

# =========================== NEON (Postgres): accounts only ===========================
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    email = db.Column(db.String(160), unique=True, nullable=False)
    college_id = db.Column(db.String(10), unique=True)
    username = db.Column(db.String(40))                  # only for the built-in admin account
    pw_hash = db.Column(db.String(255))                  # only for the built-in admin account
    picture = db.Column(db.String(500))                  # Google profile photo
    status = db.Column(db.String(12), default="pending")  # pending | approved | rejected
    is_admin = db.Column(db.Boolean, default=False)
    is_bgirl = db.Column(db.Boolean, default=False)      # may send in-app messages (Mongo)
    bday_today = db.Column(db.Boolean, default=False)    # whole site celebrates this person

class Question(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    text = db.Column(db.String(300), nullable=False)
    popup = db.Column(db.String(300), nullable=False)

NEW_COLS = {"status": "VARCHAR(12) DEFAULT 'approved'", "is_admin": "BOOLEAN DEFAULT FALSE", "college_id": "VARCHAR(10)",
            "is_bgirl": "BOOLEAN DEFAULT FALSE", "bday_today": "BOOLEAN DEFAULT FALSE",
            "username": "VARCHAR(40)", "picture": "VARCHAR(500)"}

def seed():
    from sqlalchemy import inspect, text
    db.create_all()
    have = {c["name"] for c in inspect(db.engine).get_columns("user")}
    for col, ddl in NEW_COLS.items():      # auto-migrate tables made by older versions
        if col not in have:
            db.session.execute(text(f'ALTER TABLE "user" ADD COLUMN {col} {ddl}'))
    db.session.commit()
    if Question.query.count() != len(QUESTIONS):
        Question.query.delete()
        db.session.add_all(Question(text=t, popup=p) for t, p in QUESTIONS)
        db.session.commit()
    # built-in admin; password comes only from the ADMIN_PASSWORD env var
    name, pw = env("ADMIN_USERNAME", "Chinni_Admin"), os.getenv("ADMIN_PASSWORD", "")
    if pw:
        u = User.query.filter_by(username=name).first() or User(name=name, username=name, email=f"{name.lower()}@admin.local")
        if not u.id:
            db.session.add(u)
        if not (u.pw_hash and check_password_hash(u.pw_hash, pw)):
            u.pw_hash = generate_password_hash(pw)
        u.is_admin, u.status = True, "approved"
        db.session.commit()

with app.app_context():
    seed()

# =========================== MONGODB: messages, wallpapers, media ===========================
_mongo = None
def mongo():
    global _mongo
    uri = env("MONGO_URI")
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

# =========================== Auth (Neon) ===========================
def admin_emails():
    return {e.strip().lower() for e in env("ADMIN_EMAILS").split(",") if e.strip()}

def current_user():
    uid = session.get("uid")
    return db.session.get(User, uid) if uid else None

def login_required(fn):
    @wraps(fn)
    def wrap(*a, **k):
        u = current_user()
        if not u:
            return redirect(url_for("home"))
        if not u.college_id and not u.is_admin:
            return redirect(url_for("complete_profile"))
        if u.status != "approved":
            return redirect(url_for("pending"))
        return fn(*a, **k)
    return wrap

def admin_required(fn):
    @wraps(fn)
    def wrap(*a, **k):
        u = current_user()
        if not (u and u.is_admin and u.status == "approved"):
            return redirect(url_for("admin_login"))
        return fn(*a, **k)
    return wrap

def start_session(user):
    if user.email in admin_emails() and not (user.is_admin and user.status == "approved"):
        user.is_admin, user.status = True, "approved"; db.session.commit()
    session.clear()
    session["uid"], session["name"], session["admin"] = user.id, user.name, bool(user.is_admin)

def send_mail(to, subject, body):
    host, user, pw = env("SMTP_HOST"), env("SMTP_USER"), os.getenv("SMTP_PASS", "")
    if not (host and user and pw):
        return False
    try:
        msg = EmailMessage(); msg["Subject"], msg["From"], msg["To"] = subject, user, to; msg.set_content(body)
        with smtplib.SMTP(host, int(env("SMTP_PORT", "587"))) as s:
            s.starttls(); s.login(user, pw); s.send_message(msg)
        return True
    except Exception as e:
        app.logger.error("Mail failed: %s", e)
        return False

ID_RE = re.compile(r"^[A-Z0-9]{10}$")
def clean_id(v):
    return (v or "").strip().upper()

def create_user(name, email, college_id=None, picture=None):
    admin = email in admin_emails()
    user = User(name=name, email=email, college_id=college_id, picture=picture, is_admin=admin, status="approved" if admin else "pending")
    db.session.add(user); db.session.commit()
    if user.status == "pending":
        for a in admin_emails():
            send_mail(a, f"New access request: {name}", f"{name} ({email}) is asking permission.\nReview: {url_for('admin', _external=True)}")
    return user

@app.context_processor
def inject_globals():
    out = {"bday": None, "bday_wishes": [], "unread": 0}
    try:
        b = User.query.filter_by(bday_today=True).first()
        if b:
            tpl = [w["text"] for w in mongo_list("wishes")] or WISHES
            out.update(bday=b.name, bday_wishes=[w.replace("{name}", b.name) for w in tpl])
        m = mongo()
        if session.get("uid") and m is not None:
            out["unread"] = m.messages.count_documents({"to_uid": session["uid"], "read": False})
    except Exception:
        pass
    return out

@app.get("/")
def home():
    return render_template("home.html", google=bool(env("GOOGLE_CLIENT_ID")), tab=request.args.get("tab", "login"), msg=request.args.get("msg"))

@app.post("/signup")
def signup():
    f = request.form
    name, email, cid = f.get("name", "").strip(), f.get("email", "").strip().lower(), clean_id(f.get("college_id"))
    if not name or "@" not in email or not ID_RE.match(cid):
        return redirect(url_for("home", tab="signup", msg="Please fill your full name, email and all 10 college ID boxes."))
    if User.query.filter((User.email == email) | (User.college_id == cid)).first():
        return redirect(url_for("home", msg="That email or college ID already has an account. Please log in."))
    start_session(create_user(name, email, cid))
    return redirect(url_for("landing"))

@app.post("/login")
def login():
    email, cid = request.form.get("email", "").strip().lower(), clean_id(request.form.get("college_id"))
    user = User.query.filter_by(email=email).first()
    if user and user.college_id and hmac.compare_digest(user.college_id, cid):
        start_session(user)
        return redirect(url_for("landing"))
    return redirect(url_for("oops"))

@app.route("/admin-login", methods=["GET", "POST"])
def admin_login():
    err = None
    if request.method == "POST":
        u = User.query.filter_by(username=request.form.get("username", "").strip(), is_admin=True).first()
        if u and u.pw_hash and check_password_hash(u.pw_hash, request.form.get("password", "")):
            start_session(u)
            return redirect(url_for("admin"))
        err = "Wrong admin username or password."
    return render_template("adminlogin.html", err=err)

@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))

@app.get("/pending")
def pending():
    u = current_user()
    if not u:
        return redirect(url_for("home"))
    if not u.college_id and not u.is_admin:
        return redirect(url_for("complete_profile"))
    if u.status == "approved":
        return redirect(url_for("landing"))
    return render_template("pending.html", user=u)

@app.get("/api/status")
def api_status():
    u = current_user()
    return jsonify({"status": u.status if u else "none"})

@app.route("/complete-profile", methods=["GET", "POST"])
def complete_profile():   # Google users add full name + college ID once
    u = current_user()
    if not u:
        return redirect(url_for("home"))
    if request.method == "POST":
        name, cid = request.form.get("name", "").strip(), clean_id(request.form.get("college_id"))
        if name and ID_RE.match(cid) and not User.query.filter(User.college_id == cid, User.id != u.id).first():
            u.name, u.college_id = name, cid; db.session.commit(); session["name"] = name
            return redirect(url_for("landing"))
        return render_template("profile.html", user=u, err="Enter your full name and a valid, unused 10-character college ID.")
    return render_template("profile.html", user=u, err=None)

oauth = None
if env("GOOGLE_CLIENT_ID"):
    from authlib.integrations.flask_client import OAuth
    oauth = OAuth(app)
    oauth.register("google", client_id=env("GOOGLE_CLIENT_ID"), client_secret=env("GOOGLE_CLIENT_SECRET"),
                   server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
                   client_kwargs={"scope": "openid email profile"})

@app.get("/auth/google")
def google_login():
    if not oauth:
        return redirect(url_for("oops"))
    session["g_intent"] = request.args.get("intent", "login")
    return oauth.google.authorize_redirect(url_for("google_callback", _external=True))

@app.get("/auth/google/callback")
def google_callback():
    try:
        info = oauth.google.authorize_access_token()["userinfo"]
    except Exception:
        return redirect(url_for("oops"))
    if not info.get("email_verified", True):
        return redirect(url_for("oops"))
    email, intent = info["email"].lower(), session.get("g_intent", "login")
    user = User.query.filter_by(email=email).first()
    if not user:
        if intent == "login" and email not in admin_emails():
            return redirect(url_for("home", tab="signup", msg="No account for that Google email yet. Please sign up."))
        user = create_user(info.get("given_name") or email.split("@")[0], email, picture=info.get("picture"))
    elif info.get("picture") and user.picture != info["picture"]:
        user.picture = info["picture"]; db.session.commit()
    start_session(user)
    return redirect(url_for("landing"))

@app.get("/landing")
@login_required
def landing():   # first visit of a session: birthday welcome page (if admin switched one on)
    if User.query.filter_by(bday_today=True).first() and not session.get("welcomed"):
        session["welcomed"] = True
        return redirect(url_for("welcome"))
    return redirect(url_for("wish"))

@app.get("/welcome")
@login_required
def welcome():
    b = User.query.filter_by(bday_today=True).first()
    if not b:
        return redirect(url_for("wish"))
    wishes = random.sample(WISHES_FOR(b), k=min(9, len(WISHES_FOR(b))))
    return render_template("welcome.html", is_me=(b.id == session["uid"]), wishes=wishes, greetings=GREETINGS)

def WISHES_FOR(b):
    tpl = [w["text"] for w in mongo_list("wishes")] or WISHES
    return [w.replace("{name}", b.name) for w in tpl]

# =========================== Admin ===========================
def wallpaper_ids():
    m = mongo()
    try:
        return [str(d["_id"]) for d in m.wallpapers.find({}, {"_id": 1}).sort("at", -1)] if m is not None else []
    except Exception:
        return []

@app.get("/admin")
@admin_required
def admin():
    return render_template("admin.html", users=User.query.order_by(User.id.desc()).all(), me=session["uid"],
                           walls=wallpaper_ids(), mongo_ok=mongo() is not None, msg=request.args.get("msg"))

@app.post("/admin/<int:uid>/<action>")
@admin_required
def admin_action(uid, action):
    status = {"approve": "approved", "reject": "rejected", "revoke": "rejected"}.get(action)
    u = db.session.get(User, uid)
    if u and status and u.id != session["uid"]:
        u.status = status
        if status != "approved":
            u.is_bgirl = u.bday_today = False
        db.session.commit()
        if status == "approved":
            send_mail(u.email, "You're in! 🎂", f"Hi {u.name}, the host approved you. Sign in here: {url_for('home', _external=True)}")
    return redirect(url_for("admin"))

@app.post("/admin/<int:uid>/toggle/<field>")
@admin_required
def admin_toggle(uid, field):
    if field not in ("is_bgirl", "bday_today"):
        abort(400)
    u = db.session.get(User, uid)
    if u and u.status == "approved":
        new = not getattr(u, field)
        if field == "bday_today" and new:
            User.query.update({"bday_today": False})   # one birthday star at a time
        setattr(u, field, new); db.session.commit()
    return redirect(url_for("admin"))

def compress(data):
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = 40_000_000
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    im.thumbnail((1920, 1920))
    out = io.BytesIO(); im.save(out, "JPEG", quality=82, optimize=True)
    return out.getvalue()

@app.post("/admin/wallpapers")
@admin_required
def upload_wallpapers():
    from bson import Binary
    m = mongo()
    if m is None:
        return redirect(url_for("admin", msg="MongoDB is not connected, so images can't be saved."))
    ok = bad = 0
    for f in request.files.getlist("images"):
        try:
            m.wallpapers.insert_one({"filename": f.filename[:80], "data": Binary(compress(f.read())), "at": datetime.now(timezone.utc)})
            ok += 1
        except Exception:
            bad += 1
    return redirect(url_for("admin", msg=f"Uploaded {ok} image(s)." + (f" {bad} skipped (not a valid image)." if bad else "")))

@app.post("/admin/wallpapers/<wid>/delete")
@admin_required
def delete_wallpaper(wid):
    from bson import ObjectId
    m = mongo()
    if m is not None:
        try: m.wallpapers.delete_one({"_id": ObjectId(wid)})
        except Exception: pass
    return redirect(url_for("admin"))

@app.get("/media/wp/<wid>")
def media_wallpaper(wid):   # public: wallpapers show on every page, including login
    from bson import ObjectId
    m = mongo()
    try:
        doc = m.wallpapers.find_one({"_id": ObjectId(wid)}) if m is not None else None
    except Exception:
        doc = None
    if not doc:
        abort(404)
    return Response(doc["data"], mimetype="image/jpeg", headers={"Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff"})

@app.get("/api/wallpapers")
def api_wallpapers():
    urls = [f"/media/wp/{i}" for i in wallpaper_ids()] or static_files("wallpapers")
    random.shuffle(urls)
    return jsonify(urls)

# =========================== Pages ===========================
@app.get("/oops")
def oops():
    return render_template("error.html", memes=mongo_list("memes") or [{"url": u} for u in static_files("memes")])

@app.get("/wish")
@login_required
def wish():
    u = current_user()
    return render_template("wish.html", people=PEOPLE, is_bgirl=u.is_bgirl, recipients=recipients_for(u) if u.is_bgirl else [])

@app.get("/questions")
@login_required
def questions():
    qs = Question.query.order_by(func.random()).limit(10).all()   # 10 random of the 40
    return render_template("questions.html", qs=[{"text": q.text, "popup": q.popup} for q in qs])

@app.get("/api/videos")
@login_required
def api_videos():
    person, items = request.args.get("person", ""), []
    docs = mongo_list("videos", {"person": person} if person else {})
    if docs:
        items = [{"url": d["url"], "person": d.get("person", "Other")} for d in docs]
    else:
        for p in ([person] if person else PEOPLE):
            items += [{"url": u, "person": p} for u in static_files(f"videos/{p.lower()}") if u.lower().endswith(EXT)]
    return jsonify(items)

# =========================== In-app messages (MongoDB only) ===========================
def recipients_for(u):
    sent, m = {}, mongo()
    try:
        if m is not None:
            for r in m.messages.aggregate([{"$match": {"from_uid": u.id}}, {"$group": {"_id": "$to_uid", "n": {"$sum": 1}}}]):
                sent[r["_id"]] = r["n"]
    except Exception:
        pass
    people = User.query.filter(User.status == "approved", User.id != u.id, User.username.is_(None)).order_by(User.name)
    return [{"id": x.id, "name": x.name, "left": max(0, MSG_LIMIT - sent.get(x.id, 0))} for x in people]

@app.get("/messages")
@login_required
def messages():
    u, m, inbox, ok = current_user(), mongo(), [], True
    try:
        if m is not None:
            inbox = list(m.messages.find({"to_uid": u.id}, {"_id": 0}).sort("at", -1).limit(50))
            m.messages.update_many({"to_uid": u.id, "read": False}, {"$set": {"read": True}})
    except Exception:
        ok = False
    return render_template("messages.html", inbox=inbox, mongo_ok=(m is not None and ok), limit=MSG_LIMIT,
                           is_bgirl=u.is_bgirl, recipients=recipients_for(u) if u.is_bgirl else [])

@app.post("/api/messages")
@login_required
def api_send_message():
    u = current_user()
    if not u.is_bgirl:
        abort(403)
    d = request.get_json(silent=True) or {}
    text = str(d.get("text") or "").strip()[:500]
    try:
        to = db.session.get(User, int(d.get("to") or 0))
    except (TypeError, ValueError):
        to = None
    if not text or not to or to.status != "approved" or to.id == u.id:
        return jsonify(error="Pick a person and write a message."), 400
    m = mongo()
    if m is None:
        return jsonify(error="MongoDB isn't connected on the server."), 503
    try:
        sent = m.messages.count_documents({"from_uid": u.id, "to_uid": to.id})
        if sent >= MSG_LIMIT:
            return jsonify(error=f"Limit reached: you've already sent {MSG_LIMIT} messages to {to.name}."), 429
        m.messages.insert_one({"from_uid": u.id, "from_name": u.name, "to_uid": to.id, "to_name": to.name,
                               "text": text, "read": False, "at": datetime.now(timezone.utc)})
    except Exception:
        return jsonify(error="Couldn't save the message. Try again."), 503
    return jsonify(ok=True, left=MSG_LIMIT - sent - 1)

# =========================== Chat (error page) ===========================
DEFAULT_REPLIES = [
    "ఎందుకు రా చదువు కున్నావ్ నా బాబు అంతా అంతా పాఠి చదివేస్తే ఎవరు నువ్వు నేర్చుకుంటందీ…",
    "ఒక పని చెయ్యి ఇక్కడ నువ్వు.",
    "Ame Ra bala raju emina pani chesuko ra 😭",
    "Password gurthu pettukoleva? Birthday gurthu pettukunnav kada 🤦",
]
@app.post("/api/chat")
def api_chat():
    return jsonify({"reply": random.choice([r["text"] for r in mongo_list("chat_responses")] or DEFAULT_REPLIES)})

if __name__ == "__main__":
    app.run(debug=True)
