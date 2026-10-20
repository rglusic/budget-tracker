"""Budget Tracker — Flask app.

Serves the single-page UI and exposes the published actions as
POST /api/<action> endpoints (JSON in, JSON out).

Run:
    python app.py            # http://127.0.0.1:5000

Env:
    BUDGET_DB        SQLite path (default ./budget.db, gitignored)
    SYNC_PROVIDER    null|plaid (default null)
    PLAID_*          Plaid credentials (never committed; see README)
"""
from __future__ import annotations

import os

from flask import Flask, jsonify, render_template, request

from budget import actions, db

app = Flask(__name__)


def _conn():
    return db.get_db()


# ------------------------------------------------------------------ actions
# Each published action is a POST /api/<name> endpoint. GET is also allowed
# for the read-only actions so the UI can fetch them simply.

READ_ACTIONS = {
    "get_overview": lambda c, a: actions.get_overview(c),
    "get_inbox": lambda c, a: actions.get_inbox(c),
    "get_budget": lambda c, a: actions.get_budget(c, a.get("month")),
    "get_bill_plan": lambda c, a: actions.get_bill_plan(c),
    "get_bills": lambda c, a: actions.get_bills(c),
    "get_debts": lambda c, a: actions.get_debts(c),
}

WRITE_ACTIONS = {
    "sync_now": lambda c, a: actions.sync_now(c),
    "import_seed": lambda c, a: actions.import_seed(c, a.get("seed", {})),
    "categorize_transaction": lambda c, a: actions.categorize_transaction(
        c, int(a["txn_id"]), int(a["category_id"])),
    "assign_budget": lambda c, a: actions.assign_budget(
        c, a["month"], int(a["category_id"]), int(a["amount_cents"])),
    "upsert_bill": lambda c, a: actions.upsert_bill(c, a.get("bill", {})),
    "delete_bill": lambda c, a: actions.delete_bill(c, int(a["bill_id"])),
    "mark_bill_paid": lambda c, a: actions.mark_bill_paid(
        c, int(a["bill_id"]), a.get("date")),
    "set_paycheck": lambda c, a: actions.set_paycheck(
        c, a["date"], a.get("amount_cents"),
        cadence=a.get("cadence"), month_days=a.get("month_days"),
        interval_days=a.get("interval_days")),
    "upsert_debt": lambda c, a: actions.upsert_debt(c, a.get("debt", {})),
    "update_debt_balance": lambda c, a: actions.update_debt_balance(
        c, int(a["debt_id"]), int(a["balance_cents"])),
}


@app.post("/api/<name>")
def api_post(name: str):
    handler = {**READ_ACTIONS, **WRITE_ACTIONS}.get(name)
    if not handler:
        return jsonify({"error": f"unknown action: {name}"}), 404
    args = request.get_json(silent=True) or {}
    conn = _conn()
    try:
        return jsonify(handler(conn, args))
    except (KeyError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    except RuntimeError as exc:  # e.g. sync provider misconfiguration
        return jsonify({"error": str(exc)}), 502
    finally:
        conn.close()


@app.get("/api/<name>")
def api_get(name: str):
    handler = READ_ACTIONS.get(name)
    if not handler:
        return jsonify({"error": f"unknown action: {name}"}), 404
    conn = _conn()
    try:
        args = dict(request.args)
        return jsonify(handler(conn, args))
    finally:
        conn.close()


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="127.0.0.1", port=port, debug=False)
