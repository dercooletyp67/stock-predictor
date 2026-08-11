"""
One-time script to register the /invest, /close, /status slash commands with Discord.
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
                ],
            },
        ],
    },
    {
        "name": "close",
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
