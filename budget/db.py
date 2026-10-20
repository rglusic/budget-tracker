"""SQLite schema and connection helpers.

Money is stored as INTEGER cents everywhere. Timestamps are UTC ISO-8601.
No personal data lives here — everything personal arrives via actions/DB.
"""
from __future__ import annotations

import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id               INTEGER PRIMARY KEY,
    plaid_account_id TEXT UNIQUE,
    name             TEXT NOT NULL,
    mask             TEXT,
    type             TEXT NOT NULL CHECK (type IN ('checking','savings','credit','loan','other')),
    balance_cents    INTEGER NOT NULL DEFAULT 0,
    available_cents  INTEGER,
    limit_cents      INTEGER,
    last_synced_at   TEXT,
    include_in_budget INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS transactions (
    id           INTEGER PRIMARY KEY,
    plaid_txn_id TEXT UNIQUE,
    account_id   INTEGER NOT NULL REFERENCES accounts(id),
    date         TEXT NOT NULL,              -- YYYY-MM-DD
    merchant     TEXT,
    name         TEXT,
    amount_cents INTEGER NOT NULL,           -- negative = outflow, positive = inflow
    category_id  INTEGER REFERENCES categories(id),
    pending      INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_txn_account ON transactions(account_id);
CREATE INDEX IF NOT EXISTS idx_txn_date ON transactions(date);
CREATE INDEX IF NOT EXISTS idx_txn_category ON transactions(category_id);

CREATE TABLE IF NOT EXISTS categories (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    grp        TEXT NOT NULL DEFAULT 'General',
    sort_order INTEGER NOT NULL DEFAULT 0,
    UNIQUE (grp, name)
);

CREATE TABLE IF NOT EXISTS payee_rules (
    id               INTEGER PRIMARY KEY,
    merchant_pattern TEXT NOT NULL UNIQUE,
    category_id      INTEGER NOT NULL REFERENCES categories(id),
    use_count        INTEGER NOT NULL DEFAULT 1,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS budget_months (
    month         TEXT NOT NULL,              -- YYYY-MM
    category_id   INTEGER NOT NULL REFERENCES categories(id),
    assigned_cents INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (month, category_id)
);

CREATE TABLE IF NOT EXISTS bills (
    id             INTEGER PRIMARY KEY,
    name           TEXT NOT NULL UNIQUE,
    amount_cents   INTEGER NOT NULL,
    due_day        INTEGER NOT NULL,          -- 1..31
    frequency      TEXT NOT NULL DEFAULT 'monthly',
    autopay        INTEGER NOT NULL DEFAULT 0,
    autopay_from   TEXT,
    last_paid_date TEXT,                      -- YYYY-MM-DD
    notes          TEXT,
    active         INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS debts (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    creditor      TEXT,
    balance_cents INTEGER NOT NULL DEFAULT 0,
    apr           REAL,                       -- e.g. 0.2849
    minimum_cents INTEGER,
    due_day       INTEGER,
    payoff_order  INTEGER,
    notes         TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def default_db_path() -> str:
    return os.environ.get("BUDGET_DB", os.path.join(os.path.dirname(__file__), "..", "budget.db"))


def get_db(path: str | None = None) -> sqlite3.Connection:
    """Open (and migrate) the SQLite database."""
    conn = sqlite3.connect(path or default_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn
