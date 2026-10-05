<<<<<<< HEAD
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
- Admin = `ADMIN_USERNAME`/`ADMIN_PASSWORD` (or a Google account listed in `ADMIN_EMAILS`). Nothing is hard-coded.
- New users start as `pending` and only see the waiting page until the admin approves them.
- Birthday toggle ON: today's birthday person (by date of birth, or the admin's manual pick) is highlighted on every page,
  random wallpapers appear for everyone, and the birthday person gets the special page, fun questions and upload rights.
- Wishes: 50 characters max, 10 messages per user per day (replies count), stored in MongoDB.
- Password reset: admin clicks Reset password > popup > Open email app / Open Gmail with the one-time link (valid 1 hour).
- The Gift screen is an animation demo only. It does not process or store any payment.
=======
# Radha Krishna Birthday Website

## Run locally

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
# Set the variables in .env, then load them in your shell or use your IDE's env-file setting.
flask --app app run --debug
```

Open `http://127.0.0.1:5000`.

## Add your content

- Put birthday videos in `static/media/` (`.mp4`, `.webm`, `.ogg`, or `.mov`). They are discovered automatically and play in sequence.
- Put your own Radha Krishna wallpapers in `static/images/` as `radha-krishna-1.jpg`, `radha-krishna-2.jpg`, and `radha-krishna-3.jpg`. The app rotates them; it keeps a polished gradient fallback until they are added.
- The error page’s meme cards are intentionally HTML/CSS comedy cards. Replace their markup with your own licensed Telugu meme images if desired.
- Change recipients only in `data/approved_recipients.json`. The server refuses all names and email addresses not in this list.

## NeonDB, MongoDB, and automatic email

Create a Neon project and a MongoDB Atlas database, then set the matching `DATABASE_URL` and `MONGODB_URI` in Render. Every Send/Like action is saved to both `birthday_events` collections (when configured). Add SMTP settings for automatic thank-you email. Recipients are read exclusively from `data/approved_recipients.json`, and the selected name is included in the subject and message.

## Deploy to Render

1. Push this folder to a GitHub repository.
2. In Render choose **New → Blueprint** and select that repository. It reads `render.yaml`.
3. Enter the values marked `sync: false` from `.env.example`, then deploy.
>>>>>>> 2ca04a679866463af5912f6698af40ad97731308
