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
    get_signal, get_position, set_position, compute_pnl_pct, USER_ID as ALLOWED_USER_ID,
    get_effective_config, get_default_target_pct, evaluate_position_action, compute_early_warning,
)

app = Flask(__name__)

DISCORD_PUBLIC_KEY = os.environ.get("DISCORD_PUBLIC_KEY", "")
DISCORD_API = "https://discord.com/api/v10"

HELP_TEXT = (
    "**Commands**\n"
    "`/invest ticker:NVDA direction:long` — start tracking a position at the current price. "
    "`direction` can be `long`, `short`, or `auto` (bot picks based on the live signal, or declines if it's neutral). "
    "Optional `stop_loss_pct` — SELL alert fires if you're down that much. "
    "Optional `take_profit_pct` — overrides your remembered `/goal` for just this trade.\n"
    "`/sold ticker:NVDA` — stop tracking a position (you already sold/closed it in-game).\n"
    "`/status` — private summary of everything you're tracking, with STAY/SELL and live P/L for each.\n"
    "`/best` — the single highest-confidence LONG/SHORT signal across all tracked tickers right now.\n"
    "`/check ticker:NVDA` — look up any ticker's current signal/indicators, whether or not you've invested.\n"
    "`/market` — full board: every tracked ticker's current signal, on demand.\n"
    "`/money amount:50000` — tell the bot how much money you have, so suggested position sizes stay accurate.\n"
    "`/goal percent:10` — remembered default: SELL alert once a position is up this much %, for all future `/invest` calls.\n"
    "`/help` — this message.\n\n"
    "You'll also get automatic messages: a digest when new signals appear on tickers you haven't invested in, "
    "a portfolio update (pings you when action is needed) for whatever you're tracking, and an hourly heartbeat. "
    "HOLD lines may show a ⚠️ if momentum looks like it's starting to turn — a heads-up, not a guarantee or a timer."
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
            effective_take_profit = take_profit_pct if take_profit_pct is not None else get_default_target_pct(state)
            set_position(state, ticker, direction, info["price"], stop_loss_pct, effective_take_profit)
            save_state(state)

        risk_note = ""
        if stop_loss_pct:
            risk_note += f" Stop-loss at -{stop_loss_pct}%."
        if effective_take_profit:
            risk_note += f" Take-profit at +{effective_take_profit}%."
        discord_followup(
            application_id, interaction_token,
            f"Tracking **{direction} {ticker}** from entry **${info['price']:.2f}**{note}.{risk_note} "
            f"You'll get HOLD/SELL updates on every check from now on.",
        )
    except Exception as e:
        discord_followup(application_id, interaction_token, f"Error registering `{ticker}`: {e}")


def handle_sold(application_id: str, interaction_token: str, ticker: str):
    ticker = ticker.upper()
    with STATE_LOCK:
        state = load_state()
        signal, entry_price, _, _ = get_position(state, ticker)
        if signal not in ("LONG", "SHORT"):
            discord_followup(application_id, interaction_token, f"No open position tracked for `{ticker}`.")
            return
        set_position(state, ticker, "NEUTRAL", None)
        save_state(state)
    discord_followup(application_id, interaction_token, f"Stopped tracking **{signal} {ticker}**.")


def handle_money(application_id: str, interaction_token: str, amount: float):
    if amount <= 0:
        discord_followup(application_id, interaction_token, "Amount must be a positive number.")
        return
    with STATE_LOCK:
        state = load_state()
        state["bankroll"] = amount
        save_state(state)
    discord_followup(application_id, interaction_token, f"Money set to **${amount:,.0f}**. Position sizing suggestions will now use this.")


def handle_goal(application_id: str, interaction_token: str, percent: float):
    if percent <= 0:
        discord_followup(application_id, interaction_token, "Goal must be a positive number.")
        return
    with STATE_LOCK:
        state = load_state()
        state["target_profit_pct"] = percent
        save_state(state)
    discord_followup(
        application_id, interaction_token,
        f"Default profit goal set to **+{percent}%**. Future `/invest` calls will use this unless you override with `take_profit_pct`. "
        f"Existing tracked positions aren't affected retroactively.",
    )


def handle_status(application_id: str, interaction_token: str, cfg: dict):
    with STATE_LOCK:
        state = load_state()

    open_tickers = list(state.get("positions", {}).keys())
    if not open_tickers:
        discord_followup(application_id, interaction_token, "No open positions tracked right now.")
        return

    lines = []
    for ticker in open_tickers:
        position, entry_price, stop_loss_pct, take_profit_pct = get_position(state, ticker)
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

        action = evaluate_position_action(position, entry_price, stop_loss_pct, take_profit_pct, current_signal, pnl_pct)
        if action == "STAY / HOLD" and compute_early_warning(position, info):
            action += " ⚠️ (momentum turning against you — watch closely)"

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


def handle_check(application_id: str, interaction_token: str, ticker: str, cfg: dict):
    ticker = ticker.upper()
    try:
        info = get_signal(ticker, cfg)
    except Exception as e:
        discord_followup(application_id, interaction_token, f"Error checking `{ticker}`: {e}")
        return

    if info is None:
        discord_followup(application_id, interaction_token, f"Couldn't get data for `{ticker}` — check the symbol is right.")
        return

    lines = [
        f"**{ticker}: {info['signal']}**",
        f"Price: ${info['price']:.2f}",
        f"RSI: {info['rsi']:.1f}",
        f"EMA9 / EMA21: {info['short_ema']:.2f} / {info['long_ema']:.2f}",
        f"MACD / Signal: {info['macd_line']:.3f} / {info['macd_signal_line']:.3f}",
        f"Volume confirmed: {'Yes' if info['volume_confirmed'] else 'No'}",
    ]

    if info["signal"] != "NEUTRAL":
        lines.insert(1, f"Target: ${info['target_price']:.2f} ({'+' if info['signal']=='LONG' else '-'}{info['projected_move_pct']:.2f}%)")
        lines.insert(2, f"Suggested: {info['suggested_shares']} shares (~${info['suggested_amount']:,.0f}), confidence {info['confidence']*100:.0f}%")

    discord_followup(application_id, interaction_token, "\n".join(lines))


def handle_market(application_id: str, interaction_token: str, cfg: dict):
    lines = []
    for ticker in cfg["tickers"]:
        try:
            info = get_signal(ticker, cfg)
        except Exception:
            lines.append(f"**{ticker}**: error fetching data")
            continue

        if info is None:
            lines.append(f"**{ticker}**: not enough data")
        elif info["signal"] == "NEUTRAL":
            lines.append(f"{ticker}: NEUTRAL")
        else:
            arrow = "🟢" if info["signal"] == "LONG" else "🔴"
            lines.append(f"{arrow} **{info['signal']} {ticker}** @ ${info['price']:.2f} (confidence {info['confidence']*100:.0f}%)")

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
                args=(application_id, interaction_token, options["ticker"], options["direction"], cfg, options.get("stop_loss_pct"), options.get("take_profit_pct")),
                daemon=True,
            ).start()
        elif command_name == "sold":
            threading.Thread(
                target=handle_sold,
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
        elif command_name == "check":
            threading.Thread(
                target=handle_check,
                args=(application_id, interaction_token, options["ticker"], cfg),
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
