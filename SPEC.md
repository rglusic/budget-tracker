# Budget Tracker — v1 Spec

A private, personal budgeting web app replacing YNAB. Envelope-style
budgeting from actual cash balances. Mobile-first.

## Principles (learned the hard way)

- Every balance shows its sync time. Never display a number without saying how fresh it is.
- Money math in integer cents everywhere.
- No invented data. The app seeds only through import actions with real data.
- Transfers are first-class: a card payment is ONE transfer (checking → card),
  never two unlinked one-sided transactions.
- Unlinked or stale accounts are labeled as such, never silently counted as cash.

## Data model (SQLite)

- `accounts(id, plaid_account_id UNIQUE, name, mask, type, balance_cents,
  available_cents, limit_cents, last_synced_at, include_in_budget)`
  - type in {checking, savings, credit, loan, other}
- `transactions(id, plaid_txn_id UNIQUE, account_id, date, merchant, name,
  amount_cents, category_id, pending, created_at)`
  - amount_cents: negative = outflow, positive = inflow (matches Plaid/YANB sign)
- `categories(id, name, grp, sort_order)`
- `payee_rules(id, merchant_pattern UNIQUE, category_id, use_count, updated_at)`
- `budget_months(month TEXT 'YYYY-MM', category_id, assigned_cents,
  UNIQUE(month, category_id))`
  - available = assigned - activity + rollover_from_prior_month
- `bills(id, name, amount_cents, due_day, frequency, autopay INTEGER,
  autopay_from, last_paid_date, notes, active INTEGER)`
- `debts(id, name, creditor, balance_cents, apr, minimum_cents, due_day,
  payoff_order INTEGER, notes)`
- `meta(key UNIQUE, value)` — e.g. next_paycheck_date, next_paycheck_amount_cents

## Tabs

1. **Overview** — total cash (sum of depository), total debt, net; per-account
   cards with balance + "synced Xm ago"; unassigned cash number;
   "due before next paycheck" bill strip.
2. **Inbox** — pending + uncategorized transactions, newest first. Tap →
   category chips (most-used first) + full list search. Categorizing creates or
   bumps a payee_rule. Show "suggested" badge when a rule matches.
3. **Budget** — groups → categories, each row: Assigned / Activity / Available.
   Tap a category to assign +/- dollars. Month switcher (current + prior).
   Available rolls over month to month.
4. **Bills** — sorted by next due date. Each: amount, due day/date, autopay
   badge + source, last paid. Toggle autopay, mark paid. Top section:
   "Due before next paycheck".
5. **Debts** — balance, APR, minimum, payoff order with progress bars;
   totals: monthly minimums, est. monthly interest.

## Actions (published)

- `sync_now` → Plaid accounts + transactions + liabilities refresh; returns
  {synced_at, accounts_upserted, transactions_added}
- `import_seed` {accounts, categories, transactions, bills, debts, meta} →
  one-time bulk seed; idempotent on natural keys
- `categorize_transaction` {txn_id, category_id}
- `assign_budget` {month, category_id, amount_cents} (delta, can be negative)
- `upsert_bill`, `delete_bill`, `mark_bill_paid` {bill_id, date}
- `upsert_debt`, `update_debt_balance` {debt_id, balance_cents}
- `get_overview`, `get_inbox`, `get_budget` {month}, `get_bills`, `get_debts`

## Sync behavior

- Daily morning refresh trigger: accounts, rolling 30d transactions, liabilities.
- Dedup on plaid_txn_id. Pending transactions update to posted in place.
- New uncategorized transactions land in Inbox with payee-rule suggestions.

## Seed data (provided post-build via import_seed — never invent)

- ~40 categories (Bills / Frequent / Quality of Life / Goals groups)
- All transactions since 2026-09-01 (~150) with categories
- ~25 bills with due dates, amounts, autopay status
- ~15 debts with balances, APRs, minimums, payoff order
- meta: next_paycheck_date, next_paycheck_amount_cents, pay_frequency
  (all provided at seed time; example values redacted)

## Non-goals for v1

Charts beyond progress bars, investments, receipt OCR, multi-user,
initiating payments (read-only; payments happen in bank apps).

## Engineering rules

- No secrets in code or repo. Plaid access only through the declared
  connector capability; credentials never in source, logs, or DB.
- No personal data hardcoded. Everything personal arrives via actions/DB.
- Plain, short UI copy. No AI-sounding fluff.
- All timestamps stored UTC, displayed in America/Los_Angeles.
