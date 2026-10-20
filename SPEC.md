# Budget Tracker — v2 Spec

A private, personal budgeting web app replacing YNAB. Bills-first
budgeting from actual cash balances: fixed obligations get computed
statuses (paid / covered / deferred) from the bills calendar, and only the
categories where spending decisions actually happen stay manual
green/red envelopes. Mobile-first.

## Principles (learned the hard way)

- Every balance shows its sync time. Never display a number without saying how fresh it is.
- Money math in integer cents everywhere.
- No invented data. The app seeds only through import actions with real data.
- Transfers are first-class: a card payment is ONE transfer (checking → card),
  never two unlinked one-sided transactions.
- Unlinked or stale accounts are labeled as such, never silently counted as cash.
- The schedule of paydays is data, not code: paycheck patterns live in meta
  and all projections compute from them — nothing is hardcoded.

## Data model (SQLite)

- `accounts(id, plaid_account_id UNIQUE, name, mask, type, balance_cents,
  available_cents, limit_cents, last_synced_at, include_in_budget)`
  - type in {checking, savings, depository, credit, loan, other}
  - Funding is computed from the type, never from names: checking /
    savings / depository accounts are **cash**; credit accounts are
    **card**. Every outflow is therefore either spent cash or new debt,
    and the UI says which — everywhere money goes out.
- `transactions(id, plaid_txn_id UNIQUE, account_id, date, merchant, name,
  amount_cents, category_id, pending, is_transfer, created_at)`
  - amount_cents: negative = outflow, positive = inflow (matches Plaid/YANB sign)
  - is_transfer = 1 marks internal transfers, excluded from "paid" totals
- `categories(id, name, grp, sort_order)`
- `payee_rules(id, merchant_pattern UNIQUE, category_id, use_count, updated_at)`
- `budget_months(month TEXT 'YYYY-MM', category_id, assigned_cents,
  UNIQUE(month, category_id))`
  - available = assigned - activity + rollover_from_prior_month
- `bills(id, name, amount_cents, due_day, frequency, autopay INTEGER,
  autopay_from, category_id, next_due_date, last_paid_date, notes,
  active INTEGER)`
  - autopay_from is the account label the bill pulls from (e.g. a checking
    label or a card label); labels naming a card brand (Visa, Mastercard,
    Amex, Discover) mark the bill as a card bill — it never touches cash
  - category_id links the bill to a category so a matching categorized
    outflow detects the bill as paid
  - next_due_date is an explicit next due date (YYYY-MM-DD) that overrides
    due_day math — e.g. to keep an overdue bill overdue
- `debts(id, name, creditor, balance_cents, apr, minimum_cents, due_day,
  payoff_order INTEGER, notes)`
- `meta(key UNIQUE, value)`:
  - next_paycheck_date, next_paycheck_amount_cents — the payday anchor
  - paycheck_cadence — label: weekly | biweekly | semi-monthly | monthly
  - paycheck_pattern — JSON describing the schedule, e.g.
    `{"type": "month_days", "days": [15, 31]}` (day 31 clamps to month end)
    or `{"type": "every_days", "days": 14}`. Future paydays project from
    this stored pattern + anchor — never a baked-in pattern.
  - envelope_categories — JSON list of category names that stay manual
    green/red envelopes (everything else is bill-status-driven)

## Tabs

1. **Overview** — total cash (sum of depository), total debt, net; per-account
   cards with balance + "synced Xm ago"; unassigned cash number;
   "due before next paycheck" bill strip (cash bills only — card bills
   excluded because they never touch checking).
2. **Inbox** — pending + uncategorized transactions, newest first. Tap →
   category chips (most-used first) + full list search. Categorizing creates or
   bumps a payee_rule. Show "suggested" badge when a rule matches.
3. **Budget** — two views of one model:
   - **Today**: a "This month" strip (Cash on hand / Paid this month /
     Still due this month / Put on cards this month — the month's total
     charges to credit accounts, i.e. new debt), a Safe-to-spend headline
     (cash minus unpaid cash bills due on/before the next paycheck), every
     bill with a computed status and its funding — Paid · cash (green,
     with the paid date) / Paid · on card (names the card the payment
     landed on) / Covered · cash on hand / Deferred → the paycheck date
     that funds it / On card — and the envelope categories as manual
     green/red rows with assign steppers. Each envelope row also splits
     its spent into cash vs on card (`spent_cash_cents` /
     `spent_card_cents`), the on-card part highlighted as new debt.
     Month switcher for review.
   - **Plan**: the paycheck schedule (next paydays projected from the
     stored pattern), a day-by-day projected cash-balance forecast with the
     lowest point highlighted, and a bills calendar where each bill's name,
     amount, due date, frequency, autopay, account label, and linked
     category can be added/edited/deleted.
4. **Bills** — sorted by next due date. Each: amount, due day/date, autopay
   badge + source, last paid, and the same status/funding pill as the
   Budget tab (Paid · cash / Paid · on card / Covered / Deferred). Toggle
   autopay, mark paid. Top section: "Due before next paycheck".
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
  (`mark_bill_paid` also advances the bill's next due date)
- `set_paycheck` {date, amount_cents, cadence, month_days?, interval_days?} →
  stores the payday anchor + cadence + explicit projection pattern
- `upsert_debt`, `update_debt_balance` {debt_id, balance_cents}
- `get_overview`, `get_inbox`, `get_budget` {month}, `get_bills`, `get_debts`
  - `get_budget` rows carry `spent_cash_cents` / `spent_card_cents`
    (cash + card = spent on cash/credit accounts); month summaries carry
    `put_on_cards_cents` (all non-transfer outflows on credit accounts);
    every bill carries `status`, `funding` ("cash" | "card" | null) and
    `funding_account` (the account name the money came from)
- `get_bill_plan` → Today/Plan payload: month summary, paycheck schedule,
  bill statuses, cash-balance forecast

## Sync behavior

- Daily morning refresh trigger: accounts, rolling 30d transactions, liabilities.
- Dedup on plaid_txn_id. Pending transactions update to posted in place.
- An incoming id that matches an existing row on the same account, same
  amount, and a date within 2 days merges in place (adopts the real id,
  keeps the category) instead of duplicating — imported rows with temporary
  ids never double up.
- New uncategorized transactions land in Inbox with payee-rule suggestions.

## Seed data (provided post-build via import_seed — never invent)

- ~40 categories (Bills / Frequent / Quality of Life / Goals groups)
- All transactions since a chosen start date with categories; transfers
  flagged with is_transfer
- ~25 bills with due dates, amounts, autopay status, account labels,
  and category links
- ~15 debts with balances, APRs, minimums, payoff order
- meta: next_paycheck_date, next_paycheck_amount_cents, paycheck_cadence,
  paycheck_pattern, envelope_categories
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
