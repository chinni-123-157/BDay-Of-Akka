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
