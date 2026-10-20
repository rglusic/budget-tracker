/* Budget Tracker — single-page UI. Vanilla JS, no build step. */
const view = document.getElementById('view');
const badge = document.getElementById('inbox-badge');
let state = { tab: 'overview', budgetMonth: null, budgetView: 'today' };

async function api(name, args) {
  const r = await fetch('/api/' + name, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(args || {}),
  });
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || ('HTTP ' + r.status));
  return data;
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function ago(iso) {
  if (!iso) return 'never synced';
  const mins = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
  if (mins < 1) return 'just now';
  if (mins < 60) return mins + 'm ago';
  const h = Math.floor(mins / 60);
  if (h < 24) return h + 'h ago';
  return Math.floor(h / 24) + 'd ago';
}

function local(iso) {
  if (!iso) return '—';
  return new Date(iso).toLocaleString('en-US', { timeZone: 'America/Los_Angeles' });
}

/* ---------------- Overview ---------------- */
async function renderOverview() {
  view.innerHTML = '<div class="loading">Loading…</div>';
  const o = await api('get_overview');
  let html = `
    <div class="card">
      <div class="row"><span class="muted">Cash</span><span class="big">${esc(o.cash_display)}</span></div>
      <div class="row"><span class="muted">Debt</span><span class="big neg">${esc(o.debt_display)}</span></div>
      <div class="row"><span class="muted">Net</span><span class="big">${esc(o.net_display)}</span></div>
      <div class="row"><span class="muted">Unassigned</span><span class="${o.unassigned_cents < 0 ? 'neg' : 'pos'}" style="font-weight:700">${esc(o.unassigned_display)}</span></div>
    </div>`;
  if (o.due_before_paycheck && o.due_before_paycheck.length) {
    html += `<div class="card"><h2>Due before next paycheck${o.next_paycheck_date ? ' (' + esc(o.next_paycheck_date) + ')' : ''}</h2>`;
    for (const b of o.due_before_paycheck) {
      html += `<div class="row"><span>${esc(b.name)} <span class="muted small">${esc(b.next_due_date)}</span>${b.autopay ? '<span class="pill">autopay</span>' : ''}</span><strong>${esc(b.amount_display)}</strong></div>`;
    }
    html += '</div>';
  }
  html += '<div class="card"><h2>Accounts</h2>';
  for (const a of o.accounts) {
    html += `<div class="row"><span>${esc(a.name)}${a.mask ? ' ••' + esc(a.mask) : ''}<br><span class="muted small">synced ${esc(ago(a.last_synced_at))}${a.include_in_budget ? '' : ' · not in budget'}</span></span><strong class="${a.balance_cents < 0 ? 'neg' : ''}">${esc(a.balance_display)}</strong></div>`;
  }
  html += `<div class="muted small" style="margin-top:8px">Last bank sync: ${esc(local(o.last_sync_at))}</div></div>`;
  view.innerHTML = html;
}

/* ---------------- Inbox ---------------- */
async function renderInbox() {
  view.innerHTML = '<div class="loading">Loading…</div>';
  const box = await api('get_inbox');
  badge.textContent = box.count;
  badge.classList.toggle('hidden', box.count === 0);
  if (!box.count) { view.innerHTML = '<div class="card"><h2>Inbox</h2><p class="muted">All caught up. Nothing needs categorizing.</p></div>'; return; }
  let html = `<div class="card"><h2>Inbox (${box.count})</h2>
    <input type="search" id="cat-search" placeholder="Search categories…">`;
  for (const t of box.items) {
    const chips = box.category_chips.slice(0, 12).map(c => {
      const sug = t.suggestion && t.suggestion.category_id === c.id;
      return `<button class="chip${sug ? ' suggested' : ''}" data-txn="${t.id}" data-cat="${c.id}">${esc(c.name)}${sug ? ' ★' : ''}</button>`;
    }).join('');
    html += `<div class="txn" data-txn-row="${t.id}">
      <div class="top"><span>${esc(t.merchant || t.name || 'Unknown')}</span><strong class="${t.amount_cents < 0 ? 'neg' : 'pos'}">${esc(t.amount_display)}</strong></div>
      <div class="sub">${esc(t.account_name)} · ${esc(t.date)}${t.pending ? ' · <span class="pill warn">pending</span>' : ''}${t.has_suggestion ? ` <span class="pill">suggested: ${esc(t.suggestion.category_name)}</span>` : ''}</div>
      <div class="chips" data-chips="${t.id}">${chips}</div>
    </div>`;
  }
  html += '</div>';
  view.innerHTML = html;

  const allChips = box.category_chips;
  document.getElementById('cat-search').addEventListener('input', e => {
    const q = e.target.value.toLowerCase();
    document.querySelectorAll('[data-chips]').forEach(div => {
      const txnId = div.getAttribute('data-chips');
      const t = box.items.find(x => String(x.id) === txnId);
      const list = allChips.filter(c => c.name.toLowerCase().includes(q)).slice(0, 12);
      div.innerHTML = list.map(c => {
        const sug = t.suggestion && t.suggestion.category_id === c.id;
        return `<button class="chip${sug ? ' suggested' : ''}" data-txn="${t.id}" data-cat="${c.id}">${esc(c.name)}${sug ? ' ★' : ''}</button>`;
      }).join('');
    });
  });
  view.querySelectorAll('button.chip').forEach(btn => {
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      try {
        await api('categorize_transaction', { txn_id: +btn.dataset.txn, category_id: +btn.dataset.cat });
        document.querySelector(`[data-txn-row="${btn.dataset.txn}"]`).remove();
        const left = view.querySelectorAll('[data-txn-row]').length;
        badge.textContent = left;
        badge.classList.toggle('hidden', left === 0);
        if (!left) renderInbox();
      } catch (err) { alert(err.message); btn.disabled = false; }
    });
  });
  // delegate for chips re-rendered by search
  view.addEventListener('click', async e => {
    const btn = e.target.closest('button.chip');
    if (!btn || btn.disabled) return;
    btn.disabled = true;
    try {
      await api('categorize_transaction', { txn_id: +btn.dataset.txn, category_id: +btn.dataset.cat });
      document.querySelector(`[data-txn-row="${btn.dataset.txn}"]`).remove();
    } catch (err) { alert(err.message); btn.disabled = false; }
  });
}

/* ---------------- Budget: Today + Plan views ---------------- */
function summaryStrip(s) {
  return `<div class="card"><h2>This month</h2><div class="strip">
    <div class="stat"><span class="muted small">Cash on hand</span><strong>${esc(s.cash_on_hand_display)}</strong></div>
    <div class="stat"><span class="muted small">Paid this month</span><strong>${esc(s.paid_this_month_display)}</strong></div>
    <div class="stat"><span class="muted small">Still due this month</span><strong>${esc(s.still_due_this_month_display)}</strong></div>
    <div class="stat"><span class="muted small">Put on cards this month</span><strong class="oncard">${esc(s.put_on_cards_display)}</strong></div>
  </div><div class="muted small">Put on cards = everything charged to a credit account this month. That's new debt, not spent cash.</div></div>`;
}
function billStatusRow(b) {
  let chip, cls = '';
  if (b.status === 'paid' && b.funding === 'card') {
    chip = `Paid ${esc(b.paid_date || '')} · on card${b.funding_account ? ' · ' + esc(b.funding_account) : ''}`; cls = 'card';
  }
  else if (b.status === 'paid') { chip = `Paid ${esc(b.paid_date || '')} · cash`; cls = 'ok'; }
  else if (b.status === 'covered') { chip = 'Covered · cash on hand'; cls = 'ok'; }
  else if (b.status === 'deferred') { chip = `Deferred${b.covering_paycheck_date ? ' → ' + esc(b.covering_paycheck_date) + ' paycheck' : ''}`; cls = 'bad'; }
  else { chip = "On card · doesn't use cash"; cls = 'warn'; }
  return `<div class="txn"><div class="top"><span>${esc(b.name)}</span><strong>${esc(b.amount_display)}</strong></div>
    <div class="sub">Due ${esc(b.next_due_date)}${b.autopay ? ' · autopay' : ''}${b.autopay_from ? ' · from ' + esc(b.autopay_from) : ''}</div>
    <div><span class="pill ${cls}">${chip}</span></div></div>`;
}
async function renderBudget() {
  view.innerHTML = '<div class="loading">Loading…</div>';
  if (state.budgetView === 'plan') return renderPlan();
  const b = await api('get_budget', state.budgetMonth ? { month: state.budgetMonth } : {});
  state.budgetMonth = b.month;
  const s = b.summary;
  let html = `<div class="monthbar">
      <button id="m-prev">‹ ${esc(b.prev_month)}</button>
      <strong>${esc(b.month)}</strong>
      <button id="m-next">${esc(b.next_month)} ›</button>
    </div>
    <div class="viewtoggle">
      <button data-v="today" class="active">Today</button><button data-v="plan">Plan</button>
    </div>
    ${summaryStrip(s)}
    <div class="card"><div class="row"><span class="muted">Safe to spend</span>
      <span class="big ${s.safe_to_spend_cents != null && s.safe_to_spend_cents < 0 ? 'neg' : 'pos'}">${esc(s.safe_to_spend_display)}</span></div>
      <div class="muted small">Cash on hand minus cash bills due before the next paycheck. Card bills don't count — they never touch checking.</div></div>
    <div class="card"><h2>Bills</h2>`;
  for (const bill of b.bills) html += billStatusRow(bill);
  html += `</div><div class="card"><h2>Envelopes</h2>`;
  for (const c of b.envelopes) {
    html += `<div class="txn">
        <div class="top"><span>${esc(c.name)}</span><strong class="${c.available_cents < 0 ? 'neg' : 'pos'}">${esc(c.available_display)}</strong></div>
        <div class="sub">Assigned ${esc(c.assigned_display)} · Spent ${esc(c.spent_display)} — cash ${esc(c.spent_cash_display)} · <span class="oncard">on card ${esc(c.spent_card_display)}</span></div>
        <div class="stepper" data-cat="${c.id}">
          <button data-d="-1000">−$10</button><button data-d="-100">−$1</button>
          <button data-d="100">+$1</button><button data-d="1000">+$10</button>
        </div>
      </div>`;
  }
  html += '</div>';
  view.innerHTML = html;
  document.getElementById('m-prev').onclick = () => { state.budgetMonth = b.prev_month; renderBudget(); };
  document.getElementById('m-next').onclick = () => { state.budgetMonth = b.next_month; renderBudget(); };
  view.querySelectorAll('.viewtoggle button').forEach(btn => {
    btn.onclick = () => { state.budgetView = btn.dataset.v; renderBudget(); };
  });
  view.querySelectorAll('.stepper button').forEach(btn => {
    btn.onclick = async () => {
      const catId = +btn.parentElement.dataset.cat;
      btn.disabled = true;
      try {
        await api('assign_budget', { month: b.month, category_id: catId, amount_cents: +btn.dataset.d });
        renderBudget();
      } catch (err) { alert(err.message); btn.disabled = false; }
    };
  });
}

/* ---------------- Plan view ---------------- */
async function renderPlan() {
  view.innerHTML = '<div class="loading">Loading…</div>';
  const p = await api('get_bill_plan');
  const pay = p.paycheck;
  let html = `<div class="viewtoggle">
      <button data-v="today">Today</button><button data-v="plan" class="active">Plan</button>
    </div>
    <div class="card"><h2>Paychecks</h2>
      <div class="sub">Next: ${esc(pay.next_date || 'not set')}${pay.amount_cents != null ? ' · ' + esc(pay.amount_display) : ''}${pay.cadence ? ' · ' + esc(pay.cadence) : ''}</div>
      <div class="sub">Coming up: ${p.paycheck_dates.map(esc).join(' · ') || '—'}</div>
      <form id="pay-form">
        <label class="small muted">Next payday</label>
        <input type="date" id="pay-date" value="${esc(pay.next_date || '')}" required>
        <label class="small muted">Amount ($)</label>
        <input type="number" id="pay-amount" step="0.01" value="${pay.amount_cents != null ? (pay.amount_cents / 100) : ''}">
        <label class="small muted">Cadence</label>
        <select id="pay-cadence">
          ${['semi-monthly', 'biweekly', 'weekly', 'monthly'].map(c => `<option value="${c}"${pay.cadence === c ? ' selected' : ''}>${c}</option>`).join('')}
        </select>
        <label class="small muted">Paydays of month (semi-monthly / monthly, comma-separated; 31 = last day)</label>
        <input type="text" id="pay-days" placeholder="e.g. 15, 31" value="${pay.pattern && pay.pattern.type === 'month_days' ? esc(pay.pattern.days.join(', ')) : ''}">
        <button type="submit">Save paycheck schedule</button>
      </form>
    </div>
    <div class="card"><h2>Forecast</h2>
      <div class="sub">Projected cash balance, day by day. Lowest point: <strong class="${p.forecast.lowest && p.forecast.lowest.balance_cents < 0 ? 'neg' : ''}">${esc(p.forecast.lowest ? p.forecast.lowest.balance_display + ' on ' + p.forecast.lowest.date : '—')}</strong></div>
      <div class="fc">`;
  for (const pt of p.forecast.days) {
    const isLow = p.forecast.lowest && pt.date === p.forecast.lowest.date;
    html += `<div class="fcrow${isLow ? ' low' : ''}"><span>${esc(pt.date)}</span><strong class="${pt.balance_cents < 0 ? 'neg' : ''}">${esc(pt.balance_display)}</strong></div>`;
  }
  html += `</div></div>
    <div class="card"><h2>Bills calendar</h2>`;
  for (const b of p.bills) {
    html += `<div class="txn"><div class="top"><span><span class="dot ${b.is_card ? 'card' : 'cash'}"></span>${esc(b.name)}</span><strong>${esc(b.amount_display)}</strong></div>
      <div class="sub">Due ${esc(b.next_due_date)} · ${esc(b.frequency)}${b.autopay ? ' · autopay' : ''}${b.autopay_from ? ' · from ' + esc(b.autopay_from) : ''}</div>
      <div class="stepper"><button data-edit="${b.id}">Edit</button></div>
      <form class="bill-form hidden" data-form="${b.id}">
        <label class="small muted">Name</label><input type="text" name="name" value="${esc(b.name)}">
        <label class="small muted">Amount ($)</label><input type="number" name="amount" step="0.01" value="${(b.amount_cents / 100).toFixed(2)}">
        <label class="small muted">Due day of month</label><input type="number" name="due_day" min="1" max="31" value="${b.due_day}">
        <label class="small muted">Frequency</label>
        <select name="frequency">${['monthly', 'weekly', 'biweekly', 'yearly', 'once'].map(fr => `<option value="${fr}"${b.frequency === fr ? ' selected' : ''}>${fr}</option>`).join('')}</select>
        <label class="small muted">Next due date (optional override)</label><input type="date" name="next_due_date" value="${esc(b.next_due_date)}">
        <label class="small muted">Account label (what it pulls from)</label><input type="text" name="autopay_from" value="${esc(b.autopay_from || '')}">
        <label class="small muted">Category (paid detection)</label>
        <select name="category_id"><option value="">—</option>${p.categories.map(c => `<option value="${c.id}"${b.category_id === c.id ? ' selected' : ''}>${esc(c.name)}</option>`).join('')}</select>
        <label class="small"><input type="checkbox" name="autopay" ${b.autopay ? 'checked' : ''}> Autopay</label>
        <div class="stepper"><button type="submit">Save</button><button type="button" data-del="${b.id}">Delete</button></div>
      </form></div>`;
  }
  html += `<h3>Add a bill</h3>
    <form class="bill-form" data-form="new">
      <label class="small muted">Name</label><input type="text" name="name" required>
      <label class="small muted">Amount ($)</label><input type="number" name="amount" step="0.01" required>
      <label class="small muted">Due day of month</label><input type="number" name="due_day" min="1" max="31" required>
      <label class="small muted">Frequency</label>
      <select name="frequency"><option>monthly</option><option>weekly</option><option>biweekly</option><option>yearly</option><option>once</option></select>
      <label class="small muted">Next due date (optional override)</label><input type="date" name="next_due_date">
      <label class="small muted">Account label (what it pulls from)</label><input type="text" name="autopay_from">
      <label class="small muted">Category (paid detection)</label>
      <select name="category_id"><option value="">—</option>${p.categories.map(c => `<option value="${c.id}">${esc(c.name)}</option>`).join('')}</select>
      <label class="small"><input type="checkbox" name="autopay"> Autopay</label>
      <div class="stepper"><button type="submit">Add bill</button></div>
    </form></div>`;
  view.innerHTML = html;
  view.querySelectorAll('.viewtoggle button').forEach(btn => {
    btn.onclick = () => { state.budgetView = btn.dataset.v; renderBudget(); };
  });
  document.getElementById('pay-form').addEventListener('submit', async e => {
    e.preventDefault();
    const daysRaw = document.getElementById('pay-days').value.trim();
    const args = {
      date: document.getElementById('pay-date').value,
      amount_cents: Math.round(parseFloat(document.getElementById('pay-amount').value || '0') * 100),
      cadence: document.getElementById('pay-cadence').value,
    };
    if (daysRaw) args.month_days = daysRaw.split(',').map(s => parseInt(s, 10)).filter(n => n >= 1 && n <= 31);
    try { await api('set_paycheck', args); renderPlan(); }
    catch (err) { alert(err.message); }
  });
  view.querySelectorAll('[data-edit]').forEach(btn => {
    btn.onclick = () => {
      const form = view.querySelector(`[data-form="${btn.dataset.edit}"]`);
      form.classList.toggle('hidden');
    };
  });
  view.querySelectorAll('form.bill-form').forEach(form => {
    form.addEventListener('submit', async e => {
      e.preventDefault();
      const f = new FormData(form);
      const bill = {
        name: f.get('name'),
        amount_cents: Math.round(parseFloat(f.get('amount') || '0') * 100),
        due_day: parseInt(f.get('due_day'), 10),
        frequency: f.get('frequency') || 'monthly',
        autopay: f.get('autopay') ? 1 : 0,
        autopay_from: f.get('autopay_from') || null,
        category_id: f.get('category_id') ? parseInt(f.get('category_id'), 10) : null,
        next_due_date: f.get('next_due_date') || null,
        active: 1,
      };
      try { await api('upsert_bill', { bill }); renderPlan(); }
      catch (err) { alert(err.message); }
    });
  });
  view.querySelectorAll('[data-del]').forEach(btn => {
    btn.onclick = async () => {
      if (!confirm('Delete this bill?')) return;
      const row = p.bills.find(b => String(b.id) === btn.dataset.del);
      try { await api('delete_bill', { bill_id: row.id }); renderPlan(); }
      catch (err) { alert(err.message); }
    };
  });
}

/* ---------------- Bills ---------------- */
async function renderBills() {
  view.innerHTML = '<div class="loading">Loading…</div>';
  const data = await api('get_bills');
  let html = '';
  if (data.due_before_paycheck.length) {
    html += `<div class="card"><h2>Due before next paycheck${data.next_paycheck_date ? ' (' + esc(data.next_paycheck_date) + ')' : ''}</h2>`;
    for (const b of data.due_before_paycheck) html += billRow(b, true);
    html += '</div>';
  }
  html += '<div class="card"><h2>All bills</h2>';
  for (const b of data.bills) html += billRow(b, false);
  html += '</div>';
  view.innerHTML = html;
  view.querySelectorAll('[data-paid]').forEach(btn => {
    btn.onclick = async () => {
      btn.disabled = true;
      try { await api('mark_bill_paid', { bill_id: +btn.dataset.paid }); renderBills(); }
      catch (err) { alert(err.message); btn.disabled = false; }
    };
  });
}
function fundingPill(b) {
  if (!b.status) return '';
  if (b.status === 'paid' && b.funding === 'card') return `<span class="pill card">Paid · on card${b.funding_account ? ' · ' + esc(b.funding_account) : ''}</span>`;
  if (b.status === 'paid') return '<span class="pill ok">Paid · cash</span>';
  if (b.status === 'covered') return '<span class="pill ok">Covered · cash on hand</span>';
  if (b.status === 'deferred') return `<span class="pill bad">Deferred${b.covering_paycheck_date ? ' → ' + esc(b.covering_paycheck_date) + ' paycheck' : ''}</span>`;
  if (b.status === 'card') return '<span class="pill warn">On card</span>';
  return '';
}
function billRow(b, compact) {
  return `<div class="txn"><div class="top"><span>${esc(b.name)}${b.autopay ? '<span class="pill ok">autopay</span>' : ''}${fundingPill(b)}</span><strong>${esc(b.amount_display)}</strong></div>
    <div class="sub">Due ${esc(b.next_due_date)}${b.autopay_from ? ' · from ' + esc(b.autopay_from) : ''}${b.last_paid_date ? ' · last paid ' + esc(b.last_paid_date) : ''}</div>
    ${compact ? '' : `<div class="stepper"><button data-paid="${b.id}">Mark paid</button></div>`}</div>`;
}

/* ---------------- Debts ---------------- */
async function renderDebts() {
  view.innerHTML = '<div class="loading">Loading…</div>';
  const d = await api('get_debts');
  const maxBal = Math.max(1, ...d.debts.map(x => x.balance_cents || 0));
  let html = `<div class="card"><h2>Totals</h2>
    <div class="row"><span class="muted">Monthly minimums</span><strong>${esc(d.total_minimum_display)}</strong></div>
    <div class="row"><span class="muted">Est. monthly interest</span><strong class="neg">${esc(d.est_monthly_interest_display)}</strong></div></div>
    <div class="card"><h2>Debts (payoff order)</h2>`;
  d.debts.forEach((x, i) => {
    const pct = Math.round(100 * (x.balance_cents || 0) / maxBal);
    html += `<div class="txn"><div class="top"><span>#${x.payoff_order != null ? x.payoff_order : '—'} ${esc(x.name)}</span><strong class="neg">${esc(x.balance_display)}</strong></div>
      <div class="sub">${x.creditor ? esc(x.creditor) + ' · ' : ''}${x.apr != null ? (x.apr * 100).toFixed(2) + '% APR · ' : ''}min ${esc(x.minimum_display)}${x.due_day ? ' · due day ' + x.due_day : ''}</div>
      <div class="bar"><div style="width:${pct}%"></div></div></div>`;
  });
  html += '</div>';
  view.innerHTML = html;
}

/* ---------------- shell ---------------- */
const RENDER = { overview: renderOverview, inbox: renderInbox, budget: renderBudget, bills: renderBills, debts: renderDebts };

document.querySelectorAll('nav#tabs button').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('nav#tabs button').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    state.tab = btn.dataset.tab;
    RENDER[state.tab]().catch(err => { view.innerHTML = '<div class="err">' + esc(err.message) + '</div>'; });
  });
});

document.getElementById('sync-btn').addEventListener('click', async e => {
  const btn = e.target;
  btn.disabled = true; btn.textContent = 'Syncing…';
  try {
    const r = await api('sync_now');
    btn.textContent = 'Synced';
    RENDER[state.tab]().catch(() => {});
  } catch (err) { alert('Sync failed: ' + err.message); }
  finally { setTimeout(() => { btn.disabled = false; btn.textContent = 'Sync'; }, 1500); }
});

(async function init() {
  try {
    const box = await api('get_inbox');
    badge.textContent = box.count;
    badge.classList.toggle('hidden', box.count === 0);
  } catch (e) { /* offline DB, badge stays hidden */ }
  RENDER.overview().catch(err => { view.innerHTML = '<div class="err">' + esc(err.message) + '</div>'; });
})();
