'use strict';

// ── State ─────────────────────────────────────────────────────────────────────
let events = [];
let sse    = null;

// ── Boot ──────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  loadInitial();
  connectSSE();
});

// ── Initial load ──────────────────────────────────────────────────────────────
async function loadInitial() {
  try {
    const [s, ev] = await Promise.all([
      fetch('/api/stats').then(r => r.json()),
      fetch('/api/events').then(r => r.json()),
    ]);
    syncStats(s);
    if (ev.length) { events = ev; rebuildTable(); }
  } catch (e) { console.warn('init load failed:', e); }
}

// ── SSE ───────────────────────────────────────────────────────────────────────
function connectSSE() {
  if (sse) sse.close();
  sse = new EventSource('/api/stream');

  sse.addEventListener('recovery_event', e => {
    const ev = JSON.parse(e.data);
    events.unshift(ev);
    if (events.length > 100) events.pop();
    prependRow(ev);
  });

  sse.addEventListener('stats', e => { syncStats(JSON.parse(e.data)); });
  sse.onerror = () => setTimeout(connectSSE, 3000);
}

// ── Stats sync ────────────────────────────────────────────────────────────────
function syncStats(s) {
  set('kpi-recovered',     fmtInr(s.total_recovered));
  set('kpi-events',        `${s.total_events} events processed`);
  set('kpi-rate',          s.total_events ? s.success_rate + '%' : '—');
  set('kpi-success-detail',`${s.successful} handled · ${s.failed} failed`);
  set('kpi-retries',       s.retries_scheduled);
  set('kpi-renewals',      s.renewals_sent);
  set('kpi-escalations',   s.escalations);

  const tot = s.total_events || 1;
  bar('retry',     s.retries_scheduled, tot);
  bar('collect',   s.upi_collects,      tot);
  bar('renewal',   s.renewals_sent,     tot);
  bar('whatsapp',  s.whatsapp_sent,     tot);
  bar('escalation',s.escalations,       tot);
}

function bar(key, val, tot) {
  const pct = Math.min(100, Math.round(val / tot * 100));
  const f = document.getElementById('bd-' + key);
  const c = document.getElementById('cnt-' + key);
  if (f) f.style.width = pct + '%';
  if (c) c.textContent = val;
}

// ── Table ─────────────────────────────────────────────────────────────────────
function rebuildTable() {
  const tbody = document.getElementById('events-tbody');
  tbody.innerHTML = '';
  events.forEach(ev => tbody.appendChild(makeRow(ev)));
}

function prependRow(ev) {
  const tbody = document.getElementById('events-tbody');
  const empty = document.getElementById('empty-row');
  if (empty) empty.remove();

  const row = makeRow(ev);
  row.classList.add('row-in');
  tbody.insertBefore(row, tbody.firstChild);
}

function makeRow(ev) {
  const tr = document.createElement('tr');
  tr.onclick = () => openDrawer(ev);

  const sev  = (ev.severity || 'medium').toLowerCase();
  const code = (ev.failure_code || '').toUpperCase();

  const ivHtml = (ev.interventions || []).map(iv => {
    let cls = '';
    if (iv === 'escalation')    cls = ' esc';
    if (iv === 'mandate_renewal') cls = ' renewal';
    return `<span class="iv-tag${cls}">${ivName(iv)}</span>`;
  }).join('') || '<span class="muted" style="font-size:11px">—</span>';

  tr.innerHTML = `
    <td class="muted" style="font-variant-numeric:tabular-nums;font-size:12px">${ev.timestamp || ''}</td>
    <td><span class="code-tag">${code}</span></td>
    <td class="mono">${ev.customer_vpa || ''}</td>
    <td class="muted">${ev.bank || ''}</td>
    <td class="fw6">${fmtInr(ev.amount)}</td>
    <td><span class="sev-badge sev-${sev}">${cap(sev)}</span></td>
    <td>${ivHtml}</td>
    <td>${ev.success
      ? '<span class="status-ok">✓ Recovered</span>'
      : '<span class="status-err">✗ Failed</span>'}</td>`;
  return tr;
}

// ── Drawer ────────────────────────────────────────────────────────────────────
function openDrawer(ev) {
  const sev = (ev.severity || 'medium').toLowerCase();

  document.getElementById('drawer-title').textContent =
    ev.scenario_name || `${ev.failure_code} Event`;
  document.getElementById('drawer-sub').textContent =
    ev.failure_reason || '';

  const ivHtml = (ev.interventions || []).map((iv, i) => `
    <div class="iv-block">
      <div class="iv-block-type">${ivName(iv)}</div>
      <div class="iv-block-msg">${ev.intervention_msgs?.[i] || ''}</div>
    </div>`).join('') || '<p class="muted" style="font-size:12px">None</p>';

  document.getElementById('drawer-body').innerHTML = `
    <div class="dl-section">
      <div class="dl-section-title">Event</div>
      ${row('Event Type',    ev.event_type)}
      ${row('Failure Code',  `<span class="code-tag">${ev.failure_code}</span>`)}
      ${row('Reason',        ev.failure_reason)}
      ${row('Time',          ev.timestamp)}
      ${row('Event ID',      `<span class="mono" style="font-size:11px">${ev.id}</span>`)}
    </div>
    <div class="dl-section">
      <div class="dl-section-title">Customer</div>
      ${row('Customer ID',   ev.customer_id)}
      ${row('VPA',           `<span class="mono">${ev.customer_vpa}</span>`)}
      ${row('Bank',          ev.bank)}
      ${row('Amount',        `<strong>${fmtInr(ev.amount)}</strong>`)}
      ${row('Severity',      `<span class="sev-badge sev-${sev}">${cap(sev)}</span>`)}
    </div>
    <div class="dl-section">
      <div class="dl-section-title">Interventions</div>
      ${ivHtml}
    </div>
    ${ev.scheduled_at ? `
    <div class="dl-section">
      <div class="dl-section-title">Retry Schedule</div>
      ${row('Scheduled At', ev.scheduled_at)}
    </div>` : ''}
    ${ev.action_url ? `
    <div class="dl-section">
      <div class="dl-section-title">Action URL</div>
      <code style="font-size:11px;word-break:break-all;color:var(--blue)">${ev.action_url}</code>
    </div>` : ''}`;

  document.getElementById('drawer').classList.add('open');
  document.getElementById('overlay').classList.add('open');
}

function closeDrawer() {
  document.getElementById('drawer').classList.remove('open');
  document.getElementById('overlay').classList.remove('open');
}

function row(label, value) {
  return `<div class="dl-row">
    <span class="dl-label">${label}</span>
    <span class="dl-value">${value}</span>
  </div>`;
}

// ── Simulator ─────────────────────────────────────────────────────────────────
async function runScenario(key) {
  const btn = document.getElementById('sc-' + key);
  if (btn) btn.classList.add('loading');

  try {
    const res = await fetch(`/api/simulate/${key}`, { method: 'POST' });
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    toast(`${data.scenario_name || key.toUpperCase()} processed`, 'ok');
  } catch (e) {
    toast(e.message, 'err');
  } finally {
    if (btn) btn.classList.remove('loading');
  }
}

async function runAll() {
  const allBtns = document.querySelectorAll('.sc-btn');
  const runBtn  = document.getElementById('run-all-btn');
  allBtns.forEach(b => b.classList.add('loading'));
  if (runBtn) { runBtn.textContent = 'Running…'; runBtn.disabled = true; }

  try {
    const res = await fetch('/api/simulate/all', { method: 'POST' });
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    toast(`${data.processed} scenarios processed`, 'ok');
  } catch (e) {
    toast(e.message, 'err');
  } finally {
    allBtns.forEach(b => b.classList.remove('loading'));
    if (runBtn) { runBtn.textContent = '▶\u00A0 Run All Scenarios'; runBtn.disabled = false; }
  }
}

async function resetAll() {
  await fetch('/api/reset', { method: 'POST' });
  events = [];
  const tbody = document.getElementById('events-tbody');
  tbody.innerHTML = `
    <tr id="empty-row">
      <td colspan="8">
        <div class="empty-state">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
            <path d="M9 12l2 2 4-4M7.835 4.697a3.42 3.42 0 001.946-.806 3.42 3.42 0 014.438 0 3.42 3.42 0 001.946.806 3.42 3.42 0 013.138 3.138 3.42 3.42 0 00.806 1.946 3.42 3.42 0 010 4.438 3.42 3.42 0 00-.806 1.946 3.42 3.42 0 01-3.138 3.138 3.42 3.42 0 00-1.946.806 3.42 3.42 0 01-4.438 0 3.42 3.42 0 00-1.946-.806 3.42 3.42 0 01-3.138-3.138 3.42 3.42 0 00-.806-1.946 3.42 3.42 0 010-4.438 3.42 3.42 0 00.806-1.946 3.42 3.42 0 013.138-3.138z"/>
          </svg>
          <p>No recovery events yet. Trigger a scenario →</p>
        </div>
      </td>
    </tr>`;
  syncStats({ total_events:0, total_recovered:0, successful:0, failed:0,
    success_rate:0, retries_scheduled:0, renewals_sent:0,
    escalations:0, whatsapp_sent:0, upi_collects:0 });
  toast('Dashboard cleared', 'ok');
}

// ── Toast ─────────────────────────────────────────────────────────────────────
function toast(msg, type = 'ok') {
  const root = document.getElementById('toast-root');
  const el   = document.createElement('div');
  el.className = `toast ${type}`;
  el.textContent = msg;
  root.appendChild(el);
  setTimeout(() => el.remove(), 3200);
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function fmtInr(n) {
  if (n === undefined || n === null) return '—';
  return '₹' + Number(n).toLocaleString('en-IN', { maximumFractionDigits: 0 });
}

function set(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val;
}

function cap(s) { return s ? s.charAt(0).toUpperCase() + s.slice(1) : ''; }

function ivName(iv) {
  return {
    smart_retry:      'Retry',
    upi_collect:      'UPI Collect',
    mandate_renewal:  'Renewal',
    whatsapp_nudge:   'WhatsApp',
    escalation:       'Escalated',
  }[iv] || iv;
}
