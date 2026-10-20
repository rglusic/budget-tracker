# Budget Tracker

An AI-forward personal budgeting app. Bills-first budgeting from actual
cash balances, with a categorization engine that learns your payees and a
data model designed to be read and maintained by an AI assistant — not just
clicked through by a human.

It started as a replacement for YNAB: same envelope discipline, without the
subscription, and without the confusing bits (stale balances with no sync
time, one-sided transfers, phantom "available" money). But the real idea is
bigger: a budget your assistant can actually reason about — every number
carries its freshness, every transaction teaches the system, and the whole
state lives in a plain SQLite file instead of a black box.

## What it does

- **Live balances, with receipts.** Every account shows *when* it last synced.
  A number without a sync time is a rumor; this app treats it that way.
- **Inbox that learns.** New and uncategorized transactions land in an inbox.
  Categorize once and the app remembers the payee — next time it suggests the
  category before you tap anything. Suggestions get better the more you use it.
- **Bills-first budgeting.** Most money is fixed obligations, so bills get
  computed statuses from their due dates instead of manual assignment: Paid
  (green, with the date) / Covered / Short (naming the paycheck that covers
  it) / On card. A "This month" strip shows Cash on hand, Paid this month,
  and Still due this month, plus one headline number: **Safe to spend** =
  cash minus unpaid cash bills due before the next paycheck. Only the
  categories where you actually make spending decisions stay manual
  green/red envelopes.
- **Plan view with a forecast.** Paydays are data, not code: the schedule
  lives in settings (an anchor date plus a stored pattern), and the app
  projects checking balance day by day from paydays and bill due dates —
  including the lowest point before the next payday.
- **Bills board.** Every bill with its due date, autopay status, and last-paid
  date. A "due before next paycheck" strip answers the only question that
  matters mid-month.
- **Debt tracker.** Balances, APRs, minimums, and payoff order with progress
  bars, plus totals for monthly minimums and estimated monthly interest.
- **Transfers are first-class.** A card payment is one transfer, checking →
  card. Never two unlinked one-sided transactions drifting apart.

## AI-forward by design

- The database schema is small, documented, and stable — an assistant can
  query it directly to answer "what do I owe before Friday?" without scraping
  a UI.
- Categorization is a learning loop, not a form: payee rules accumulate with
  use counts, so suggestions improve over time and can be audited.
- All published actions are plain HTTP endpoints (`POST /api/<action>`) with
  JSON in/out — the same surface the UI uses is the surface an agent uses.
- Sync is a swappable provider: Plaid today, anything tomorrow.

## Quick start

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python app.py            # http://127.0.0.1:5000
```

The database is created automatically at `./budget.db` (override with
`BUDGET_DB`). It is gitignored — your data never enters the repo.

Run the tests:

```bash
python tests/test_actions.py
```

## Bank sync (Plaid)

Sync is read-only. The app never moves money; payments happen in your bank's
app. To enable live sync, set these environment variables (never commit
them — see `.gitignore`):

| Variable            | Meaning                                              |
|---------------------|------------------------------------------------------|
| `SYNC_PROVIDER`     | `plaid` to enable, anything else disables (`null`)   |
| `PLAID_CLIENT_ID`   | Plaid client ID                                      |
| `PLAID_SECRET`      | Plaid secret                                         |
| `PLAID_ACCESS_TOKEN`| Access token for your institution                    |
| `PLAID_ENV`         | `sandbox` \| `development` \| `production` (default `sandbox`) |

Then hit **Sync** in the UI or `POST /api/sync_now`. The provider refreshes
account balances, pulls the last 30 days of transactions (deduped on Plaid's
transaction IDs; pending transactions update in place when they post), and
stamps everything with a sync time.

No credentials? The app runs fine without them — seed data via
`POST /api/import_seed` and enter transactions however you like.

## Seeding your data

`POST /api/import_seed` takes `{accounts, categories, transactions, bills,
debts, meta}` and is idempotent on natural keys (Plaid IDs, category names,
bill/debt names), so re-running it never duplicates anything. This is how
real data enters the system — the repo ships with none.

## API

All endpoints are `POST /api/<action>` with a JSON body:

| Action | Args | What it does |
|---|---|---|
| `sync_now` | — | Plaid refresh; returns `{synced_at, accounts_upserted, transactions_added}` |
| `import_seed` | `seed` | Bulk seed, idempotent |
| `categorize_transaction` | `txn_id`, `category_id` | Categorize + learn/refresh payee rule |
| `assign_budget` | `month`, `category_id`, `amount_cents` | Delta to assigned (may be negative) |
| `upsert_bill` / `delete_bill` / `mark_bill_paid` | bill fields / `bill_id` / `bill_id`, `date` | Manage bills |
| `set_paycheck` | `date`, `amount_cents`, `cadence`, `month_days` / `interval_days` | Store the payday anchor + schedule pattern |
| `upsert_debt` / `update_debt_balance` | debt fields / `debt_id`, `balance_cents` | Manage debts |
| `get_overview` | — | Cash, debt, net, unassigned, accounts, bills due before payday |
| `get_inbox` | — | Pending + uncategorized, with suggestions |
| `get_budget` | `month` | Month summary strip, bill statuses, envelope Assigned / Spent / Available |
| `get_bill_plan` | — | Paycheck schedule, bill statuses, day-by-day cash forecast |
| `get_bills` | — | Bills sorted by next due date |
| `get_debts` | — | Debts in payoff order + totals |

Read actions also accept GET.

## Engineering notes

- **Integer cents everywhere.** No floats touch money math.
- **Timestamps stored UTC**, displayed in America/Los_Angeles.
- **Available = assigned − spent + rollover**, with full rollover of the prior
  month's available. Unassigned = on-budget cash − Σ available.
- **Schedules are data.** Payday patterns (semi-monthly, biweekly, weekly,
  monthly) are stored as a pattern + anchor in the database and projected
  from there; which categories are manual envelopes is a stored list too.
  Nothing about your job, your bills, or your paydays exists in the source.
- **No personal data in this repo.** Schema, code, docs, and synthetic test
  fixtures only. Real data lives in `seed/` (gitignored, local-only) and the
  SQLite file (gitignored).
- **No secrets in this repo.** Credentials come from the environment.

## Project layout

```
app.py                 Flask app + API routes
budget/
  db.py                SQLite schema
  actions.py           published actions (business logic)
  sync.py              swappable sync providers (Plaid)
templates/index.html   app shell
static/app.js          tab UI (Overview, Inbox, Budget, Bills, Debts)
static/style.css       mobile-first styles
docs/index.html        project landing page (GitHub Pages)
tests/                 synthetic-fixture tests
```

## Non-goals (v1)

Charts beyond progress bars, investments, receipt OCR, multi-user, and
initiating payments — this app is read-only toward banks by design.
