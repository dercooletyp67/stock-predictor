"""
Polls real-time-ish stock data and posts long/short signals to a Discord webhook.

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


def get_webhook_url(cfg: dict) -> str:
    return os.environ.get("DISCORD_WEBHOOK_URL") or cfg.get("discord_webhook_url", "")


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

    returns = close.pct_change().dropna()
    bar_volatility = float(returns.tail(cfg["volume_period"]).std())
    projected_move_pct = bar_volatility * (cfg["horizon_bars"] ** 0.5) * 100
    direction = 1 if signal == "LONG" else -1
    target_price = price * (1 + direction * projected_move_pct / 100)

    return {
        "confidence": confidence,
        "suggested_amount": suggested_amount,
        "risk_pct": risk_pct,
        "projected_move_pct": projected_move_pct,
        "target_price": target_price,
        "bankroll": cfg["bankroll"],
    }


def post_to_discord(webhook_url: str, info: dict):
    color = {"LONG": 3066993, "SHORT": 15158332, "NEUTRAL": 9807270}[info["signal"]]
    action_line = f"**{info['signal']} {info['ticker']}**"
    if info["signal"] != "NEUTRAL":
        action_line += f" — invest **${info['suggested_amount']:,.0f}** (confidence {info['confidence']*100:.0f}%)"

    embed = {
        "title": action_line,
        "color": color,
        "fields": [
            {"name": "Current price", "value": f"${info['price']:.2f}", "inline": True},
            {"name": "RSI", "value": f"{info['rsi']:.1f}", "inline": True},
            {"name": "EMA9 / EMA21", "value": f"{info['short_ema']:.2f} / {info['long_ema']:.2f}", "inline": True},
            {"name": "MACD / Signal", "value": f"{info['macd_line']:.3f} / {info['macd_signal_line']:.3f}", "inline": True},
            {"name": "Volume confirmed", "value": "Yes" if info["volume_confirmed"] else "No", "inline": True},
        ],
        "footer": {"text": "Multi-indicator signal (EMA+MACD+volume). Not a guarantee. Not financial advice."},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if info["signal"] != "NEUTRAL":
        embed["fields"].insert(0, {
            "name": "Predicted target (next ~1hr)",
            "value": f"${info['target_price']:.2f}  ({'+' if info['signal']=='LONG' else '-'}{info['projected_move_pct']:.2f}%)",
            "inline": False,
        })
        embed["fields"].insert(1, {
            "name": "Suggested amount",
            "value": f"${info['suggested_amount']:,.0f} of ${info['bankroll']:,.0f} bankroll ({info['risk_pct']*100:.1f}% risk)",
            "inline": False,
        })

    resp = requests.post(webhook_url, json={"embeds": [embed]}, timeout=10)
    resp.raise_for_status()


def post_exit_to_discord(webhook_url: str, ticker: str, closing_position: str, info: dict):
    reason = "signal reversed" if info["signal"] != "NEUTRAL" else "signal faded to neutral"
    embed = {
        "title": f"**SELL / CLOSE {closing_position} {ticker}**",
        "color": 15844367,  # amber
        "description": f"Exit your {closing_position} position — {reason}.",
        "fields": [
            {"name": "Current price", "value": f"${info['price']:.2f}", "inline": True},
            {"name": "RSI", "value": f"{info['rsi']:.1f}", "inline": True},
        ],
        "footer": {"text": "Exit signal from the same indicator logic. Not a guarantee. Not financial advice."},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    resp = requests.post(webhook_url, json={"embeds": [embed]}, timeout=10)
    resp.raise_for_status()


def post_heartbeat_to_discord(webhook_url: str, current_signals: dict, errors: dict):
    lines = []
    for ticker, signal in current_signals.items():
        marker = " (data error)" if ticker in errors else ""
        lines.append(f"**{ticker}**: {signal}{marker}")

    embed = {
        "title": "Predictor heartbeat — still running",
        "color": 3447003,  # blue
        "description": "\n".join(lines) if lines else "No ticker data available this check.",
        "footer": {"text": "Periodic health check. No action needed unless a ticker shows a data error."},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    resp = requests.post(webhook_url, json={"embeds": [embed]}, timeout=10)
    resp.raise_for_status()


def run_pass(cfg: dict, webhook_url: str, last_signal: dict) -> dict:
    current_signals = {}
    errors = {}

    for ticker in cfg["tickers"]:
        try:
            info = get_signal(ticker, cfg)
            if info is None:
                print(f"[{ticker}] not enough data yet, skipping")
                continue

            previous = last_signal.get(ticker)
            previous_position = previous if previous in ("LONG", "SHORT") else None
            new_signal = info["signal"]
            notify_enabled = not cfg.get("only_notify_on_change", True) or new_signal != previous

            if previous_position and new_signal != previous_position:
                post_exit_to_discord(webhook_url, ticker, previous_position, info)
                print(f"[{ticker}] posted EXIT for {previous_position} @ ${info['price']:.2f}")

            if new_signal != "NEUTRAL" and notify_enabled:
                post_to_discord(webhook_url, info)
                print(f"[{ticker}] posted signal: {new_signal} @ ${info['price']:.2f}")
            elif not (previous_position and new_signal != previous_position):
                print(f"[{ticker}] {new_signal} @ ${info['price']:.2f} (no notify)")

            last_signal[ticker] = new_signal
            current_signals[ticker] = new_signal
        except Exception as e:
            print(f"[{ticker}] error: {e}")
            errors[ticker] = str(e)
            current_signals[ticker] = last_signal.get(ticker, "UNKNOWN")

    heartbeat_interval = cfg.get("heartbeat_interval_seconds", 3600)
    last_heartbeat_str = last_signal.get("_last_heartbeat")
    now = datetime.now(timezone.utc)
    due_for_heartbeat = True
    if last_heartbeat_str:
        last_heartbeat = datetime.fromisoformat(last_heartbeat_str)
        due_for_heartbeat = (now - last_heartbeat).total_seconds() >= heartbeat_interval

    if due_for_heartbeat:
        try:
            post_heartbeat_to_discord(webhook_url, current_signals, errors)
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
    webhook_url = get_webhook_url(cfg)
    if not webhook_url:
        raise SystemExit("No Discord webhook URL configured (set discord_webhook_url in config.json or DISCORD_WEBHOOK_URL env var).")

    if args.once:
        last_signal = load_state()
        last_signal = run_pass(cfg, webhook_url, last_signal)
        save_state(last_signal)
    else:
        last_signal = {}
        print("Starting predictor loop. Ctrl+C to stop.")
        while True:
            last_signal = run_pass(cfg, webhook_url, last_signal)
            time.sleep(cfg["poll_interval_seconds"])


if __name__ == "__main__":
    main()
