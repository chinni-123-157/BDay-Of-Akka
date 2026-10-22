# Birthday Hub

Flask app: Neon Postgres (accounts, settings, reset tokens) + MongoDB (wishes, media via GridFS, announcements, daily quotas).

## Deploy on Render
1. Push this folder to GitHub, then create a **Web Service** (or use `render.yaml`).
2. Build: `pip install -r requirements.txt`  Start: `gunicorn app:app --workers 2 --threads 4 --timeout 120 --bind 0.0.0.0:$PORT`
3. Health check path: `/healthz`
4. Set environment variables: `DATABASE_URL`, `MONGO_URI`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`,
   `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `SECRET_KEY`, `ADMIN_EMAILS` (optional), `PYTHON_VERSION=3.12.8`.
5. Google Cloud Console > Credentials > your OAuth client > add Authorized redirect URI:
   `https://<your-service>.onrender.com/auth/google/callback`
6. MongoDB Atlas: allow Render's outbound IPs (or 0.0.0.0/0 for the free tier) in Network Access.

Tables are created automatically on first start.

## Optional variables
`APP_TIMEZONE` (default Asia/Kolkata, decides "today" and daily limits), `MAX_UPLOAD_MB` (default 30),
`MEME_MESSAGE` (text on the error page), `APP_ENV=development` (local HTTP cookies).

## Local run
```
pip install -r requirements.txt
export $(grep -v '^#' .env | xargs)   # after copying .env.example to .env
APP_ENV=development python app.py
```

## Behaviour summary
- **Admin** = `ADMIN_USERNAME`/`ADMIN_PASSWORD` (or a Google account in `ADMIN_EMAILS`). Nothing is hard-coded.
- **New users** start as `pending` and only see the waiting page until the admin approves them.
- **Admin switches** (Overview tab):
  - *Birthday today*: highlights birthday people on every page and shows random wallpapers to everyone.
  - *Birthday privileges*: the birthday person can comment privately on posts, post media and play the fun questions.
  - *Upload for everyone*: any approved member can post. Each member also has their own "Can post" switch (Members tab).
- **Birthday people**: tick one or more members, or tick none to use each member's date of birth.
- **Comments**: only the birthday person can write them, and only the post's owner and the admin can read them.
- **Activity**: likes ("Birthday user Akka liked your post") and comments show for the post's owner; the admin sees everything.
- **Gifts**: any member can send text, voice or video. All approved members see them on the Gift wall and dashboards.
- **Deleting** a post, gift, wish, announcement or wallpaper removes the database record, the stored file and related activity.
- **Wishes**: 50 characters max, 10 messages per user per day (replies count), stored in MongoDB.
- **Password reset**: admin clicks Reset password > popup > Open email app / Open Gmail with a one-time link (valid 1 hour).
- Existing databases upgrade automatically on start (`can_upload` column and new settings are added if missing).
