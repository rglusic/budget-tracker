/* Budget Tracker — single-page UI. Vanilla JS, no build step. */
const view = document.getElementById('view');
const badge = document.getElementById('inbox-badge');
let state = { tab: 'overview', budgetMonth: null };

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

/* ---------------- Budget ---------------- */
async function renderBudget() {
  view.innerHTML = '<div class="loading">Loading…</div>';
  const b = await api('get_budget', state.budgetMonth ? { month: state.budgetMonth } : {});
  state.budgetMonth = b.month;
  let html = `<div class="monthbar">
      <button id="m-prev">‹ ${esc(b.prev_month)}</button>
      <strong>${esc(b.month)}</strong>
      <button id="m-next">${esc(b.next_month)} ›</button>
    </div>
    <div class="card"><div class="row"><span class="muted">Unassigned</span>
      <strong class="${b.unassigned_cents < 0 ? 'neg' : 'pos'}">${esc(b.unassigned_display)}</strong></div></div>`;
  for (const g of b.groups) {
    html += `<div class="card"><h2>${esc(g.name)}</h2>`;
    for (const c of g.categories) {
      html += `<div class="txn">
        <div class="top"><span>${esc(c.name)}</span><strong class="${c.available_cents < 0 ? 'neg' : ''}">${esc(c.available_display)}</strong></div>
        <div class="sub">Assigned ${esc(c.assigned_display)} · Spent ${esc(c.spent_display)}</div>
        <div class="stepper" data-cat="${c.id}">
          <button data-d="-1000">−$10</button><button data-d="-100">−$1</button>
          <button data-d="100">+$1</button><button data-d="1000">+$10</button>
        </div>
      </div>`;
    }
    html += '</div>';
  }
  view.innerHTML = html;
  document.getElementById('m-prev').onclick = () => { state.budgetMonth = b.prev_month; renderBudget(); };
  document.getElementById('m-next').onclick = () => { state.budgetMonth = b.next_month; renderBudget(); };
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
function billRow(b, compact) {
  return `<div class="txn"><div class="top"><span>${esc(b.name)}${b.autopay ? '<span class="pill ok">autopay</span>' : ''}</span><strong>${esc(b.amount_display)}</strong></div>
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
