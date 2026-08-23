/* ── app.js — RecoverIQ Dashboard ─────────────────────────────────────────── */

'use strict';

// ── State ────────────────────────────────────────────────────────────────────
let stats    = {};
let events   = [];
let sse      = null;
let firstRow = true;

// ── Init ──────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  loadInitial();
  connectSSE();
  renderEmptyTable();
});

// ── Load initial data ─────────────────────────────────────────────────────────
async function loadInitial() {
  try {
    const [s, ev] = await Promise.all([
      fetch('/api/stats').then(r => r.json()),
      fetch('/api/events').then(r => r.json()),
    ]);
    updateStats(s);
    if (ev.length) {
      events = ev;
      renderTable();
    }
  } catch (e) {
    console.warn('Initial load failed:', e);
  }
}

// ── SSE connection ────────────────────────────────────────────────────────────
function connectSSE() {
  if (sse) sse.close();
  sse = new EventSource('/api/stream');

  sse.addEventListener('recovery_event', e => {
    const ev = JSON.parse(e.data);
    events.unshift(ev);
    if (events.length > 100) events.pop();
    prependRow(ev);
  });

  sse.addEventListener('stats', e => {
    updateStats(JSON.parse(e.data));
  });

  sse.onerror = () => {
    setTimeout(connectSSE, 3000);
  };
}

// ── Stats ─────────────────────────────────────────────────────────────────────
function updateStats(s) {
  stats = s;

  setText('stat-recovered',     fmt(s.total_recovered));
  setText('stat-events',        `${s.total_events} events processed`);
  setText('stat-rate',          s.total_events ? s.success_rate + '%' : '—');
  setText('stat-success-detail',`${s.successful} successful / ${s.failed} failed`);
  setText('stat-retries',       s.retries_scheduled);
  setText('stat-renewals',      s.renewals_sent);
  setText('stat-escalations',   s.escalations);

  // Breakdown bars
  const total = s.total_events || 1;
  setBar('retry',     s.retries_scheduled, total);
  setBar('collect',   s.upi_collects,      total);
  setBar('renewal',   s.renewals_sent,     total);
  setBar('whatsapp',  s.whatsapp_sent,     total);
  setBar('escalation',s.escalations,       total);
}

function setBar(key, val, total) {
  const pct = Math.min(100, Math.round((val / total) * 100));
  const bar = document.getElementById('bar-' + key);
  const cnt = document.getElementById('cnt-' + key);
  if (bar) bar.style.width = pct + '%';
  if (cnt) cnt.textContent = val;
}

// ── Table ─────────────────────────────────────────────────────────────────────
function renderEmptyTable() {
  const tbody = document.getElementById('events-tbody');
  tbody.innerHTML = `
    <tr class="empty-row">
      <td colspan="8">
        <div class="empty-state">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
            <circle cx="12" cy="12" r="10"/>
            <line x1="12" y1="8" x2="12" y2="12"/>
            <line x1="12" y1="16" x2="12.01" y2="16"/>
          </svg>
          <p>No events yet — run a scenario from the simulator</p>
        </div>
      </td>
    </tr>`;
}

function renderTable() {
  const tbody = document.getElementById('events-tbody');
  tbody.innerHTML = '';
  events.forEach(ev => tbody.appendChild(makeRow(ev)));
}

function prependRow(ev) {
  const tbody = document.getElementById('events-tbody');
  // Remove empty-state row if present
  const empty = tbody.querySelector('.empty-row');
  if (empty) empty.remove();

  const row = makeRow(ev);
  row.classList.add('new-row');
  tbody.insertBefore(row, tbody.firstChild);
}

function makeRow(ev) {
  const tr = document.createElement('tr');
  tr.onclick = () => openDrawer(ev);

  const ivChips = (ev.interventions || [])
    .map(iv => `<span class="iv-chip">${ivLabel(iv)}</span>`)
    .join('');

  const sev = (ev.severity || 'medium').toLowerCase();
  const code = (ev.failure_code || '').toUpperCase();

  tr.innerHTML = `
    <td style="color:var(--text-2); font-size:12px; font-variant-numeric:tabular-nums">${ev.timestamp || ''}</td>
    <td><span class="code-badge ${sevClass(sev)}">${code}</span></td>
    <td class="vpa-cell">${ev.customer_vpa || ''}</td>
    <td style="color:var(--text-2)">${ev.bank || ''}</td>
    <td class="amount-cell">${fmt(ev.amount)}</td>
    <td><span class="badge badge-${sevClass(sev)}">${capFirst(sev)}</span></td>
    <td>${ivChips || '<span style="color:var(--text-3);font-size:12px">—</span>'}</td>
    <td>
      ${ev.success
        ? '<span class="status-ok">✓ Handled</span>'
        : '<span class="status-err">✗ Failed</span>'}
    </td>`;
  return tr;
}

// ── Drawer ────────────────────────────────────────────────────────────────────
function openDrawer(ev) {
  const sev = (ev.severity || 'medium').toLowerCase();
  document.getElementById('drawer-title').textContent =
    ev.scenario_name || `${ev.failure_code} Event`;
  document.getElementById('drawer-sub').textContent =
    ev.failure_reason || '';

  const body = document.getElementById('drawer-body');
  const ivBlocks = (ev.interventions || []).map((iv, i) => `
    <div class="iv-block">
      <div class="iv-block-type">${ivLabel(iv)}</div>
      <div class="iv-block-msg">${ev.intervention_msgs?.[i] || ''}</div>
    </div>`).join('');

  body.innerHTML = `
    <div class="drawer-section">
      <div class="drawer-section-title">Event Details</div>
      ${field('Event Type',    ev.event_type)}
      ${field('Failure Code',  `<span class="code-badge ${sevClass(sev)}">${ev.failure_code}</span>`)}
      ${field('Failure Reason',ev.failure_reason)}
      ${field('Timestamp',     ev.timestamp)}
      ${field('Event ID',      `<span style="font-family:monospace;font-size:11px">${ev.id}</span>`)}
    </div>
    <div class="drawer-section">
      <div class="drawer-section-title">Customer</div>
      ${field('Customer ID',   ev.customer_id)}
      ${field('VPA',           `<span style="font-family:monospace;font-size:12px">${ev.customer_vpa}</span>`)}
      ${field('Bank',          ev.bank)}
      ${field('Amount',        `<strong>${fmt(ev.amount)}</strong>`)}
      ${field('Severity',      `<span class="badge badge-${sevClass(sev)}">${capFirst(sev)}</span>`)}
    </div>
    <div class="drawer-section">
      <div class="drawer-section-title">Interventions Fired</div>
      ${ivBlocks || '<p style="color:var(--text-3);font-size:12px">No interventions</p>'}
    </div>
    ${ev.scheduled_at ? `
    <div class="drawer-section">
      <div class="drawer-section-title">Retry Schedule</div>
      ${field('Scheduled At', ev.scheduled_at)}
    </div>` : ''}
    ${ev.action_url ? `
    <div class="drawer-section">
      <div class="drawer-section-title">Action URL</div>
      <code style="font-size:11.5px;word-break:break-all;color:var(--blue)">${ev.action_url}</code>
    </div>` : ''}`;

  document.getElementById('drawer').classList.add('open');
  document.getElementById('drawer-overlay').classList.add('open');
}

function closeDrawer() {
  document.getElementById('drawer').classList.remove('open');
  document.getElementById('drawer-overlay').classList.remove('open');
}

function field(label, value) {
  return `
    <div class="drawer-field">
      <span class="drawer-field-label">${label}</span>
      <span class="drawer-field-value">${value}</span>
    </div>`;
}

// ── Simulator ─────────────────────────────────────────────────────────────────
async function runScenario(key) {
  const btns = document.querySelectorAll('.scenario-btn');
  btns.forEach(b => b.classList.add('loading'));

  try {
    const res = await fetch(`/api/simulate/${key}`, { method: 'POST' });
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    const name = data.scenario_name || key.toUpperCase();
    toast(`✓ ${name} — interventions fired`, 'success');
  } catch (e) {
    toast(`Failed: ${e.message}`, 'error');
  } finally {
    btns.forEach(b => b.classList.remove('loading'));
  }
}

async function runAll() {
  const btns = document.querySelectorAll('.scenario-btn');
  const runBtn = document.querySelector('.btn-block');
  btns.forEach(b => b.classList.add('loading'));
  if (runBtn) { runBtn.textContent = 'Running...'; runBtn.disabled = true; }

  try {
    const res = await fetch('/api/simulate/all', { method: 'POST' });
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    toast(`✓ ${data.processed} scenarios processed`, 'success');
  } catch (e) {
    toast(`Failed: ${e.message}`, 'error');
  } finally {
    btns.forEach(b => b.classList.remove('loading'));
    if (runBtn) { runBtn.textContent = '▶ Run All Scenarios'; runBtn.disabled = false; }
  }
}

async function resetAll() {
  await fetch('/api/reset', { method: 'POST' });
  events = [];
  renderEmptyTable();
  updateStats({
    total_events:0, total_recovered:0, successful:0, failed:0,
    success_rate:0, retries_scheduled:0, renewals_sent:0,
    escalations:0, whatsapp_sent:0, upi_collects:0
  });
  toast('Dashboard cleared', 'success');
}

// ── Toast ─────────────────────────────────────────────────────────────────────
function toast(msg, type = 'success') {
  let container = document.getElementById('toast-container');
  if (!container) {
    container = document.createElement('div');
    container.id = 'toast-container';
    document.body.appendChild(container);
  }
  const t = document.createElement('div');
  t.className = `toast ${type}`;
  t.textContent = msg;
  container.appendChild(t);
  setTimeout(() => t.remove(), 3500);
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function fmt(n) {
  if (!n && n !== 0) return '—';
  return '₹' + Number(n).toLocaleString('en-IN', {
    minimumFractionDigits: 0, maximumFractionDigits: 0
  });
}

function setText(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val;
}

function capFirst(s) {
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : '';
}

function sevClass(sev) {
  const map = { critical:'critical', high:'high', medium:'medium', low:'low' };
  return map[sev] || 'medium';
}

function ivLabel(iv) {
  const map = {
    smart_retry:      'Smart Retry',
    upi_collect:      'UPI Collect',
    mandate_renewal:  'Mandate Renewal',
    whatsapp_nudge:   'WhatsApp',
    escalation:       'Escalated',
  };
  return map[iv] || iv;
}
