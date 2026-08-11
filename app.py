"""
Small HTTP wrapper around predictor.py so it can run as a Render free Web Service.
An external pinger (e.g. cron-job.org) hits /run-check on a schedule to trigger checks,
since Render's free tier only allows Web Services (not standalone background workers/cron).
"""
import os

from flask import Flask, request, jsonify

from predictor import load_config, get_webhook_url, load_state, save_state, run_pass

app = Flask(__name__)


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

    state = load_state()
    state = run_pass(cfg, webhook_url, state)
    save_state(state)

    return jsonify({"status": "ok", "signals": {k: v for k, v in state.items() if not k.startswith("_")}})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
