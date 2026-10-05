import os, io, re, time, random, smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from functools import wraps
try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = lambda: None
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, abort, Response
from flask_sqlalchemy import SQLAlchemy
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
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
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=bool(os.getenv("RENDER")), MAX_CONTENT_LENGTH=30 * 1024 * 1024)

def db_url():
    url = env("DATABASE_URL", "sqlite:///local.db")
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url
app.config["SQLALCHEMY_DATABASE_URI"] = db_url()
db = SQLAlchemy(app)

IST = timezone(timedelta(hours=5, minutes=30))
MSG_LIMIT, MSG_CHARS = 10, 50          # messages per user per day, characters per message
UPLOAD_LIMIT, VIDEO_MB = 10, 25        # uploads per user per day, max video size

# =========================== NEON (Postgres): accounts + switches ===========================
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    email = db.Column(db.String(160), unique=True, nullable=False)
    college_id = db.Column(db.String(10), unique=True)
    username = db.Column(db.String(40))
    pw_hash = db.Column(db.String(255))
    picture = db.Column(db.String(500))
    status = db.Column(db.String(12), default="pending")   # pending | approved | rejected
    is_admin = db.Column(db.Boolean, default=False)
    bday_today = db.Column(db.Boolean, default=False)      # Birthday toggle
    can_upload = db.Column(db.Boolean, default=True)       # Upload toggle

class Question(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    text = db.Column(db.String(300), nullable=False)
    popup = db.Column(db.String(300), nullable=False)

NEW_COLS = {"status": "VARCHAR(12) DEFAULT 'approved'", "is_admin": "BOOLEAN DEFAULT FALSE", "college_id": "VARCHAR(10)",
            "bday_today": "BOOLEAN DEFAULT FALSE", "username": "VARCHAR(40)", "picture": "VARCHAR(500)",
            "can_upload": "BOOLEAN DEFAULT TRUE"}

def seed():
    from sqlalchemy import inspect, text
    db.create_all()
    have = {c["name"] for c in inspect(db.engine).get_columns("user")}
    for col, ddl in NEW_COLS.items():
        if col not in have:
            db.session.execute(text(f'ALTER TABLE "user" ADD COLUMN {col} {ddl}'))
    db.session.commit()
    if Question.query.count() != len(QUESTIONS):
        Question.query.delete()
        db.session.add_all(Question(text=t, popup=p) for t, p in QUESTIONS)
        db.session.commit()
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

# =========================== MONGODB: messages, media, announcements ===========================
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

def grid():
    import gridfs
    return gridfs.GridFS(mongo())

def mfind(coll, q=None, limit=20, sort="at"):
    """Documents with string `id`, newest first. Never raises."""
    m = mongo()
    try:
        if m is None:
            return []
        out = []
        for d in m[coll].find(q or {}).sort(sort, -1).limit(limit):
            d["id"] = str(d.pop("_id")); out.append(d)
        return out
    except Exception as e:
        app.logger.warning("Mongo unavailable: %s", e)
        return []

def mongo_list(coll, query=None):
    try:
        m = mongo()
        return list(m[coll].find(query or {}, {"_id": 0})) if m is not None else []
    except Exception:
        return []

def static_files(folder):
    path = os.path.join(app.static_folder, folder)
    if not os.path.isdir(path):
        return []
    return [f"/static/{folder}/{f}" for f in sorted(os.listdir(path)) if not f.startswith(".")]

def day_start():   # start of today in India time, as UTC
    return datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)

@app.template_filter("ist")
def ist(dt):
    return dt.replace(tzinfo=timezone.utc).astimezone(IST).strftime("%d %b, %I:%M %p") if dt else ""

# =========================== Auth (Neon) ===========================
def admin_emails():
    return {e.strip().lower() for e in env("ADMIN_EMAILS").split(",") if e.strip()}

def current_user():
    uid = session.get("uid")
    return db.session.get(User, uid) if uid else None

def bday_user():
    return User.query.filter_by(bday_today=True, status="approved").first()

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
            return redirect(url_for("home", msg="Please log in with the admin account."))
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
USER_RE = re.compile(r"^[A-Za-z0-9_.-]{3,30}$")
def clean_id(v):
    return (v or "").strip().upper()

def create_user(name, email, college_id=None, picture=None, username=None, pw=None):
    admin = email in admin_emails()
    user = User(name=name, email=email, college_id=college_id, picture=picture, username=username,
                pw_hash=generate_password_hash(pw) if pw else None, is_admin=admin, status="approved" if admin else "pending")
    db.session.add(user); db.session.commit()
    if user.status == "pending":
        for a in admin_emails():
            send_mail(a, f"New access request: {name}", f"{name} ({email}) is asking permission.\nReview: {url_for('admin', _external=True)}")
    return user

@app.context_processor
def inject_globals():
    out = {"bday": None, "bday_id": None, "bday_wishes": [], "unread": 0}
    try:
        b = bday_user()
        if b:
            tpl = [w["text"] for w in mongo_list("wishes")] or WISHES
            out.update(bday=b.name, bday_id=b.id, bday_wishes=[w.replace("{name}", b.name) for w in tpl])
        m = mongo()
        if session.get("uid") and m is not None:
            out["unread"] = m.messages.count_documents({"to_uid": session["uid"], "read": False})
    except Exception:
        pass
    return out

@app.get("/")
def home():
    return render_template("home.html", google=bool(env("GOOGLE_CLIENT_ID")), tab=request.args.get("tab", "login"), msg=request.args.get("msg"))

_fails = {}
def throttled():
    now, k = time.time(), request.remote_addr
    _fails[k] = [t for t in _fails.get(k, []) if now - t < 600]
    return len(_fails[k]) >= 8

def after_login(user):   # admin -> admin dashboard, everyone else -> their dashboard
    return url_for("admin") if user.is_admin and user.status == "approved" else url_for("landing")

@app.post("/signup")
def signup():
    f = request.form
    name, username, email = f.get("name", "").strip(), f.get("username", "").strip(), f.get("email", "").strip().lower()
    cid, pw = clean_id(f.get("college_id")), f.get("password", "")
    if not name or not USER_RE.match(username) or "@" not in email or not ID_RE.match(cid) or len(pw) < 6:
        return redirect(url_for("home", tab="signup", msg="Fill every field: full name, username (3-30 letters/numbers), email, all 10 college ID boxes and a password of 6+ characters."))
    if User.query.filter((User.email == email) | (User.college_id == cid) | (func.lower(User.username) == username.lower())).first():
        return redirect(url_for("home", msg="That username, email or college ID is already registered. Please log in."))
    user = create_user(name, email, cid, username=username, pw=pw)
    start_session(user)
    return redirect(after_login(user))

@app.post("/login")
def login():
    if throttled():
        return redirect(url_for("home", msg="Too many wrong attempts. Please wait a few minutes."))
    ident, pw = request.form.get("username", "").strip().lower(), request.form.get("password", "")
    col = func.lower(User.email) if "@" in ident else func.lower(User.username)
    user = User.query.filter(col == ident).first() if ident else None
    if user and user.pw_hash and check_password_hash(user.pw_hash, pw):
        start_session(user)
        return redirect(after_login(user))
    _fails.setdefault(request.remote_addr, []).append(time.time())
    return redirect(url_for("oops"))          # fun fallback: meme + error page

@app.route("/admin-login")
def admin_login():
    return redirect(url_for("home", msg="Log in with the admin username and password."))

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
def complete_profile():
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
    return oauth.google.authorize_redirect(google_redirect_uri())

def google_redirect_uri():
    """Must match an 'Authorized redirect URI' in Google Cloud exactly. Override with GOOGLE_REDIRECT_URI if needed."""
    return env("GOOGLE_REDIRECT_URI") or request.url_root.rstrip("/") + "/auth/callback"

@app.get("/auth/callback")
@app.get("/auth/google/callback")      # old path still works
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
    return redirect(after_login(user))

@app.get("/landing")
@login_required
def landing():   # celebration screen once per session while a birthday is on, otherwise the dashboard
    if bday_user() and not session.get("welcomed"):
        session["welcomed"] = True
        return redirect(url_for("welcome"))
    return redirect(url_for("dashboard"))

@app.get("/welcome")
@login_required
def welcome():
    b = bday_user()
    if not b:
        return redirect(url_for("dashboard"))
    tpl = [w["text"] for w in mongo_list("wishes")] or WISHES
    pool = [w.replace("{name}", b.name) for w in tpl]
    return render_template("welcome.html", is_me=(b.id == session["uid"]), wishes=random.sample(pool, k=min(9, len(pool))), greetings=GREETINGS)

# =========================== User dashboard ===========================
@app.get("/dashboard")
@login_required
def dashboard():
    me, b = current_user(), bday_user()
    is_bday = bool(b and b.id == me.id)
    b_photo = None
    if b:
        ph = mfind("media", {"owner_uid": b.id, "kind": "photo"}, limit=1)
        b_photo = f"/media/{ph[0]['id']}" if ph else b.picture
    wishes = mfind("messages", {"to_uid": me.id}, limit=8) if is_bday else []
    return render_template("dashboard.html", me=me, b=b, is_bday=is_bday, b_photo=b_photo, wishes=wishes,
                           notes=mfind("announcements", limit=5), posts=mfind("media", limit=24),
                           left=max(0, MSG_LIMIT - sent_today(me.id)), msg=request.args.get("msg"),
                           chars=MSG_CHARS, mongo_ok=mongo() is not None)

def sniff(raw):
    if raw[4:8] == b"ftyp":
        return "video", ("video/quicktime" if raw[8:12] == b"qt  " else "video/mp4")
    if raw[:4] == b"\x1a\x45\xdf\xa3":
        return "video", "video/webm"
    if raw[:3] == b"\xff\xd8\xff" or raw[:8] == b"\x89PNG\r\n\x1a\n" or (raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"):
        return "photo", "image/jpeg"
    return None, None

def compress(data):
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = 40_000_000
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    im.thumbnail((1920, 1920))
    out = io.BytesIO(); im.save(out, "JPEG", quality=82, optimize=True)
    return out.getvalue()

@app.post("/upload")
@login_required
def upload():
    me, m = current_user(), mongo()
    def back(msg): return redirect(url_for("dashboard", msg=msg))
    if m is None:
        return back("MongoDB isn't connected, so uploads are unavailable.")
    if not (me.can_upload or me.is_admin):
        return back("Uploads are switched off for your account.")
    f = request.files.get("file")
    if not f or not f.filename:
        return back("Choose a photo or a video first.")
    if m.media.count_documents({"owner_uid": me.id, "at": {"$gte": day_start()}}) >= UPLOAD_LIMIT:
        return back(f"Daily upload limit reached ({UPLOAD_LIMIT}).")
    raw = f.read()
    kind, ctype = sniff(raw)
    if kind == "video" and len(raw) > VIDEO_MB * 1024 * 1024:
        return back(f"Video is too large (max {VIDEO_MB} MB).")
    if kind == "photo":
        try: raw = compress(raw)
        except Exception: kind = None
    if not kind:
        return back("Only photos (JPG, PNG, WebP) and videos (MP4, WebM, MOV) are allowed.")
    fid = grid().put(raw, filename=f.filename[:80], content_type=ctype)
    m.media.insert_one({"file_id": fid, "kind": kind, "content_type": ctype, "owner_uid": me.id, "owner_name": me.name,
                        "caption": request.form.get("caption", "").strip()[:80], "at": datetime.now(timezone.utc)})
    return back("Posted! 🎉")

@app.errorhandler(413)
def too_big(_):
    return redirect(url_for("dashboard", msg=f"That file is too large (max {VIDEO_MB} MB)."))

@app.get("/media/<mid>")
@login_required
def media(mid):
    from bson import ObjectId
    m = mongo()
    try:
        doc = m.media.find_one({"_id": ObjectId(mid)}) if m is not None else None
        f = grid().get(doc["file_id"]) if doc else None
    except Exception:
        f = None
    if not f:
        abort(404)
    size, start, end, status = f.length, 0, f.length - 1, 200
    mt = re.match(r"bytes=(\d*)-(\d*)", request.headers.get("Range", ""))
    if mt:                      # byte ranges so videos can seek / play on iPhone
        a, b = mt.groups()
        start, end = (max(0, size - int(b)), size - 1) if (a == "" and b) else (int(a or 0), min(int(b), size - 1) if b else size - 1)
        if start > end or start >= size:
            return Response(status=416, headers={"Content-Range": f"bytes */{size}"})
        end, status = min(end, start + 4 * 1024 * 1024 - 1), 206
    f.seek(start); data = f.read(end - start + 1)
    h = {"Accept-Ranges": "bytes", "Content-Length": str(len(data)), "Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff"}
    if status == 206:
        h["Content-Range"] = f"bytes {start}-{end}/{size}"
    return Response(data, status=status, mimetype=doc["content_type"], headers=h)

@app.post("/media/<mid>/delete")
@login_required
def media_delete(mid):
    from bson import ObjectId
    me, m = current_user(), mongo()
    try:
        doc = m.media.find_one({"_id": ObjectId(mid)}) if m is not None else None
        if doc and (me.is_admin or doc["owner_uid"] == me.id):
            grid().delete(doc["file_id"]); m.media.delete_one({"_id": doc["_id"]})
    except Exception:
        pass
    return redirect(request.referrer or url_for("dashboard"))

# =========================== Reels, questions, wallpapers ===========================
@app.get("/wish")
@login_required
def wish():
    return render_template("wish.html")

@app.get("/api/videos")
@login_required
def api_videos():
    items = [{"url": f"/media/{d['id']}", "person": d["owner_name"], "caption": d.get("caption", "")} for d in mfind("media", {"kind": "video"}, limit=60)]
    items += [{"url": d["url"], "person": d.get("person", "Other"), "caption": ""} for d in mongo_list("videos")]
    for p in ("leela", "chinni", "mahi", "charan", "other"):
        items += [{"url": u, "person": p.title(), "caption": ""} for u in static_files(f"videos/{p}") if u.lower().endswith((".mp4", ".webm", ".mov"))]
    return jsonify(items)

@app.get("/questions")
@login_required
def questions():
    qs = Question.query.order_by(func.random()).limit(10).all()   # 10 random of the 40
    return render_template("questions.html", qs=[{"text": q.text, "popup": q.popup} for q in qs])

def wallpaper_ids():
    m = mongo()
    try:
        return [str(d["_id"]) for d in m.wallpapers.find({}, {"_id": 1}).sort("at", -1)] if m is not None else []
    except Exception:
        return []

@app.get("/api/wallpapers")
def api_wallpapers():
    """Birthday on -> the birthday person's photos become the background (members only). Otherwise admin wallpapers."""
    urls, u = [], current_user()
    if u and u.status == "approved":
        b = bday_user()
        if b:
            urls = [f"/media/{d['id']}" for d in mfind("media", {"owner_uid": b.id, "kind": "photo"}, limit=30)]
    urls = urls or [f"/media/wp/{i}" for i in wallpaper_ids()] or static_files("wallpapers")
    random.shuffle(urls)
    return jsonify(urls)

@app.get("/media/wp/<wid>")
def media_wallpaper(wid):
    from bson import ObjectId
    m = mongo()
    try:
        doc = m.wallpapers.find_one({"_id": ObjectId(wid)}) if m is not None else None
    except Exception:
        doc = None
    if not doc:
        abort(404)
    return Response(doc["data"], mimetype="image/jpeg", headers={"Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff"})

# =========================== Wishes & replies (MongoDB, internal only) ===========================
def sent_today(uid):
    m = mongo()
    try:
        return m.messages.count_documents({"from_uid": uid, "at": {"$gte": day_start()}}) if m is not None else 0
    except Exception:
        return 0

@app.get("/messages")
@login_required
def messages():
    me, b, m = current_user(), bday_user(), mongo()
    inbox = mfind("messages", {"to_uid": me.id}, limit=60)
    sent = mfind("messages", {"from_uid": me.id}, limit=20)
    try:
        if m is not None:
            m.messages.update_many({"to_uid": me.id, "read": False}, {"$set": {"read": True}})
    except Exception:
        pass
    return render_template("messages.html", inbox=inbox, sent=sent, b=b, is_bday=bool(b and b.id == me.id),
                           left=max(0, MSG_LIMIT - sent_today(me.id)), chars=MSG_CHARS, limit=MSG_LIMIT, mongo_ok=m is not None)

@app.post("/api/messages")
@login_required
def api_send_message():
    me, d, m = current_user(), request.get_json(silent=True) or {}, mongo()
    text = str(d.get("text") or "").strip()
    if not text:
        return jsonify(error="Write something first."), 400
    if len(text) > MSG_CHARS:
        return jsonify(error=f"Keep it under {MSG_CHARS} characters."), 400
    b = bday_user()
    if not b:
        return jsonify(error="There's no birthday today, so wishes are closed."), 400
    if m is None:
        return jsonify(error="MongoDB isn't connected on the server."), 503
    try:
        if me.id == b.id:   # the birthday person replies to someone who wished them
            to = db.session.get(User, int(d.get("to") or 0))
            if not to or to.id == me.id or not m.messages.count_documents({"from_uid": to.id, "to_uid": me.id}):
                return jsonify(error="You can reply to people who wished you."), 400
            kind = "reply"
        else:
            to, kind = b, "wish"
        used = sent_today(me.id)
        if used >= MSG_LIMIT:
            return jsonify(error=f"Daily limit reached ({MSG_LIMIT} messages). Come back tomorrow!"), 429
        m.messages.insert_one({"from_uid": me.id, "from_name": me.name, "to_uid": to.id, "to_name": to.name,
                               "text": text, "kind": kind, "read": False, "at": datetime.now(timezone.utc)})
    except (TypeError, ValueError):
        return jsonify(error="Pick someone to reply to."), 400
    except Exception:
        return jsonify(error="Couldn't save the message. Try again."), 503
    return jsonify(ok=True, left=MSG_LIMIT - used - 1)

# =========================== Admin dashboard ===========================
@app.get("/admin")
@admin_required
def admin():
    users = sorted(User.query.all(), key=lambda x: (x.status != "pending", -x.id))   # waiting requests first
    stats = {"total": len(users), "pending": sum(x.status == "pending" for x in users),
             "approved": sum(x.status == "approved" for x in users), "bday": next((x.name for x in users if x.bday_today), "—")}
    return render_template("admin.html", users=users, me=session["uid"], stats=stats, walls=wallpaper_ids(),
                           notes=mfind("announcements", limit=10), builtin=env("ADMIN_USERNAME", "Chinni_Admin"),
                           mongo_ok=mongo() is not None, msg=request.args.get("msg"))

@app.post("/admin/<int:uid>/<action>")
@admin_required
def admin_action(uid, action):
    status = {"approve": "approved", "reject": "rejected", "revoke": "rejected"}.get(action)
    u = db.session.get(User, uid)
    if u and status and u.id != session["uid"]:
        u.status = status
        if status != "approved":
            u.bday_today = False
        db.session.commit()
        if status == "approved":
            send_mail(u.email, "You're in! 🎂", f"Hi {u.name}, the host approved you. Sign in here: {url_for('home', _external=True)}")
    return redirect(url_for("admin"))

@app.post("/admin/<int:uid>/toggle/<field>")
@admin_required
def admin_toggle(uid, field):
    if field not in ("bday_today", "can_upload"):
        abort(400)
    u = db.session.get(User, uid)
    if u and u.status == "approved":
        new = not getattr(u, field)
        if field == "bday_today" and new:
            User.query.update({"bday_today": False})   # one birthday star at a time
        setattr(u, field, new); db.session.commit()
    return redirect(url_for("admin"))

def reset_serializer():
    return URLSafeTimedSerializer(app.secret_key, salt="pw-reset")

@app.post("/admin/<int:uid>/reset")
@admin_required
def admin_reset(uid):
    u = db.session.get(User, uid)
    if not u or (u.is_admin and u.username == env("ADMIN_USERNAME", "Chinni_Admin")):
        return jsonify(error="Change the admin password with the ADMIN_PASSWORD variable on Render."), 400
    token = reset_serializer().dumps({"id": u.id, "h": (u.pw_hash or "")[-10:]})   # dies once the password changes
    return jsonify(ok=True, name=u.name, email=u.email, link=url_for("reset_password", token=token, _external=True))

@app.route("/reset/<token>", methods=["GET", "POST"])
def reset_password(token):
    try:
        d = reset_serializer().loads(token, max_age=86400)
        u = db.session.get(User, d["id"])
        if not u or (u.pw_hash or "")[-10:] != d["h"]:
            raise BadSignature("used")
    except (BadSignature, SignatureExpired):
        return render_template("reset.html", bad=True, err=None), 400
    if request.method == "POST":
        pw, pw2 = request.form.get("password", ""), request.form.get("confirm", "")
        if len(pw) < 6 or pw != pw2:
            return render_template("reset.html", bad=False, err="Passwords must match and be at least 6 characters.", name=u.name)
        u.pw_hash = generate_password_hash(pw); db.session.commit()
        return redirect(url_for("home", msg="Password updated. Please log in."))
    return render_template("reset.html", bad=False, err=None, name=u.name)

@app.post("/admin/announce")
@admin_required
def announce():
    text, m = request.form.get("text", "").strip()[:200], mongo()
    if text and m is not None:
        m.announcements.insert_one({"text": text, "by": session["name"], "at": datetime.now(timezone.utc)})
    return redirect(url_for("admin", msg="Message posted to every dashboard." if text else None))

@app.post("/admin/announce/<aid>/delete")
@admin_required
def announce_delete(aid):
    from bson import ObjectId
    m = mongo()
    try: m.announcements.delete_one({"_id": ObjectId(aid)})
    except Exception: pass
    return redirect(url_for("admin"))

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
            m.wallpapers.insert_one({"filename": f.filename[:80], "data": Binary(compress(f.read())), "at": datetime.now(timezone.utc)}); ok += 1
        except Exception:
            bad += 1
    return redirect(url_for("admin", msg=f"Uploaded {ok} wallpaper(s)." + (f" {bad} skipped." if bad else "")))

@app.post("/admin/wallpapers/<wid>/delete")
@admin_required
def delete_wallpaper(wid):
    from bson import ObjectId
    m = mongo()
    try: m.wallpapers.delete_one({"_id": ObjectId(wid)})
    except Exception: pass
    return redirect(url_for("admin"))

# =========================== Fun fallback (login failed) ===========================
@app.get("/oops")
def oops():
    return render_template("error.html", memes=mongo_list("memes") or [{"url": u} for u in static_files("memes")])

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
