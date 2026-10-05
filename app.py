<<<<<<< HEAD
"""Birthday Hub - Flask app. Postgres (Neon) = accounts/settings, MongoDB = messages, media (GridFS)."""
import hashlib
import hmac
import logging
import os
import random
import re
import secrets
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from urllib.parse import quote
from zoneinfo import ZoneInfo

import gridfs
import psycopg
from authlib.integrations.flask_client import OAuth
from bson import ObjectId
from bson.errors import InvalidId
from flask import (Flask, Response, abort, g, jsonify, redirect,
                   render_template, request, session, url_for)
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFError, CSRFProtect
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pymongo import DESCENDING, MongoClient, ReturnDocument
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from questions import NO_POPUPS, QUESTIONS, YES_POPUPS

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("birthday-hub")

# ---------------------------------------------------------------- config
IS_DEV = os.environ.get("APP_ENV") == "development"
ADMIN_USER = os.environ.get("ADMIN_USERNAME", "")
ADMIN_PASS = os.environ.get("ADMIN_PASSWORD", "")
ADMIN_EMAILS = {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}
GOOGLE_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
TZ = ZoneInfo(os.environ.get("APP_TIMEZONE", "Asia/Kolkata"))
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "30"))
MEME_MESSAGE = os.environ.get("MEME_MESSAGE", "Ame Ra bala raju emina pani chesuko ra")
WISH_MAX_LEN, DAILY_WISH_LIMIT, DAILY_UPLOAD_LIMIT = 50, 10, 10
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
CID_RE = re.compile(r"^[A-Z0-9]{10}$")

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config.update(
    SECRET_KEY=os.environ["SECRET_KEY"],
    MAX_CONTENT_LENGTH=MAX_UPLOAD_MB * 1024 * 1024 + 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=not IS_DEV,
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
    WTF_CSRF_TIME_LIMIT=None,
)
csrf = CSRFProtect(app)
limiter = Limiter(get_remote_address, app=app, storage_uri="memory://", default_limits=[])

# ---------------------------------------------------------------- databases
pool = ConnectionPool(
    os.environ["DATABASE_URL"], min_size=1, max_size=5, open=False,
    kwargs={"row_factory": dict_row}, check=ConnectionPool.check_connection,
)
pool.open()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id SERIAL PRIMARY KEY,
  full_name TEXT NOT NULL,
  college_id TEXT NOT NULL UNIQUE CHECK (college_id ~ '^[A-Z0-9]{10}$'),
  email TEXT NOT NULL,
  password_hash TEXT NOT NULL,
  dob DATE,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','denied')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE UNIQUE INDEX IF NOT EXISTS users_email_lower ON users (lower(email));
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reset_tokens(
  token_hash TEXT PRIMARY KEY,
  user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  expires_at TIMESTAMPTZ NOT NULL,
  used BOOLEAN NOT NULL DEFAULT FALSE);
INSERT INTO settings(key, value) VALUES ('birthday_on','0'),('upload_on','0'),('birthday_user_id','0')
  ON CONFLICT (key) DO NOTHING;
"""
with pool.connection() as _c:
    _c.execute("SELECT pg_advisory_xact_lock(727274)")  # safe with several gunicorn workers
    _c.execute(SCHEMA)

mongo = MongoClient(os.environ["MONGO_URI"], serverSelectionTimeoutMS=8000)
mdb = mongo.get_default_database(default="birthday_app")
fs = gridfs.GridFS(mdb)
c_msgs, c_media, c_wall = mdb.messages, mdb.media, mdb.wallpapers
c_ann, c_quota = mdb.announcements, mdb.quota
c_msgs.create_index([("to_id", 1), ("created_at", -1)])
c_msgs.create_index([("from_id", 1), ("created_at", -1)])
c_media.create_index([("created_at", -1)])
c_quota.create_index("at", expireAfterSeconds=172800)


def q(sql, params=(), fetch=None):
    with pool.connection() as conn:
        cur = conn.execute(sql, params)
        if fetch == "one":
            return cur.fetchone()
        if fetch == "all":
            return cur.fetchall()
        return cur.rowcount


# ---------------------------------------------------------------- helpers
def now():
    return datetime.now(TZ)


def today():
    return now().date()


def utcnow():
    return datetime.now(timezone.utc)


def wants_json():
    return request.path.startswith("/api/") or request.headers.get("X-Requested-With") == "fetch"


def jerr(msg, code=400):
    return jsonify(error=msg), code


def fail(code, msg=""):
    if wants_json():
        return jerr(msg or "Something went wrong.", code)
    return render_template("meme.html", page="meme", detail=msg, message=MEME_MESSAGE,
                           retry=url_for("index")), code


def settings():
    if not hasattr(g, "_s"):
        g._s = {r["key"]: r["value"] for r in q("SELECT key, value FROM settings", fetch="all")}
    return g._s


def flag(key):
    return settings().get(key, "0") == "1"


def todays_birthdays():
    if hasattr(g, "_bd"):
        return g._bd
    override = int(settings().get("birthday_user_id", "0") or 0)
    if override:
        rows = q("SELECT id, full_name FROM users WHERE id=%s AND status='approved'", (override,), "all")
    else:
        t = today()
        rows = q("SELECT id, full_name FROM users WHERE status='approved' AND dob IS NOT NULL "
                 "AND EXTRACT(MONTH FROM dob)=%s AND EXTRACT(DAY FROM dob)=%s ORDER BY full_name",
                 (t.month, t.day), "all")
    g._bd = rows
    return rows


def is_bday_user():
    u = getattr(g, "user", None)
    return bool(u and not g.is_admin and flag("birthday_on")
                and any(b["id"] == u["id"] for b in todays_birthdays()))


def can_upload():
    return bool(getattr(g, "user", None) and (g.is_admin or flag("upload_on") or is_bday_user()))


def take_quota(key, limit):
    """Atomic per-day counter. Returns True if the action is allowed."""
    qid = f"{key}:{today().isoformat()}"
    doc = c_quota.find_one_and_update(
        {"_id": qid}, {"$inc": {"n": 1}, "$setOnInsert": {"at": utcnow()}},
        upsert=True, return_document=ReturnDocument.AFTER)
    if doc["n"] > limit:
        c_quota.update_one({"_id": qid}, {"$inc": {"n": -1}})
        return False
    return True


def quota_left(key, limit):
    d = c_quota.find_one({"_id": f"{key}:{today().isoformat()}"})
    return max(limit - (d["n"] if d else 0), 0)


def sniff(head: bytes):
    """Identify real file type from magic bytes (never trust client MIME)."""
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg", "image"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png", "image"
    if head[:4] == b"GIF8":
        return "image/gif", "image"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp", "image"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"heic", b"heix", b"mif1", b"avif", b"msf1"):
            return None, None
        return ("video/quicktime" if brand == b"qt  " else "video/mp4"), "video"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "video/webm", "video"
    return None, None


def store_upload(f, images_only=False):
    head = f.stream.read(16)
    f.stream.seek(0)
    mime, kind = sniff(head)
    if not mime or (images_only and (kind != "image" or mime == "image/gif")):
        return None
    name = secure_filename(f.filename or "") or "upload"
    fid = fs.put(f.stream, filename=name[:80], content_type=mime)
    return fid, mime, kind


def pick_wallpaper():
    doc = next(c_wall.aggregate([{"$sample": {"size": 1}}]), None)
    return url_for("serve_file", fid=str(doc["file_id"])) if doc else None


def media_items(limit=50):
    uid = g.user["id"]
    out = []
    for m in c_media.find().sort("created_at", DESCENDING).limit(limit):
        likes = m.get("likes", [])
        out.append(dict(id=str(m["_id"]), kind=m["kind"], caption=m.get("caption", ""),
                        url=url_for("serve_file", fid=str(m["file_id"])),
                        uploader_name=m["uploader_name"], uploader_email=m.get("uploader_email", ""),
                        likes=len(likes), liked=uid in likes))
    return out


def valid_password(pw):
    return 8 <= len(pw) <= 128


def find_user(ident):
    ident = ident.strip()
    if not ident:
        return None
    return q("SELECT * FROM users WHERE lower(email)=lower(%s) OR college_id=upper(%s)", (ident, ident), "one")


# ---------------------------------------------------------------- request lifecycle
@app.before_request
def load_user():
    g.user, g.is_admin = None, False
    if request.endpoint in ("static", "healthz"):
        return
    if session.get("admin"):
        g.is_admin = True
        g.user = {"id": 0, "full_name": session.get("admin_name", "Admin"), "email": "", "status": "approved"}
    elif session.get("uid"):
        g.user = q("SELECT * FROM users WHERE id=%s", (session["uid"],), "one")
        if not g.user:
            session.clear()


@app.context_processor
def inject():
    u = getattr(g, "user", None)
    on = bool(u and flag("birthday_on"))
    return dict(user=u, is_admin=getattr(g, "is_admin", False), bday_on=on,
                bdays=todays_birthdays() if on else [], is_bday=is_bday_user() if u else False,
                can_upload=can_upload(), max_mb=MAX_UPLOAD_MB, wish_max=WISH_MAX_LEN,
                pick_wallpaper=pick_wallpaper)


@app.after_request
def headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
    if resp.mimetype == "text/html":
        resp.headers["Cache-Control"] = "no-store"
    return resp


def approved_required(f):
    @wraps(f)
    def w(*a, **k):
        if not g.user:
            return fail(401, "Please sign in.") if wants_json() else redirect(url_for("login"))
        if not g.is_admin and g.user["status"] != "approved":
            return fail(403, "Waiting for admin approval.") if wants_json() else redirect(url_for("pending"))
        return f(*a, **k)
    return w


def admin_required(f):
    @wraps(f)
    def w(*a, **k):
        if not g.is_admin:
            return fail(403, "Admin only.") if wants_json() else redirect(url_for("login"))
        return f(*a, **k)
    return w


@app.errorhandler(CSRFError)
def _csrf(e):
    return fail(400, "Your session expired. Refresh the page and try again.")


for _code, _msg in ((403, "You don't have access to this."), (404, "Page not found."),
                    (413, f"File too large. Max {MAX_UPLOAD_MB} MB."), (429, "Too many tries. Wait a minute.")):
    app.register_error_handler(_code, lambda e, c=_code, m=_msg: fail(c, m))


@app.get("/healthz")
def healthz():
    return "ok"


# ---------------------------------------------------------------- auth
@app.get("/")
def index():
    if g.is_admin:
        return redirect(url_for("admin"))
    return redirect(url_for("dashboard" if g.user else "login"))


@app.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute", methods=["POST"])
def login():
    if request.method == "GET":
        if g.user:
            return redirect(url_for("index"))
        return render_template("login.html", page="login", google=bool(GOOGLE_ID and GOOGLE_SECRET))
    ident = request.form.get("identifier", "").strip()
    pw = request.form.get("password", "")
    user_ok = bool(ADMIN_USER) and hmac.compare_digest(ident.encode(), ADMIN_USER.encode())
    pass_ok = bool(ADMIN_PASS) and hmac.compare_digest(pw.encode(), ADMIN_PASS.encode())
    if user_ok and pass_ok:
        session.clear()
        session.permanent = True
        session.update(admin=True, admin_name=ident)
        return redirect(url_for("admin"))
    u = find_user(ident)
    if u and check_password_hash(u["password_hash"], pw):
        session.clear()
        session.permanent = True
        session["uid"] = u["id"]
        return redirect(url_for("dashboard"))
    return fail(401)  # meme page only; no hint about which field was wrong


@app.route("/signup", methods=["GET", "POST"])
@limiter.limit("10 per hour", methods=["POST"])
def signup():
    if request.method == "GET":
        return render_template("signup.html", page="signup",
                               g_email=session.get("g_email", ""), g_name=session.get("g_name", ""))
    name = " ".join(request.form.get("full_name", "").split())
    cid = "".join(request.form.getlist("cid")).strip().upper()
    email = (session.get("g_email") or request.form.get("email", "")).strip().lower()
    pw = request.form.get("password", "")
    dob_raw = request.form.get("dob", "").strip()
    dob = None
    if not 2 <= len(name) <= 80:
        return fail(400, "Enter your full name.")
    if not CID_RE.match(cid):
        return fail(400, "College ID must be exactly 10 letters or digits.")
    if len(email) > 254 or not EMAIL_RE.match(email):
        return fail(400, "Enter a valid email address.")
    if not valid_password(pw):
        return fail(400, "Password must be 8 to 128 characters.")
    if dob_raw:
        try:
            dob = date.fromisoformat(dob_raw)
        except ValueError:
            return fail(400, "Enter a valid date of birth.")
        if not date(1950, 1, 1) <= dob <= today():
            return fail(400, "Enter a valid date of birth.")
    try:
        row = q("INSERT INTO users(full_name, college_id, email, password_hash, dob) VALUES (%s,%s,%s,%s,%s) RETURNING id",
                (name, cid, email, generate_password_hash(pw), dob), "one")
    except psycopg.errors.UniqueViolation:
        return fail(400, "That College ID or email is already registered.")
    session.clear()
    session.permanent = True
    session["uid"] = row["id"]
    return redirect(url_for("pending"))


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/pending")
def pending():
    if not g.user:
        return redirect(url_for("login"))
    if g.is_admin or g.user["status"] == "approved":
        return redirect(url_for("index"))
    return render_template("pending.html", page="pending")


oauth = OAuth(app)
if GOOGLE_ID and GOOGLE_SECRET:
    oauth.register("google", client_id=GOOGLE_ID, client_secret=GOOGLE_SECRET,
                   server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
                   client_kwargs={"scope": "openid email profile"})


@app.get("/auth/google")
def google_login():
    if not (GOOGLE_ID and GOOGLE_SECRET):
        abort(404)
    return oauth.google.authorize_redirect(url_for("google_callback", _external=True))


@app.get("/auth/google/callback")
def google_callback():
    if not (GOOGLE_ID and GOOGLE_SECRET):
        abort(404)
    try:
        info = oauth.google.authorize_access_token().get("userinfo") or {}
    except Exception:
        log.exception("Google OAuth failed")
        return fail(401)
    email = (info.get("email") or "").lower()
    if not email or not info.get("email_verified"):
        return fail(401)
    if email in ADMIN_EMAILS:
        session.clear()
        session.permanent = True
        session.update(admin=True, admin_name=info.get("name") or "Admin")
        return redirect(url_for("admin"))
    u = q("SELECT id FROM users WHERE lower(email)=%s", (email,), "one")
    session.clear()
    if u:
        session.permanent = True
        session["uid"] = u["id"]
        return redirect(url_for("dashboard"))
    session.update(g_email=email, g_name=info.get("name", ""))
    return redirect(url_for("signup"))


def _hash_token(t):
    return hashlib.sha256(t.encode()).hexdigest()


@app.route("/reset/<token>", methods=["GET", "POST"])
@limiter.limit("20 per hour", methods=["POST"])
def reset(token):
    row = q("SELECT * FROM reset_tokens WHERE token_hash=%s AND NOT used AND expires_at>now()",
            (_hash_token(token),), "one")
    if not row:
        return render_template("reset.html", page="reset", valid=False), 400
    if request.method == "GET":
        return render_template("reset.html", page="reset", valid=True)
    pw = request.form.get("password", "")
    if not valid_password(pw) or pw != request.form.get("confirm", ""):
        return fail(400, "Passwords must match and be 8 to 128 characters.")
    q("UPDATE users SET password_hash=%s WHERE id=%s", (generate_password_hash(pw), row["user_id"]))
    q("UPDATE reset_tokens SET used=TRUE WHERE user_id=%s", (row["user_id"],))
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------- user pages
@app.get("/dashboard")
@approved_required
def dashboard():
    if g.is_admin:
        return redirect(url_for("admin"))
    if is_bday_user():
        return redirect(url_for("birthday"))
    ann = [dict(text=a["text"], at=a["created_at"]) for a in c_ann.find().sort("created_at", -1).limit(5)]
    return render_template("dashboard.html", page="dashboard", announcements=ann,
                           items=media_items(6), left=quota_left(f"w:{g.user['id']}", DAILY_WISH_LIMIT))


@app.get("/birthday")
@approved_required
def birthday():
    if not is_bday_user():
        return redirect(url_for("index"))
    return render_template("birthday.html", page="birthday", items=media_items(12),
                           left=quota_left(f"w:{g.user['id']}", DAILY_WISH_LIMIT))


@app.get("/questions")
@approved_required
def questions():
    if not (g.is_admin or is_bday_user()):
        return redirect(url_for("index"))
    return render_template("questions.html", page="questions")


@app.get("/reels")
@approved_required
def reels():
    ids = {b["id"] for b in todays_birthdays()} if flag("birthday_on") else set()
    rows = q("SELECT id, full_name FROM users WHERE status='approved' ORDER BY full_name LIMIT 40", fetch="all")
    people = [dict(first=r["full_name"].split()[0], hl=r["id"] in ids) for r in rows]
    people.sort(key=lambda p: not p["hl"])
    return render_template("reels.html", page="reels", items=media_items(50), people=people[:6],
                           more=max(len(people) - 6, 0))


@app.get("/gift")
@approved_required
def gift():
    return render_template("gift.html", page="gift")


# ---------------------------------------------------------------- files
@app.get("/f/<fid>")
@approved_required
def serve_file(fid):
    try:
        f = fs.get(ObjectId(fid))
    except (InvalidId, gridfs.errors.NoFile):
        abort(404)
    size, start, end, status = f.length, 0, f.length - 1, 200
    m = re.match(r"bytes=(\d*)-(\d*)$", request.headers.get("Range", ""))
    if m and (m[1] or m[2]):
        if m[1]:
            start = int(m[1])
            end = int(m[2]) if m[2] else size - 1
        else:
            start = max(size - int(m[2]), 0)
        end = min(end, size - 1)
        if start > end or start >= size:
            return Response(status=416, headers={"Content-Range": f"bytes */{size}"})
        status = 206
    f.seek(start)
    remaining = end - start + 1

    def gen():
        left = remaining
        while left > 0:
            chunk = f.read(min(256 * 1024, left))
            if not chunk:
                break
            left -= len(chunk)
            yield chunk

    resp = Response(gen(), status=status, mimetype=f.content_type or "application/octet-stream",
                    direct_passthrough=True)
    resp.headers.update({"Accept-Ranges": "bytes", "Content-Length": str(remaining),
                         "Cache-Control": "private, max-age=3600"})
    if status == 206:
        resp.headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return resp


# ---------------------------------------------------------------- API: wishes
def _fmt_msg(m, me):
    return dict(id=str(m["_id"]), from_id=m["from_id"], from_name=m["from_name"], to_id=m["to_id"],
                to_name=m.get("to_name", ""), text=m["text"], parent_id=m.get("parent_id"),
                at=m["created_at"].replace(tzinfo=timezone.utc).isoformat(), mine=m["from_id"] == me)


@app.get("/api/wishes")
@approved_required
def api_wishes_list():
    me = g.user["id"]
    if g.is_admin:
        return jsonify(wishes=[], left=0)
    flt = {"$or": [{"to_id": me}, {"from_id": me}]}
    docs = c_msgs.find(flt).sort("created_at", DESCENDING).limit(100)
    return jsonify(wishes=[_fmt_msg(m, me) for m in docs], left=quota_left(f"w:{me}", DAILY_WISH_LIMIT))


@app.post("/api/wishes")
@approved_required
def api_wishes_post():
    if g.is_admin:
        return jerr("Admins can't send wishes.", 403)
    if not flag("birthday_on"):
        return jerr("Wishes open on the birthday.", 403)
    d = request.get_json(silent=True) or {}
    text = " ".join(str(d.get("text", "")).split())
    if not text:
        return jerr("Write a message first.")
    if len(text) > WISH_MAX_LEN:
        return jerr(f"Keep it within {WISH_MAX_LEN} characters.")
    me, bds = g.user["id"], {b["id"]: b["full_name"] for b in todays_birthdays()}
    try:
        to_id = int(d.get("to_id") or 0)
    except (TypeError, ValueError):
        return jerr("Invalid recipient.")
    if me in bds:  # birthday person replies to someone who wished them
        if not c_msgs.find_one({"from_id": to_id, "to_id": me}):
            return jerr("You can only reply to people who wished you.")
        row = q("SELECT full_name FROM users WHERE id=%s", (to_id,), "one")
        to_name = row["full_name"] if row else ""
    else:
        if not to_id and len(bds) == 1:
            to_id = next(iter(bds))
        if to_id not in bds:
            return jerr("Choose who to wish.")
        to_name = bds[to_id]
    if not take_quota(f"w:{me}", DAILY_WISH_LIMIT):
        return jerr(f"Daily limit reached ({DAILY_WISH_LIMIT} messages). Try again tomorrow.", 429)
    c_msgs.insert_one(dict(from_id=me, from_name=g.user["full_name"], to_id=to_id, to_name=to_name,
                           text=text, parent_id=d.get("parent_id"), created_at=utcnow()))
    return jsonify(ok=True, left=quota_left(f"w:{me}", DAILY_WISH_LIMIT))


# ---------------------------------------------------------------- API: questions
@app.get("/api/questions")
@approved_required
def api_questions():
    if not (g.is_admin or is_bday_user()):
        return jerr("Only the birthday person can play.", 403)
    picks = random.sample(QUESTIONS, 10)
    return jsonify(questions=[dict(q=t, yes=random.choice(YES_POPUPS), no=random.choice(NO_POPUPS)) for t in picks])


# ---------------------------------------------------------------- API: media
@app.post("/api/media")
@approved_required
def api_media_upload():
    if not can_upload():
        return jerr("Uploads are closed right now.", 403)
    f = request.files.get("file")
    if not f or not f.filename:
        return jerr("Choose a file first.")
    uid = g.user["id"]
    if not take_quota(f"u:{uid}", DAILY_UPLOAD_LIMIT):
        return jerr(f"Daily upload limit reached ({DAILY_UPLOAD_LIMIT}).", 429)
    stored = store_upload(f)
    if not stored:
        c_quota.update_one({"_id": f"u:{uid}:{today().isoformat()}"}, {"$inc": {"n": -1}})
        return jerr("Only JPG, PNG, WEBP, GIF, MP4, WEBM or MOV files are allowed.")
    fid, mime, kind = stored
    c_media.insert_one(dict(file_id=fid, kind=kind, mime=mime, caption=request.form.get("caption", "").strip()[:80],
                            uploader_id=uid, uploader_name=g.user["full_name"], uploader_email=g.user.get("email", ""),
                            likes=[], created_at=utcnow()))
    return jsonify(ok=True)


@app.post("/api/media/<mid>/like")
@approved_required
def api_media_like(mid):
    try:
        oid = ObjectId(mid)
    except InvalidId:
        return jerr("Not found.", 404)
    uid = g.user["id"]
    m = c_media.find_one({"_id": oid}, {"likes": 1})
    if not m:
        return jerr("Not found.", 404)
    liked = uid not in m.get("likes", [])
    c_media.update_one({"_id": oid}, {"$addToSet": {"likes": uid}} if liked else {"$pull": {"likes": uid}})
    count = len(c_media.find_one({"_id": oid}, {"likes": 1}).get("likes", []))
    return jsonify(liked=liked, count=count)


# ---------------------------------------------------------------- admin
@app.get("/admin")
@admin_required
def admin():
    users = q("SELECT id, full_name, college_id, email, dob, status, created_at FROM users "
              "ORDER BY (status='pending') DESC, created_at DESC", fetch="all")
    counts = {r["status"]: r["c"] for r in q("SELECT status, count(*) c FROM users GROUP BY status", fetch="all")}
    walls = [dict(id=str(w["_id"]), url=url_for("serve_file", fid=str(w["file_id"]))) for w in c_wall.find().sort("created_at", -1)]
    anns = [dict(id=str(a["_id"]), text=a["text"]) for a in c_ann.find().sort("created_at", -1).limit(10)]
    return render_template("admin.html", page="admin", users=users, counts=counts, s=settings(), walls=walls,
                           anns=anns, items=media_items(12),
                           approved=[u for u in users if u["status"] == "approved"])


@app.post("/admin/toggle/<key>")
@admin_required
def admin_toggle(key):
    if key not in ("birthday_on", "upload_on"):
        abort(404)
    on = bool((request.get_json(silent=True) or {}).get("value"))
    q("INSERT INTO settings(key,value) VALUES (%s,%s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
      (key, "1" if on else "0"))
    return jsonify(ok=True, value=on)


@app.post("/admin/birthday-user")
@admin_required
def admin_birthday_user():
    try:
        uid = int((request.get_json(silent=True) or {}).get("user_id") or 0)
    except (TypeError, ValueError):
        return jerr("Invalid user.")
    if uid and not q("SELECT 1 FROM users WHERE id=%s AND status='approved'", (uid,), "one"):
        return jerr("Pick an approved member.")
    q("UPDATE settings SET value=%s WHERE key='birthday_user_id'", (str(uid),))
    return jsonify(ok=True)


@app.post("/admin/users/<int:uid>/status")
@admin_required
def admin_status(uid):
    st = (request.get_json(silent=True) or {}).get("status")
    if st not in ("approved", "denied", "pending"):
        return jerr("Invalid status.")
    if not q("UPDATE users SET status=%s WHERE id=%s", (st, uid)):
        return jerr("User not found.", 404)
    return jsonify(ok=True)


@app.post("/admin/users/<int:uid>/reset")
@admin_required
def admin_reset(uid):
    u = q("SELECT id, full_name, email FROM users WHERE id=%s", (uid,), "one")
    if not u:
        return jerr("User not found.", 404)
    token = secrets.token_urlsafe(32)
    q("DELETE FROM reset_tokens WHERE user_id=%s OR expires_at<now()", (uid,))
    q("INSERT INTO reset_tokens(token_hash, user_id, expires_at) VALUES (%s,%s,now()+interval '1 hour')",
      (_hash_token(token), uid))
    link = url_for("reset", token=token, _external=True)
    subject = "Reset your Birthday Hub password"
    body = (f"Hi {u['full_name']},\n\nUse this link to set a new password (valid for 1 hour):\n{link}\n\n"
            "If you didn't expect this, ignore this email.")
    qs = f"subject={quote(subject)}&body={quote(body)}"
    return jsonify(ok=True, name=u["full_name"], email=u["email"], link=link,
                   mailto=f"mailto:{quote(u['email'])}?{qs}",
                   gmail=f"https://mail.google.com/mail/?view=cm&fs=1&to={quote(u['email'])}&su={quote(subject)}&body={quote(body)}")


@app.post("/admin/wallpapers")
@admin_required
def admin_wall_add():
    files = [f for f in request.files.getlist("files") if f and f.filename]
    if not files:
        return jerr("Choose at least one image.")
    if c_wall.count_documents({}) + len(files) > 30:
        return jerr("Wallpaper limit is 30.")
    added = 0
    for f in files:
        stored = store_upload(f, images_only=True)
        if stored:
            c_wall.insert_one(dict(file_id=stored[0], created_at=utcnow()))
            added += 1
    if not added:
        return jerr("Wallpapers must be JPG, PNG or WEBP.")
    return jsonify(ok=True, added=added)


def _delete_doc(coll, did):
    try:
        doc = coll.find_one_and_delete({"_id": ObjectId(did)})
    except InvalidId:
        return jerr("Not found.", 404)
    if not doc:
        return jerr("Not found.", 404)
    if doc.get("file_id"):
        fs.delete(doc["file_id"])
    return jsonify(ok=True)


@app.post("/admin/wallpapers/<wid>/delete")
@admin_required
def admin_wall_del(wid):
    return _delete_doc(c_wall, wid)


@app.post("/admin/media/<mid>/delete")
@admin_required
def admin_media_del(mid):
    return _delete_doc(c_media, mid)


@app.post("/admin/announce")
@admin_required
def admin_announce():
    text = " ".join(str((request.get_json(silent=True) or {}).get("text", "")).split())
    if not 1 <= len(text) <= 200:
        return jerr("Message must be 1 to 200 characters.")
    c_ann.insert_one(dict(text=text, created_at=utcnow()))
    return jsonify(ok=True)


@app.post("/admin/announce/<aid>/delete")
@admin_required
def admin_announce_del(aid):
    return _delete_doc(c_ann, aid)


if __name__ == "__main__":
    app.run(debug=IS_DEV, port=int(os.environ.get("PORT", 5000)))
=======
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
>>>>>>> 2ca04a679866463af5912f6698af40ad97731308
