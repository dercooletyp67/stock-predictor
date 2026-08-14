"""
Registers the bot's slash commands with Discord. Re-run this whenever a command name,
description, or option changes. It replaces the whole command list, so removed commands
disappear on their own.

Requires env vars (not committed anywhere):
  DISCORD_APPLICATION_ID
  DISCORD_BOT_TOKEN
"""
import os

import requests

APPLICATION_ID = os.environ["DISCORD_APPLICATION_ID"]
BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]

STOCK_OPTION = {"name": "stock", "description": "Which stock, e.g. NVDA", "type": 3, "required": True}

COMMANDS = [
    {
        "name": "best",
        "description": "What's the best stock to bet on right now?",
        "options": [],
    },
    {
        "name": "invest",
        "description": "Tell me about a bet you placed, and I'll tell you when to sell",
        "options": [
            STOCK_OPTION,
            {
                "name": "bet",
                "description": "Are you betting it goes up or down?",
                "type": 3,
                "required": True,
                "choices": [
                    {"name": "up (Long)", "value": "LONG"},
                    {"name": "down (Short)", "value": "SHORT"},
                    {"name": "you pick for me", "value": "AUTO"},
                ],
            },
            {
                "name": "stop_if_down",
                "description": "Optional: tell me to sell if I lose this many percent",
                "type": 10,  # NUMBER
                "required": False,
                "min_value": 0.1,
                "max_value": 100,
            },
            {
                "name": "sell_at_profit",
                "description": "Optional: sell once I gain this many percent (just for this bet)",
                "type": 10,  # NUMBER
                "required": False,
                "min_value": 0.1,
                "max_value": 1000,
            },
        ],
    },
    {
        "name": "status",
        "description": "How are my bets doing? Should I sell anything?",
        "options": [],
    },
    {
        "name": "sold",
        "description": "I sold this one in the game — stop watching it",
        "options": [STOCK_OPTION],
    },
    {
        "name": "check",
        "description": "How is one stock doing right now?",
        "options": [STOCK_OPTION],
    },
    {
        "name": "market",
        "description": "How is every stock doing right now?",
        "options": [],
    },
    {
        "name": "money",
        "description": "Tell me how much money you have, so I suggest the right amount to bet",
        "options": [
            {"name": "amount", "description": "How much money you have, e.g. 50000", "type": 10, "required": True, "min_value": 1},
        ],
    },
    {
        "name": "goal",
        "description": "How much profit do you want per bet before selling?",
        "options": [
            {"name": "percent", "description": "e.g. 10 means sell once you're up 10%", "type": 10, "required": True, "min_value": 0.1, "max_value": 1000},
        ],
    },
    {
        "name": "help",
        "description": "What can this bot do?",
        "options": [],
    },
]

# Hides every command from everyone in the server by default; only server admins see them.
# The bot ALSO checks the user id on each command, so this is just the first layer.
for _command in COMMANDS:
    _command["default_member_permissions"] = "0"

if __name__ == "__main__":
    resp = requests.put(
        f"https://discord.com/api/v10/applications/{APPLICATION_ID}/commands",
        headers={"Authorization": f"Bot {BOT_TOKEN}"},
        json=COMMANDS,
        timeout=30,
    )
    resp.raise_for_status()
    print("Registered commands:", [c["name"] for c in resp.json()])
