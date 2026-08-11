"""
Small HTTP wrapper around predictor.py so it can run as a Render free Web Service.
An external pinger (e.g. UptimeRobot) hits /run-check on a schedule to trigger checks,
since Render's free tier only allows Web Services (not standalone background workers/cron).

Also hosts a Discord Interactions endpoint so /invest, /close, /status slash commands
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
    get_signal, get_position, set_position, compute_pnl_pct, USER_ID as ALLOWED_USER_ID,
)

app = Flask(__name__)

DISCORD_PUBLIC_KEY = os.environ.get("DISCORD_PUBLIC_KEY", "")
DISCORD_API = "https://discord.com/api/v10"

HELP_TEXT = (
    "**Commands**\n"
    "`/invest ticker:NVDA direction:long` — start tracking a position at the current price. "
    "`direction` can be `long`, `short`, or `auto` (bot picks based on the live signal, or declines if it's neutral). "
    "Optional `stop_loss_pct` — SELL alert fires if you're down that much, even without a signal reversal.\n"
    "`/close ticker:NVDA` — stop tracking a position (you already sold/closed it in-game).\n"
    "`/status` — private summary of everything you're tracking, with STAY/SELL and live P/L for each.\n"
    "`/best` — the single highest-confidence LONG/SHORT signal across all tracked tickers right now.\n"
    "`/help` — this message.\n\n"
    "You'll also get automatic messages: a digest when new signals appear on tickers you haven't invested in, "
    "a portfolio update for whatever you're tracking, and an hourly heartbeat confirming the bot is alive."
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


def handle_invest(application_id: str, interaction_token: str, ticker: str, direction: str, cfg: dict, stop_loss_pct=None):
    ticker = ticker.upper()
    direction = direction.upper()
    try:
        info = get_signal(ticker, cfg)
        if info is None:
            discord_followup(application_id, interaction_token, f"Couldn't get data for `{ticker}` — check the symbol is right.")
            return

        note = ""
        if direction == "AUTO":
            if info["signal"] == "NEUTRAL":
                discord_followup(
                    application_id, interaction_token,
                    f"`{ticker}` has no clear signal right now (NEUTRAL) — the bot won't guess a direction. "
                    f"Try again once it flips LONG or SHORT, or specify a direction yourself.",
                )
                return
            direction = info["signal"]
            note = f" (bot chose {direction.lower()} based on the current signal)"

        with STATE_LOCK:
            state = load_state()
            set_position(state, ticker, direction, info["price"], stop_loss_pct)
            save_state(state)

        stop_loss_note = f" Stop-loss set at -{stop_loss_pct}%." if stop_loss_pct else ""
        discord_followup(
            application_id, interaction_token,
            f"Tracking **{direction} {ticker}** from entry **${info['price']:.2f}**{note}.{stop_loss_note} "
            f"You'll get HOLD/SELL updates on every check from now on.",
        )
    except Exception as e:
        discord_followup(application_id, interaction_token, f"Error registering `{ticker}`: {e}")


def handle_close(application_id: str, interaction_token: str, ticker: str):
    ticker = ticker.upper()
    with STATE_LOCK:
        state = load_state()
        signal, entry_price, _ = get_position(state, ticker)
        if signal not in ("LONG", "SHORT"):
            discord_followup(application_id, interaction_token, f"No open position tracked for `{ticker}`.")
            return
        set_position(state, ticker, "NEUTRAL", None)
        save_state(state)
    discord_followup(application_id, interaction_token, f"Stopped tracking **{signal} {ticker}**.")


def handle_status(application_id: str, interaction_token: str, cfg: dict):
    with STATE_LOCK:
        state = load_state()

    open_tickers = list(state.get("positions", {}).keys())
    if not open_tickers:
        discord_followup(application_id, interaction_token, "No open positions tracked right now.")
        return

    lines = []
    for ticker in open_tickers:
        position, entry_price, stop_loss_pct = get_position(state, ticker)
        try:
            info = get_signal(ticker, cfg)
        except Exception:
            info = None

        if info is None:
            lines.append(f"**{ticker}**: {position} — price unavailable, can't confirm")
            continue

        price = info["price"]
        current_signal = info["signal"]
        pnl_pct = compute_pnl_pct(entry_price, price, position)
        pnl_str = f" ({'+' if pnl_pct >= 0 else ''}{pnl_pct:.2f}%)" if pnl_pct is not None else ""

        stop_loss_hit = (
            stop_loss_pct is not None and pnl_pct is not None and pnl_pct <= -abs(stop_loss_pct)
        )

        if stop_loss_hit:
            action = f"SELL — STOP LOSS HIT ({pnl_pct:.2f}% ≤ -{abs(stop_loss_pct):.1f}%)"
        elif current_signal == position:
            action = "STAY / HOLD"
        elif current_signal == "NEUTRAL":
            action = "SELL — signal faded to neutral"
        else:
            action = f"SELL — signal reversed to {current_signal}"

        entry_str = f"${entry_price:.2f}" if entry_price else "unknown"
        lines.append(f"**{ticker}** ({position} @ {entry_str}, now ${price:.2f}{pnl_str}): **{action}**")

    discord_followup(application_id, interaction_token, "\n".join(lines))


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
            "No ticker has a clear LONG/SHORT signal right now — nothing stands out. Try again in a bit.",
        )
        return

    candidates.sort(key=lambda i: i["confidence"], reverse=True)
    best = candidates[0]

    lines = [
        f"**Best right now: {best['signal']} {best['ticker']}** (confidence {best['confidence']*100:.0f}%)",
        f"Price: ${best['price']:.2f} → target ${best['target_price']:.2f} "
        f"({'+' if best['signal']=='LONG' else '-'}{best['projected_move_pct']:.2f}%)",
        f"Suggested: {best['suggested_shares']} shares (~${best['suggested_amount']:,.0f})",
    ]

    runner_ups = candidates[1:4]
    if runner_ups:
        lines.append("")
        lines.append("Other live signals:")
        for c in runner_ups:
            lines.append(f"• {c['signal']} {c['ticker']} (confidence {c['confidence']*100:.0f}%)")

    discord_followup(application_id, interaction_token, "\n".join(lines))


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

        cfg = load_config()
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
                args=(application_id, interaction_token, options["ticker"], options["direction"], cfg, options.get("stop_loss_pct")),
                daemon=True,
            ).start()
        elif command_name == "close":
            threading.Thread(
                target=handle_close,
                args=(application_id, interaction_token, options["ticker"]),
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

        return jsonify({"type": 5, "data": {"flags": 64}})  # DEFERRED, ephemeral (only you can see it)

    return jsonify({"error": "unhandled interaction type"}), 400


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
