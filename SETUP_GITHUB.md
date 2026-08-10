# Running the predictor 24/7 via GitHub Actions

## 1. Create a public GitHub repo
Go to https://github.com/new, name it (e.g. `stock-predictor`), set visibility to **Public**, don't initialize with a README.

## 2. Push this project
Run from this folder (`C:\Users\mrfor\Desktop\StockTest`):

```bash
git init
git add .
git commit -m "Initial predictor setup"
git branch -M main
git remote add origin https://github.com/<your-username>/<repo-name>.git
git push -u origin main
```

`config.json` (your real webhook + settings) is gitignored and will NOT be pushed — only `config.example.json` (no secret) goes to the public repo.

## 3. Add your Discord webhook as a repo secret
In the GitHub repo: **Settings > Secrets and variables > Actions > New repository secret**
- Name: `DISCORD_WEBHOOK_URL`
- Value: your webhook URL (the one currently in your local `config.json`)

## 4. Done
The workflow (`.github/workflows/predictor.yml`) runs every 5 minutes automatically (GitHub's practical minimum), checks all 4 tickers, and posts to Discord on any LONG/SHORT signal — no PC required. You can also trigger it manually anytime from the repo's **Actions** tab ("Run workflow").

Signal state (to avoid duplicate notifications) persists via `state.json`, which the workflow commits back to the repo after each run.
