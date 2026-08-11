# Setting up /invest, /close, /status slash commands

This lets you tell the bot exactly what you invested in (ticker + long/short), and it'll
send you HOLD/SELL updates for that specific position — independent of whatever the
automatic scanner would have suggested.

## 1. Create a Discord Application
- Go to https://discord.com/developers/applications
- Click **New Application**, name it anything (e.g. "Captain Hook")
- In the left sidebar, go to **Bot** > click **Reset Token** > copy the token somewhere safe
  (you'll need it once, for step 4 — don't share it with anyone, including me)
- Go to **General Information** and copy:
  - **Application ID**
  - **Public Key**

## 2. Add the Public Key to Render
- In your Render service > **Environment** tab, add:
  - `DISCORD_PUBLIC_KEY` = the Public Key from step 1

## 3. Set the Interactions Endpoint URL
- Still in the Discord Developer Portal, **General Information** page
- Set **Interactions Endpoint URL** to:
  ```
  https://stock-predictor-ztsk.onrender.com/discord-interactions
  ```
- Discord will immediately test this URL (sends a PING) — it should show a green checkmark/save
  successfully. If it fails, double check `DISCORD_PUBLIC_KEY` is set correctly on Render and
  the service is awake (visit the URL in a browser first to wake it if needed).

## 4. Register the slash commands (one-time, run locally)
In this folder, run:

```bash
DISCORD_APPLICATION_ID=your_application_id DISCORD_BOT_TOKEN=your_bot_token python register_commands.py
```

(On Windows PowerShell: `$env:DISCORD_APPLICATION_ID="..."; $env:DISCORD_BOT_TOKEN="..."; python register_commands.py`)

This registers `/invest`, `/sold`, `/status` globally — can take up to an hour to show up
everywhere, but usually appears within a few minutes.

## 5. Invite the bot to your server
- In the Developer Portal, go to **OAuth2 > URL Generator**
- Scopes: check `applications.commands` (and `bot` if you want it to appear as a member)
- Copy the generated URL, open it, and add it to your Discord server

## Usage
- `/invest ticker:NVDA direction:long` — starts tracking a LONG NVDA position at the current price
- `/invest ticker:MSFT direction:short` — same for a short
- `/sold ticker:NVDA` — stop tracking (you already sold/closed it in-game)
- `/status` — see all currently tracked positions with live P/L

You'll then get the same HOLD/SELL alerts as the automatic scanner, but tied to what you
actually told it — even for tickers the automatic scanner wasn't already tracking.
