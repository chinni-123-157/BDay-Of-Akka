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
