'use strict';

// â”€â”€ State â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
let events = [];
let sse    = null;

// â”€â”€ Theme â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
function initTheme() {
  const saved = localStorage.getItem('riq-theme') || 'light';
  document.documentElement.setAttribute('data-theme', saved);
  const icon = document.getElementById('theme-icon');
  if (icon) icon.textContent = saved === 'dark' ? '☀️' : '🌙';
}

function toggleTheme() {
  const root = document.documentElement;
  const isDark = root.getAttribute('data-theme') === 'dark';
  const next   = isDark ? 'light' : 'dark';
  root.setAttribute('data-theme', next);
  localStorage.setItem('riq-theme', next);
  const icon = document.getElementById('theme-icon');
  if (icon) icon.textContent = next === 'dark' ? '☀️' : '🌙';
}

// â”€â”€ Toast notifications â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
function toast(msg, type = 'blue') {
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  el.textContent = msg;
  document.body.appendChild(el);
  requestAnimationFrame(() => el.classList.add('toast-visible'));
  setTimeout(() => {
    el.classList.remove('toast-visible');
    setTimeout(() => el.remove(), 400);
  }, 3500);
}

// â”€â”€ Boot â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
document.addEventListener('DOMContentLoaded', () => {
  initTheme();
  loadInitial();
  connectSSE();
  loadModules();                         // load P2P, Checkout, B2B
  setInterval(loadModules, 15_000);      // refresh every 15s
});

// â”€â”€ Initial load â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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

// â”€â”€ SSE â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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

  // Refresh all module panels when the backend signals data has changed
  // (emitted after every scenario run — without this listener the panels
  //  only update on the 15-second polling interval)
  sse.addEventListener('modules_updated', () => { loadModules(); });

  sse.onerror = () => setTimeout(connectSSE, 3000);
}

// â”€â”€ Stats sync â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
function syncStats(s) {
  animCount('kpi-recovered',     s.total_recovered, true);
  set('kpi-events',              `${s.total_events} events processed`);
  set('kpi-rate',                s.total_events ? s.success_rate + '%' : '—');
  set('kpi-success-detail',      `${s.successful} handled · ${s.failed} failed`);
  animCount('kpi-retries',       s.retries_scheduled);
  animCount('kpi-renewals',      s.renewals_sent);
  animCount('kpi-escalations',   s.escalations);

  const tot = s.total_events || 1;
  bar('retry',     s.retries_scheduled, tot);
  bar('collect',   s.upi_collects,      tot);
  bar('renewal',   s.renewals_sent,     tot);
  bar('whatsapp',  s.whatsapp_sent,     tot);
  bar('escalation',s.escalations,       tot);
}

// Animate a KPI number rolling up to the new value
function animCount(id, target, isMoney = false) {
  const el = document.getElementById(id);
  if (!el) return;

  // Parse current displayed value
  const raw   = el.textContent.replace(/[^\d.]/g, '');
  const start = parseFloat(raw) || 0;
  if (start === target) return;           // no change

  // Trigger pop + glow
  el.classList.remove('pop');
  void el.offsetWidth;                    // reflow
  el.classList.add('pop');
  setTimeout(() => el.classList.remove('pop'), 450);

  const kpiCard = el.closest('.kpi');
  if (kpiCard) {
    kpiCard.classList.remove('updated');
    void kpiCard.offsetWidth;
    kpiCard.classList.add('updated');
    setTimeout(() => kpiCard.classList.remove('updated'), 900);
  }

  // Roll up counter
  const duration = 600;
  const startTime = performance.now();
  const easeOut = t => 1 - Math.pow(1 - t, 3);

  function tick(now) {
    const t = Math.min((now - startTime) / duration, 1);
    const cur = start + (target - start) * easeOut(t);
    el.textContent = isMoney ? fmtInr(Math.round(cur)) : Math.round(cur);
    if (t < 1) requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}

function bar(key, val, tot) {
  const pct = Math.min(100, Math.round(val / tot * 100));
  const f = document.getElementById('bd-' + key);
  const c = document.getElementById('cnt-' + key);
  if (f) f.style.width = pct + '%';
  if (c) c.textContent = val;
}

// â”€â”€ Table â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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
  row.classList.add('row-in', 'row-flash');
  setTimeout(() => { row.classList.remove('row-in', 'row-flash'); }, 1400);
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

  // Quick-create P2P action (stop click bubbling to row drawer)
  const actHtml = `<button class="btn-act btn-blue" title="Create P2P from this event"
    onclick="event.stopPropagation();quickCreateP2P('${ev.customer_vpa}',${ev.amount},'${ev.bank}','${ev.failure_code}')"
  >+P2P</button>`;

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
      : '<span class="status-err">✗ Failed</span>'}</td>
    <td>${actHtml}</td>`;
  return tr;
}

// â”€â”€ Drawer â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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

// â”€â”€ Simulator â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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
      <td colspan="9">
        <div class="empty-state">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
            <path d="M9 12l2 2 4-4M7.835 4.697a3.42 3.42 0 001.946-.806 3.42 3.42 0 014.438 0 3.42 3.42 0 001.946.806 3.42 3.42 0 013.138 3.138 3.42 3.42 0 00.806 1.946 3.42 3.42 0 010 4.438 3.42 3.42 0 00-.806 1.946 3.42 3.42 0 01-3.138 3.138 3.42 3.42 0 00-1.946.806 3.42 3.42 0 01-4.438 0 3.42 3.42 0 00-1.946-.806 3.42 3.42 0 01-3.138-3.138 3.42 3.42 0 00-.806-1.946 3.42 3.42 0 010-4.438 3.42 3.42 0 00.806-1.946 3.42 3.42 0 013.138-3.138z"/>
          </svg>
          <p>No recovery events yet. Trigger a scenario â†’</p>
        </div>
      </td>
    </tr>`;
  syncStats({ total_events:0, total_recovered:0, successful:0, failed:0,
    success_rate:0, retries_scheduled:0, renewals_sent:0,
    escalations:0, whatsapp_sent:0, upi_collects:0 });
  toast('Dashboard cleared', 'ok');
}

// â”€â”€ Seed Demo â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
async function seedDemo() {
  const btn = document.getElementById('seed-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Seeding…'; }
  try {
    const res = await fetch('/api/seed', { method: 'POST' });
    if (!res.ok) throw new Error(await res.text());
    toast('🌱 Demo data seeded successfully', 'ok');
    await loadModules();
  } catch (e) {
    toast('Seed failed: ' + e.message, 'err');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '🌱 Seed Demo'; }
  }
}

// â”€â”€ Hard Reset â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
async function hardReset() {
  const btn = document.getElementById('hard-reset-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Resetting…'; }
  try {
    await fetch('/api/reset', { method: 'POST' });
    events = [];
    const tbody = document.getElementById('events-tbody');
    tbody.innerHTML = `
      <tr id="empty-row"><td colspan="9">
        <div class="empty-state"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
          <path d="M9 12l2 2 4-4M7.835 4.697a3.42 3.42 0 001.946-.806 3.42 3.42 0 014.438 0 3.42 3.42 0 001.946.806 3.42 3.42 0 013.138 3.138 3.42 3.42 0 00.806 1.946 3.42 3.42 0 010 4.438 3.42 3.42 0 00-.806 1.946 3.42 3.42 0 01-3.138 3.138 3.42 3.42 0 00-1.946.806 3.42 3.42 0 01-4.438 0 3.42 3.42 0 00-1.946-.806 3.42 3.42 0 01-3.138-3.138 3.42 3.42 0 00-.806-1.946 3.42 3.42 0 010-4.438 3.42 3.42 0 00.806-1.946 3.42 3.42 0 013.138-3.138z"/>
        </svg><p>No recovery events yet. Trigger a scenario â†’</p></div>
      </td></tr>`;
    syncStats({ total_events:0, total_recovered:0, successful:0, failed:0,
      success_rate:0, retries_scheduled:0, renewals_sent:0,
      escalations:0, whatsapp_sent:0, upi_collects:0 });
    await loadModules();
    toast('💥 All state cleared — fresh start', 'ok');
  } catch (e) {
    toast('Reset failed: ' + e.message, 'err');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '💥 Hard Reset'; }
  }
}

// â”€â”€ Toast â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
function toast(msg, type = 'ok') {
  const root = document.getElementById('toast-root');
  const el   = document.createElement('div');
  el.className = `toast ${type}`;
  el.textContent = msg;
  root.appendChild(el);
  setTimeout(() => el.remove(), 3200);
}

// â”€â”€ Helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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

// â”€â”€ Custom Scenario Modal â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
let _jsonPayload = null;

function openCreateModal() {
  document.getElementById('create-modal').classList.add('open');
  document.getElementById('create-backdrop').classList.add('open');
  document.body.style.overflow = 'hidden';
}

function closeCreateModal() {
  document.getElementById('create-modal').classList.remove('open');
  document.getElementById('create-backdrop').classList.remove('open');
  document.body.style.overflow = '';
}

// Close on Escape key
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') closeCreateModal();
});

function switchTab(tab) {
  ['form', 'json'].forEach(t => {
    document.getElementById(`mpanel-${t}`).classList.toggle('hidden', t !== tab);
    document.getElementById(`mtab-${t}`).classList.toggle('active', t === tab);
  });
}

// â”€â”€ Form submit â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
async function submitCustomForm(e) {
  e.preventDefault();
  const btn = document.getElementById('cf-submit');
  btn.disabled = true;
  btn.textContent = 'Running…';

  const payload = {
    scenario_name: document.getElementById('cf-name').value,
    failure_code:  document.getElementById('cf-code').value,
    vpa:           document.getElementById('cf-vpa').value,
    bank:          document.getElementById('cf-bank').value,
    amount:        parseFloat(document.getElementById('cf-amount').value),
    mandate_state: document.getElementById('cf-state').value,
    retry_attempt: parseInt(document.getElementById('cf-retry').value, 10),
  };

  try {
    const res = await fetch('/api/custom', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(payload),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || res.statusText);
    }
    const data = await res.json();
    toast(`✓ "${payload.scenario_name}" processed`, 'ok');
    closeCreateModal();
  } catch (err) {
    toast(`Error: ${err.message}`, 'err');
  } finally {
    btn.disabled = false;
    btn.innerHTML = '&#9654;&nbsp; Run Scenario';
  }
}

// â”€â”€ JSON Upload / Drop zone â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
function dzDragover(e) {
  e.preventDefault();
  document.getElementById('drop-zone').classList.add('drag-over');
}

function dzDragleave(e) {
  document.getElementById('drop-zone').classList.remove('drag-over');
}

function dzDrop(e) {
  e.preventDefault();
  document.getElementById('drop-zone').classList.remove('drag-over');
  const file = e.dataTransfer.files[0];
  if (file) loadJsonFile(file);
}

function dzFileSelect(e) {
  const file = e.target.files[0];
  if (file) loadJsonFile(file);
}

function loadJsonFile(file) {
  const reader = new FileReader();
  reader.onload = ev => {
    try {
      const parsed = JSON.parse(ev.target.result);
      _jsonPayload = parsed;
      document.getElementById('json-filename').textContent = file.name;
      document.getElementById('json-pre').textContent = JSON.stringify(parsed, null, 2);
      document.getElementById('json-preview').classList.remove('hidden');
      document.getElementById('json-submit').disabled = false;
    } catch {
      toast('Invalid JSON file', 'err');
    }
  };
  reader.readAsText(file);
}

function clearJsonFile() {
  _jsonPayload = null;
  document.getElementById('json-preview').classList.add('hidden');
  document.getElementById('json-submit').disabled = true;
  document.getElementById('json-file-input').value = '';
}

async function submitJsonUpload() {
  if (!_jsonPayload) return;
  const btn = document.getElementById('json-submit');
  btn.disabled = true;
  btn.textContent = 'Running…';

  try {
    const res = await fetch('/api/custom', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(_jsonPayload),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || res.statusText);
    }
    toast(`✓ JSON scenario processed`, 'ok');
    closeCreateModal();
    clearJsonFile();
  } catch (err) {
    toast(`Error: ${err.message}`, 'err');
  } finally {
    btn.disabled = false;
    btn.innerHTML = '&#9654;&nbsp; Run JSON Scenario';
  }
}

// â”€â”€ Auto Demo â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
// Cycles through all scenarios automatically — great for live hackathon demos
const AUTO_DEMO_KEYS     = ['u30', 'bt01', 'tm', 'u69', 'bt02', 'u13'];
const AUTO_DEMO_INTERVAL = 3500; // ms between events

let _autoDemoTimer  = null;
let _autoDemoIdx    = 0;
let _autoDemoActive = false;

function toggleAutoDemo() {
  const btn = document.getElementById('auto-demo-btn');
  if (_autoDemoActive) {
    // Stop
    clearTimeout(_autoDemoTimer);
    _autoDemoTimer  = null;
    _autoDemoActive = false;
    btn.innerHTML   = '&#9654; Auto Demo';
    btn.classList.remove('running');
    toast('Auto demo stopped', 'ok');
  } else {
    // Start
    _autoDemoActive = true;
    _autoDemoIdx    = 0;
    btn.innerHTML   = '&#9646;&#9646; Stop Demo';
    btn.classList.add('running');
    toast('Auto demo started — scenarios firing every 3.5s', 'ok');
    fireNextAutoDemo();
  }
}

async function fireNextAutoDemo() {
  if (!_autoDemoActive) return;

  const key = AUTO_DEMO_KEYS[_autoDemoIdx % AUTO_DEMO_KEYS.length];
  _autoDemoIdx++;

  try {
    await fetch(`/api/simulate/${key}`, { method: 'POST' });
  } catch (e) { /* ignore — SSE will handle display */ }

  if (_autoDemoActive) {
    _autoDemoTimer = setTimeout(fireNextAutoDemo, AUTO_DEMO_INTERVAL);
  }
}

// â”€â”€ Module Panels: Promise-to-Pay, Checkout, B2B â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

async function loadModules() {
  await Promise.allSettled([loadP2P(), loadCheckout(), loadB2B(), loadLedger(), loadROI()]);
}

// â”€â”€ Promise-to-Pay â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

async function loadP2P() {
  try {
    const res  = await fetch('/api/promises');
    const data = await res.json();
    renderP2PStats(data.stats);
    renderP2PTable(data.promises);
  } catch (e) { console.warn('P2P load failed', e); }
}

function renderP2PStats(s) {
  set('p2p-pending',   `${s.pending}   pending`);
  set('p2p-fulfilled', `${s.fulfilled} fulfilled`);
  set('p2p-broken',    `${s.broken}    broken`);
}

function renderP2PTable(promises) {
  const tbody = document.getElementById('p2p-tbody');
  if (!tbody) return;
  if (!promises.length) {
    tbody.innerHTML = '<tr><td colspan="8"><div class="empty-state"><p>No promises recorded yet — run a scenario or create one via the form above</p></div></td></tr>';
    return;
  }
  tbody.innerHTML = promises.map(p => {
    const deadline  = new Date(p.deadline).toLocaleString('en-IN', {
      day:'2-digit', month:'short', hour:'2-digit', minute:'2-digit', hour12:true
    });
    const overdue   = p.is_overdue ? ' âš ï¸' : '';
    const statusCls = `p2p-${p.status}`;
    const isPending = p.status === 'pending';
    const btns = isPending
      ? `<div class="row-actions">
           <button class="btn-act btn-green" onclick="p2pFulfil('${p.promise_id}',this)">✓ Fulfilled</button>
           <button class="btn-act btn-red"   onclick="p2pBreak('${p.promise_id}',this)">✗ Broken</button>
         </div>`
      : `<span class="muted" style="font-size:11px">${p.status.toUpperCase()}</span>`;
    return `<tr id="p2p-row-${p.promise_id}">
      <td class="mono fw6">${p.promise_id}</td>
      <td>${p.vpa}</td>
      <td class="fw6">${fmtInr(p.amount)}</td>
      <td class="${p.is_overdue ? 'status-err' : 'muted'}">${deadline}${overdue}</td>
      <td class="muted">${p.channel}</td>
      <td class="${statusCls}">${p.status.toUpperCase()}</td>
      <td class="muted" style="max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${p.notes}">${p.notes || '—'}</td>
      <td>${btns}</td>
    </tr>`;
  }).join('');
}

async function p2pFulfil(id, btn) {
  btn.disabled = true; btn.textContent = '…';
  try {
    await fetch(`/api/promises/${id}/fulfill`, {method:'POST'});
    toast(`✓ Promise ${id} marked FULFILLED — ledger updated`, 'green');
    await loadP2P(); await loadLedger(); await loadROI();
  } catch(e) { toast('Failed: ' + e.message, 'red'); btn.disabled = false; btn.textContent = '✓ Fulfilled'; }
}

async function p2pBreak(id, btn) {
  btn.disabled = true; btn.textContent = '…';
  try {
    await fetch(`/api/promises/${id}/break`, {method:'POST'});
    toast(`✗ Promise ${id} marked BROKEN — escalation logged`, 'red');
    await loadP2P(); await loadLedger(); await loadROI();
  } catch(e) { toast('Failed: ' + e.message, 'red'); btn.disabled = false; btn.textContent = '✗ Broken'; }
}

// â”€â”€ Checkout Drop-off â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

async function loadCheckout() {
  try {
    const res  = await fetch('/api/checkout');
    const data = await res.json();
    renderCheckoutStats(data.stats);
    renderCheckoutTable(data.sessions);
  } catch (e) { console.warn('Checkout load failed', e); }
}

function renderCheckoutStats(s) {
  set('chk-total',     `${s.total_sessions} sessions`);
  set('chk-recovered', `${s.recovered} recovered`);
  set('chk-nudges',    `${s.nudges_sent} nudges sent`);
}

const REASON_LABELS = {
  payment_page_exit:    'Payment exit',
  otp_timeout:          'OTP timeout',
  bank_error_exit:      'Bank error',
  upi_intent_abandoned: 'UPI abandoned',
  address_form_exit:    'Address exit',
  session_expired:      'Session expired',
  unknown:              'Unknown',
};

const CHK_STATUS_CLS = {
  open:      'muted',
  contacted: 'p2p-pending',
  recovered: 'status-ok',
  expired:   'status-err',
};

function renderCheckoutTable(sessions) {
  const tbody = document.getElementById('chk-tbody');
  if (!tbody) return;
  if (!sessions.length) {
    tbody.innerHTML = '<tr><td colspan="7"><div class="empty-state"><p>No drop-off sessions yet</p></div></td></tr>';
    return;
  }
  tbody.innerHTML = sessions.map(s => {
    const reason = REASON_LABELS[s.drop_off_reason] || s.drop_off_reason;
    const stCls  = CHK_STATUS_CLS[s.status] || 'muted';
    const isOpen = s.status === 'open' || s.status === 'contacted';
    const btn    = isOpen
      ? `<button class="btn-act btn-green" onclick="chkRecover('${s.session_id}',this)">✓ Recovered</button>`
      : `<span class="muted" style="font-size:11px">${s.status.toUpperCase()}</span>`;
    return `<tr id="chk-row-${s.session_id}">
      <td class="mono fw6">${s.session_id}</td>
      <td>${s.customer_vpa}</td>
      <td class="fw6">${fmtInr(s.cart_amount)}</td>
      <td><span class="reason-badge">${reason}</span></td>
      <td class="${stCls}">${s.status.toUpperCase()}</td>
      <td><span class="chk-msg" title="${s.recovery_message}">${s.recovery_message || '—'}</span></td>
      <td>${btn}</td>
    </tr>`;
  }).join('');
}

async function chkRecover(id, btn) {
  btn.disabled = true; btn.textContent = '…';
  try {
    await fetch(`/api/checkout/${id}/recover`, {method:'POST'});
    toast(`✓ Checkout ${id} marked RECOVERED — payment confirmed, ledger updated`, 'green');
    await loadCheckout(); await loadLedger(); await loadROI();
  } catch(e) { toast('Failed: ' + e.message, 'red'); btn.disabled = false; btn.textContent = '✓ Recovered'; }
}

// â”€â”€ B2B Receivables â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

async function loadB2B() {
  try {
    const res  = await fetch('/api/b2b');
    const data = await res.json();
    renderB2BStats(data.stats);
    renderAgingBuckets(data.stats.buckets);
    renderB2BTable(data.receivables);
  } catch (e) { console.warn('B2B load failed', e); }
}

function renderB2BStats(s) {
  set('b2b-total',       `${s.total} invoices`);
  set('b2b-outstanding', fmtInr(s.total_outstanding));
  set('b2b-escalated',   `${s.escalated} escalated`);
}

function renderAgingBuckets(buckets) {
  if (!buckets) return;
  const maxAmt = Math.max(...Object.values(buckets).map(b => b.amount), 1);
  const maps   = {
    '0-30d':  { barId: 'aging-bar-0-30',  metaId: 'aging-meta-0-30' },
    '31-60d': { barId: 'aging-bar-31-60', metaId: 'aging-meta-31-60' },
    '61-90d': { barId: 'aging-bar-61-90', metaId: 'aging-meta-61-90' },
    '90d+':   { barId: 'aging-bar-90plus',metaId: 'aging-meta-90plus' },
  };
  for (const [key, {barId, metaId}] of Object.entries(maps)) {
    const b    = buckets[key] || {count:0, amount:0};
    const pct  = Math.round(b.amount / maxAmt * 100);
    const bar  = document.getElementById(barId);
    const meta = document.getElementById(metaId);
    if (bar)  bar.style.width  = pct + '%';
    if (meta) meta.textContent = `${b.count} invoice${b.count !== 1 ? 's' : ''} · ${fmtInr(b.amount)}`;
  }
}

const BUCKET_CLS = {
  '0-30d':  'bucket-0-30',
  '31-60d': 'bucket-31-60',
  '61-90d': 'bucket-61-90',
  '90d+':   'bucket-90plus',
};

const B2B_STATUS_CLS = {
  active:    'muted',
  promised:  'p2p-pending',
  escalated: 'status-err',
  settled:   'status-ok',
  written_off: 'muted',
};

function renderB2BTable(receivables) {
  const tbody = document.getElementById('b2b-tbody');
  if (!tbody) return;
  if (!receivables.length) {
    tbody.innerHTML = '<tr><td colspan="10"><div class="empty-state"><p>No receivables loaded</p></div></td></tr>';
    return;
  }
  tbody.innerHTML = receivables.map(r => {
    const bucketCls = BUCKET_CLS[r.aging_bucket] || '';
    const tierCls   = `tier-${r.debtor_tier.toLowerCase()}`;
    const stCls     = B2B_STATUS_CLS[r.status] || 'muted';
    const lastAct   = r.actions && r.actions.length
      ? `<span class="muted" title="${r.actions[r.actions.length-1].message}">${r.actions[r.actions.length-1].channel}</span>`
      : '<span class="muted">—</span>';
    const canAct = r.status !== 'settled' && r.status !== 'written_off';
    const btns   = canAct
      ? `<div class="row-actions">
           <button class="btn-act btn-blue"  onclick="b2bChase('${r.receivable_id}',this)">↺ Chase</button>
           <button class="btn-act btn-green" onclick="b2bSettle('${r.receivable_id}','${r.debtor_name}',${r.amount},this)">₹ Settle</button>
         </div>`
      : `<span class="muted" style="font-size:11px">${r.status.toUpperCase()}</span>`;
    return `<tr id="b2b-row-${r.receivable_id}">
      <td class="mono fw6">${r.invoice_number}</td>
      <td>${r.debtor_name}</td>
      <td class="fw6">${fmtInr(r.amount)}</td>
      <td class="${r.days_overdue > 60 ? 'status-err' : r.days_overdue > 30 ? 'p2p-pending' : 'muted'}">${r.days_overdue}d</td>
      <td><span class="bucket-badge ${bucketCls}">${r.aging_bucket}</span></td>
      <td class="${tierCls}">Tier ${r.debtor_tier}</td>
      <td class="muted">${fmtInr(r.interest_accrued)}</td>
      <td class="${stCls}">${r.status.toUpperCase()}</td>
      <td>${lastAct}</td>
      <td>${btns}</td>
    </tr>`;
  }).join('');
}

async function b2bChase(id, btn) {
  btn.disabled = true; btn.textContent = '…';
  try {
    const res  = await fetch(`/api/b2b/receivables/${id}/chase`, {method:'POST'});
    const data = await res.json();
    toast(`↺ Chase dispatched via ${data.channel || 'channel'} — message sent & ledger logged`, 'blue');
    await loadB2B(); await loadLedger(); await loadROI();
  } catch(e) { toast('Chase failed: ' + e.message, 'red'); }
  finally     { btn.disabled = false; btn.textContent = '↺ Chase'; }
}

async function b2bSettle(id, name, amount, btn) {
  // Open proper settle dialog instead of browser prompt()
  openSettleDialog(id, name, amount);
}

// â”€â”€ Recovery Ledger â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

const CHANNEL_UNIT_COSTS = {
  whatsapp: 0.50, sms: 0.15, ivr: 1.50, email: 0.05,
  smart_retry: 0.00, upi_collect: 0.25, mandate_renewal: 0.50,
  escalation: 25.00, legal: 500.00, ar_specialist: 150.00,
};

async function loadLedger() {
  try {
    const res  = await fetch('/api/ledger?limit=30');
    const data = await res.json();
    const o    = data.overall_roi;
    set('ldg-entries',  `${o.total_entries} entries`);
    set('ldg-avg-conf', `conf: ${Math.round(o.avg_confidence * 100)}%`);
    renderLedgerTable(data.entries);
  } catch (e) { console.warn('Ledger load failed', e); }
}

function confPips(conf) {
  const total  = 5;
  const filled = Math.round(conf * total);
  const cls    = conf >= 0.75 ? 'filled-high' : conf >= 0.50 ? 'filled-med' : 'filled-low';
  const pips   = Array.from({length: total}, (_, i) =>
    `<div class="conf-pip ${i < filled ? cls : ''}"></div>`
  ).join('');
  return `<div class="conf-bar"><div class="conf-pips">${pips}</div><span class="conf-num">${Math.round(conf * 100)}%</span></div>`;
}

const LEDGER_OUTCOME_CLS = {
  success: 'outcome-success',
  failure: 'outcome-failure',
  pending: 'outcome-pending',
  skipped: 'outcome-skipped',
};

function renderLedgerTable(entries) {
  const tbody = document.getElementById('ledger-tbody');
  if (!tbody) return;
  if (!entries.length) {
    tbody.innerHTML = '<tr><td colspan="7"><div class="empty-state"><p>No ledger entries yet — run a scenario to populate.</p></div></td></tr>';
    return;
  }
  tbody.innerHTML = entries.map(e => {
    const typeCls    = `ledger-type type-${e.event_type}`;
    const outcomeCls = LEDGER_OUTCOME_CLS[e.outcome] || 'muted';
    return `<tr>
      <td class="mono muted">${e.ts}</td>
      <td><span class="${typeCls}">${e.event_type}</span></td>
      <td class="muted">${e.vpa}</td>
      <td class="fw6">${fmtInr(e.amount)}</td>
      <td>${confPips(e.confidence)}</td>
      <td><span class="ledger-reasoning" title="${e.reasoning}">${e.reasoning}</span></td>
      <td class="${outcomeCls}">${e.outcome.toUpperCase()}</td>
    </tr>`;
  }).join('');
}

// â”€â”€ Recovery ROI â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

async function loadROI() {
  try {
    const res  = await fetch('/api/roi');
    const data = await res.json();
    const o    = data.overall;
    set('roi-recovered', fmtInr(o.total_recovered));
    set('roi-costs',     fmtInr(o.total_cost));
    const netEl = document.getElementById('roi-netval');
    if (netEl) {
      netEl.textContent = fmtInr(o.net_roi);
      netEl.className   = 'roi-value ' + (o.net_roi >= 0 ? 'green' : 'red');
    }
    set('roi-stake', fmtInr(o.total_at_stake));
    set('roi-net',   fmtInr(o.net_roi) + ' net');
    set('roi-rate',  o.recovery_rate_pct + '% rate');
    renderROITable(data.by_channel);
  } catch (e) { console.warn('ROI load failed', e); }
}

function renderROITable(byChannel) {
  const tbody = document.getElementById('roi-tbody');
  if (!tbody) return;
  const rows = Object.entries(byChannel);
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="7"><div class="empty-state"><p>No ROI data yet</p></div></td></tr>';
    return;
  }
  rows.sort((a, b) => b[1].net_roi - a[1].net_roi);
  tbody.innerHTML = rows.map(([ch, s]) => {
    const unitCost = CHANNEL_UNIT_COSTS[ch] ?? 0;
    const roiCls   = s.net_roi >= 0 ? 'status-ok fw6' : 'status-err fw6';
    return `<tr>
      <td class="fw6" style="text-transform:capitalize">${ch.replace(/_/g,' ')}</td>
      <td class="muted">${s.count}</td>
      <td class="muted">&#8377;${unitCost.toFixed(2)}</td>
      <td class="muted">${fmtInr(s.total_cost)}</td>
      <td class="fw6 status-ok">${fmtInr(s.total_recovered)}</td>
      <td class="${roiCls}">${s.net_roi >= 0 ? '+' : ''}${fmtInr(s.net_roi)}</td>
      <td>${confPips(s.avg_confidence)}</td>
    </tr>`;
  }).join('');
}

function openP2PForm() {
  document.getElementById('p2p-form').classList.remove('hidden');
  document.getElementById('p2p-vpa').focus();
}
function closeP2PForm() {
  document.getElementById('p2p-form').classList.add('hidden');
}
async function submitP2PForm() {
  const vpa   = document.getElementById('p2p-vpa').value.trim();
  const amt   = parseFloat(document.getElementById('p2p-amount').value);
  const bank  = document.getElementById('p2p-bank').value;
  const code  = document.getElementById('p2p-code').value;
  const hours = parseFloat(document.getElementById('p2p-hours').value) || 48;
  const chan  = document.getElementById('p2p-channel').value;
  const notes = document.getElementById('p2p-notes').value.trim();
  if (!vpa || !amt) { toast('VPA and amount are required', 'err'); return; }
  const btn = document.getElementById('p2p-submit');
  btn.disabled = true; btn.textContent = 'Creating…';
  try {
    const res = await fetch('/api/promises', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ vpa, amount: amt, bank, failure_code: code,
                             deadline_hours: hours, channel: chan, notes }),
    });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
    toast(`✓ Promise created for ${vpa}`, 'ok');
    closeP2PForm();
    ['p2p-vpa','p2p-amount','p2p-notes'].forEach(id => document.getElementById(id).value = '');
    await loadP2P(); await loadLedger(); await loadROI();
  } catch (e) { toast('Failed: ' + e.message, 'err'); }
  finally { btn.disabled = false; btn.innerHTML = '&#9654;&nbsp; Create Promise'; }
}

// Quick-fill P2P from the Events table "+P2P" button
function openQuickP2P() {
  openP2PForm();
  document.getElementById('p2p-form').scrollIntoView({ behavior: 'smooth', block: 'center' });
}
async function quickCreateP2P(vpa, amount, bank, code) {
  document.getElementById('p2p-vpa').value    = vpa;
  document.getElementById('p2p-amount').value = amount;
  const bankSel = document.getElementById('p2p-bank');
  for (const opt of bankSel.options) {
    if (opt.value === bank || opt.text === bank) { bankSel.value = opt.value; break; }
  }
  const codeSel = document.getElementById('p2p-code');
  for (const opt of codeSel.options) {
    if (opt.value === code) { codeSel.value = code; break; }
  }
  openP2PForm();
  document.getElementById('p2p-form').scrollIntoView({ behavior: 'smooth', block: 'center' });
  toast(`Pre-filled P2P form for ${vpa} · ${fmtInr(amount)}`, 'ok');
}

// â”€â”€ Checkout Inline Form â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

function openCheckoutForm() {
  document.getElementById('chk-form').classList.remove('hidden');
  document.getElementById('chk-vpa').focus();
}
function closeCheckoutForm() {
  document.getElementById('chk-form').classList.add('hidden');
}
async function submitCheckoutForm() {
  const vpa      = document.getElementById('chk-vpa').value.trim();
  const phone    = document.getElementById('chk-phone').value.trim();
  const amt      = parseFloat(document.getElementById('chk-amount').value);
  const merchant = document.getElementById('chk-merchant').value.trim() || 'Demo Merchant';
  const reason   = document.getElementById('chk-reason').value;
  const lang     = document.getElementById('chk-lang').value;
  if (!vpa || !amt) { toast('VPA and cart amount are required', 'err'); return; }
  const btn = document.getElementById('chk-submit');
  btn.disabled = true; btn.textContent = 'Logging…';
  try {
    const res = await fetch('/api/checkout/drop', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ customer_vpa: vpa, customer_phone: phone,
                             cart_amount: amt, merchant,
                             drop_off_reason: reason, language: lang }),
    });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
    toast(`✓ Drop-off session logged for ${vpa}`, 'ok');
    closeCheckoutForm();
    ['chk-vpa','chk-phone','chk-amount'].forEach(id => document.getElementById(id).value = '');
    await loadCheckout(); await loadLedger(); await loadROI();
  } catch (e) { toast('Failed: ' + e.message, 'err'); }
  finally { btn.disabled = false; btn.innerHTML = '&#9654;&nbsp; Log Drop-off'; }
}

// â”€â”€ B2B Inline Form â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

function openB2BForm() {
  document.getElementById('b2b-form').classList.remove('hidden');
  document.getElementById('b2b-debtor').focus();
}
function closeB2BForm() {
  document.getElementById('b2b-form').classList.add('hidden');
}
async function submitB2BForm() {
  const name  = document.getElementById('b2b-debtor').value.trim();
  const vpa   = document.getElementById('b2b-vpa').value.trim();
  const inv   = document.getElementById('b2b-inv').value.trim();
  const amt   = parseFloat(document.getElementById('b2b-inv-amount').value);
  const due   = document.getElementById('b2b-due').value;
  const phone = document.getElementById('b2b-phone').value.trim();
  if (!name || !vpa || !inv || !amt || !due) {
    toast('All fields except phone are required', 'err'); return;
  }
  const btn = document.getElementById('b2b-submit');
  btn.disabled = true; btn.textContent = 'Adding…';
  try {
    const res = await fetch('/api/b2b/receivables', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ debtor_name: name, debtor_vpa: vpa,
                             debtor_phone: phone, invoice_number: inv,
                             amount: amt, due_date: due }),
    });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
    toast(`✓ Invoice ${inv} added for ${name}`, 'ok');
    closeB2BForm();
    ['b2b-debtor','b2b-vpa','b2b-inv','b2b-inv-amount','b2b-due','b2b-phone']
      .forEach(id => document.getElementById(id).value = '');
    await loadB2B(); await loadLedger(); await loadROI();
  } catch (e) { toast('Failed: ' + e.message, 'err'); }
  finally { btn.disabled = false; btn.innerHTML = '&#9654;&nbsp; Add Invoice'; }
}

// â”€â”€ Settle Dialog (replaces prompt()) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

function openSettleDialog(id, name, amount) {
  document.getElementById('settle-id').value     = id;
  document.getElementById('settle-full').value   = amount;
  document.getElementById('settle-amount').value = '';
  document.getElementById('settle-amount').placeholder = amount;
  document.getElementById('settle-heading').textContent = `Settle — ${name}`;
  document.getElementById('settle-sub').textContent =
    `Full invoice: ${fmtInr(amount)} · Leave blank for full settlement`;
  document.getElementById('settle-hint').textContent =
    `Leave blank to settle the full amount of ${fmtInr(amount)}`;
  document.getElementById('settle-modal').classList.add('open');
  document.getElementById('settle-backdrop').classList.add('open');
  setTimeout(() => document.getElementById('settle-amount').focus(), 50);
}
function closeSettleDialog() {
  document.getElementById('settle-modal').classList.remove('open');
  document.getElementById('settle-backdrop').classList.remove('open');
}
async function confirmSettle() {
  const id       = document.getElementById('settle-id').value;
  const full     = parseFloat(document.getElementById('settle-full').value);
  const input    = document.getElementById('settle-amount').value;
  const received = parseFloat(input) || full;
  const btn = document.getElementById('settle-submit');
  btn.disabled = true; btn.textContent = 'Settling…';
  try {
    await fetch(`/api/b2b/receivables/${id}/settle?amount_received=${received}`, { method: 'POST' });
    toast(`₹ Settled — ${fmtInr(received)} received, recovery logged`, 'green');
    closeSettleDialog();
    await loadB2B(); await loadLedger(); await loadROI();
  } catch (e) { toast('Settle failed: ' + e.message, 'red'); }
  finally { btn.disabled = false; btn.innerHTML = '&#8377;&nbsp; Confirm Settlement'; }
}

// Escape closes all inline forms + settle dialog
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    closeSettleDialog();
    closeP2PForm();
    closeCheckoutForm();
    closeB2BForm();
    closeCreateModal();
  }
});
