'use strict';

// ── XSS Sanitization Helper ──────────────────────────────────────────────────
function esc(str) {
  if (str === null || str === undefined) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// ── State ────────────────────────────────────────────────────────────────────
let events = [];
let sse    = null;

// ── Theme ────────────────────────────────────────────────────────────────────
function initTheme() {
  const saved = localStorage.getItem('riq-theme') || 'dark';
  document.documentElement.setAttribute('data-theme', saved);
  const icon = document.getElementById('theme-icon');
  if (icon) icon.textContent = saved === 'dark' ? '☀️' : '🌙';
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


// â”€â”€ Boot â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
document.addEventListener('DOMContentLoaded', () => {
  initTheme();
  loadInitial();
  connectSSE();
  loadModules();                         // load P2P, Checkout, B2B
  setInterval(loadModules, 15_000);      // refresh every 15s
  loadBenchmark();                       // load benchmark panel on boot
});

// ── Initial load & Data Loaders ──────────────────────────────────────────────
async function loadStats() {
  try {
    const s = await fetch('/api/stats').then(r => r.json());
    syncStats(s);
  } catch (e) { console.warn('Stats load failed:', e); }
}

async function loadEvents(btn) {
  if (btn) { btn.disabled = true; btn.textContent = '↻ Refreshing…'; btn.style.opacity = '0.75'; }
  try {
    const ev = await fetch('/api/events').then(r => r.json());
    if (ev) { events = ev; rebuildTable(); }
    if (btn) toast('⚡ Live events stream refreshed', 'ok');
  } catch (e) {
    console.warn('Events load failed:', e);
    if (btn) toast('Failed to refresh events', 'err');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '↻ Refresh'; btn.style.opacity = '1'; }
  }
}

async function loadInitial() {
  try {
    await Promise.all([loadStats(), loadEvents()]);
  } catch (e) { console.warn('init load failed:', e); }
}

// ── SSE ──────────────────────────────────────────────────────────────────────
function connectSSE() {
  if (sse) sse.close();
  sse = new EventSource('/api/stream');

  sse.addEventListener('recovery_event', e => {
    const ev = JSON.parse(e.data);
    const existingIdx = events.findIndex(x => x.id === ev.id);
    if (existingIdx >= 0) {
      events[existingIdx] = ev;
      const existingRow = document.getElementById('event-row-' + ev.id);
      if (existingRow) {
        existingRow.replaceWith(makeRow(ev));
      }
      return;
    }
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

// ── Stats sync ────────────────────────────────────────────────────────────────
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

// ── Customer Filtering ────────────────────────────────────────────────────────
let currentCustomerFilter = '';

function filterEventsByCustomer(query) {
  currentCustomerFilter = (query || '').trim().toLowerCase();
  const clearBtn = document.getElementById('cust-filter-clear');
  if (clearBtn) clearBtn.style.display = currentCustomerFilter ? 'inline-block' : 'none';
  const filterInput = document.getElementById('cust-filter-input');
  if (filterInput && filterInput.value !== query) filterInput.value = query;
  rebuildTable();
}

function clearCustomerFilter() {
  const filterInput = document.getElementById('cust-filter-input');
  if (filterInput) filterInput.value = '';
  filterEventsByCustomer('');
}

// ── Table ─────────────────────────────────────────────────────────────────────
function rebuildTable() {
  const tbody = document.getElementById('events-tbody');
  tbody.innerHTML = '';
  const seen = new Set();
  let filtered = events.filter(ev => {
    if (!ev.id) return true;
    if (seen.has(ev.id)) return false;
    seen.add(ev.id);
    return true;
  });

  if (currentCustomerFilter) {
    filtered = filtered.filter(ev => {
      const vpa = (ev.customer_vpa || '').toLowerCase();
      const cid = (ev.customer_id || '').toLowerCase();
      return vpa.includes(currentCustomerFilter) || cid.includes(currentCustomerFilter);
    });
  }

  if (filtered.length === 0) {
    const emptyTr = document.createElement('tr');
    emptyTr.id = 'empty-row';
    emptyTr.innerHTML = `
      <td colspan="11">
        <div class="empty-state">
          <p>${currentCustomerFilter ? `No events matching customer "${esc(currentCustomerFilter)}".` : 'No recovery events yet. Trigger a scenario →'}</p>
        </div>
      </td>`;
    tbody.appendChild(emptyTr);
    return;
  }

  filtered.forEach(ev => tbody.appendChild(makeRow(ev)));
}

function prependRow(ev) {
  const tbody = document.getElementById('events-tbody');
  const empty = document.getElementById('empty-row');
  if (empty) empty.remove();

  if (ev.id) {
    const existing = document.getElementById('event-row-' + ev.id);
    if (existing) {
      existing.replaceWith(makeRow(ev));
      return;
    }
  }

  const row = makeRow(ev);
  row.classList.add('row-in', 'row-flash');
  setTimeout(() => { row.classList.remove('row-in', 'row-flash'); }, 1400);
  tbody.insertBefore(row, tbody.firstChild);
}

function makeRow(ev) {
  const tr = document.createElement('tr');
  if (ev.id) tr.id = 'event-row-' + esc(ev.id);
  tr.onclick = () => openDrawer(ev);

  const sev  = esc((ev.severity || 'medium').toLowerCase());
  const code = esc((ev.failure_code || '').toUpperCase());

  const ivHtml = (ev.interventions || []).map(iv => {
    let cls = '';
    if (iv === 'escalation')      cls = ' esc';
    if (iv === 'mandate_renewal') cls = ' renewal';
    return `<span class="iv-tag${cls}">${esc(ivName(iv))}</span>`;
  }).join('') || '<span class="muted" style="font-size:11px">—</span>';

  // Trust score badge (colour-coded)
  const ts    = ev.trust_score != null ? ev.trust_score : 0.5;
  const tsCls = ts >= 0.75 ? 'trust-high' : ts >= 0.40 ? 'trust-med' : 'trust-low';
  const tsTxt = (ts * 100).toFixed(0) + '%';

  // Spend Pattern badge (Critical Spike vs Normal)
  let patternBadgeHtml = '';
  if (ev.is_pattern_critical) {
    const r = (ev.pattern_spike_ratio || 1.0).toFixed(1);
    patternBadgeHtml = `<span class="pattern-badge critical" title="${esc(ev.pattern_summary || 'Sudden upward critical spike')}">⚡ ${r}x Spike</span>`;
  } else if (ev.pattern_spike_ratio && ev.pattern_spike_ratio >= 2.0) {
    const r = (ev.pattern_spike_ratio).toFixed(1);
    patternBadgeHtml = `<span class="pattern-badge elevated" title="${esc(ev.pattern_summary || 'Elevated spend')}">⚠️ ${r}x Elev</span>`;
  } else {
    patternBadgeHtml = `<span class="pattern-badge normal" title="${esc(ev.pattern_summary || 'Within normal historical baseline')}">✓ Normal</span>`;
  }

  // Quick-create P2P action
  const actTd = document.createElement('td');
  const actBtn = document.createElement('button');
  actBtn.className = 'btn-act btn-blue';
  actBtn.title = 'Create P2P from this event';
  actBtn.textContent = '+P2P';
  actBtn.onclick = (e) => {
    e.stopPropagation();
    quickCreateP2P(ev.customer_vpa || '', ev.amount || 0, ev.bank || '', ev.failure_code || '');
  };
  actTd.appendChild(actBtn);

  const displayIdent = ev.customer_vpa || ev.customer_id || '';

  tr.innerHTML = `
    <td class="muted" style="font-variant-numeric:tabular-nums;font-size:12px">${esc(ev.timestamp || '')}</td>
    <td><span class="code-tag">${code}</span></td>
    <td class="mono vpa-filter-cell" title="Click to filter customer history" style="cursor:pointer;color:var(--blue);">${esc(ev.customer_vpa || '')}</td>
    <td class="muted">${esc(ev.bank || '')}</td>
    <td class="fw6">${fmtInr(ev.amount)}</td>
    <td><span class="sev-badge sev-${sev}">${cap(sev)}</span></td>
    <td>${patternBadgeHtml}</td>
    <td><span class="trust-badge ${tsCls}" title="Payer trust score from P2P history">${tsTxt}</span></td>
    <td>${ivHtml}</td>
    <td>${ev.status === 'escalated' || (ev.interventions && ev.interventions.includes('escalation') && !ev.success)
      ? '<span class="status-esc">🚨 Escalated</span>'
      : ev.success
      ? '<span class="status-ok">✓ Recovered</span>'
      : '<span class="status-err">✗ Failed</span>'}</td>`;

  const vpaCell = tr.querySelector('.vpa-filter-cell');
  if (vpaCell) {
    vpaCell.addEventListener('click', (e) => {
      e.stopPropagation();
      filterEventsByCustomer(displayIdent);
    });
  }

  tr.appendChild(actTd);
  return tr;
}

// ── Drawer ───────────────────────────────────────────────────────────────────
function openDrawer(ev) {
  const sev = esc((ev.severity || 'medium').toLowerCase());
  const ident = ev.customer_vpa || ev.customer_id || '';

  document.getElementById('drawer-title').textContent =
    ev.scenario_name || `${ev.failure_code || ''} Event`;
  document.getElementById('drawer-sub').textContent =
    ev.failure_reason || '';

  const ivHtml = (ev.interventions || []).map((iv, i) => `
    <div class="iv-block">
      <div class="iv-block-type">${esc(ivName(iv))}</div>
      <div class="iv-block-msg">${esc(ev.intervention_msgs?.[i] || '')}</div>
    </div>`).join('') || '<p class="muted" style="font-size:12px">None</p>';

  document.getElementById('drawer-body').innerHTML = `
    <div class="dl-section">
      <div class="dl-section-title">Event</div>
      ${row('Event Type',    esc(ev.event_type))}
      ${row('Failure Code',  `<span class="code-tag">${esc(ev.failure_code)}</span>`)}
      ${row('Reason',        esc(ev.failure_reason))}
      ${row('Time',          esc(ev.timestamp))}
      ${row('Event ID',      `<span class="mono" style="font-size:11px">${esc(ev.id)}</span>`)}
    </div>
    <div class="dl-section">
      <div class="dl-section-title">Customer</div>
      ${row('Customer ID',   esc(ev.customer_id))}
      ${row('VPA',           `<span class="mono">${esc(ev.customer_vpa)}</span>`)}
      ${row('Bank',          esc(ev.bank))}
      ${row('Amount',        `<strong>${fmtInr(ev.amount)}</strong>`)}
      ${row('Severity',      `<span class="sev-badge sev-${sev}">${cap(sev)}</span>`)}
    </div>
    <div class="dl-section" id="drawer-customer-360">
      <div class="dl-section-title">👤 Customer 360 &amp; Unified Behavioral History</div>
      <div style="font-size:12px;color:var(--text-sub);padding:6px 0;">Loading customer history &amp; linked identities…</div>
    </div>
    <div class="dl-section">
      <div class="dl-section-title">📊 Spend Pattern &amp; Anomaly Analysis</div>
      ${row('Baseline History', `<span style="font-size:12px;color:var(--text-sub)">${esc(ev.pattern_baseline || 'Historical baseline computed from past transactions')}</span>`)}
      ${row('Spike Multiplier', `<strong>${(ev.pattern_spike_ratio || 1.0).toFixed(1)}x</strong> ${ev.is_pattern_critical ? '<span class="sev-badge sev-critical">CRITICAL SPIKE</span>' : '<span class="sev-badge sev-low">NORMAL VARIATION</span>'}`)}
      ${row('AI Assessment', `<span style="font-size:12px;color:var(--text-sub)">${esc(ev.pattern_summary || (ev.is_pattern_critical ? 'Extreme upward transaction anomaly detected against historical pattern.' : 'Transaction amount is consistent with customer spend history.'))}</span>`)}
      ${row('Safety Guardrail', ev.is_pattern_critical ? '<span class="status-esc">GR10: Blind retries blocked. Payer anomaly protection active.</span>' : '<span class="status-ok">GR10: Approved for standard retry pipeline.</span>')}
    </div>
    <div class="dl-section">
      <div class="dl-section-title">Interventions</div>
      ${ivHtml}
    </div>
    ${ev.scheduled_at ? `
    <div class="dl-section">
      <div class="dl-section-title">Retry Schedule</div>
      ${row('Scheduled At', esc(ev.scheduled_at))}
    </div>` : ''}
    ${ev.action_url ? `
    <div class="dl-section">
      <div class="dl-section-title">Action URL</div>
      <code style="font-size:11px;word-break:break-all;color:var(--blue)">${esc(ev.action_url)}</code>
    </div>` : ''}
    <div class="dl-section">
      <div class="dl-section-title">🔐 Payer Trust Score</div>
      ${(()=>{
        const ts = ev.trust_score != null ? ev.trust_score : 0.5;
        const cls = ts >= 0.75 ? 'trust-high' : ts >= 0.40 ? 'trust-med' : 'trust-low';
        const label = ts >= 0.75 ? 'HIGH — reliable payer, likely self-cures' :
                      ts >= 0.40 ? 'MED — mixed history, nudge recommended' :
                                   'LOW — broken promises, escalate sooner';
        return row('Score', `<span class="trust-badge ${cls}">${(ts*100).toFixed(0)}%</span> ${label}`);
      })()}
      ${row('Source', 'Promise-to-Pay fulfillment history (CRED-style)')}
    </div>
    ${ev.aa_check ? `
    <div class="dl-section">
      <div class="dl-section-title">🏦 Account Aggregator Check</div>
      ${row('Provider', 'Setu AA Sandbox (RBI-regulated)')}
      ${row('Result', `<span style="font-size:12px;color:var(--text-sub)">${esc(ev.aa_check)}</span>`)}
      ${row('Why', '"We don\'t guess when the customer can pay — we ask, with consent, and check."')}
    </div>` : ''}`;

  document.getElementById('drawer').classList.add('open');
  document.getElementById('overlay').classList.add('open');

  // Load customer 360 async
  loadCustomer360InDrawer(ident);
}

async function loadCustomer360InDrawer(identifier) {
  const container = document.getElementById('drawer-customer-360');
  if (!container || !identifier) return;

  try {
    const res = await fetch(`/api/customer/${encodeURIComponent(identifier)}/history`);
    if (!res.ok) return;
    const data = await res.json();

    const prof = data.profile || {};
    const aliases = prof.aliases || [];
    const aliasBadges = aliases.map(a => `<span class="code-tag" style="font-size:10.5px;padding:2px 6px;">${esc(a)}</span>`).join(' ');

    const hist = data.spend_history || [];
    const histChips = hist.length > 0
      ? hist.slice(-8).map(amt => `<span class="iv-tag" style="font-size:10.5px;">₹${Number(amt).toLocaleString('en-IN')}</span>`).join(' ')
      : '<span class="muted">No prior transactions</span>';

    const prevEventsCount = data.total_events_count || 0;
    const prevDecisionsCount = data.total_ledger_decisions || 0;

    container.innerHTML = `
      <div class="dl-section-title">👤 Customer 360 &amp; Unified Behavioral History</div>
      ${row('Canonical Profile', `<strong>${esc(prof.primary_name || data.canonical_id)}</strong>`)}
      ${row('Linked Identifiers', `<div style="display:flex;gap:4px;flex-wrap:wrap;">${aliasBadges || esc(identifier)}</div>`)}
      ${row('Spend History (Last 8)', `<div style="display:flex;gap:4px;flex-wrap:wrap;">${histChips}</div>`)}
      ${row('Historical Mean', `<strong>₹${(data.spend_profile?.mean_amount || 0).toLocaleString('en-IN', {maximumFractionDigits:0})}</strong> (${hist.length} transactions)`)}
      ${row('Cumulative Activity', `${prevEventsCount} recovery events · ${prevDecisionsCount} ledger decisions`)}
      ${data.is_suppressed ? row('Compliance Status', `<span class="sev-badge sev-critical">HOLD: ${esc(data.suppression_reason)}</span>`) : ''}
      <div style="margin-top:8px;" id="cust-360-filter-box"></div>
    `;

    const filterBox = container.querySelector('#cust-360-filter-box');
    if (filterBox) {
      const btn = document.createElement('button');
      btn.className = 'btn-ghost';
      btn.style.cssText = 'font-size:11px;padding:4px 8px;width:100%;';
      btn.textContent = '🔍 Filter All Events for this Customer';
      btn.onclick = () => {
        closeDrawer();
        filterEventsByCustomer(identifier);
      };
      filterBox.appendChild(btn);
    }
  } catch (e) {
    console.debug('Failed to load customer 360:', e);
  }
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

    if (key === 'proactive_mandate_expiry') {
      const card = document.getElementById('mandate-expiry-card');
      if (card) {
        card.classList.add('pulse-card');
        setTimeout(() => card.classList.remove('pulse-card'), 3000);
      }
      await loadExpiringMandates();
    }
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
    btn.innerHTML = '▶&nbsp; Run Scenario';
  }
}

// ——— JSON Upload / Drop zone —————————————————————————————————————————————————————
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
    btn.innerHTML = '▶&nbsp; Run JSON Scenario';
  }
}

// ——— Auto Demo ———————————————————————————————————————————————————————————————————
// Cycles through all scenarios automatically — great for live hackathon demos
const AUTO_DEMO_KEYS     = [
  'spike_critical', 'normal_variation', 'u30', 'u29', 'bt01', 'bt02', 'u13',
  'tm', 'u69', 'ba', 'xb', 'te', 'rb', 'u66', 'rbi_threshold',
  'rbi_enhanced_insurance', 'rbi_enhanced_breach', 'proactive_mandate_expiry'
];
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
    btn.innerHTML   = '▶ Auto Demo';
    btn.classList.remove('running');
    toast('Auto demo stopped', 'ok');
  } else {
    // Start
    _autoDemoActive = true;
    _autoDemoIdx    = 0;
    btn.innerHTML   = '|| Stop Demo';
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
    const res = await fetch(`/api/simulate/${key}`, { method: 'POST' });
    if (key === 'proactive_mandate_expiry') {
      const card = document.getElementById('mandate-expiry-card');
      if (card) {
        card.classList.add('pulse-card');
        setTimeout(() => card.classList.remove('pulse-card'), 3000);
      }
      await loadExpiringMandates();
      toast('🔔 Proactive Expiry: 1-Click WhatsApp Renewal link dispatched before BT02 lapse!', 'blue');
    }
  } catch (e) { /* ignore — SSE will handle display */ }

  if (_autoDemoActive) {
    _autoDemoTimer = setTimeout(fireNextAutoDemo, AUTO_DEMO_INTERVAL);
  }
}

// ── Module Panels: Promise-to-Pay, Checkout, B2B, Expiring Mandates ─────────

async function loadBandit() {
  try {
    const res = await fetch('/api/bandit');
    if (res.ok) return await res.json();
  } catch (e) {
    // optional bandit inspector
  }
}

async function loadModules() {
  await Promise.allSettled([
    loadStats(),
    loadEvents(),
    loadExpiringMandates(),
    loadP2P(),
    loadCheckout(),
    loadB2B(),
    loadLedger(),
    loadROI(),
    loadBandit(),
  ]);
}

async function refreshAllDashboard(btn) {
  if (btn) {
    btn.disabled = true;
    btn.textContent = '↻ Refreshing…';
    btn.style.opacity = '0.75';
  }
  try {
    await Promise.allSettled([
      loadStats(),
      loadEvents(),
      loadExpiringMandates(),
      loadP2P(),
      loadCheckout(),
      loadB2B(),
      loadLedger(),
      loadROI(),
      loadBandit(),
      loadBenchmark(),
    ]);
    toast('✨ All dashboard panels & metrics refreshed!', 'ok');
  } catch (e) {
    toast('Refresh failed: ' + e.message, 'err');
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = '↻ Refresh All';
      btn.style.opacity = '1';
    }
  }
}

// ── Proactive Mandate Expiry Interceptor ─────────────────────────────────────
let currentExpiringMandates = [];
let currentPreviewMandateId = null;

async function loadExpiringMandates(btn) {
  if (btn) {
    btn.disabled = true;
    btn.textContent = '↻ Refreshing…';
    btn.style.opacity = '0.75';
  }
  try {
    const res = await fetch('/api/mandates/expiring?within_hours=72');
    const data = await res.json();
    currentExpiringMandates = data.mandates || [];
    renderExpiringStats(data.stats);
    renderExpiringTable(currentExpiringMandates);
    if (btn) toast('🔔 Expiring mandates refreshed', 'ok');
  } catch (e) {
    console.warn('Expiring mandates load failed', e);
    if (btn) toast('Failed to refresh expiring mandates', 'err');
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = '↻ Refresh';
      btn.style.opacity = '1';
    }
  }
}

function renderExpiringStats(s) {
  if (!s) return;
  const lapsedText = s.mandates_lapsed ? ` · ${s.mandates_lapsed} lapsed (BT02)` : '';
  set('exp-at-risk', `${s.expiring_within_72h} mandates expiring (<72h)${lapsedText}`);
  set('exp-nudged', `${s.nudges_dispatched} nudges sent`);
  set('exp-protected', `${fmtInr(s.revenue_protected)} churn prevented`);
}

function renderExpiringTable(mandates) {
  const tbody = document.getElementById('exp-tbody');
  if (!tbody) return;
  if (!mandates || !mandates.length) {
    tbody.innerHTML = '<tr><td colspan="9"><div class="empty-state"><p>No mandates expiring in the next 72 hours ✅</p></div></td></tr>';
    return;
  }
  tbody.innerHTML = '';
  mandates.forEach(m => {
    const tr = document.createElement('tr');
    tr.id = `exp-row-${esc(m.mandate_id)}`;
    const hrs = m.hours_remaining;
    const hrsBadge = hrs <= 24 ? `<span class="stat-chip chip-red" style="font-size:11px">${hrs}h left ⚠️</span>`
                              : `<span class="stat-chip chip-amber" style="font-size:11px">${hrs}h left</span>`;
    
    let stBadge = '';
    if (m.status === 'RENEWED') {
      stBadge = `<span class="stat-chip chip-green" style="font-size:10.5px">🛡️ Churn Prevented</span>`;
    } else if (m.status === 'LAPSED') {
      stBadge = `<span class="stat-chip chip-red" style="font-size:10.5px">⚠️ Lapsed (Expired)</span>`;
    } else if (m.status === 'NUDGED') {
      stBadge = `<span class="stat-chip chip-blue" style="font-size:10.5px">📱 WhatsApp Dispatched</span>`;
    } else {
      stBadge = `<span class="stat-chip chip-amber" style="font-size:10.5px">⏳ Awaiting Nudge</span>`;
    }

    tr.innerHTML = `
      <td class="mono fw6">${esc(m.mandate_id)}</td>
      <td><strong>${esc(m.customer_name)}</strong><br><span class="muted" style="font-size:11px">${esc(m.customer_vpa)}</span></td>
      <td>${esc(m.plan_name)}</td>
      <td class="fw6">${fmtInr(m.amount)}</td>
      <td>${esc(m.bank_name)}</td>
      <td class="muted" style="font-size:11px">${esc(m.expiry_date)}</td>
      <td>${hrsBadge}</td>
      <td>${stBadge}</td>
      <td class="exp-actions-td"></td>
    `;
    const actionTd = tr.querySelector('.exp-actions-td');
    if (m.status === 'PENDING') {
      const wrap = document.createElement('div');
      wrap.style.display = 'inline-flex';
      wrap.style.gap = '6px';
      wrap.style.alignItems = 'center';

      const nudgeBtn = document.createElement('button');
      nudgeBtn.className = 'btn-act btn-amber';
      nudgeBtn.textContent = '⚡ Send Nudge';
      nudgeBtn.title = 'Send proactive WhatsApp 1-click renewal link';
      nudgeBtn.onclick = function() { triggerProactiveNudge(m.mandate_id, this); };

      const lapseBtn = document.createElement('button');
      lapseBtn.className = 'btn-act';
      lapseBtn.style.background = 'rgba(239, 68, 68, 0.12)';
      lapseBtn.style.color = '#ef4444';
      lapseBtn.style.border = '1px solid rgba(239, 68, 68, 0.3)';
      lapseBtn.style.padding = '4px 8px';
      lapseBtn.style.fontSize = '11px';
      lapseBtn.style.borderRadius = '5px';
      lapseBtn.textContent = '⏭ Force Lapse';
      lapseBtn.title = 'Simulate expiry cutoff pass without renewal -> triggers live BT02 failure event';
      lapseBtn.onclick = function() { forceLapseMandate(m.mandate_id, this); };

      wrap.appendChild(nudgeBtn);
      wrap.appendChild(lapseBtn);
      actionTd.appendChild(wrap);
    } else if (m.status === 'NUDGED') {
      const wrap = document.createElement('div');
      wrap.style.display = 'inline-flex';
      wrap.style.gap = '6px';
      wrap.style.alignItems = 'center';

      const renewBtn = document.createElement('button');
      renewBtn.className = 'btn-act btn-green';
      renewBtn.textContent = '✓ Renew';
      renewBtn.title = 'Simulate customer completing 1-click renewal';
      renewBtn.onclick = function() { simulateProactiveRenewal(m.mandate_id, this); };

      const prevBtn = document.createElement('button');
      prevBtn.className = 'btn-act btn-blue';
      prevBtn.textContent = '💬 Preview';
      prevBtn.title = 'Preview personalized WhatsApp message';
      prevBtn.onclick = function() { openWaPreviewModal(m.mandate_id); };

      const lapseBtn = document.createElement('button');
      lapseBtn.className = 'btn-act';
      lapseBtn.style.background = 'rgba(239, 68, 68, 0.12)';
      lapseBtn.style.color = '#ef4444';
      lapseBtn.style.border = '1px solid rgba(239, 68, 68, 0.3)';
      lapseBtn.style.padding = '4px 8px';
      lapseBtn.style.fontSize = '11px';
      lapseBtn.style.borderRadius = '5px';
      lapseBtn.textContent = '⏭ Lapse';
      lapseBtn.title = 'Simulate customer ignoring nudge -> triggers live BT02 failure event';
      lapseBtn.onclick = function() { forceLapseMandate(m.mandate_id, this); };

      wrap.appendChild(renewBtn);
      wrap.appendChild(prevBtn);
      wrap.appendChild(lapseBtn);
      actionTd.appendChild(wrap);
    } else if (m.status === 'LAPSED') {
      const wrap = document.createElement('div');
      wrap.style.display = 'inline-flex';
      wrap.style.gap = '6px';
      wrap.style.alignItems = 'center';

      const viewBtn = document.createElement('button');
      viewBtn.className = 'btn-act btn-ghost';
      viewBtn.style.padding = '3px 8px';
      viewBtn.style.fontSize = '11px';
      viewBtn.textContent = '⚡ View BT02 Event';
      viewBtn.title = 'Scroll to top and filter Recovery Events by this customer';
      viewBtn.onclick = function() {
        window.scrollTo({ top: 0, behavior: 'smooth' });
        filterEventsByCustomer(m.customer_vpa);
      };

      wrap.appendChild(viewBtn);
      actionTd.appendChild(wrap);
    } else {
      const wrap = document.createElement('div');
      wrap.style.display = 'inline-flex';
      wrap.style.gap = '6px';
      wrap.style.alignItems = 'center';

      const span = document.createElement('span');
      span.className = 'status-ok';
      span.style.fontSize = '11.5px';
      span.style.fontWeight = '600';
      span.textContent = '✓ Pre-Empted';

      const viewBtn = document.createElement('button');
      viewBtn.className = 'btn-act btn-ghost';
      viewBtn.style.padding = '2px 6px';
      viewBtn.style.fontSize = '10.5px';
      viewBtn.textContent = '💬 View';
      viewBtn.onclick = function() { openWaPreviewModal(m.mandate_id); };

      wrap.appendChild(span);
      wrap.appendChild(viewBtn);
      actionTd.appendChild(wrap);
    }
    tbody.appendChild(tr);
  });
}

async function triggerProactiveNudge(id, btn) {
  if (btn) { btn.disabled = true; btn.textContent = 'Sending…'; }
  try {
    const res = await fetch(`/api/mandates/proactive-nudge/${encodeURIComponent(id)}`, { method: 'POST' });
    const data = await res.json();
    toast(`🔔 Proactive 1-click renewal link sent to ${data.mandate.customer_vpa} — ledger updated`, 'blue');

    // Append outbound WhatsApp notification to 2-Way WhatsApp Live Chat Window
    if (typeof appendChatBubble === 'function') {
      const m = data.mandate;
      const hrsLeft = Math.round(m.hours_remaining);
      const text = m.whatsapp_message || `Namaste ${m.customer_name}! 🔔 Aapka ${m.plan_name} ka UPI Autopay mandate (${m.mandate_id}) agle ${hrsLeft} ghante mein expire ho raha hai. Service uninterrupted rakhne ke liye 1-click mein renew karein: ${m.renewal_link}`;
      appendChatBubble('agent', text, {
        intent: 'PROACTIVE_NUDGE',
        confidence: 0.95,
        action: `Dispatched 1-click renewal link before BT02 expiry (${m.customer_vpa})`,
        time: new Date().toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', hour12: true })
      });
      // Pre-fill the custom reply box with this customer's details
      const phoneInput = document.getElementById('wa-input-phone');
      const amountInput = document.getElementById('wa-input-amount');
      if (phoneInput) phoneInput.value = m.customer_vpa;
      if (amountInput) amountInput.value = m.amount;
    }

    await loadExpiringMandates(); await loadLedger(); await loadROI();
  } catch (e) { toast('Failed: ' + e.message, 'red'); }
  finally { if (btn) { btn.disabled = false; btn.textContent = '⚡ Send WhatsApp Nudge'; } }
}

async function nudgeAllExpiring(btn) {
  if (btn) { btn.disabled = true; btn.textContent = '⚡ Sending All…'; }
  try {
    const res = await fetch('/api/mandates/nudge-all?within_hours=72', { method: 'POST' });
    const data = await res.json();
    toast(`🚀 ${data.message}!`, 'green');
    await loadExpiringMandates(); await loadLedger(); await loadROI();
  } catch (e) { toast('Failed: ' + e.message, 'red'); }
  finally { if (btn) { btn.disabled = false; btn.textContent = '⚡ Nudge All (<72h)'; } }
}

async function simulateProactiveRenewal(id, btn) {
  if (btn) { btn.disabled = true; btn.textContent = 'Renewing…'; }
  try {
    const res = await fetch(`/api/mandates/renew/${encodeURIComponent(id)}`, { method: 'POST' });
    const data = await res.json();
    toast(`🎉 ${data.message}`, 'green');
    await loadExpiringMandates(); await loadLedger(); await loadROI();
  } catch (e) { toast('Failed: ' + e.message, 'red'); }
  finally { if (btn) { btn.disabled = false; btn.textContent = '✓ Simulate Renewal'; } }
}

async function forceLapseMandate(id, btn) {
  if (btn) { btn.disabled = true; btn.textContent = '⏭ Lapsing…'; }
  try {
    const res = await fetch(`/api/mandates/force-lapse/${encodeURIComponent(id)}`, { method: 'POST' });
    const data = await res.json();
    if (res.ok) {
      toast(`⚠️ Mandate ${id} lapsed into real BT02 failure event! Live stream updated.`, 'err');
      await Promise.all([loadExpiringMandates(), loadEvents(), loadLedger(), loadROI()]);
      if (data.event && data.event.id) {
        setTimeout(() => {
          const row = document.getElementById('event-row-' + data.event.id);
          if (row) {
            row.style.outline = '2px solid #ef4444';
            row.scrollIntoView({ behavior: 'smooth', block: 'center' });
            setTimeout(() => { row.style.outline = ''; }, 3000);
          }
        }, 300);
      }
    } else {
      toast('Failed to lapse: ' + (data.detail || 'Unknown error'), 'red');
    }
  } catch (e) {
    toast('Failed: ' + e.message, 'red');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '⏭ Force Lapse'; }
  }
}

function openWaPreviewModal(mandateId) {
  const m = currentExpiringMandates.find(x => x.mandate_id === mandateId);
  if (!m) return;
  currentPreviewMandateId = mandateId;

  set('wa-modal-name', m.customer_name);
  set('wa-modal-vpa', `${m.customer_vpa} · ${m.bank_name} Bank`);
  set('wa-modal-msg', m.whatsapp_message || `Namaste ${m.customer_name}! 🔔 Aapka ${m.plan_name} ka UPI Autopay mandate (${m.mandate_id}) agle ${Math.round(m.hours_remaining)} ghante mein expire ho raha hai. Service uninterrupted rakhne ke liye 1-click mein renew karein: ${m.renewal_link}`);
  set('wa-modal-link', m.renewal_link || `https://rzp.io/l/demo-mandate-${m.customer_id}`);
  set('wa-modal-bubble-time', new Date().toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', hour12: false }));
  set('wa-modal-timestamp', `Proactive Lookahead Nudge · ${m.hours_remaining}h before expiry`);

  const webBtn = document.getElementById('wa-modal-web-btn');
  if (webBtn) {
    const encodedText = encodeURIComponent(m.whatsapp_message || '');
    webBtn.href = `https://wa.me/919800000001?text=${encodedText}`;
  }

  const renewBtn = document.getElementById('wa-modal-renew-action');
  if (renewBtn) {
    if (m.status === 'RENEWED') {
      renewBtn.textContent = '✓ Already Renewed';
      renewBtn.disabled = true;
    } else {
      renewBtn.textContent = `✓ Simulate Renewal (${fmtInr(m.amount)})`;
      renewBtn.disabled = false;
    }
  }

  const backdrop = document.getElementById('wa-preview-backdrop');
  const modal = document.getElementById('wa-preview-modal');
  if (backdrop) backdrop.classList.add('open');
  if (modal) modal.classList.add('open');
}

function closeWaPreviewModal() {
  const backdrop = document.getElementById('wa-preview-backdrop');
  const modal = document.getElementById('wa-preview-modal');
  if (backdrop) backdrop.classList.remove('open');
  if (modal) modal.classList.remove('open');
  currentPreviewMandateId = null;
}

async function renewFromPreviewModal() {
  if (!currentPreviewMandateId) return;
  const id = currentPreviewMandateId;
  closeWaPreviewModal();
  await simulateProactiveRenewal(id);
}

// ── Promise-to-Pay ───────────────────────────────────────────────────────────

async function loadP2P(btn) {
  if (btn) { btn.disabled = true; btn.textContent = '↻ Refreshing…'; btn.style.opacity = '0.75'; }
  try {
    const res  = await fetch('/api/promises');
    const data = await res.json();
    renderP2PStats(data.stats);
    renderP2PTable(data.promises);
    if (btn) toast('🤝 Promise-to-Pay tracker refreshed', 'ok');
  } catch (e) {
    console.warn('P2P load failed', e);
    if (btn) toast('Failed to refresh promises', 'err');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '↻ Refresh'; btn.style.opacity = '1'; }
  }
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
  tbody.innerHTML = '';
  promises.forEach(p => {
    const tr = document.createElement('tr');
    tr.id = `p2p-row-${esc(p.promise_id)}`;
    const deadline  = new Date(p.deadline).toLocaleString('en-IN', {
      day:'2-digit', month:'short', hour:'2-digit', minute:'2-digit', hour12:true
    });
    const overdue   = p.is_overdue ? ' ⚠️' : '';
    const statusCls = `p2p-${esc(p.status)}`;
    const isPending = p.status === 'pending';

    tr.innerHTML = `
      <td class="mono fw6">${esc(p.promise_id)}</td>
      <td>${esc(p.vpa)}</td>
      <td class="fw6">${fmtInr(p.amount)}</td>
      <td class="${p.is_overdue ? 'status-err' : 'muted'}">${deadline}${overdue}</td>
      <td class="muted">${esc(p.channel)}</td>
      <td class="${statusCls}">${esc(p.status.toUpperCase())}</td>
      <td class="muted" style="max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(p.notes)}">${esc(p.notes || '—')}</td>
      <td class="p2p-actions-td"></td>
    `;
    const actionTd = tr.querySelector('.p2p-actions-td');
    if (isPending) {
      const actDiv = document.createElement('div');
      actDiv.className = 'row-actions';
      const fulfillBtn = document.createElement('button');
      fulfillBtn.className = 'btn-act btn-green';
      fulfillBtn.textContent = '✓ Fulfilled';
      fulfillBtn.onclick = function() { p2pFulfil(p.promise_id, this); };
      const breakBtn = document.createElement('button');
      breakBtn.className = 'btn-act btn-red';
      breakBtn.textContent = '✗ Broken';
      breakBtn.onclick = function() { p2pBreak(p.promise_id, this); };
      actDiv.appendChild(fulfillBtn);
      actDiv.appendChild(breakBtn);
      actionTd.appendChild(actDiv);
    } else {
      const span = document.createElement('span');
      span.className = 'muted';
      span.style.fontSize = '11px';
      span.textContent = p.status.toUpperCase();
      actionTd.appendChild(span);
    }
    tbody.appendChild(tr);
  });
}

async function p2pFulfil(id, btn) {
  btn.disabled = true; btn.textContent = '…';
  try {
    const res = await fetch(`/api/promises/${encodeURIComponent(id)}/fulfill`, {method:'POST'});
    const data = await res.json();
    toast(`✓ Promise ${id} marked FULFILLED — ledger updated`, 'green');

    // Append verified confirmation to 2-Way WhatsApp Live Chat Window
    if (typeof appendChatBubble === 'function') {
      const confMsg = `Thank you! 🟢 Aapka ₹${data.amount || 0} ka payment against Promise #${id} successfully receive ho gaya hai. Aapka account active & in good standing hai.`;
      appendChatBubble('agent', confMsg, {
        intent: 'P2P_FULFILLED_CONFIRMATION',
        confidence: 1.0,
        action: `P2P Recovery Verified (${data.vpa || ''})`,
        time: new Date().toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', hour12: true })
      });
      const phoneInput = document.getElementById('wa-input-phone');
      const amountInput = document.getElementById('wa-input-amount');
      if (phoneInput && data.vpa) phoneInput.value = data.vpa;
      if (amountInput && data.amount) amountInput.value = data.amount;
    }

    await Promise.all([loadP2P(), loadLedger(), loadROI(), loadStats()]);
  } catch(e) { toast('Failed: ' + e.message, 'red'); btn.disabled = false; btn.textContent = '✓ Fulfilled'; }
}

async function p2pBreak(id, btn) {
  btn.disabled = true; btn.textContent = '…';
  try {
    const res = await fetch(`/api/promises/${encodeURIComponent(id)}/break`, {method:'POST'});
    const data = await res.json();
    toast(`✗ Promise ${id} marked BROKEN — escalation logged`, 'red');

    // Append urgent escalation follow-up to 2-Way WhatsApp Live Chat Window
    if (typeof appendChatBubble === 'function') {
      const urgentMsg = `Namaste! ⚠️ Aapka ₹${data.amount || 0} ka payment commitment deadline miss ho gaya hai. Account escalation aur service interruption se bachne ke liye kripya abhi settle karein: https://rzp.io/l/p2p-urgent-${encodeURIComponent(data.vpa || 'pay')}`;
      appendChatBubble('agent', urgentMsg, {
        intent: 'P2P_BROKEN_ESCALATION',
        confidence: 0.99,
        action: `Broken Promise Escalation Nudge (${data.vpa || ''})`,
        time: new Date().toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', hour12: true })
      });
      // Pre-fill the 2-Way Live Chat input box
      const phoneInput = document.getElementById('wa-input-phone');
      const amountInput = document.getElementById('wa-input-amount');
      if (phoneInput && data.vpa) phoneInput.value = data.vpa;
      if (amountInput && data.amount) amountInput.value = data.amount;
    }

    await Promise.all([loadP2P(), loadLedger(), loadROI(), loadStats()]);
  } catch(e) { toast('Failed: ' + e.message, 'red'); btn.disabled = false; btn.textContent = '✗ Broken'; }
}

// ── Checkout Drop-off ────────────────────────────────────────────────────────

async function loadCheckout(btn) {
  if (btn) { btn.disabled = true; btn.textContent = '↻ Refreshing…'; btn.style.opacity = '0.75'; }
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
  tbody.innerHTML = '';
  sessions.forEach(s => {
    const tr = document.createElement('tr');
    tr.id = `chk-row-${esc(s.session_id)}`;
    const reason = REASON_LABELS[s.drop_off_reason] || s.drop_off_reason || 'Unknown';
    const stCls  = CHK_STATUS_CLS[s.status] || 'muted';
    const isOpen = s.status === 'open' || s.status === 'contacted';

    tr.innerHTML = `
      <td class="mono fw6">${esc(s.session_id)}</td>
      <td>${esc(s.customer_vpa)}</td>
      <td class="fw6">${fmtInr(s.cart_amount)}</td>
      <td><span class="reason-badge">${esc(reason)}</span></td>
      <td class="${stCls}">${esc(s.status.toUpperCase())}</td>
      <td><span class="chk-msg" title="${esc(s.recovery_message)}">${esc(s.recovery_message || '—')}</span></td>
      <td class="chk-actions-td"></td>
    `;
    const actionTd = tr.querySelector('.chk-actions-td');
    if (isOpen) {
      const btn = document.createElement('button');
      btn.className = 'btn-act btn-green';
      btn.textContent = '✓ Recovered';
      btn.onclick = function() { chkRecover(s.session_id, this); };
      actionTd.appendChild(btn);
    } else {
      const span = document.createElement('span');
      span.className = 'muted';
      span.style.fontSize = '11px';
      span.textContent = s.status.toUpperCase();
      actionTd.appendChild(span);
    }
    tbody.appendChild(tr);
  });
}

async function chkRecover(id, btn) {
  btn.disabled = true; btn.textContent = '…';
  try {
    await fetch(`/api/checkout/${encodeURIComponent(id)}/recover`, {method:'POST'});
    toast(`✓ Checkout ${id} marked RECOVERED — payment confirmed, ledger updated`, 'green');
    await loadCheckout(); await loadLedger(); await loadROI();
  } catch(e) { toast('Failed: ' + e.message, 'red'); btn.disabled = false; btn.textContent = '✓ Recovered'; }
}

// ── B2B Receivables ──────────────────────────────────────────────────────────

async function loadB2B(btn) {
  if (btn) { btn.disabled = true; btn.textContent = '↻ Refreshing…'; btn.style.opacity = '0.75'; }
  try {
    const res  = await fetch('/api/b2b');
    const data = await res.json();
    renderB2BStats(data.stats);
    renderAgingBuckets(data.stats.buckets);
    renderB2BTable(data.receivables);
    if (btn) toast('🏢 B2B Receivables refreshed', 'ok');
  } catch (e) {
    console.warn('B2B load failed', e);
    if (btn) toast('Failed to refresh B2B receivables', 'err');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '↻ Refresh'; btn.style.opacity = '1'; }
  }
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
  tbody.innerHTML = '';
  receivables.forEach(r => {
    const tr = document.createElement('tr');
    tr.id = `b2b-row-${esc(r.receivable_id)}`;
    const bucketCls = BUCKET_CLS[r.aging_bucket] || '';
    const tierCls   = `tier-${esc((r.debtor_tier || '').toLowerCase())}`;
    const stCls     = B2B_STATUS_CLS[r.status] || 'muted';
    const lastAct   = r.actions && r.actions.length
      ? `<span class="muted" title="${esc(r.actions[r.actions.length-1].message)}">${esc(r.actions[r.actions.length-1].channel)}</span>`
      : '<span class="muted">—</span>';
    const canAct = r.status !== 'settled' && r.status !== 'written_off';

    tr.innerHTML = `
      <td class="mono fw6">${esc(r.invoice_number)}</td>
      <td>${esc(r.debtor_name)}</td>
      <td class="fw6">${fmtInr(r.amount)}</td>
      <td class="${r.days_overdue > 60 ? 'status-err' : r.days_overdue > 30 ? 'p2p-pending' : 'muted'}">${r.days_overdue}d</td>
      <td><span class="bucket-badge ${bucketCls}">${esc(r.aging_bucket)}</span></td>
      <td class="${tierCls}">Tier ${esc(r.debtor_tier)}</td>
      <td class="muted">${fmtInr(r.interest_accrued)}</td>
      <td class="${stCls}">${esc(r.status.toUpperCase())}</td>
      <td>${lastAct}</td>
      <td class="b2b-actions-td"></td>
    `;
    const actionTd = tr.querySelector('.b2b-actions-td');
    if (canAct) {
      const actDiv = document.createElement('div');
      actDiv.className = 'row-actions';
      const chaseBtn = document.createElement('button');
      chaseBtn.className = 'btn-act btn-blue';
      chaseBtn.textContent = '↺ Chase';
      chaseBtn.onclick = function() { b2bChase(r.receivable_id, this); };
      const settleBtn = document.createElement('button');
      settleBtn.className = 'btn-act btn-green';
      settleBtn.textContent = '₹ Settle';
      settleBtn.onclick = function() { b2bSettle(r.receivable_id, r.debtor_name, r.amount, this); };
      actDiv.appendChild(chaseBtn);
      actDiv.appendChild(settleBtn);
      actionTd.appendChild(actDiv);
    } else {
      const span = document.createElement('span');
      span.className = 'muted';
      span.style.fontSize = '11px';
      span.textContent = r.status.toUpperCase();
      actionTd.appendChild(span);
    }
    tbody.appendChild(tr);
  });
}

async function b2bChase(id, btn) {
  btn.disabled = true; btn.textContent = '…';
  try {
    const res  = await fetch(`/api/b2b/receivables/${encodeURIComponent(id)}/chase`, {method:'POST'});
    const data = await res.json();
    toast(`↺ Chase dispatched via ${data.channel || 'channel'} — message sent & ledger logged`, 'blue');
    await loadB2B(); await loadLedger(); await loadROI();
  } catch(e) { toast('Chase failed: ' + e.message, 'red'); }
  finally     { btn.disabled = false; btn.textContent = '↺ Chase'; }
}

async function b2bSettle(id, name, amount, btn) {
  openSettleDialog(id, name, amount);
}

// ── Recovery Ledger ──────────────────────────────────────────────────────────

const CHANNEL_UNIT_COSTS = {
  whatsapp: 0.50, sms: 0.15, ivr: 1.50, email: 0.05,
  smart_retry: 0.00, upi_collect: 0.25, mandate_renewal: 0.50,
  escalation: 25.00, legal: 500.00, ar_specialist: 150.00,
};

async function loadLedger(btn) {
  if (btn) { btn.disabled = true; btn.textContent = '↻ Refreshing…'; btn.style.opacity = '0.75'; }
  try {
    const res  = await fetch('/api/ledger?limit=30');
    const data = await res.json();
    const o    = data.overall_roi;
    set('ldg-entries',  `${o.total_entries} entries`);
    set('ldg-avg-conf', `conf: ${Math.round(o.avg_confidence * 100)}%`);
    renderLedgerTable(data.entries);
    if (btn) toast('📋 Recovery audit ledger refreshed', 'ok');
  } catch (e) {
    console.warn('Ledger load failed', e);
    if (btn) toast('Failed to refresh ledger', 'err');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '↻ Refresh'; btn.style.opacity = '1'; }
  }
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
    const isProactive = e.recovery_type === 'proactive' || (e.channel === 'mandate_renewal' && e.event_type === 'recover');
    const modeBadge  = isProactive 
      ? `<span class="stat-chip chip-amber" style="font-size:9.5px;padding:2px 5px;font-weight:700;letter-spacing:0.02em;">🛡️ PROACTIVE</span>`
      : `<span class="stat-chip chip-blue" style="font-size:9.5px;padding:2px 5px;font-weight:700;letter-spacing:0.02em;">⚡ REACTIVE</span>`;
    const typeCls    = `ledger-type type-${esc(e.event_type)}`;
    const outcomeCls = LEDGER_OUTCOME_CLS[e.outcome] || 'muted';
    return `<tr>
      <td class="mono muted">${esc(e.ts)}</td>
      <td><div style="display:flex;align-items:center;gap:6px;"><span class="${typeCls}">${esc(e.event_type)}</span>${modeBadge}</div></td>
      <td class="muted">${esc(e.vpa)}</td>
      <td class="fw6">${fmtInr(e.amount)}</td>
      <td>${confPips(e.confidence)}</td>
      <td><span class="ledger-reasoning" title="${esc(e.reasoning)}">${esc(e.reasoning)}</span></td>
      <td class="${outcomeCls}">${esc(e.outcome.toUpperCase())}</td>
    </tr>`;
  }).join('');
}

// ── Recovery ROI ─────────────────────────────────────────────────────────────

async function loadROI(btn) {
  if (btn) { btn.disabled = true; btn.textContent = '↻ Refreshing…'; btn.style.opacity = '0.75'; }
  try {
    const res  = await fetch('/api/roi');
    const data = await res.json();
    const o    = data.overall;
    set('roi-recovered', fmtInr(o.total_recovered));
    set('roi-reactive',  fmtInr(o.reactive_recovered ?? 0));
    set('roi-proactive', fmtInr(o.proactive_protected ?? 0));
    set('roi-costs',     fmtInr(o.total_cost));
    const netEl = document.getElementById('roi-netval');
    if (netEl) {
      netEl.textContent = fmtInr(o.net_roi);
      netEl.className   = 'roi-value ' + (o.net_roi >= 0 ? 'green' : 'red');
    }
    set('roi-stake', fmtInr(o.total_at_stake) + ' at stake');
    set('roi-net',   fmtInr(o.net_roi) + ' net');
    set('roi-rate',  o.recovery_rate_pct + '% rate');
    renderROITable(data.by_channel);
    if (btn) toast('💰 Recovery ROI metrics refreshed', 'ok');
  } catch (e) {
    console.warn('ROI load failed', e);
    if (btn) toast('Failed to refresh ROI metrics', 'err');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '↻ Refresh'; btn.style.opacity = '1'; }
  }
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
    const isProactive = s.recovery_type === 'proactive' || ch === 'mandate_renewal';
    const modeTag = isProactive ? ' <span class="stat-chip chip-amber" style="font-size:9px;padding:1px 4px;margin-left:4px">PROACTIVE</span>' : '';
    return `<tr>
      <td class="fw6" style="text-transform:capitalize">${ch.replace(/_/g,' ')}${modeTag}</td>
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

// ── Benchmark Panel ──────────────────────────────────────────────────────────
async function loadBenchmark(btn) {
  const refreshBtn = btn || document.getElementById('bm-refresh-btn');
  const chip = document.getElementById('bm-uplift-chip');
  if (refreshBtn)  { refreshBtn.disabled = true; refreshBtn.textContent = '↻ Running Monte Carlo…'; refreshBtn.style.opacity = '0.75'; }
  if (chip) { chip.textContent = 'Running (n=50)…'; chip.className = 'stat-chip chip-blue'; }
  try {
    const data = await fetch('/api/benchmark').then(r => r.json());
    const b    = data.baseline;
    const a    = data.ai_agent;
    const d    = data.delta;

    // Headline KPIs
    setText('bm-stake',      fmtInr(a.total_at_stake));
    setText('bm-base-rec',   fmtInr(b.total_recovered));
    setText('bm-ai-rec',     fmtInr(a.total_recovered) + (a.total_recovered_std ? ` ± ${fmtInr(a.total_recovered_std)}` : ''));
    setText('bm-base-rate',  b.recovery_rate_pct + '% rate (fixed)');
    setText('bm-ai-rate',    a.recovery_rate_pct + '%' + (a.recovery_rate_std ? ` ± ${a.recovery_rate_std}%` : '') + ' rate');
    setText('bm-delta',      '+' + fmtInr(d.revenue_recovered_uplift));
    setText('bm-rate-delta', '+' + d.recovery_rate_pts + ' pts recovery rate');
    setText('bm-violations', b.compliance_violations + ' (baseline)');
    setText('bm-roi-uplift', '+' + fmtInr(d.net_roi_uplift));

    // Uplift chip
    if (chip) {
      chip.textContent  = '+' + fmtInr(d.revenue_recovered_uplift) + ' uplift (+' + d.recovery_rate_pts + ' pts)';
      chip.className    = 'stat-chip chip-green';
    }

    // Comparison table
    const tbody = document.getElementById('bm-tbody');
    if (tbody) {
      const rows = [
        ['Revenue at Stake',           fmtInr(b.total_at_stake),          fmtInr(a.total_at_stake),         '—'],
        ['Revenue Recovered',          fmtInr(b.total_recovered),         fmtInr(a.total_recovered) + (a.total_recovered_std ? ` ± ${fmtInr(a.total_recovered_std)}` : ''), '+' + fmtInr(d.revenue_recovered_uplift) + ' (' + ((d.revenue_recovered_uplift / b.total_recovered) * 100).toFixed(0) + '%)'],
        ['Recovery Rate',              b.recovery_rate_pct + '%',         a.recovery_rate_pct + '%' + (a.recovery_rate_std ? ` ± ${a.recovery_rate_std}%` : ''), '+' + d.recovery_rate_pts + ' percentage points'],
        ['Compliance Violations',      b.compliance_violations + ' (RBI/TRAI)', a.compliance_violations + ' ✅', '-' + d.violations_eliminated + ' violations eliminated'],
        ['Wasted Retries',             b.retries + ' (blind flood)',      a.retries + ' (salary-targeted)', '-' + (b.retries - a.retries) + ' retries saved'],
        ['Intervention Channel Costs', fmtInr(b.channel_costs),           fmtInr(a.channel_costs),          fmtInr(a.channel_costs - b.channel_costs)],
        ['Net ROI (Recovered − Cost)', fmtInr(b.net_roi),                 fmtInr(a.net_roi),                '+' + fmtInr(d.net_roi_uplift) + ' net uplift'],
      ];
      tbody.innerHTML = rows.map(([label, bval, aval, delta]) =>
        `<tr>
          <td><strong>${label}</strong></td>
          <td style="color:var(--red)">${bval}</td>
          <td style="color:var(--green)">${aval}</td>
          <td style="color:var(--accent);font-weight:600">${delta}</td>
        </tr>`
      ).join('');
    }
    if (btn) toast('📊 Monte Carlo benchmark refreshed (50 runs)', 'ok');
  } catch (e) {
    console.error('Benchmark load failed:', e);
    if (chip) { chip.textContent = 'Error'; chip.className = 'stat-chip chip-red'; }
    const tbody = document.getElementById('bm-tbody');
    if (tbody) tbody.innerHTML = '<tr><td colspan="4"><div class="empty-state"><p>Benchmark failed to load. Is the server running?</p></div></td></tr>';
  } finally {
    if (refreshBtn) { refreshBtn.disabled = false; refreshBtn.textContent = '↻ Refresh'; refreshBtn.style.opacity = '1'; }
  }
}

function setText(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val;
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

// ── 2-Way WhatsApp Interactive Simulator ──────────────────────────────────────
const WA_PRESETS = {
  promise_24h: {
    message: "Bhai kal pakka pay kar dunga, abhi travel kar raha hu",
    amount: 999.0,
  },
  promise_salary: {
    message: "Salary 5th ko aayegi tab transfer kar dungi",
    amount: 1499.0,
  },
  already_paid: {
    message: "Mera account se ₹999 debit ho gaya hai check your statement",
    amount: 999.0,
  },
  dispute: {
    message: "Maine ye service cancel kar di thi, refund karo fraud mat karo",
    amount: 2499.0,
  },
  hardship: {
    message: "Meri job chali gayi hai aur hospital emergency hai, abhi paise nahi hain",
    amount: 3200.0,
  },
  wrong_number: {
    message: "Galat number hai bhai, stop messaging me not my account",
    amount: 999.0,
  },
};

async function simulateInboundPreset(key) {
  const preset = WA_PRESETS[key];
  if (!preset) return;
  const phone = document.getElementById('wa-input-phone').value.trim() || '+91-9876543210';
  document.getElementById('wa-input-msg').value = preset.message;
  document.getElementById('wa-input-amount').value = preset.amount;
  await sendInboundMessage(phone, preset.message, preset.amount);
}

async function submitCustomInbound(event) {
  event.preventDefault();
  const phone = document.getElementById('wa-input-phone').value.trim();
  const amount = parseFloat(document.getElementById('wa-input-amount').value) || 999;
  const msg = document.getElementById('wa-input-msg').value.trim();
  if (!msg) { toast('Please enter a reply message', 'err'); return; }
  await sendInboundMessage(phone, msg, amount);
  document.getElementById('wa-input-msg').value = '';
}

async function sendInboundMessage(fromPhone, message, amount) {
  const btn = document.getElementById('wa-send-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Processing…'; }
  
  // Append user bubble to chat window
  appendChatBubble('user', message, {
    from: fromPhone,
    time: new Date().toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', hour12: true })
  });

  try {
    const res = await fetch('/api/webhook/whatsapp/inbound', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        from_phone: fromPhone,
        customer_vpa: '',
        message: message,
        amount: amount,
      }),
    });
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    
    // Append AI Agent bubble
    appendChatBubble('agent', data.reply_text, {
      intent: data.intent,
      confidence: data.confidence,
      action: data.action_taken,
      time: new Date().toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', hour12: true })
    });

    toast(`💬 Intent classified: ${data.intent.toUpperCase()} (${Math.round(data.confidence * 100)}%)`, 'ok');
    await loadModules();
  } catch (err) {
    toast(`Inbound error: ${err.message}`, 'err');
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = '&#9654;&nbsp; Send Reply'; }
  }
}

function appendChatBubble(type, text, meta) {
  const win = document.getElementById('wa-chat-window');
  const empty = document.getElementById('wa-chat-empty');
  if (empty) empty.style.display = 'none';

  const wrap = document.createElement('div');
  wrap.className = `wa-bubble-wrap ${type}`;

  let extraHtml = '';
  if (type === 'user') {
    extraHtml = `<div class="wa-bubble-meta"><span>${esc(meta.from || 'Customer')}</span> · <span>${esc(meta.time || '')}</span></div>`;
  } else {
    const intentClass = `tag-${esc(meta.intent || 'promise')}`;
    extraHtml = `
      <div class="wa-bubble-meta">
        <span class="wa-chip-tag ${intentClass}">${esc((meta.intent || 'AI AGENT').toUpperCase())} ${Math.round((meta.confidence || 0.9) * 100)}%</span>
        <span>${esc(meta.time || '')}</span>
      </div>
      <div class="wa-bubble-action">⚡ ${esc(meta.action || 'Recovery workflow updated')}</div>
    `;
  }

  wrap.innerHTML = `<div class="wa-bubble ${type}">${esc(text)}</div>${extraHtml}`;
  win.appendChild(wrap);
  win.scrollTop = win.scrollHeight;
}

function clearWhatsAppChat() {
  const win = document.getElementById('wa-chat-window');
  win.innerHTML = `<div class="wa-chat-empty" id="wa-chat-empty">
    <p>Click any quick scenario or send a custom reply to see the AI Intent Classifier and auto-response in real time.</p>
  </div>`;
}

// ── Project AI Assistant Chatbot (Powered by Gemini) ─────────────────────────
let projectChatHistory = [];

function toggleProjectChat() {
  const drawer = document.getElementById('project-chat-drawer');
  if (!drawer) return;
  drawer.classList.toggle('hidden');
  if (!drawer.classList.contains('hidden')) {
    setTimeout(() => {
      const inp = document.getElementById('project-chat-input');
      if (inp) inp.focus();
    }, 100);
  }
}

function askQuickProjectQuestion(query) {
  const inp = document.getElementById('project-chat-input');
  if (inp) inp.value = query;
  submitProjectChat();
}

async function runPromptScenarioFromChip(promptText) {
  const inp = document.getElementById('project-chat-input');
  if (inp) inp.value = '';
  await executePromptScenario(promptText);
}

async function runClassifierEvalFromChat() {
  appendProjectChatMessage('user', 'Run Intent Classifier Accuracy Benchmark (30 Held-Out Messages)');
  const typingId = showProjectChatTyping();
  try {
    const res = await fetch('/api/classifier/eval');
    removeProjectChatTyping(typingId);
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    
    let summary = `**🎯 Inbound Intent Classifier Benchmark Results (30 Items):**\n\n`;
    summary += `- **Overall Accuracy**: **${data.accuracy_pct}** (${data.total_samples} labeled messages)\n`;
    summary += `- **Hardship Recall**: **${Math.round(data.compliance_intent_recall.hardship_recall * 100)}%**\n`;
    summary += `- **Wrong Number Recall**: **${Math.round(data.compliance_intent_recall.wrong_number_recall * 100)}%**\n`;
    summary += `- **Status**: ${data.compliance_intent_recall.status}\n\n`;
    summary += `**Per-Intent F1 Scores:**\n`;
    for (const [intent, m] of Object.entries(data.per_intent_metrics || {})) {
      summary += `- **${intent.toUpperCase()}**: Precision ${m.precision} · Recall ${m.recall} · F1 **${m.f1_score}** (n=${m.support})\n`;
    }
    
    appendProjectChatMessage('bot', summary, 'cached_benchmark');
    toast(`🎯 Classifier Eval: ${data.accuracy_pct} accuracy (100% compliance recall)`, 'green');
  } catch (err) {
    removeProjectChatTyping(typingId);
    appendProjectChatMessage('bot', `⚠️ Could not fetch classifier evaluation: ${err.message}`);
  }
}

async function executePromptScenario(promptText) {
  appendProjectChatMessage('user', promptText);
  const typingId = showProjectChatTyping();
  const btn = document.getElementById('project-chat-send-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Generating…'; }

  try {
    const res = await fetch('/api/prompt-to-scenario', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt: promptText }),
    });

    removeProjectChatTyping(typingId);
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();

    const echoText = data.echo || 'Scenario executed';
    const sc = data.scenario || {};
    const ev = data.event || {};

    let reply = `⚡ **${echoText}**\n\n`;
    reply += `- **Failure Code**: \`${sc.failure_code || 'U30'}\` · **Bank**: ${sc.bank || 'SBI'}\n`;
    reply += `- **Amount**: ₹${Number(sc.amount || 0).toLocaleString('en-IN')}\n`;
    reply += `- **Payer VPA**: \`${sc.vpa || 'user@upi'}\`\n`;
    if (ev.decision) {
      reply += `- **Decision**: **${ev.decision.approved ? 'APPROVED' : 'GUARDRAIL BLOCKED'}** (Confidence: ${Math.round((ev.decision.confidence || 0.9) * 100)}%)\n`;
      reply += `- **Intervention**: ${ev.decision.chosen_channel || 'Smart Retry Scheduled'}\n`;
    }

    appendProjectChatMessage('bot', reply, data.provider);
    toast(`✨ Executed Natural Language Scenario: ${sc.scenario_name || sc.failure_code}`, 'green');
    await loadModules();
  } catch (err) {
    removeProjectChatTyping(typingId);
    appendProjectChatMessage('bot', `⚠️ Could not execute scenario: ${err.message}`);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Send ➔'; }
  }
}

async function submitProjectChat(e) {
  if (e) e.preventDefault();
  const inp = document.getElementById('project-chat-input');
  const btn = document.getElementById('project-chat-send-btn');
  const msg = inp ? inp.value.trim() : '';
  if (!msg) return;

  inp.value = '';

  // Smart scenario trigger: explicit prefix OR natural simulation intent phrasing
  const lower = msg.toLowerCase();
  const isSimIntent = lower.startsWith('/sim') || 
                      lower.startsWith('/scenario') || 
                      lower.startsWith('simulate ') || 
                      lower.startsWith('sim:') || 
                      lower.startsWith('show me ') || 
                      lower.startsWith('what happens if ') ||
                      lower.includes('mandate expiring') || 
                      lower.includes('mandate expires') ||
                      lower.includes('mandate lapse') ||
                      lower.includes('force lapse');

  if (isSimIntent) {
    const cleanPrompt = msg.replace(/^(\/sim|\/scenario|simulate|sim:)\s*/i, '');
    await executePromptScenario(cleanPrompt || msg);
    return;
  }

  appendProjectChatMessage('user', msg);

  // Show typing indicator
  const typingId = showProjectChatTyping();
  if (btn) { btn.disabled = true; btn.textContent = 'Thinking…'; }

  try {
    const res = await fetch('/api/project-chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        message: msg,
        history: projectChatHistory.slice(-6),
      }),
    });
    
    removeProjectChatTyping(typingId);
    
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    const reply = data.reply || "Sorry, I couldn't process that question.";

    appendProjectChatMessage('bot', reply, data.provider);

    // Save to local session history
    projectChatHistory.push({ role: 'user', content: msg });
    projectChatHistory.push({ role: 'assistant', content: reply });
  } catch (err) {
    removeProjectChatTyping(typingId);
    appendProjectChatMessage('bot', `⚠️ Could not reach Gemini Assistant: ${err.message}`);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Send ➔'; }
    if (inp) inp.focus();
  }
}

function formatMarkdown(text) {
  if (!text) return '';
  // 1. Centralized HTML Entity Escaping (OWASP innerHTML XSS Prevention)
  const safe = esc(String(text));
  // 2. Markdown formatting on safely escaped text
  return safe
    .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.*?)\*/g, '<em>$1</em>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/^-\s+(.*)$/gm, '• $1')
    .replace(/\n\n/g, '<br/><br/>')
    .replace(/\n/g, '<br/>');
}

function appendProjectChatMessage(sender, text, provider) {
  const container = document.getElementById('project-chat-messages');
  if (!container) return;

  const msgDiv = document.createElement('div');
  msgDiv.className = `ai-msg ai-msg-${sender}`;

  const bubbleDiv = document.createElement('div');
  bubbleDiv.className = 'ai-msg-bubble';

  if (sender === 'user') {
    bubbleDiv.textContent = text;
  } else {
    bubbleDiv.innerHTML = formatMarkdown(text);
    if (provider) {
      const badge = document.createElement('div');
      badge.style.fontSize = '10px';
      badge.style.color = 'var(--text-3)';
      badge.style.marginTop = '6px';
      badge.textContent = `⚡ Grounded response via ${provider.toUpperCase()}`;
      bubbleDiv.appendChild(badge);
    }
  }

  msgDiv.appendChild(bubbleDiv);
  container.appendChild(msgDiv);
  container.scrollTop = container.scrollHeight;
}

function showProjectChatTyping() {
  const container = document.getElementById('project-chat-messages');
  if (!container) return null;

  const id = 'typing-' + Date.now();
  const typingDiv = document.createElement('div');
  typingDiv.className = 'ai-msg ai-msg-bot';
  typingDiv.id = id;
  typingDiv.innerHTML = `
    <div class="ai-msg-bubble" style="background:var(--canvas);">
      <div class="ai-typing-indicator">
        <span class="ai-typing-dot"></span>
        <span class="ai-typing-dot"></span>
        <span class="ai-typing-dot"></span>
      </div>
    </div>
  `;
  container.appendChild(typingDiv);
  container.scrollTop = container.scrollHeight;
  return id;
}

function removeProjectChatTyping(id) {
  if (!id) return;
  const el = document.getElementById(id);
  if (el) el.remove();
}


