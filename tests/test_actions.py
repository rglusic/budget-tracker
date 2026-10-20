"""Smoke tests for the actions layer. All fixtures are SYNTHETIC — no real
personal data. Run:  python -m pytest tests/   (or python tests/test_actions.py)
"""
import sqlite3
import sys
import os
import datetime as _dt

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from budget import actions, db

# Keep date-sensitive fixtures relative to real today so the suite is stable.
_TODAY = _dt.date.today()
_NEXT_PAYCHECK = (_TODAY + _dt.timedelta(days=20)).isoformat()


def fresh():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    return conn


def test_seed_and_categorize():
    conn = fresh()
    res = actions.import_seed(conn, {
        "accounts": [
            {"plaid_account_id": "acc-check-1", "name": "Test Checking",
             "mask": "0001", "type": "checking", "balance_cents": 100000,
             "last_synced_at": "2026-01-01T00:00:00Z"},
        ],
        "categories": [
            {"name": "Groceries", "grp": "Frequent", "sort_order": 1},
            {"name": "Fun", "grp": "Frequent", "sort_order": 2},
        ],
        "transactions": [
            {"plaid_txn_id": "txn-1", "account_id": "acc-check-1",
             "date": "2026-01-05", "merchant": "Test Grocer", "name": "TEST GROCER",
             "amount_cents": -2500, "category_id": None, "pending": 0},
            {"plaid_txn_id": "txn-2", "account_id": "acc-check-1",
             "date": "2026-01-06", "merchant": "Test Employer", "name": "PAYROLL",
             "amount_cents": 50000, "category_id": None, "pending": 0},
        ],
        "bills": [
            {"name": "Test Electric", "amount_cents": 8000,
             "due_day": min(28, _TODAY.day + 5),  # lands before the fake paycheck
             "frequency": "monthly", "autopay": 1, "autopay_from": "Test Checking"},
        ],
        "debts": [
            {"name": "Test Card", "creditor": "Test Bank", "balance_cents": 120000,
             "apr": 0.24, "minimum_cents": 3000, "due_day": 10, "payoff_order": 1},
        ],
        "meta": {"next_paycheck_date": _NEXT_PAYCHECK},
    })
    assert res["ok"] and res["imported"]["transactions"] == 2

    # idempotent re-import
    res2 = actions.import_seed(conn, {
        "transactions": [
            {"plaid_txn_id": "txn-1", "account_id": "acc-check-1",
             "date": "2026-01-05", "merchant": "Test Grocer",
             "amount_cents": -2500},
        ],
    })
    n = conn.execute("SELECT COUNT(*) c FROM transactions").fetchone()["c"]
    assert n == 2, f"expected 2 txns after idempotent re-import, got {n}"

    inbox = actions.get_inbox(conn)
    assert inbox["count"] == 2

    groceries = conn.execute("SELECT id FROM categories WHERE name='Groceries'").fetchone()["id"]
    txn1 = conn.execute("SELECT id FROM transactions WHERE plaid_txn_id='txn-1'").fetchone()["id"]
    actions.categorize_transaction(conn, txn1, groceries)

    # payee rule learned
    rule = conn.execute("SELECT * FROM payee_rules WHERE merchant_pattern='Test Grocer'").fetchone()
    assert rule and rule["category_id"] == groceries

    # suggestion fires for a new matching txn
    conn.execute(
        "INSERT INTO transactions (plaid_txn_id, account_id, date, merchant, name, amount_cents, created_at)"
        " VALUES ('txn-3', 1, '2026-01-07', 'Test Grocer Downtown', 'TEST GROCER DT', -1200, '2026-01-01T00:00:00Z')"
    )
    inbox = actions.get_inbox(conn)
    sugg = [t for t in inbox["items"] if t["plaid_txn_id"] == "txn-3"][0]["suggestion"]
    assert sugg and sugg["category_id"] == groceries, "payee rule should suggest Groceries"

    # budget math: assign $100, spend $25 + $12 -> available $63
    fun = conn.execute("SELECT id FROM categories WHERE name='Fun'").fetchone()["id"]
    actions.assign_budget(conn, "2026-01", fun, 10000)
    actions.assign_budget(conn, "2026-01", fun, -1000)  # delta
    conn.execute("UPDATE transactions SET category_id=?, date='2026-01-10' WHERE plaid_txn_id='txn-1'",
                 (fun,))
    b = actions.get_budget(conn, "2026-01")
    row = [c for g in b["groups"] for c in g["categories"] if c["name"] == "Fun"][0]
    assert row["assigned_cents"] == 9000, row
    assert row["spent_cents"] == 2500, row
    assert row["available_cents"] == 6500, row  # 9000 - 2500 + 0 rollover

    # rollover into next month
    b2 = actions.get_budget(conn, "2026-02")
    row2 = [c for g in b2["groups"] for c in g["categories"] if c["name"] == "Fun"][0]
    assert row2["available_cents"] == 6500, row2

    # unassigned = cash - sum(available)
    ov = actions.get_overview(conn)
    assert ov["cash_cents"] == 100000
    assert ov["unassigned_cents"] == 100000 - 6500 - 0  # Groceries available 0... check below
    print("overview unassigned:", ov["unassigned_cents"])

    # bills + debts
    bills = actions.get_bills(conn)
    assert any(x["name"] == "Test Electric" for x in bills["bills"])
    bid = conn.execute("SELECT id FROM bills WHERE name='Test Electric'").fetchone()["id"]
    actions.mark_bill_paid(conn, bid, "2026-01-15")
    assert conn.execute("SELECT last_paid_date FROM bills WHERE id=?", (bid,)).fetchone()[0] == "2026-01-15"

    debts = actions.get_debts(conn)
    assert debts["total_minimum_cents"] == 3000
    assert debts["est_monthly_interest_cents"] == round(120000 * 0.24 / 12)

    # due-before-paycheck strip
    assert any(x["name"] == "Test Electric" for x in bills["due_before_paycheck"])
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    test_seed_and_categorize()
