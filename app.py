"""
Small HTTP wrapper around predictor.py so it can run as a Render free Web Service.
An external pinger (e.g. UptimeRobot) hits /run-check on a schedule to trigger checks,
since Render's free tier only allows Web Services (not standalone background workers/cron).

Also hosts a Discord Interactions endpoint so /invest, /sold, /status slash commands
let you register your OWN real in-game position and get HOLD/SELL alerts for it,
independent of what the automatic scanner would have suggested.
"""
import os
import threading

import requests
from flask import Flask, request, jsonify
from nacl.signing import VerifyKey
from nacl.exceptions import BadSignatureError

from predictor import (
    load_config, get_bot_token, get_channel_id, load_state, save_state, run_pass,
    get_signal, get_position, set_position, USER_ID as ALLOWED_USER_ID,
    get_effective_config, get_default_target_pct, format_bet_line, plain_bet, plain_reasons,
    GUESS_NOTE,
)

app = Flask(__name__)

DISCORD_PUBLIC_KEY = os.environ.get("DISCORD_PUBLIC_KEY", "")
DISCORD_API = "https://discord.com/api/v10"

HELP_TEXT = (
    "**What I do**\n"
    "I watch real stock prices and tell you when one looks good to bet on in the game, "
    "then I watch your bet and tell you when to sell. I'll @ you when it's time to sell.\n\n"
    "**How to use me**\n"
    "`/best` — what's the best bet right now?\n"
    "`/invest stock:NVDA bet:auto` — I'll watch this one for you. "
    "`bet` can be `up`, `down`, or `auto` (I pick). "
    "You can add `stop_if_down` (sell if you lose this many %) and `sell_at_profit` (sell once you gain this many %).\n"
    "`/status` — how are my bets doing? Should I sell?\n"
    "`/sold stock:NVDA` — I sold it in the game, stop watching it.\n"
    "`/check stock:NVDA` — how is this one stock doing?\n"
    "`/market` — how is every stock doing?\n"
    "`/money amount:50000` — how much money I have, so you suggest the right amount to bet.\n"
    "`/goal percent:10` — how much profit I want per bet before selling.\n"
    "`/help` — this message.\n\n"
    "**Messages I send on my own**\n"
    "• When a stock becomes worth betting on — in the signals channel\n"
    "• How your bets are doing, and when to sell — in the trades channel (I @ you if you need to sell)\n"
    "• Once an hour, a quick note that I'm still running — in the heartbeat channel\n\n"
    f"{GUESS_NOTE}"
)

# Serializes state.json read-modify-write across concurrent requests (Discord interactions
# and /run-check can overlap on Render's single worker).
STATE_LOCK = threading.Lock()


@app.route("/")
def health():
    return "OK", 200


@app.route("/run-check")
def run_check():
    token = request.args.get("token")
    expected = os.environ.get("CHECK_SECRET")
    if not expected or token != expected:
        return jsonify({"error": "unauthorized"}), 403

    cfg = load_config()
    bot_token = get_bot_token(cfg)
    signals_channel_id = get_channel_id(cfg, "signals")
    trades_channel_id = get_channel_id(cfg, "trades")
    heartbeat_channel_id = get_channel_id(cfg, "heartbeat")
    if not bot_token:
        return jsonify({"error": "no bot token configured"}), 500

    with STATE_LOCK:
        state = load_state()
        state = run_pass(cfg, bot_token, signals_channel_id, trades_channel_id, heartbeat_channel_id, state)
        save_state(state)

    return jsonify({
        "status": "ok",
        "scanner_signals": state.get("_scanner_signals", {}),
        "positions": state.get("positions", {}),
    })


def verify_discord_signature(req) -> bool:
    if not DISCORD_PUBLIC_KEY:
        return False
    signature = req.headers.get("X-Signature-Ed25519", "")
    timestamp = req.headers.get("X-Signature-Timestamp", "")
    body = req.get_data(as_text=True)
    try:
        VerifyKey(bytes.fromhex(DISCORD_PUBLIC_KEY)).verify(
            f"{timestamp}{body}".encode(), bytes.fromhex(signature)
        )
        return True
    except (BadSignatureError, ValueError):
        return False


def discord_followup(application_id: str, interaction_token: str, content: str):
    url = f"{DISCORD_API}/webhooks/{application_id}/{interaction_token}/messages/@original"
    requests.patch(url, json={"content": content}, timeout=10)


def handle_invest(application_id: str, interaction_token: str, ticker: str, direction: str, cfg: dict, stop_loss_pct=None, take_profit_pct=None):
    ticker = ticker.upper()
    direction = direction.upper()
    try:
        info = get_signal(ticker, cfg)
        if info is None:
            discord_followup(application_id, interaction_token, f"I can't find a stock called `{ticker}`. Check the spelling?")
            return

        note = ""
        if direction == "AUTO":
            if info["signal"] == "NEUTRAL":
                discord_followup(
                    application_id, interaction_token,
                    f"**{ticker}** isn't doing anything clear right now, so I won't guess up or down for you. "
                    f"Ask me again later, or tell me `up` or `down` yourself if you already placed the bet.",
                )
                return
            direction = info["signal"]
            note = f" I picked this because that's the way it's moving right now."

        with STATE_LOCK:
            state = load_state()
            effective_take_profit = take_profit_pct if take_profit_pct is not None else get_default_target_pct(state)
            set_position(state, ticker, direction, info["price"], stop_loss_pct, effective_take_profit)
            save_state(state)

        bet_word = "goes UP" if direction == "LONG" else "goes DOWN"
        rules = []
        if stop_loss_pct:
            rules.append(f"tell you to sell if you lose {stop_loss_pct:.0f}%")
        if effective_take_profit:
            rules.append(f"tell you to sell once you gain {effective_take_profit:.0f}%")
        rules_note = (" I'll also " + " and ".join(rules) + ".") if rules else ""

        discord_followup(
            application_id, interaction_token,
            f"Got it — you're betting **{ticker} {bet_word}**, starting at **${info['price']:.2f}**.{note}\n"
            f"I'll check on it every few minutes and @ you when it's time to sell.{rules_note}",
        )
    except Exception as e:
        discord_followup(application_id, interaction_token, f"Something went wrong with `{ticker}`: {e}")


def handle_sold(application_id: str, interaction_token: str, ticker: str):
    ticker = ticker.upper()
    with STATE_LOCK:
        state = load_state()
        signal, entry_price, _, _ = get_position(state, ticker)
        if signal not in ("LONG", "SHORT"):
            discord_followup(application_id, interaction_token, f"I wasn't watching any bet on **{ticker}**.")
            return
        set_position(state, ticker, "NEUTRAL", None)
        save_state(state)
    discord_followup(application_id, interaction_token, f"Done — I've stopped watching **{ticker}**. Good luck with the next one.")


def handle_money(application_id: str, interaction_token: str, amount: float):
    if amount <= 0:
        discord_followup(application_id, interaction_token, "That needs to be a number bigger than 0.")
        return
    with STATE_LOCK:
        state = load_state()
        state["bankroll"] = amount
        save_state(state)
    discord_followup(
        application_id, interaction_token,
        f"Noted — you have **${amount:,.0f}**. I'll use that to work out how many shares to suggest.",
    )


def handle_goal(application_id: str, interaction_token: str, percent: float):
    if percent <= 0:
        discord_followup(application_id, interaction_token, "That needs to be a number bigger than 0.")
        return
    with STATE_LOCK:
        state = load_state()
        state["target_profit_pct"] = percent
        save_state(state)
    discord_followup(
        application_id, interaction_token,
        f"Got it — from now on I'll tell you to sell once a bet is up **{percent:.0f}%**. "
        f"I'll remember this for every new bet. (Bets you already have keep whatever they were set up with.)",
    )


def handle_status(application_id: str, interaction_token: str, cfg: dict):
    with STATE_LOCK:
        state = load_state()

    positions = state.get("positions", {})
    if not positions:
        discord_followup(
            application_id, interaction_token,
            "You're not in any bets right now. Try `/best` to see what's worth betting on.",
        )
        return

    lines = []
    for ticker, pos in positions.items():
        try:
            info = get_signal(ticker, cfg)
        except Exception:
            info = None
        line, _ = format_bet_line(ticker, pos, info)
        lines.append(line)

    discord_followup(application_id, interaction_token, "\n\n".join(lines))


def handle_best(application_id: str, interaction_token: str, cfg: dict):
    candidates = []
    for ticker in cfg["tickers"]:
        try:
            info = get_signal(ticker, cfg)
        except Exception:
            continue
        if info and info["signal"] != "NEUTRAL":
            candidates.append(info)

    if not candidates:
        discord_followup(
            application_id, interaction_token,
            "Nothing looks good to bet on right now — every stock is just drifting. Check back in a bit.",
        )
        return

    candidates.sort(key=lambda i: i["confidence"], reverse=True)
    best = candidates[0]
    move = f"{'up' if best['signal'] == 'LONG' else 'down'} {best['projected_move_pct']:.1f}%"

    lines = [
        f"**Best bet right now: {best['ticker']}** — {plain_bet(best['signal'])}",
        f"Costs **${best['price']:.2f}** each right now.",
        f"Buy about **{best['suggested_shares']} shares** (roughly ${best['suggested_amount']:,.0f} of your money).",
        f"I think it could go {move}, to about ${best['target_price']:.2f}, over the next hour or so.",
        f"How sure am I? **{best['confidence']*100:.0f}%**",
        "",
        "Why:",
    ]
    lines += [f"• {r}" for r in plain_reasons(best)]

    runner_ups = candidates[1:4]
    if runner_ups:
        lines.append("")
        lines.append("Other decent ones:")
        for c in runner_ups:
            lines.append(f"• **{c['ticker']}** — {plain_bet(c['signal'])} · {c['confidence']*100:.0f}% sure")

    lines.append("")
    lines.append(f"To have me watch it: `/invest stock:{best['ticker']} bet:auto`")
    discord_followup(application_id, interaction_token, "\n".join(lines))


def handle_check(application_id: str, interaction_token: str, ticker: str, cfg: dict):
    ticker = ticker.upper()
    try:
        info = get_signal(ticker, cfg)
    except Exception as e:
        discord_followup(application_id, interaction_token, f"Something went wrong looking up `{ticker}`: {e}")
        return

    if info is None:
        discord_followup(application_id, interaction_token, f"I can't find a stock called `{ticker}`. Check the spelling?")
        return

    if info["signal"] == "NEUTRAL":
        lines = [
            f"**{ticker}** — not worth betting on right now.",
            f"It costs ${info['price']:.2f}, but it's just drifting with no clear direction.",
        ]
    else:
        move = f"{'up' if info['signal'] == 'LONG' else 'down'} {info['projected_move_pct']:.1f}%"
        lines = [
            f"**{ticker}** — {plain_bet(info['signal'])}",
            f"Costs **${info['price']:.2f}** each right now.",
            f"Buy about **{info['suggested_shares']} shares** (roughly ${info['suggested_amount']:,.0f}).",
            f"I think it could go {move}, to about ${info['target_price']:.2f}, over the next hour or so.",
            f"How sure am I? **{info['confidence']*100:.0f}%**",
        ]

    lines.append("")
    lines.append("Why:")
    lines += [f"• {r}" for r in plain_reasons(info)]
    discord_followup(application_id, interaction_token, "\n".join(lines))


def handle_market(application_id: str, interaction_token: str, cfg: dict):
    good = []
    waiting = []
    broken = []
    for ticker in cfg["tickers"]:
        try:
            info = get_signal(ticker, cfg)
        except Exception:
            broken.append(ticker)
            continue

        if info is None:
            broken.append(ticker)
        elif info["signal"] == "NEUTRAL":
            waiting.append(ticker)
        else:
            arrow = "🟢" if info["signal"] == "LONG" else "🔴"
            good.append(
                f"{arrow} **{ticker}** — {plain_bet(info['signal'])} · "
                f"${info['price']:.2f} · {info['confidence']*100:.0f}% sure"
            )

    parts = []
    parts.append("**Worth a bet right now:**\n" + "\n".join(good) if good else "**Nothing looks worth betting on right now.**")
    if waiting:
        parts.append(f"**Just drifting ({len(waiting)}):** " + ", ".join(waiting))
    if broken:
        parts.append("**Couldn't get prices for:** " + ", ".join(broken))

    discord_followup(application_id, interaction_token, "\n\n".join(parts))


@app.route("/discord-interactions", methods=["POST"])
def discord_interactions():
    if not verify_discord_signature(request):
        return "invalid request signature", 401

    body = request.get_json()

    if body["type"] == 1:  # PING
        return jsonify({"type": 1})

    if body["type"] == 2:  # APPLICATION_COMMAND
        invoking_user = (body.get("member") or {}).get("user") or body.get("user") or {}
        if invoking_user.get("id") != ALLOWED_USER_ID:
            return jsonify({
                "type": 4,  # CHANNEL_MESSAGE_WITH_SOURCE
                "data": {"content": "You're not authorized to use this bot.", "flags": 64},  # 64 = ephemeral
            })

        with STATE_LOCK:
            cfg = get_effective_config(load_config(), load_state())
        application_id = body["application_id"]
        interaction_token = body["token"]
        command_name = body["data"]["name"]
        options = {opt["name"]: opt["value"] for opt in body["data"].get("options", [])}

        if command_name == "help":
            return jsonify({
                "type": 4,  # CHANNEL_MESSAGE_WITH_SOURCE — no need to defer, this is static
                "data": {"content": HELP_TEXT, "flags": 64},
            })

        if command_name == "invest":
            threading.Thread(
                target=handle_invest,
                args=(application_id, interaction_token, options["stock"], options["bet"], cfg, options.get("stop_if_down"), options.get("sell_at_profit")),
                daemon=True,
            ).start()
        elif command_name == "sold":
            threading.Thread(
                target=handle_sold,
                args=(application_id, interaction_token, options["stock"]),
                daemon=True,
            ).start()
        elif command_name == "status":
            threading.Thread(
                target=handle_status,
                args=(application_id, interaction_token, cfg),
                daemon=True,
            ).start()
        elif command_name == "best":
            threading.Thread(
                target=handle_best,
                args=(application_id, interaction_token, cfg),
                daemon=True,
            ).start()
        elif command_name == "check":
            threading.Thread(
                target=handle_check,
                args=(application_id, interaction_token, options["stock"], cfg),
                daemon=True,
            ).start()
        elif command_name == "money":
            threading.Thread(
                target=handle_money,
                args=(application_id, interaction_token, options["amount"]),
                daemon=True,
            ).start()
        elif command_name == "goal":
            threading.Thread(
                target=handle_goal,
                args=(application_id, interaction_token, options["percent"]),
                daemon=True,
            ).start()
        elif command_name == "market":
            threading.Thread(
                target=handle_market,
                args=(application_id, interaction_token, cfg),
                daemon=True,
            ).start()

        return jsonify({"type": 5, "data": {"flags": 64}})  # DEFERRED, ephemeral (only you can see it)

    return jsonify({"error": "unhandled interaction type"}), 400


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
