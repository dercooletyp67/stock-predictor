"""
One-time script to register the /invest, /sold, /status slash commands with Discord.
Run locally after creating your Discord Application (see SETUP_DISCORD_BOT.md).

Requires env vars (not committed anywhere):
  DISCORD_APPLICATION_ID
  DISCORD_BOT_TOKEN
"""
import os

import requests

APPLICATION_ID = os.environ["DISCORD_APPLICATION_ID"]
BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]

COMMANDS = [
    {
        "name": "invest",
        "description": "Tell the bot what you invested in, so it can track HOLD/SELL for it",
        "options": [
            {"name": "ticker", "description": "Stock ticker, e.g. NVDA", "type": 3, "required": True},
            {
                "name": "direction",
                "description": "Long or short",
                "type": 3,
                "required": True,
                "choices": [
                    {"name": "long", "value": "LONG"},
                    {"name": "short", "value": "SHORT"},
                    {"name": "auto (bot decides)", "value": "AUTO"},
                ],
            },
            {
                "name": "stop_loss_pct",
                "description": "Optional: alert if you're down this much %, even without a signal reversal",
                "type": 10,  # NUMBER
                "required": False,
                "min_value": 0.1,
                "max_value": 100,
            },
            {
                "name": "take_profit_pct",
                "description": "Optional: overrides your remembered /goal for just this trade",
                "type": 10,  # NUMBER
                "required": False,
                "min_value": 0.1,
                "max_value": 1000,
            },
        ],
    },
    {
        "name": "sold",
        "description": "Stop tracking a position (you already sold/closed it in-game)",
        "options": [
            {"name": "ticker", "description": "Stock ticker, e.g. NVDA", "type": 3, "required": True},
        ],
    },
    {
        "name": "status",
        "description": "List all positions currently being tracked, with live P/L",
        "options": [],
    },
    {
        "name": "best",
        "description": "Show the single highest-confidence LONG/SHORT opportunity right now",
        "options": [],
    },
    {
        "name": "check",
        "description": "Look up any ticker's current signal and indicators, even if you haven't invested",
        "options": [
            {"name": "ticker", "description": "Stock ticker, e.g. NVDA", "type": 3, "required": True},
        ],
    },
    {
        "name": "help",
        "description": "List all commands and what they do",
        "options": [],
    },
    {
        "name": "money",
        "description": "Tell the bot how much money you currently have, for accurate position sizing",
        "options": [
            {"name": "amount", "description": "Your current money, e.g. 50000", "type": 10, "required": True, "min_value": 1},
        ],
    },
    {
        "name": "goal",
        "description": "Set your remembered default profit goal %, used for future /invest calls",
        "options": [
            {"name": "percent", "description": "e.g. 10 for +10%", "type": 10, "required": True, "min_value": 0.1, "max_value": 1000},
        ],
    },
    {
        "name": "market",
        "description": "Full board: every tracked ticker's current signal, on demand",
        "options": [],
    },
]

if __name__ == "__main__":
    resp = requests.put(
        f"https://discord.com/api/v10/applications/{APPLICATION_ID}/commands",
        headers={"Authorization": f"Bot {BOT_TOKEN}"},
        json=COMMANDS,
        timeout=30,
    )
    resp.raise_for_status()
    print("Registered commands:", [c["name"] for c in resp.json()])
