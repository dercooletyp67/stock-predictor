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
    load_config, get_webhook_url, load_state, save_state, run_pass,
    get_signal, get_position, set_position, compute_pnl_pct,
)

app = Flask(__name__)

DISCORD_PUBLIC_KEY = os.environ.get("DISCORD_PUBLIC_KEY", "")
DISCORD_API = "https://discord.com/api/v10"
ALLOWED_USER_ID = "937305776526065675"

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
    webhook_url = get_webhook_url(cfg)
    if not webhook_url:
        return jsonify({"error": "no webhook url configured"}), 500

    with STATE_LOCK:
        state = load_state()
        state = run_pass(cfg, webhook_url, state)
        save_state(state)

    return jsonify({"status": "ok", "signals": {k: v for k, v in state.items() if not k.startswith("_")}})


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


def handle_invest(application_id: str, interaction_token: str, ticker: str, direction: str, cfg: dict):
    ticker = ticker.upper()
    direction = direction.upper()
    try:
        info = get_signal(ticker, cfg)
        if info is None:
            discord_followup(application_id, interaction_token, f"Couldn't get data for `{ticker}` — check the symbol is right.")
            return

        with STATE_LOCK:
            state = load_state()
            set_position(state, ticker, direction, info["price"])
            save_state(state)

        discord_followup(
            application_id, interaction_token,
            f"Tracking **{direction} {ticker}** from entry **${info['price']:.2f}**. "
            f"You'll get HOLD/SELL updates on every check from now on.",
        )
    except Exception as e:
        discord_followup(application_id, interaction_token, f"Error registering `{ticker}`: {e}")


def handle_close(application_id: str, interaction_token: str, ticker: str):
    ticker = ticker.upper()
    with STATE_LOCK:
        state = load_state()
        signal, entry_price = get_position(state, ticker)
        if signal not in ("LONG", "SHORT"):
            discord_followup(application_id, interaction_token, f"No open position tracked for `{ticker}`.")
            return
        set_position(state, ticker, "NEUTRAL", None)
        save_state(state)
    discord_followup(application_id, interaction_token, f"Stopped tracking **{signal} {ticker}**.")


def handle_status(application_id: str, interaction_token: str, cfg: dict):
    with STATE_LOCK:
        state = load_state()

    open_tickers = [t for t in state if not t.startswith("_") and get_position(state, t)[0] in ("LONG", "SHORT")]
    if not open_tickers:
        discord_followup(application_id, interaction_token, "No open positions tracked right now.")
        return

    lines = []
    for ticker in open_tickers:
        signal, entry_price = get_position(state, ticker)
        try:
            info = get_signal(ticker, cfg)
            price = info["price"] if info else None
        except Exception:
            price = None

        if price is None:
            lines.append(f"**{ticker}**: {signal} (price unavailable)")
            continue

        pnl_pct = compute_pnl_pct(entry_price, price, signal)
        pnl_str = f" ({'+' if pnl_pct >= 0 else ''}{pnl_pct:.2f}%)" if pnl_pct is not None else ""
        lines.append(f"**{ticker}**: {signal} @ ${price:.2f}{pnl_str}")

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

        if command_name == "invest":
            threading.Thread(
                target=handle_invest,
                args=(application_id, interaction_token, options["ticker"], options["direction"], cfg),
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

        return jsonify({"type": 5})  # DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE

    return jsonify({"error": "unhandled interaction type"}), 400


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
