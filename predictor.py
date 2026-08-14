"""
Polls real-time-ish stock data and posts long/short signals to Discord via the bot,
routed to different channels (signals / trades / heartbeat) by channel ID.

Signal is based on simple technical indicators (EMA crossover + RSI) — it is a
decision aid, not a guarantee. No tool can reliably predict short-term price moves.
"""
import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

CONFIG_PATH = Path(__file__).parent / "config.json"
CONFIG_EXAMPLE_PATH = Path(__file__).parent / "config.example.json"
STATE_PATH = Path(__file__).parent / "state.json"


def load_config():
    path = CONFIG_PATH if CONFIG_PATH.exists() else CONFIG_EXAMPLE_PATH
    with open(path, "r") as f:
        return json.load(f)


DISCORD_API = "https://discord.com/api/v10"
USER_ID = "937305776526065675"


def get_bot_token(cfg: dict) -> str:
    return os.environ.get("DISCORD_BOT_TOKEN") or cfg.get("discord_bot_token", "")


def get_channel_id(cfg: dict, name: str) -> str:
    env_key = f"DISCORD_{name.upper()}_CHANNEL_ID"
    cfg_key = f"discord_{name}_channel_id"
    return os.environ.get(env_key) or cfg.get(cfg_key, "")


def button_rows(buttons: list) -> list:
    """Discord allows 5 buttons per row and 5 rows; anything past 25 is dropped."""
    rows = []
    for i in range(0, min(len(buttons), 25), 5):
        rows.append({"type": 1, "components": buttons[i:i + 5]})
    return rows


def post_channel_message(bot_token: str, channel_id: str, embed: dict, ping: bool = False, buttons: list = None):
    payload = {"embeds": [embed]}
    if ping:
        payload["content"] = f"<@{USER_ID}>"
    if buttons:
        payload["components"] = button_rows(buttons)
    resp = requests.post(
        f"{DISCORD_API}/channels/{channel_id}/messages",
        headers={"Authorization": f"Bot {bot_token}"},
        json=payload,
        timeout=10,
    )
    resp.raise_for_status()


def load_state() -> dict:
    if STATE_PATH.exists():
        with open(STATE_PATH, "r") as f:
            return json.load(f)
    return {}


def save_state(state: dict):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f)


def compute_rsi(close: pd.Series, period: int) -> float:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1])


def compute_macd_series(close: pd.Series, fast: int, slow: int, signal: int):
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line, signal_line


def percentile_rank(series: pd.Series, value: float) -> float:
    """Where `value` ranks (0-1) against the historical distribution of `series`."""
    clean = series.dropna()
    if len(clean) < 2:
        return 0.5
    return float((clean < value).sum() / len(clean))


def get_signal(ticker: str, cfg: dict):
    min_bars = max(cfg["long_ema_period"], cfg["macd_slow"], cfg["volume_period"]) + 1
    data = yf.download(
        ticker,
        period="5d",
        interval="15m",
        progress=False,
        auto_adjust=True,
    )
    if data.empty or len(data) < min_bars:
        return None

    close = data["Close"]
    volume = data["Volume"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    if isinstance(volume, pd.DataFrame):
        volume = volume.iloc[:, 0]

    short_ema = close.ewm(span=cfg["short_ema_period"], adjust=False).mean()
    long_ema = close.ewm(span=cfg["long_ema_period"], adjust=False).mean()
    rsi = compute_rsi(close, cfg["rsi_period"])
    macd_line_series, macd_signal_series = compute_macd_series(
        close, cfg["macd_fast"], cfg["macd_slow"], cfg["macd_signal"]
    )
    macd_line = float(macd_line_series.iloc[-1])
    macd_signal_line = float(macd_signal_series.iloc[-1])

    avg_volume_series = volume.rolling(cfg["volume_period"]).mean()
    avg_volume = float(avg_volume_series.iloc[-1])
    current_volume = float(volume.iloc[-1])
    volume_confirmed = current_volume > avg_volume * cfg["volume_multiplier"]

    trend = float(short_ema.iloc[-1] - long_ema.iloc[-1])
    price = float(close.iloc[-1])

    trend_up = trend > 0
    trend_down = trend < 0
    macd_bullish = macd_line > macd_signal_line
    macd_bearish = macd_line < macd_signal_line

    if trend_up and macd_bullish and volume_confirmed and rsi < cfg["rsi_overbought"]:
        signal = "LONG"
    elif trend_down and macd_bearish and volume_confirmed and rsi > cfg["rsi_oversold"]:
        signal = "SHORT"
    else:
        signal = "NEUTRAL"

    result = {
        "ticker": ticker,
        "price": price,
        "signal": signal,
        "rsi": rsi,
        "short_ema": float(short_ema.iloc[-1]),
        "long_ema": float(long_ema.iloc[-1]),
        "macd_line": macd_line,
        "macd_signal_line": macd_signal_line,
        "volume_confirmed": volume_confirmed,
    }

    if signal != "NEUTRAL":
        result.update(compute_confidence_and_sizing(
            close, volume, avg_volume_series,
            macd_line_series, macd_signal_series, rsi, price, signal, cfg,
        ))

    return result


def compute_confidence_and_sizing(close, volume, avg_volume_series, macd_line_series, macd_signal_series, rsi, price, signal, cfg):
    # Each factor is scored relative to this ticker's OWN recent history (percentile rank),
    # not a fixed threshold — a "strong" move for KO isn't the same size as one for TSLA.
    macd_gap_series = (macd_line_series - macd_signal_series).abs() / close * 100
    macd_strength = percentile_rank(macd_gap_series, macd_gap_series.iloc[-1])

    rsi_strength = min(abs(rsi - 50) / 30, 1.0)  # RSI is already a bounded 0-100 scale, comparable across tickers

    volume_ratio_series = volume / avg_volume_series
    volume_strength = percentile_rank(volume_ratio_series, volume_ratio_series.iloc[-1])

    confidence = (macd_strength + rsi_strength + volume_strength) / 3

    risk_pct = cfg["min_risk_pct"] + confidence * (cfg["max_risk_pct"] - cfg["min_risk_pct"])
    suggested_amount = cfg["bankroll"] * risk_pct
    suggested_shares = max(1, round(suggested_amount / price))

    returns = close.pct_change().dropna()
    bar_volatility = float(returns.tail(cfg["volume_period"]).std())
    projected_move_pct = bar_volatility * (cfg["horizon_bars"] ** 0.5) * 100
    direction = 1 if signal == "LONG" else -1
    target_price = price * (1 + direction * projected_move_pct / 100)

    return {
        "confidence": confidence,
        "suggested_amount": suggested_amount,
        "suggested_shares": suggested_shares,
        "risk_pct": risk_pct,
        "projected_move_pct": projected_move_pct,
        "target_price": target_price,
        "bankroll": cfg["bankroll"],
    }


def compute_pnl_pct(entry_price, current_price, position):
    if not entry_price:
        return None
    direction = 1 if position == "LONG" else -1
    return direction * (current_price - entry_price) / entry_price * 100


GUESS_NOTE = "This is a guess from price patterns. It can be wrong."


def plain_bet(signal: str) -> str:
    """The in-game button to press, said in plain words."""
    if signal == "LONG":
        return "press LONG (betting the price goes UP)"
    if signal == "SHORT":
        return "press SHORT (betting the price goes DOWN)"
    return "wait — no clear move"


def plain_reasons(info: dict) -> list:
    """Explain what the indicators are saying, without the indicator names."""
    reasons = []
    reasons.append(
        "Price direction: going up" if info["short_ema"] > info["long_ema"]
        else "Price direction: going down"
    )
    reasons.append(
        "Speed of the move: picking up" if info["macd_line"] > info["macd_signal_line"]
        else "Speed of the move: slowing down"
    )

    rsi = info["rsi"]
    if rsi >= 70:
        reasons.append(f"Looks expensive right now ({rsi:.0f} out of 100)")
    elif rsi <= 30:
        reasons.append(f"Looks cheap right now ({rsi:.0f} out of 100)")
    else:
        reasons.append(f"Price is in a normal range ({rsi:.0f} out of 100)")

    reasons.append(
        "Lots of people trading it, so the move looks real" if info["volume_confirmed"]
        else "Not many people trading it, so the move is weak"
    )
    return reasons


def post_digest_to_discord(bot_token: str, channel_id: str, new_signals: list):
    """One message listing every stock that just became worth a bet."""
    lines = []
    for info in new_signals:
        arrow = "🟢" if info["signal"] == "LONG" else "🔴"
        lines.append(
            f"{arrow} **{info['ticker']}** — {plain_bet(info['signal'])}\n"
            f"Costs ${info['price']:.2f} each · buy about **{info['suggested_shares']} shares** · "
            f"could reach ${info['target_price']:.2f} · {info['confidence']*100:.0f}% sure"
        )

    count = len(new_signals)
    buttons = [
        {
            "type": 2,
            "style": 3 if info["signal"] == "LONG" else 4,  # green for up, red for down
            "label": f"Watch {info['ticker']}",
            "custom_id": f"track:{info['ticker']}:{info['signal']}",
        }
        for info in new_signals
    ]
    embed = {
        "title": f"{count} new chance{'s' if count != 1 else ''} to bet",
        "color": 3447003,
        "description": "\n\n".join(lines),
        "footer": {"text": f"Tap a button once you've placed the bet in the game. {GUESS_NOTE}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    post_channel_message(bot_token, channel_id, embed, buttons=buttons)


def post_portfolio_to_discord(bot_token: str, channel_id: str, lines: list, needs_sell: bool, tickers: list = None):
    """One message covering every bet you told me about with /invest."""
    buttons = [
        {"type": 2, "style": 2, "label": f"Sold {t}", "custom_id": f"sold:{t}"}
        for t in (tickers or [])
    ]
    embed = {
        "title": "Your bets — SELL SOMETHING NOW" if needs_sell else "Your bets — nothing to do",
        "color": 15158332 if needs_sell else 10181046,  # red if you need to act, else purple
        "description": "\n\n".join(lines),
        "footer": {"text": f"Tap a button once you've sold it in the game. {GUESS_NOTE}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    post_channel_message(bot_token, channel_id, embed, ping=needs_sell, buttons=buttons)


def post_heartbeat_to_discord(bot_token: str, channel_id: str, current_signals: dict, errors: dict):
    good_lines = []
    waiting = []
    broken = []
    for ticker, signal in current_signals.items():
        if ticker in errors:
            broken.append(ticker)
        elif signal == "LONG":
            good_lines.append(f"🟢 **{ticker}** — press LONG (bet it goes up)")
        elif signal == "SHORT":
            good_lines.append(f"🔴 **{ticker}** — press SHORT (bet it goes down)")
        else:
            waiting.append(ticker)

    parts = []
    if good_lines:
        parts.append("**Worth a bet right now:**\n" + "\n".join(good_lines))
    if waiting:
        parts.append(f"**Nothing happening ({len(waiting)}):** " + ", ".join(waiting))
    if broken:
        parts.append("**Could not get prices for:** " + ", ".join(broken))

    embed = {
        "title": "I'm still running",
        "color": 3447003,  # blue
        "description": "\n\n".join(parts) if parts else "No prices available this time.",
        "footer": {"text": "Just checking in every hour so you know I'm alive. Nothing to do here."},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    post_channel_message(bot_token, channel_id, embed)


def get_position(state: dict, ticker: str):
    """The bet you told me about with /invest, or None. Bets ONLY exist here if you declared them."""
    return state.get("positions", {}).get(ticker)


def set_position(state: dict, ticker: str, signal: str, entry_price, stop_loss_pct=None, take_profit_pct=None, shares=None):
    positions = state.setdefault("positions", {})
    if signal in ("LONG", "SHORT"):
        positions[ticker] = {
            "signal": signal, "entry_price": entry_price, "shares": shares,
            "stop_loss_pct": stop_loss_pct, "take_profit_pct": take_profit_pct,
            "opened_at": datetime.now(timezone.utc).isoformat(),
        }
    else:
        positions.pop(ticker, None)


def default_share_count(cfg: dict, price: float) -> int:
    """
    A sensible share count when the user didn't give one and there's no live suggestion
    (e.g. they bet on a stock that isn't signalling). Uses the smallest risk step.
    """
    if not price or price <= 0:
        return 1
    return max(1, round(cfg["bankroll"] * cfg["min_risk_pct"] / price))


def compute_profit(entry_price, current_price, position: str, shares):
    """Actual money made or lost, in dollars. None if we don't know how many shares."""
    if not entry_price or not shares:
        return None
    direction = 1 if position == "LONG" else -1
    return direction * (current_price - entry_price) * shares


def record_closed_bet(state: dict, ticker: str, pos: dict, exit_price, profit):
    """Keep a running record so you can see how you're doing overall."""
    history = state.setdefault("history", [])
    history.append({
        "ticker": ticker,
        "signal": pos["signal"],
        "entry_price": pos.get("entry_price"),
        "exit_price": exit_price,
        "shares": pos.get("shares"),
        "profit": profit,
        "closed_at": datetime.now(timezone.utc).isoformat(),
    })
    del history[:-100]  # keep the last 100 bets, no need for more
    if profit is not None:
        state["total_profit"] = state.get("total_profit", 0) + profit


def summarize_record(state: dict):
    """One plain line about how you've done overall, or None if you haven't finished any bets yet."""
    history = state.get("history", [])
    scored = [h for h in history if h.get("profit") is not None]
    if not scored:
        return None
    wins = sum(1 for h in scored if h["profit"] > 0)
    total = state.get("total_profit", 0)
    updown = "up" if total >= 0 else "down"
    return (
        f"Overall: **{updown} ${abs(total):,.0f}** across {len(scored)} finished bet"
        f"{'s' if len(scored) != 1 else ''} ({wins} good, {len(scored) - wins} bad)."
    )


def get_bankroll(cfg: dict, state: dict) -> float:
    return state.get("bankroll", cfg["bankroll"])


def get_effective_config(cfg: dict, state: dict) -> dict:
    """cfg with the user's current /money amount applied, so sizing suggestions stay accurate."""
    return {**cfg, "bankroll": get_bankroll(cfg, state)}


def get_default_target_pct(state: dict):
    return state.get("target_profit_pct")


def evaluate_position_action(position: str, entry_price, stop_loss_pct, take_profit_pct, current_signal: str, pnl_pct):
    """
    Decide what to do with a bet, in plain words.
    Returns (what_to_do, sell_now) so callers don't have to read the text to know if action is needed.
    """
    stop_loss_hit = stop_loss_pct is not None and pnl_pct is not None and pnl_pct <= -abs(stop_loss_pct)
    take_profit_hit = take_profit_pct is not None and pnl_pct is not None and pnl_pct >= abs(take_profit_pct)

    if stop_loss_hit:
        return f"**SELL NOW** — you're down {abs(pnl_pct):.1f}%, worse than the {abs(stop_loss_pct):.0f}% loss you said to stop at", True
    if take_profit_hit:
        return f"**SELL NOW** — you made {pnl_pct:.1f}%, you hit the {abs(take_profit_pct):.0f}% profit you were aiming for", True
    if current_signal == position:
        return "**KEEP IT** — still going your way", False
    if current_signal == "NEUTRAL":
        return "**SELL NOW** — the move has run out of steam", True
    return "**SELL NOW** — it's turning the other way", True


def compute_early_warning(position: str, info: dict) -> bool:
    """
    True when exactly ONE of the two directional confirmations (trend, MACD) has already
    flipped against the position but the other hasn't yet — a real, data-derived heads-up
    that a reversal may be close. Not a time estimate (e.g. "3 min before") — that can't be
    honestly predicted. If BOTH had flipped, the signal itself would already show SELL.
    """
    if position == "LONG":
        trend_against = info["short_ema"] < info["long_ema"]
        macd_against = info["macd_line"] < info["macd_signal_line"]
    else:
        trend_against = info["short_ema"] > info["long_ema"]
        macd_against = info["macd_line"] > info["macd_signal_line"]
    return trend_against != macd_against


def format_bet_line(ticker: str, pos: dict, info):
    """
    One plain-English block describing a bet, what it's worth, and what to do about it.
    Returns (text, sell_now). Used by both the automatic message and /status so they always agree.
    """
    position = pos["signal"]
    entry_price = pos.get("entry_price")
    shares = pos.get("shares")
    bet_word = "betting it goes UP" if position == "LONG" else "betting it goes DOWN"

    if info is None:
        return f"**{ticker}** ({bet_word})\nCan't get the price right now — I'll try again next check.", False

    price = info["price"]
    pnl_pct = compute_pnl_pct(entry_price, price, position)
    profit = compute_profit(entry_price, price, position, shares)
    action, sell_now = evaluate_position_action(
        position, entry_price, pos.get("stop_loss_pct"), pos.get("take_profit_pct"), info["signal"], pnl_pct,
    )
    if not sell_now and compute_early_warning(position, info):
        action += "\n⚠️ but it's starting to turn — keep an eye on it"

    rows = [f"**{ticker}** ({bet_word})"]
    if entry_price:
        share_note = f"{shares} shares, " if shares else ""
        rows.append(f"{share_note}bought at ${entry_price:.2f}, now ${price:.2f}")
    else:
        rows.append(f"Now ${price:.2f}")

    if profit is not None:
        made_lost = "make" if profit >= 0 else "lose"
        rows.append(f"💰 Sell now and you **{made_lost} ${abs(profit):,.2f}** ({pnl_pct:+.1f}%)")
    elif pnl_pct is not None:
        rows.append(f"You're **{'up' if pnl_pct >= 0 else 'down'} {abs(pnl_pct):.1f}%** "
                    f"(tell me your share count with /invest to see this in dollars)")

    rows.append(f"👉 {action}")
    return "\n".join(rows), sell_now


def get_scanner_signal(state: dict, ticker: str) -> str:
    """Last signal the automatic scanner observed for this ticker, independent of any declared position."""
    return state.get("_scanner_signals", {}).get(ticker, "NEUTRAL")


def set_scanner_signal(state: dict, ticker: str, signal: str):
    state.setdefault("_scanner_signals", {})[ticker] = signal


def run_pass(cfg: dict, bot_token: str, signals_channel_id: str, trades_channel_id: str, heartbeat_channel_id: str, last_signal: dict) -> dict:
    cfg = get_effective_config(cfg, last_signal)
    current_signals = {}
    errors = {}
    new_signals = []

    for ticker in cfg["tickers"]:
        try:
            info = get_signal(ticker, cfg)
            if info is None:
                print(f"[{ticker}] not enough data yet, skipping")
                continue

            new_signal = info["signal"]
            previously_seen = get_scanner_signal(last_signal, ticker)
            if new_signal != "NEUTRAL" and new_signal != previously_seen:
                new_signals.append(info)

            set_scanner_signal(last_signal, ticker, new_signal)
            current_signals[ticker] = new_signal
        except Exception as e:
            print(f"[{ticker}] error: {e}")
            errors[ticker] = str(e)
            current_signals[ticker] = get_scanner_signal(last_signal, ticker)

    if new_signals:
        try:
            post_digest_to_discord(bot_token, signals_channel_id, new_signals)
            print(f"posted digest with {len(new_signals)} new signal(s)")
        except Exception as e:
            print(f"digest post error: {e}")

    positions = last_signal.get("positions", {})
    if positions:
        lines = []
        any_sell = False
        for ticker, pos in positions.items():
            try:
                info = get_signal(ticker, cfg)
            except Exception:
                info = None

            line, sell_now = format_bet_line(ticker, pos, info)
            lines.append(line)
            any_sell = any_sell or sell_now

        record = summarize_record(last_signal)
        if record:
            lines.append(record)

        try:
            post_portfolio_to_discord(bot_token, trades_channel_id, lines, any_sell, list(positions.keys()))
            print(f"posted portfolio update for {len(positions)} position(s)")
        except Exception as e:
            print(f"portfolio post error: {e}")

    heartbeat_interval = cfg.get("heartbeat_interval_seconds", 3600)
    last_heartbeat_str = last_signal.get("_last_heartbeat")
    now = datetime.now(timezone.utc)
    due_for_heartbeat = True
    if last_heartbeat_str:
        last_heartbeat = datetime.fromisoformat(last_heartbeat_str)
        due_for_heartbeat = (now - last_heartbeat).total_seconds() >= heartbeat_interval

    if due_for_heartbeat:
        try:
            post_heartbeat_to_discord(bot_token, heartbeat_channel_id, current_signals, errors)
            print("posted heartbeat")
            last_signal["_last_heartbeat"] = now.isoformat()
        except Exception as e:
            print(f"heartbeat post error: {e}")

    return last_signal


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single pass and exit, persisting state to state.json (used by scheduled runners like GitHub Actions).",
    )
    args = parser.parse_args()

    cfg = load_config()
    bot_token = get_bot_token(cfg)
    signals_channel_id = get_channel_id(cfg, "signals")
    trades_channel_id = get_channel_id(cfg, "trades")
    heartbeat_channel_id = get_channel_id(cfg, "heartbeat")
    if not bot_token:
        raise SystemExit("No Discord bot token configured (set discord_bot_token in config.json or DISCORD_BOT_TOKEN env var).")
    if not (signals_channel_id and trades_channel_id and heartbeat_channel_id):
        raise SystemExit("Missing one or more channel IDs (discord_signals_channel_id, discord_trades_channel_id, discord_heartbeat_channel_id).")

    if args.once:
        last_signal = load_state()
        last_signal = run_pass(cfg, bot_token, signals_channel_id, trades_channel_id, heartbeat_channel_id, last_signal)
        save_state(last_signal)
    else:
        last_signal = {}
        print("Starting predictor loop. Ctrl+C to stop.")
        while True:
            last_signal = run_pass(cfg, bot_token, signals_channel_id, trades_channel_id, heartbeat_channel_id, last_signal)
            time.sleep(cfg["poll_interval_seconds"])


if __name__ == "__main__":
    main()
