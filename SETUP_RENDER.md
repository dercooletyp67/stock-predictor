# Running the predictor via Render + cron-job.org (no credit card)

## 1. Create a Render account
Go to https://render.com and sign up (e.g. with your GitHub account) — no card required for the free tier.

## 2. Create the Web Service
- In Render: **New +** > **Web Service**
- Connect your GitHub account and select the `stock-predictor` repo
- Render should detect `render.yaml` automatically (a "Blueprint") — if not, set manually:
  - **Runtime**: Python
  - **Build Command**: `pip install -r requirements.txt`
  - **Start Command**: `gunicorn app:app`
  - **Plan**: Free

## 3. Add environment variables
In the service's **Environment** tab, add:
- `DISCORD_WEBHOOK_URL` = your webhook URL (from your local `config.json`)
- `CHECK_SECRET` = any random password-like string you make up (e.g. `xk29fjq83nz`) — this stops random internet strangers from spamming your endpoint and your Discord.

## 4. Deploy and get your URL
Once deployed, Render gives you a URL like `https://stock-predictor-xxxx.onrender.com`. Test it works by visiting:
```
https://stock-predictor-xxxx.onrender.com/run-check?token=YOUR_CHECK_SECRET
```
You should see `{"status": "ok", "signals": {...}}`.

## 5. Set up the external pinger (cron-job.org)
This is what actually schedules the checks — Render's free web service alone would spin down after 15 min idle without it.
- Go to https://cron-job.org and create a free account (no card)
- Create a new cron job:
  - **URL**: `https://stock-predictor-xxxx.onrender.com/run-check?token=YOUR_CHECK_SECRET`
  - **Schedule**: every 3-5 minutes
  - **Enabled**: yes
- Save it

That's it — cron-job.org will now hit your Render service every few minutes, keeping it awake and triggering a real check each time, without any card on either service.

## Notes
- The old GitHub Actions workflow can stay as a backup, or you can disable it in the repo's **Actions** tab if you want to avoid duplicate notifications.
- If you disable GitHub Actions, `state.json` will now live only on Render's ephemeral disk — it may reset on redeploys/restarts, which just means it might re-notify once after a restart. Not a big deal.
