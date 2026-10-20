"""Business logic: the app's published actions.

Every function takes an open sqlite3 connection (row_factory = Row) and
returns plain JSON-serializable dicts/lists. Money is integer cents.
amount_cents sign convention: negative = outflow, positive = inflow.
"""
from __future__ import annotations

import calendar
import datetime as _dt
from typing import Any
from zoneinfo import ZoneInfo

from .sync import get_provider, utcnow_iso

DISPLAY_TZ = ZoneInfo("America/Los_Angeles")


# ---------------------------------------------------------------- helpers

def _row_to_dict(row) -> dict:
    return dict(row) if row is not None else None


def _today_la() -> _dt.date:
    return _dt.datetime.now(DISPLAY_TZ).date()


def _month_add(month: str, delta: int) -> str:
    y, m = int(month[:4]), int(month[5:7])
    m += delta
    while m < 1:
        m += 12
        y -= 1
    while m > 12:
        m -= 12
        y += 1
    return f"{y:04d}-{m:02d}"


def _meta_get(conn, key: str, default=None):
    r = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def _meta_set(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def _fmt_cents(cents: int | None) -> str:
    if cents is None:
        return "—"
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) // 100:,}.{abs(cents) % 100:02d}"


# ---------------------------------------------------------------- budget math

def _spent_cents(conn, category_id: int, month: str) -> int:
    """Positive cents spent (outflows) in a category for YYYY-MM."""
    r = conn.execute(
        """SELECT COALESCE(SUM(CASE WHEN amount_cents < 0 THEN -amount_cents ELSE 0 END), 0) AS spent,
                  COALESCE(SUM(CASE WHEN amount_cents > 0 THEN amount_cents ELSE 0 END), 0) AS inflow
           FROM transactions
           WHERE category_id = ? AND substr(date, 1, 7) = ?""",
        (category_id, month),
    ).fetchone()
    return int(r["spent"] - r["inflow"])


def _assigned_cents(conn, category_id: int, month: str) -> int:
    r = conn.execute(
        "SELECT COALESCE(assigned_cents, 0) AS a FROM budget_months WHERE month=? AND category_id=?",
        (month, category_id),
    ).fetchone()
    return int(r["a"]) if r else 0


def _earliest_month(conn) -> str | None:
    r = conn.execute(
        """SELECT MIN(m) AS m FROM (
               SELECT substr(date,1,7) AS m FROM transactions
               UNION SELECT month AS m FROM budget_months
           )"""
    ).fetchone()
    return r["m"] if r and r["m"] else None


def category_available(conn, category_id: int, month: str, _memo: dict | None = None) -> int:
    """Available = assigned - spent + rollover from prior month (full rollover)."""
    _memo = _memo if _memo is not None else {}
    key = (category_id, month)
    if key in _memo:
        return _memo[key]
    earliest = _earliest_month(conn)
    if earliest is None or month <= earliest:
        rollover = 0
    else:
        rollover = category_available(conn, category_id, _month_add(month, -1), _memo)
    avail = (_assigned_cents(conn, category_id, month)
             - _spent_cents(conn, category_id, month) + rollover)
    _memo[key] = avail
    return avail


def _cash_cents(conn) -> int:
    r = conn.execute(
        """SELECT COALESCE(SUM(balance_cents), 0) AS c FROM accounts
           WHERE include_in_budget = 1 AND type IN ('checking', 'savings')"""
    ).fetchone()
    return int(r["c"])


def unassigned_cents(conn, month: str) -> int:
    cats = conn.execute("SELECT id FROM categories").fetchall()
    memo: dict = {}
    assigned_total = sum(category_available(conn, c["id"], month, memo) for c in cats)
    return _cash_cents(conn) - assigned_total


# ---------------------------------------------------------------- overview

def get_overview(conn) -> dict[str, Any]:
    month = _today_la().strftime("%Y-%m")
    cash = _cash_cents(conn)
    debt_rows = conn.execute(
        """SELECT id, name, balance_cents FROM accounts
           WHERE type IN ('credit', 'loan') AND balance_cents < 0"""
    ).fetchall()
    debt = sum(-r["balance_cents"] for r in debt_rows)
    accounts = [
        {
            "id": r["id"], "name": r["name"], "mask": r["mask"], "type": r["type"],
            "balance_cents": r["balance_cents"],
            "balance_display": _fmt_cents(r["balance_cents"]),
            "last_synced_at": r["last_synced_at"],
            "include_in_budget": bool(r["include_in_budget"]),
        }
        for r in conn.execute(
            "SELECT * FROM accounts ORDER BY type, name"
        ).fetchall()
    ]
    return {
        "month": month,
        "cash_cents": cash,
        "cash_display": _fmt_cents(cash),
        "debt_cents": debt,
        "debt_display": _fmt_cents(debt),
        "net_cents": cash - debt,
        "net_display": _fmt_cents(cash - debt),
        "unassigned_cents": unassigned_cents(conn, month),
        "unassigned_display": _fmt_cents(unassigned_cents(conn, month)),
        "accounts": accounts,
        "due_before_paycheck": _due_before_paycheck(conn),
        "last_sync_at": _meta_get(conn, "last_sync_at"),
    }


# ---------------------------------------------------------------- inbox

def _suggest_category(conn, merchant: str | None, name: str | None) -> dict | None:
    hay = f"{merchant or ''} {name or ''}".lower()
    if not hay.strip():
        return None
    rules = conn.execute(
        """SELECT pr.merchant_pattern, pr.category_id, pr.use_count, c.name AS category_name
           FROM payee_rules pr JOIN categories c ON c.id = pr.category_id
           ORDER BY pr.use_count DESC"""
    ).fetchall()
    for r in rules:
        if r["merchant_pattern"].lower() in hay:
            return {"category_id": r["category_id"], "category_name": r["category_name"]}
    return None


def _category_chips(conn) -> list[dict]:
    rows = conn.execute(
        """SELECT c.id, c.name, c.grp,
                  (SELECT COUNT(*) FROM transactions t WHERE t.category_id = c.id) AS uses,
                  (SELECT COALESCE(SUM(pr.use_count), 0) FROM payee_rules pr
                   WHERE pr.category_id = c.id) AS rule_uses
           FROM categories c
           ORDER BY (uses + rule_uses) DESC, c.grp, c.sort_order"""
    ).fetchall()
    return [{"id": r["id"], "name": r["name"], "grp": r["grp"]} for r in rows]


def get_inbox(conn) -> dict[str, Any]:
    rows = conn.execute(
        """SELECT t.*, a.name AS account_name, c.name AS category_name
           FROM transactions t
           JOIN accounts a ON a.id = t.account_id
           LEFT JOIN categories c ON c.id = t.category_id
           WHERE t.pending = 1 OR t.category_id IS NULL
           ORDER BY t.date DESC, t.id DESC
           LIMIT 200"""
    ).fetchall()
    items = []
    for r in rows:
        d = _row_to_dict(r)
        d["amount_display"] = _fmt_cents(d["amount_cents"])
        suggestion = _suggest_category(conn, d.get("merchant"), d.get("name"))
        d["suggestion"] = suggestion
        d["has_suggestion"] = suggestion is not None
        items.append(d)
    return {"count": len(items), "items": items, "category_chips": _category_chips(conn)}


def categorize_transaction(conn, txn_id: int, category_id: int) -> dict[str, Any]:
    txn = conn.execute("SELECT * FROM transactions WHERE id=?", (txn_id,)).fetchone()
    if not txn:
        raise KeyError(f"transaction {txn_id} not found")
    cat = conn.execute("SELECT * FROM categories WHERE id=?", (category_id,)).fetchone()
    if not cat:
        raise KeyError(f"category {category_id} not found")
    conn.execute("UPDATE transactions SET category_id=? WHERE id=?", (category_id, txn_id))
    # Learn / reinforce the payee rule.
    pattern = (txn["merchant"] or txn["name"] or "").strip()
    if pattern:
        now = utcnow_iso()
        conn.execute(
            """INSERT INTO payee_rules (merchant_pattern, category_id, use_count, updated_at)
               VALUES (?, ?, 1, ?)
               ON CONFLICT(merchant_pattern) DO UPDATE SET
                 category_id=excluded.category_id,
                 use_count=payee_rules.use_count + 1,
                 updated_at=excluded.updated_at""",
            (pattern, category_id, now),
        )
    conn.commit()
    return {"ok": True, "txn_id": txn_id, "category_id": category_id,
            "category_name": cat["name"]}


# ---------------------------------------------------------------- budget tab

def get_budget(conn, month: str | None = None) -> dict[str, Any]:
    month = month or _today_la().strftime("%Y-%m")
    memo: dict = {}
    groups: dict[str, list] = {}
    for c in conn.execute("SELECT * FROM categories ORDER BY grp, sort_order, name").fetchall():
        assigned = _assigned_cents(conn, c["id"], month)
        spent = _spent_cents(conn, c["id"], month)
        available = category_available(conn, c["id"], month, memo)
        groups.setdefault(c["grp"], []).append({
            "id": c["id"], "name": c["name"],
            "assigned_cents": assigned, "assigned_display": _fmt_cents(assigned),
            "spent_cents": spent, "spent_display": _fmt_cents(spent),
            "available_cents": available, "available_display": _fmt_cents(available),
        })
    un = unassigned_cents(conn, month)
    return {
        "month": month,
        "prev_month": _month_add(month, -1),
        "next_month": _month_add(month, 1),
        "groups": [{"name": g, "categories": cats} for g, cats in groups.items()],
        "unassigned_cents": un,
        "unassigned_display": _fmt_cents(un),
    }


def assign_budget(conn, month: str, category_id: int, amount_cents: int) -> dict[str, Any]:
    """Apply a delta (may be negative) to a category's assigned amount."""
    if not conn.execute("SELECT 1 FROM categories WHERE id=?", (category_id,)).fetchone():
        raise KeyError(f"category {category_id} not found")
    conn.execute(
        """INSERT INTO budget_months (month, category_id, assigned_cents)
           VALUES (?, ?, ?)
           ON CONFLICT(month, category_id) DO UPDATE SET
             assigned_cents = budget_months.assigned_cents + excluded.assigned_cents""",
        (month, category_id, int(amount_cents)),
    )
    conn.commit()
    return {"ok": True, "month": month, "category_id": category_id,
            "assigned_cents": _assigned_cents(conn, category_id, month),
            "available_cents": category_available(conn, category_id, month)}


# ---------------------------------------------------------------- bills

def _next_due_date(due_day: int, frequency: str, today: _dt.date) -> str:
    freq = (frequency or "monthly").lower()
    if freq == "weekly":
        delta = (due_day - 1) % 7  # due_day 1..7 = Mon..Sun offset base
        base_weekday = (due_day - 1) % 7
        days_ahead = (base_weekday - today.weekday()) % 7
        return (today + _dt.timedelta(days=days_ahead)).isoformat()
    if freq == "yearly":
        for year in (today.year, today.year + 1):
            try:
                cand = _dt.date(year, due_day // 100, due_day % 100)
            except (ValueError, ZeroDivisionError):
                continue
            if cand >= today:
                return cand.isoformat()
        return _dt.date(today.year + 1, 1, 1).isoformat()
    # monthly (default)
    for delta_m in range(0, 3):
        y, m = today.year, today.month + delta_m
        while m > 12:
            m -= 12
            y += 1
        last = calendar.monthrange(y, m)[1]
        cand = _dt.date(y, m, min(due_day, last))
        if cand >= today:
            return cand.isoformat()
    return today.isoformat()


def _bill_dict(conn, r, today: _dt.date) -> dict:
    d = _row_to_dict(r)
    d["amount_display"] = _fmt_cents(d["amount_cents"])
    d["next_due_date"] = _next_due_date(d["due_day"], d["frequency"], today)
    d["autopay"] = bool(d["autopay"])
    d["active"] = bool(d["active"])
    return d


def get_bills(conn) -> dict[str, Any]:
    today = _today_la()
    rows = conn.execute("SELECT * FROM bills WHERE active=1").fetchall()
    bills = [_bill_dict(conn, r, today) for r in rows]
    bills.sort(key=lambda b: b["next_due_date"])
    return {
        "today": today.isoformat(),
        "next_paycheck_date": _meta_get(conn, "next_paycheck_date"),
        "bills": bills,
        "due_before_paycheck": _due_before_paycheck(conn),
    }


def _due_before_paycheck(conn) -> list[dict]:
    today = _today_la()
    npc = _meta_get(conn, "next_paycheck_date")
    if not npc:
        return []
    bills = [_bill_dict(conn, r, today)
             for r in conn.execute("SELECT * FROM bills WHERE active=1").fetchall()]
    out = [b for b in bills if b["next_due_date"] <= npc]
    out.sort(key=lambda b: b["next_due_date"])
    return out


def upsert_bill(conn, bill: dict) -> dict[str, Any]:
    fields = ("name", "amount_cents", "due_day", "frequency", "autopay",
              "autopay_from", "last_paid_date", "notes", "active")
    bill = _with_defaults(bill, {"frequency": "monthly", "autopay": 0,
                                 "autopay_from": None, "last_paid_date": None,
                                 "notes": None, "active": 1})
    if not bill.get("name") or bill.get("amount_cents") is None or not bill.get("due_day"):
        raise ValueError("bill needs name, amount_cents, due_day")
    conn.execute(
        """INSERT INTO bills (name, amount_cents, due_day, frequency, autopay,
                              autopay_from, last_paid_date, notes, active)
           VALUES (:name, :amount_cents, :due_day, :frequency, :autopay,
                   :autopay_from, :last_paid_date, :notes, :active)
           ON CONFLICT(name) DO UPDATE SET
             amount_cents=excluded.amount_cents, due_day=excluded.due_day,
             frequency=excluded.frequency, autopay=excluded.autopay,
             autopay_from=excluded.autopay_from, last_paid_date=excluded.last_paid_date,
             notes=excluded.notes, active=excluded.active""",
        bill,
    )
    conn.commit()
    r = conn.execute("SELECT * FROM bills WHERE name=?", (bill["name"],)).fetchone()
    return _bill_dict(conn, r, _today_la())


def delete_bill(conn, bill_id: int) -> dict[str, Any]:
    conn.execute("DELETE FROM bills WHERE id=?", (bill_id,))
    conn.commit()
    return {"ok": True, "bill_id": bill_id}


def mark_bill_paid(conn, bill_id: int, date: str | None = None) -> dict[str, Any]:
    date = date or _today_la().isoformat()
    conn.execute("UPDATE bills SET last_paid_date=? WHERE id=?", (date, bill_id))
    conn.commit()
    r = conn.execute("SELECT * FROM bills WHERE id=?", (bill_id,)).fetchone()
    if not r:
        raise KeyError(f"bill {bill_id} not found")
    return _bill_dict(conn, r, _today_la())


# ---------------------------------------------------------------- debts

def get_debts(conn) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT * FROM debts ORDER BY COALESCE(payoff_order, 9999), balance_cents DESC"
    ).fetchall()
    debts = []
    total_min = 0
    total_interest_mo = 0
    for r in rows:
        d = _row_to_dict(r)
        d["balance_display"] = _fmt_cents(d["balance_cents"])
        d["minimum_display"] = _fmt_cents(d["minimum_cents"])
        if d["minimum_cents"]:
            total_min += d["minimum_cents"]
        if d["balance_cents"] and d["apr"]:
            total_interest_mo += int(round(d["balance_cents"] * d["apr"] / 12))
        debts.append(d)
    return {
        "debts": debts,
        "total_minimum_cents": total_min,
        "total_minimum_display": _fmt_cents(total_min),
        "est_monthly_interest_cents": total_interest_mo,
        "est_monthly_interest_display": _fmt_cents(total_interest_mo),
    }


def upsert_debt(conn, debt: dict) -> dict[str, Any]:
    fields = ("name", "creditor", "balance_cents", "apr", "minimum_cents",
              "due_day", "payoff_order", "notes")
    debt = _with_defaults(debt, {"creditor": None, "balance_cents": 0, "apr": None,
                                 "minimum_cents": None, "due_day": None,
                                 "payoff_order": None, "notes": None})
    if not debt.get("name"):
        raise ValueError("debt needs a name")
    conn.execute(
        """INSERT INTO debts (name, creditor, balance_cents, apr, minimum_cents,
                              due_day, payoff_order, notes)
           VALUES (:name, :creditor, :balance_cents, :apr, :minimum_cents,
                   :due_day, :payoff_order, :notes)
           ON CONFLICT(name) DO UPDATE SET
             creditor=excluded.creditor, balance_cents=excluded.balance_cents,
             apr=excluded.apr, minimum_cents=excluded.minimum_cents,
             due_day=excluded.due_day, payoff_order=excluded.payoff_order,
             notes=excluded.notes""",
        debt,
    )
    conn.commit()
    return _row_to_dict(conn.execute("SELECT * FROM debts WHERE name=?", (debt["name"],)).fetchone())


def update_debt_balance(conn, debt_id: int, balance_cents: int) -> dict[str, Any]:
    conn.execute("UPDATE debts SET balance_cents=? WHERE id=?", (int(balance_cents), debt_id))
    conn.commit()
    r = conn.execute("SELECT * FROM debts WHERE id=?", (debt_id,)).fetchone()
    if not r:
        raise KeyError(f"debt {debt_id} not found")
    return _row_to_dict(r)


# ---------------------------------------------------------------- sync & seed

def sync_now(conn) -> dict[str, Any]:
    provider = get_provider()
    return provider.sync(conn)


def import_seed(conn, seed: dict) -> dict[str, Any]:
    """One-time bulk seed. Idempotent on natural keys. `seed` shape:
    {accounts:[...], categories:[...], transactions:[...], bills:[...],
     debts:[...], meta:{...}} — keys match table columns."""
    counts: dict[str, int] = {}
    for a in seed.get("accounts", []):
        conn.execute(
            """INSERT INTO accounts (plaid_account_id, name, mask, type, balance_cents,
                                     available_cents, limit_cents, last_synced_at, include_in_budget)
               VALUES (:plaid_account_id, :name, :mask, :type, :balance_cents,
                       :available_cents, :limit_cents, :last_synced_at,
                       COALESCE(:include_in_budget, 1))
               ON CONFLICT(plaid_account_id) DO NOTHING""",
            _with_defaults(a, {
                "plaid_account_id": None, "mask": None, "balance_cents": 0,
                "available_cents": None, "limit_cents": None,
                "last_synced_at": None, "include_in_budget": 1,
            }),
        )
        counts["accounts"] = counts.get("accounts", 0) + 1
    for c in seed.get("categories", []):
        conn.execute(
            """INSERT INTO categories (name, grp, sort_order)
               VALUES (:name, :grp, :sort_order)
               ON CONFLICT(grp, name) DO NOTHING""",
            _with_defaults(c, {"grp": "General", "sort_order": 0}),
        )
        counts["categories"] = counts.get("categories", 0) + 1
    acct_by_plaid = {r["plaid_account_id"]: r["id"] for r in
                     conn.execute("SELECT id, plaid_account_id FROM accounts").fetchall()
                     if r["plaid_account_id"]}
    cat_by_name = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM categories").fetchall()}
    for t in seed.get("transactions", []):
        t = dict(t)
        aid = t.get("account_id")
        if isinstance(aid, str):  # allow plaid_account_id reference in seed
            aid = acct_by_plaid.get(aid)
        cid = t.get("category_id")
        if isinstance(cid, str):  # allow category name reference in seed
            cid = cat_by_name.get(cid)
        conn.execute(
            """INSERT INTO transactions (plaid_txn_id, account_id, date, merchant, name,
                                         amount_cents, category_id, pending, created_at)
               VALUES (:plaid_txn_id, :account_id, :date, :merchant, :name,
                       :amount_cents, :category_id, :pending, :created_at)
               ON CONFLICT(plaid_txn_id) DO NOTHING""",
            {
                "plaid_txn_id": t.get("plaid_txn_id"),
                "account_id": aid,
                "date": t.get("date"),
                "merchant": t.get("merchant"),
                "name": t.get("name"),
                "amount_cents": t.get("amount_cents"),
                "category_id": cid,
                "pending": 1 if t.get("pending") else 0,
                "created_at": t.get("created_at") or utcnow_iso(),
            },
        )
        counts["transactions"] = counts.get("transactions", 0) + 1
    for b in seed.get("bills", []):
        upsert_bill(conn, b)
        counts["bills"] = counts.get("bills", 0) + 1
    for d in seed.get("debts", []):
        upsert_debt(conn, d)
        counts["debts"] = counts.get("debts", 0) + 1
    for k, v in (seed.get("meta") or {}).items():
        _meta_set(conn, k, v)
        counts["meta"] = counts.get("meta", 0) + 1
    conn.commit()
    return {"ok": True, "imported": counts}


def _with_defaults(d: dict, defaults: dict) -> dict:
    out = dict(defaults)
    out.update({k: v for k, v in d.items() if v is not None})
    return out
