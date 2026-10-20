"""Business logic: the app's published actions.

Every function takes an open sqlite3 connection (row_factory = Row) and
returns plain JSON-serializable dicts/lists. Money is integer cents.
amount_cents sign convention: negative = outflow, positive = inflow.
"""
from __future__ import annotations

import calendar
import datetime as _dt
import json
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


# ------------------------------------------------------- paycheck schedule
# The paycheck schedule is DATA, never a baked-in pattern. Meta stores:
#   next_paycheck_date        anchor: the next actual payday (YYYY-MM-DD)
#   next_paycheck_amount_cents  typical paycheck amount
#   paycheck_cadence          label: weekly | biweekly | semi-monthly | monthly
#   paycheck_pattern          JSON: {"type": "month_days", "days": [d, ...]}
#                               or {"type": "every_days", "days": N}
# Future paydays are projected from the stored pattern + anchor.

def _month_last_day(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def _pattern_from_cadence(cadence: str | None, anchor: _dt.date) -> dict | None:
    """Derive a pattern from a cadence label + anchor when no explicit
    pattern is stored. Returns None when nothing usable is known."""
    if cadence == "weekly":
        return {"type": "every_days", "days": 7}
    if cadence == "biweekly":
        return {"type": "every_days", "days": 14}
    if cadence == "monthly":
        return {"type": "month_days", "days": [anchor.day]}
    if cadence == "semi-monthly":
        # Two paydays per month: the anchor's day is one of them; the other
        # sits in the opposite half of the month.
        first = anchor.day if anchor.day <= 15 else 15
        second = anchor.day if anchor.day > 15 else 31  # 31 clamps to month end
        return {"type": "month_days", "days": sorted({first, second})}
    return None


def paycheck_meta(conn) -> dict[str, Any]:
    raw_amount = _meta_get(conn, "next_paycheck_amount_cents")
    try:
        amount = int(raw_amount) if raw_amount is not None else None
    except ValueError:
        amount = None
    pattern = None
    raw_pattern = _meta_get(conn, "paycheck_pattern")
    if raw_pattern:
        try:
            pattern = json.loads(raw_pattern)
        except ValueError:
            pattern = None
    return {
        "next_date": _meta_get(conn, "next_paycheck_date"),
        "amount_cents": amount,
        "cadence": _meta_get(conn, "paycheck_cadence"),
        "pattern": pattern,
    }


def paycheck_dates(conn, count: int = 8, from_date: _dt.date | None = None) -> list[_dt.date]:
    """Project the next `count` payday dates from the stored pattern."""
    meta = paycheck_meta(conn)
    if not meta["next_date"]:
        return []
    try:
        anchor = _dt.date.fromisoformat(meta["next_date"])
    except ValueError:
        return []
    pattern = meta["pattern"] or _pattern_from_cadence(meta["cadence"], anchor)
    start = from_date or _today_la()
    if pattern is None:
        return [anchor] if anchor >= start else []
    if pattern.get("type") == "every_days":
        step = _dt.timedelta(days=max(1, int(pattern.get("days") or 14)))
        d = anchor
        while d < start:
            d += step
        out = []
        while len(out) < count:
            out.append(d)
            d += step
        return out
    # month_days pattern
    days = sorted({int(d) for d in pattern.get("days") or [anchor.day]})
    out: list[_dt.date] = []
    y, m = anchor.year, anchor.month
    if (start.year, start.month) > (y, m):
        y, m = start.year, start.month
    while len(out) < count:
        last = _month_last_day(y, m)
        for pd in days:
            cand = _dt.date(y, m, min(pd, last))
            if (y, m) == (anchor.year, anchor.month) and anchor >= start:
                # The anchor (often weekend-shifted) takes the pattern slot
                # in its half of the month — never emit both.
                if (cand.day <= 15) == (anchor.day <= 15):
                    cand = anchor
            if cand >= start and cand not in out:
                out.append(cand)
                if len(out) >= count:
                    break
        m += 1
        if m > 12:
            m = 1
            y += 1
    out.sort()
    return out[:count]


def set_paycheck(conn, date: str, amount_cents: int | None = None,
                 cadence: str | None = None, month_days: list | None = None,
                 interval_days: int | None = None) -> dict[str, Any]:
    """Store the paycheck schedule (anchor + cadence + explicit pattern)."""
    anchor = _dt.date.fromisoformat(date)  # raises ValueError on bad input
    _meta_set(conn, "next_paycheck_date", anchor.isoformat())
    if amount_cents is not None:
        _meta_set(conn, "next_paycheck_amount_cents", int(amount_cents))
    if cadence:
        _meta_set(conn, "paycheck_cadence", cadence)
    pattern = None
    if month_days:
        pattern = {"type": "month_days",
                   "days": sorted({int(d) for d in month_days if 1 <= int(d) <= 31})}
    elif interval_days:
        pattern = {"type": "every_days", "days": int(interval_days)}
    elif cadence:
        pattern = _pattern_from_cadence(cadence, anchor)
    if pattern:
        _meta_set(conn, "paycheck_pattern", json.dumps(pattern))
    conn.commit()
    return {"ok": True, "paycheck": paycheck_meta(conn),
            "paycheck_dates": [d.isoformat() for d in paycheck_dates(conn)]}


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
        "next_paycheck_date": _meta_get(conn, "next_paycheck_date"),
        "summary": month_summary(conn, month),
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

def _envelope_names(conn) -> list[str] | None:
    """Categories that stay manual green/red envelopes. Data-driven: stored
    in meta as a JSON list of category names. None = every category is an
    envelope (legacy behavior)."""
    raw = _meta_get(conn, "envelope_categories")
    if not raw:
        return None
    try:
        names = json.loads(raw)
        return [str(n) for n in names] if isinstance(names, list) else None
    except ValueError:
        return None


def get_budget(conn, month: str | None = None) -> dict[str, Any]:
    month = month or _today_la().strftime("%Y-%m")
    memo: dict = {}
    groups: dict[str, list] = {}
    envelopes: list[dict] = []
    env_names = _envelope_names(conn)
    for c in conn.execute("SELECT * FROM categories ORDER BY grp, sort_order, name").fetchall():
        assigned = _assigned_cents(conn, c["id"], month)
        spent = _spent_cents(conn, c["id"], month)
        available = category_available(conn, c["id"], month, memo)
        row = {
            "id": c["id"], "name": c["name"], "grp": c["grp"],
            "assigned_cents": assigned, "assigned_display": _fmt_cents(assigned),
            "spent_cents": spent, "spent_display": _fmt_cents(spent),
            "available_cents": available, "available_display": _fmt_cents(available),
        }
        groups.setdefault(c["grp"], []).append(row)
        if env_names is None or c["name"] in env_names:
            envelopes.append(row)
    un = unassigned_cents(conn, month)
    return {
        "month": month,
        "prev_month": _month_add(month, -1),
        "next_month": _month_add(month, 1),
        "groups": [{"name": g, "categories": cats} for g, cats in groups.items()],
        "envelopes": envelopes,
        "summary": month_summary(conn, month),
        "bills": bill_statuses(conn, month),
        "unassigned_cents": un,
        "unassigned_display": _fmt_cents(un),
    }


# ---------------------------------------------------------------- plan view

def _advance_occurrence(d: _dt.date, frequency: str, due_day: int) -> _dt.date:
    freq = (frequency or "monthly").lower()
    if freq == "weekly":
        return d + _dt.timedelta(days=7)
    if freq == "biweekly":
        return d + _dt.timedelta(days=14)
    if freq == "yearly":
        try:
            return d.replace(year=d.year + 1)
        except ValueError:  # Feb 29
            return d.replace(year=d.year + 1, day=28)
    if freq == "once":
        return d + _dt.timedelta(days=36500)
    # monthly: same due_day next month, clamped
    y, m = d.year, d.month + 1
    if m > 12:
        m = 1
        y += 1
    return _dt.date(y, m, min(due_day, _month_last_day(y, m)))


def forecast(conn, days: int = 62) -> dict[str, Any]:
    """Day-by-day projected cash balance: paydays add the stored paycheck
    amount; cash bills subtract on their due dates. Card bills never touch
    cash, so they are excluded."""
    today = _today_la()
    end = today + _dt.timedelta(days=days)
    balance = _cash_cents(conn)
    start = balance
    pay_amount = paycheck_meta(conn)["amount_cents"] or 0
    paydays = {d: pay_amount for d in paycheck_dates(conn, count=16) if d <= end}
    due: dict[_dt.date, int] = {}
    month = today.strftime("%Y-%m")
    for r in conn.execute("SELECT * FROM bills WHERE active=1").fetchall():
        if _is_card_label(r["autopay_from"]):
            continue
        d = _bill_next_due(r, today)
        if _paid_match(conn, r, month) and d.strftime("%Y-%m") == month:
            # Already paid this month: the next real outflow is the following
            # occurrence, not the computed due date that just passed.
            d = _advance_occurrence(d, r["frequency"], r["due_day"])
        while d <= end:
            if d >= today:
                due[d] = due.get(d, 0) + int(r["amount_cents"])
            d = _advance_occurrence(d, r["frequency"], r["due_day"])
    points = []
    lowest = None
    for i in range(days + 1):
        d = today + _dt.timedelta(days=i)
        balance += paydays.get(d, 0)
        balance -= due.get(d, 0)
        pt = {"date": d.isoformat(), "balance_cents": balance,
              "balance_display": _fmt_cents(balance)}
        points.append(pt)
        if lowest is None or balance < lowest["balance_cents"]:
            lowest = pt
    return {"start_cents": start, "days": points, "lowest": lowest}


def get_bill_plan(conn) -> dict[str, Any]:
    today = _today_la()
    month = today.strftime("%Y-%m")
    meta = paycheck_meta(conn)
    cats = [{"id": r["id"], "name": r["name"], "grp": r["grp"]}
            for r in conn.execute("SELECT * FROM categories ORDER BY grp, sort_order, name")]
    return {
        "today": today.isoformat(),
        "month": month,
        "summary": month_summary(conn, month),
        "paycheck": {
            "next_date": meta["next_date"],
            "amount_cents": meta["amount_cents"],
            "amount_display": _fmt_cents(meta["amount_cents"]),
            "cadence": meta["cadence"],
            "pattern": meta["pattern"],
        },
        "paycheck_dates": [d.isoformat() for d in paycheck_dates(conn, count=6)],
        "bills": bill_statuses(conn, month),
        "forecast": forecast(conn),
        "categories": cats,
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


def _bill_next_due(r, today: _dt.date) -> _dt.date:
    """Effective next due date: an explicit stored date wins (it is how an
    overdue bill stays overdue); otherwise project from due_day/frequency."""
    if r["next_due_date"]:
        return _dt.date.fromisoformat(r["next_due_date"])
    return _dt.date.fromisoformat(_next_due_date(r["due_day"], r["frequency"], today))


def _bill_dict(conn, r, today: _dt.date) -> dict:
    d = _row_to_dict(r)
    d["amount_display"] = _fmt_cents(d["amount_cents"])
    d["next_due_date"] = _bill_next_due(r, today).isoformat()
    d["autopay"] = bool(d["autopay"])
    d["active"] = bool(d["active"])
    return d


# A bill "pulls from a card" when its account label names a card brand.
# Card bills never touch checking, so they never reduce safe-to-spend.
_CARD_LABEL_HINTS = ("visa", "mastercard", "master card", "amex",
                     "american express", "discover")


def _is_card_label(label: str | None) -> bool:
    if not label:
        return False
    low = label.lower()
    return any(h in low for h in _CARD_LABEL_HINTS)


def _paid_match(conn, bill, month: str) -> str | None:
    """Return the paid date if this bill was paid in `month`, else None.

    A bill counts as paid when an outflow transaction in the bill's linked
    category matches the bill amount exactly that month, or when it was
    manually marked paid (last_paid_date) in the month.
    """
    if bill["last_paid_date"] and bill["last_paid_date"][:7] == month:
        return bill["last_paid_date"]
    if bill["category_id"]:
        r = conn.execute(
            """SELECT date FROM transactions
               WHERE category_id = ? AND substr(date, 1, 7) = ? AND amount_cents = ?
               ORDER BY date LIMIT 1""",
            (bill["category_id"], month, -int(bill["amount_cents"])),
        ).fetchone()
        if r:
            return r["date"]
    return None


def _covering_paycheck(conn, cash: int, amount: int) -> str | None:
    """First projected payday at which cash + paychecks received cover `amount`."""
    dates = paycheck_dates(conn, count=12)
    if not dates:
        return None
    per = paycheck_meta(conn)["amount_cents"] or 0
    cumulative = cash
    for d in dates:
        cumulative += per
        if cumulative >= amount:
            return d.isoformat()
    return dates[-1].isoformat()


def bill_statuses(conn, month: str | None = None) -> list[dict]:
    """Each active bill with a computed status for `month`:

    paid    — money already left (shows paid_date; renders green)
    covered — cash on hand covers it (checked cumulatively in due-date order)
    short   — cash is short; covering_paycheck_date names the payday that fixes it
    card    — pulls from a card, not from cash
    """
    today = _today_la()
    month = month or today.strftime("%Y-%m")
    cash = _cash_cents(conn)
    rows = conn.execute("SELECT * FROM bills WHERE active=1").fetchall()
    bills = []
    for r in rows:
        d = _bill_dict(conn, r, today)
        d["is_card"] = _is_card_label(r["autopay_from"])
        d["paid_date"] = _paid_match(conn, r, month)
        d["status"] = "paid" if d["paid_date"] else ("card" if d["is_card"] else None)
        d["covering_paycheck_date"] = None
        bills.append(d)
    remaining = cash
    pending = sorted((b for b in bills if b["status"] is None),
                     key=lambda b: b["next_due_date"])
    for b in pending:
        if remaining >= b["amount_cents"]:
            b["status"] = "covered"
            remaining -= b["amount_cents"]
        else:
            b["status"] = "short"
            b["covering_paycheck_date"] = _covering_paycheck(conn, cash, b["amount_cents"])
    bills.sort(key=lambda b: (b["status"] != "paid", b["next_due_date"]))
    return bills


def safe_to_spend_cents(conn) -> int | None:
    """Cash on hand minus unpaid cash bills due on/before the next paycheck.

    Card bills are excluded (they never touch checking). Returns None when
    no paycheck schedule is stored."""
    dates = paycheck_dates(conn, count=1)
    if not dates:
        return None
    npc = dates[0]
    today = _today_la()
    month = today.strftime("%Y-%m")
    due = 0
    for r in conn.execute("SELECT * FROM bills WHERE active=1").fetchall():
        if _is_card_label(r["autopay_from"]):
            continue
        if _paid_match(conn, r, month):
            continue
        if _bill_next_due(r, today) <= npc:
            due += int(r["amount_cents"])
    return _cash_cents(conn) - due


def _paid_this_month_cents(conn, month: str) -> int:
    """Gross outflows in the month, transfers excluded."""
    r = conn.execute(
        """SELECT COALESCE(SUM(-amount_cents), 0) AS s FROM transactions
           WHERE substr(date, 1, 7) = ? AND amount_cents < 0 AND is_transfer = 0""",
        (month,),
    ).fetchone()
    return int(r["s"])


def _still_due_cents(conn, month: str) -> int:
    """Active bills whose next due date falls in `month`, not yet paid."""
    today = _today_la()
    total = 0
    for r in conn.execute("SELECT * FROM bills WHERE active=1").fetchall():
        if _bill_next_due(r, today).strftime("%Y-%m") != month:
            continue
        if _paid_match(conn, r, month):
            continue
        total += int(r["amount_cents"])
    return total


def month_summary(conn, month: str) -> dict[str, Any]:
    """The 'This month' strip: cash on hand / paid / still due / safe to spend."""
    cash = _cash_cents(conn)
    paid = _paid_this_month_cents(conn, month)
    due = _still_due_cents(conn, month)
    safe = safe_to_spend_cents(conn)
    return {
        "cash_on_hand_cents": cash, "cash_on_hand_display": _fmt_cents(cash),
        "paid_this_month_cents": paid, "paid_this_month_display": _fmt_cents(paid),
        "still_due_this_month_cents": due, "still_due_this_month_display": _fmt_cents(due),
        "safe_to_spend_cents": safe, "safe_to_spend_display": _fmt_cents(safe),
    }


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
    """Cash bills due on/before the next paycheck (the cash-only model the
    Budget tab uses: card bills and already-paid bills are excluded)."""
    today = _today_la()
    dates = paycheck_dates(conn, count=1)
    if not dates:
        return []
    npc = dates[0].isoformat()
    month = today.strftime("%Y-%m")
    out = []
    for r in conn.execute("SELECT * FROM bills WHERE active=1").fetchall():
        if _is_card_label(r["autopay_from"]):
            continue
        if _paid_match(conn, r, month):
            continue
        b = _bill_dict(conn, r, today)
        if b["next_due_date"] <= npc:
            out.append(b)
    out.sort(key=lambda b: b["next_due_date"])
    return out


def upsert_bill(conn, bill: dict) -> dict[str, Any]:
    bill = dict(bill)
    # Allow linking the bill's category by name (seed data uses names).
    if bill.get("category_id") is None and bill.get("category"):
        r = conn.execute("SELECT id FROM categories WHERE name=?",
                         (bill["category"],)).fetchone()
        bill["category_id"] = r["id"] if r else None
    bill = _with_defaults(bill, {"frequency": "monthly", "autopay": 0,
                                 "autopay_from": None, "category_id": None,
                                 "next_due_date": None, "last_paid_date": None,
                                 "notes": None, "active": 1})
    if not bill.get("name") or bill.get("amount_cents") is None or not bill.get("due_day"):
        raise ValueError("bill needs name, amount_cents, due_day")
    conn.execute(
        """INSERT INTO bills (name, amount_cents, due_day, frequency, autopay,
                              autopay_from, category_id, next_due_date,
                              last_paid_date, notes, active)
           VALUES (:name, :amount_cents, :due_day, :frequency, :autopay,
                   :autopay_from, :category_id, :next_due_date,
                   :last_paid_date, :notes, :active)
           ON CONFLICT(name) DO UPDATE SET
             amount_cents=excluded.amount_cents, due_day=excluded.due_day,
             frequency=excluded.frequency, autopay=excluded.autopay,
             autopay_from=excluded.autopay_from, category_id=excluded.category_id,
             next_due_date=excluded.next_due_date,
             last_paid_date=excluded.last_paid_date,
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
    r = conn.execute("SELECT * FROM bills WHERE id=?", (bill_id,)).fetchone()
    if not r:
        raise KeyError(f"bill {bill_id} not found")
    # Advance an explicit next due date past the payment.
    next_due = _next_due_date(r["due_day"], r["frequency"],
                              _dt.date.fromisoformat(date) + _dt.timedelta(days=1))
    conn.execute("UPDATE bills SET last_paid_date=?, next_due_date=? WHERE id=?",
                 (date, next_due, bill_id))
    conn.commit()
    r = conn.execute("SELECT * FROM bills WHERE id=?", (bill_id,)).fetchone()
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
    merged_ids: set[int] = set()
    for t in seed.get("transactions", []):
        t = dict(t)
        aid = t.get("account_id")
        if isinstance(aid, str):  # allow plaid_account_id reference in seed
            aid = acct_by_plaid.get(aid)
        cid = t.get("category_id")
        if isinstance(cid, str):  # allow category name reference in seed
            cid = cat_by_name.get(cid)
        new_id = t.get("plaid_txn_id")
        exact = None
        if new_id:
            exact = conn.execute("SELECT id FROM transactions WHERE plaid_txn_id=?",
                                 (new_id,)).fetchone()
        if not exact and new_id and aid is not None and t.get("date"):
            # Merge, don't duplicate: an existing imported row on the same
            # account with the same amount and a date within 2 days is the
            # same transaction under a temporary id. Adopt the real id and
            # the fresh pending flag; keep the row's category.
            cand = conn.execute(
                """SELECT id FROM transactions
                   WHERE account_id = ? AND amount_cents = ?
                     AND plaid_txn_id IS NOT NULL AND plaid_txn_id != ?
                     AND abs(julianday(date) - julianday(?)) <= 2
                   ORDER BY abs(julianday(date) - julianday(?)) LIMIT 1""",
                (aid, t.get("amount_cents"), new_id, t["date"], t["date"]),
            ).fetchone()
            if cand and cand["id"] not in merged_ids:
                merged_ids.add(cand["id"])
                conn.execute(
                    "UPDATE transactions SET plaid_txn_id=?, pending=?, date=? WHERE id=?",
                    (new_id, 1 if t.get("pending") else 0, t["date"], cand["id"]),
                )
                counts["transactions_merged"] = counts.get("transactions_merged", 0) + 1
                continue
        conn.execute(
            """INSERT INTO transactions (plaid_txn_id, account_id, date, merchant, name,
                                         amount_cents, category_id, pending, is_transfer,
                                         created_at)
               VALUES (:plaid_txn_id, :account_id, :date, :merchant, :name,
                       :amount_cents, :category_id, :pending, :is_transfer,
                       :created_at)
               ON CONFLICT(plaid_txn_id) DO NOTHING""",
            {
                "plaid_txn_id": new_id,
                "account_id": aid,
                "date": t.get("date"),
                "merchant": t.get("merchant"),
                "name": t.get("name"),
                "amount_cents": t.get("amount_cents"),
                "category_id": cid,
                "pending": 1 if t.get("pending") else 0,
                "is_transfer": 1 if t.get("is_transfer") else 0,
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
