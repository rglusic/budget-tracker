"""Bank sync providers.

The sync layer is swappable: pick an implementation with SYNC_PROVIDER.
Only the Plaid provider talks to a bank, and it reads every credential
from environment variables — nothing is hardcoded, logged, or stored.

Required env vars for the Plaid provider:
    PLAID_CLIENT_ID, PLAID_SECRET, PLAID_ACCESS_TOKEN
Optional:
    PLAID_ENV            one of sandbox|development|production (default sandbox)
    PLAID_COUNTRY_CODES   default "US"
"""
from __future__ import annotations

import datetime as _dt
import os
from typing import Any

import requests

from . import db as _dbmod

_PLAID_HOSTS = {
    "sandbox": "https://sandbox.plaid.com",
    "development": "https://development.plaid.com",
    "production": "https://production.plaid.com",
}

DISPLAY_TZ = "America/Los_Angeles"


def utcnow_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class SyncProvider:
    """Interface every sync provider implements."""

    name = "base"

    def sync(self, conn) -> dict[str, Any]:
        """Refresh accounts/transactions/liabilities. Returns a summary dict."""
        raise NotImplementedError


class NullSyncProvider(SyncProvider):
    """Default provider: does nothing. Used when no bank is configured."""

    name = "null"

    def sync(self, conn) -> dict[str, Any]:
        return {
            "provider": self.name,
            "synced_at": utcnow_iso(),
            "accounts_upserted": 0,
            "transactions_added": 0,
            "note": "No sync provider configured (set SYNC_PROVIDER=plaid).",
        }


class PlaidSyncProvider(SyncProvider):
    """Plaid sync via raw HTTPS calls (no SDK needed)."""

    name = "plaid"

    def __init__(self) -> None:
        self.client_id = os.environ.get("PLAID_CLIENT_ID", "")
        self.secret = os.environ.get("PLAID_SECRET", "")
        self.access_token = os.environ.get("PLAID_ACCESS_TOKEN", "")
        env = os.environ.get("PLAID_ENV", "sandbox").lower()
        if env not in _PLAID_HOSTS:
            raise ValueError(f"PLAID_ENV must be one of {sorted(_PLAID_HOSTS)}")
        self.host = _PLAID_HOSTS[env]
        missing = [k for k, v in {
            "PLAID_CLIENT_ID": self.client_id,
            "PLAID_SECRET": self.secret,
            "PLAID_ACCESS_TOKEN": self.access_token,
        }.items() if not v]
        if missing:
            raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")

    # -- low-level ---------------------------------------------------------
    def _post(self, path: str, payload: dict) -> dict:
        body = {
            "client_id": self.client_id,
            "secret": self.secret,
            **payload,
        }
        resp = requests.post(self.host + path, json=body, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("error_code"):
            raise RuntimeError(f"Plaid error {data.get('error_code')}: {data.get('error_message')}")
        return data

    # -- sync --------------------------------------------------------------
    def sync(self, conn) -> dict[str, Any]:
        now = utcnow_iso()
        accounts_upserted = self._sync_accounts(conn, now)
        transactions_added = self._sync_transactions(conn, now)
        self._sync_liabilities(conn)
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('last_sync_at', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (now,),
        )
        conn.commit()
        return {
            "provider": self.name,
            "synced_at": now,
            "accounts_upserted": accounts_upserted,
            "transactions_added": transactions_added,
        }

    def _sync_accounts(self, conn, now: str) -> int:
        data = self._post("/accounts/balance/get", {
            "access_token": self.access_token,
            "options": {"min_last_updated_datetime": None},
        })
        n = 0
        for a in data.get("accounts", []):
            bal = a.get("balances") or {}
            cur = bal.get("current")
            avail = bal.get("available")
            limit = bal.get("limit")
            ptype = (a.get("type") or "").lower()
            subtype = (a.get("subtype") or "").lower()
            if ptype == "depository":
                atype = "savings" if subtype == "savings" else "checking"
            elif ptype == "credit":
                atype = "credit"
            elif ptype == "loan":
                atype = "loan"
            else:
                atype = "other"
            include = 1 if atype in ("checking", "savings", "credit") else 0
            conn.execute(
                """INSERT INTO accounts
                   (plaid_account_id, name, mask, type, balance_cents,
                    available_cents, limit_cents, last_synced_at, include_in_budget)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(plaid_account_id) DO UPDATE SET
                     name=excluded.name, mask=excluded.mask, type=excluded.type,
                     balance_cents=excluded.balance_cents,
                     available_cents=excluded.available_cents,
                     limit_cents=excluded.limit_cents,
                     last_synced_at=excluded.last_synced_at""",
                (
                    a.get("account_id"),
                    a.get("name") or "Account",
                    a.get("mask"),
                    atype,
                    _to_cents(cur),
                    _to_cents(avail),
                    _to_cents(limit),
                    now,
                    include,
                ),
            )
            n += 1
        conn.commit()
        return n

    def _sync_transactions(self, conn, now: str) -> int:
        today = _dt.date.today()
        start = (today - _dt.timedelta(days=30)).isoformat()
        data = self._post("/transactions/get", {
            "access_token": self.access_token,
            "start_date": start,
            "end_date": today.isoformat(),
            "options": {"include_personal_finance_category": False, "pending": True},
        })
        acct_map = {
            r["plaid_account_id"]: r["id"]
            for r in conn.execute("SELECT id, plaid_account_id FROM accounts WHERE plaid_account_id IS NOT NULL")
        }
        added = 0
        for t in data.get("transactions", []):
            tid = t.get("transaction_id")
            local_acct = acct_map.get(t.get("account_id"))
            if not local_acct:
                continue  # account not synced yet; skip rather than invent one
            amount_cents = _to_cents(t.get("amount"))
            # Plaid: positive amount = money OUT. We store negative = outflow.
            if amount_cents is not None:
                amount_cents = -amount_cents
            pending = 1 if t.get("pending") else 0
            merchant = t.get("merchant_name") or (t.get("counterparties") or [{}])[0].get("name")

            # Pending -> posted transition: Plaid issues a new id, linked via
            # pending_transaction_id. Update the existing row in place.
            if not pending and t.get("pending_transaction_id"):
                row = conn.execute(
                    "SELECT id FROM transactions WHERE plaid_txn_id = ?",
                    (t["pending_transaction_id"],),
                ).fetchone()
                if row:
                    conn.execute(
                        """UPDATE transactions SET plaid_txn_id=?, amount_cents=?,
                           pending=0, date=?, merchant=COALESCE(?, merchant), name=?
                           WHERE id=?""",
                        (tid, amount_cents, t.get("date"), merchant, t.get("name"), row["id"]),
                    )
                    continue

            cur = conn.execute(
                "SELECT id, pending FROM transactions WHERE plaid_txn_id = ?", (tid,)
            ).fetchone()
            if cur:
                # Update a still-pending row in place (amount/date can shift).
                if cur["pending"]:
                    conn.execute(
                        "UPDATE transactions SET amount_cents=?, date=?, pending=? WHERE id=?",
                        (amount_cents, t.get("date"), pending, cur["id"]),
                    )
                continue

            conn.execute(
                """INSERT INTO transactions
                   (plaid_txn_id, account_id, date, merchant, name,
                    amount_cents, category_id, pending, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)""",
                (tid, local_acct, t.get("date"), merchant, t.get("name"),
                 amount_cents, pending, now),
            )
            added += 1
        conn.commit()
        return added

    def _sync_liabilities(self, conn) -> None:
        try:
            data = self._post("/liabilities/get", {"access_token": self.access_token})
        except Exception:
            return  # liabilities are best-effort; balances already refreshed
        for item in (data.get("liabilities") or {}).get("credit", []) or []:
            for acct in item.get("accounts") or []:
                aprs = item.get("aprs") or []
                _ = aprs  # reserved for future debt-APR refresh; not auto-applied
                conn.execute(
                    "UPDATE accounts SET last_synced_at=? WHERE plaid_account_id=?",
                    (utcnow_iso(), acct.get("account_id")),
                )
        conn.commit()


def _to_cents(value) -> int | None:
    if value is None:
        return None
    return int(round(float(value) * 100))


def get_provider() -> SyncProvider:
    which = os.environ.get("SYNC_PROVIDER", "null").lower()
    if which == "plaid":
        return PlaidSyncProvider()
    if which == "null":
        return NullSyncProvider()
    raise ValueError(f"Unknown SYNC_PROVIDER={which!r} (expected 'plaid' or 'null')")
