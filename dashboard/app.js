'use strict';

// â”€â”€ State â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
let events = [];
let sse    = null;
let uploadedData = null; // holds parsed JSON from uploaded file

// â”€â”€ Boot â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
document.addEventListener('DOMContentLoaded', () => {
  loadInitial();
  connectSSE();
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
  sse.onerror = () => setTimeout(connectSSE, 3000);
}

// â”€â”€ Stats sync â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
function syncStats(s) {
  set('kpi-recovered',     fmtInr(s.total_recovered));
  set('kpi-events',        `${s.total_events} events processed`);
  set('kpi-rate',          s.total_events ? s.success_rate + '%' : 'â€”');
  set('kpi-success-detail',`${s.successful} handled Â· ${s.failed} failed`);
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
  }).join('') || '<span class="muted" style="font-size:11px">â€”</span>';

  tr.innerHTML = `
    <td class="muted" style="font-variant-numeric:tabular-nums;font-size:12px">${ev.timestamp || ''}</td>
    <td><span class="code-tag">${code}</span></td>
    <td class="mono">${ev.customer_vpa || ''}</td>
    <td class="muted">${ev.bank || ''}</td>
    <td class="fw6">${fmtInr(ev.amount)}</td>
    <td><span class="sev-badge sev-${sev}">${cap(sev)}</span></td>
    <td>${ivHtml}</td>
    <td>${ev.success
      ? '<span class="status-ok">âœ“ Recovered</span>'
      : '<span class="status-err">âœ— Failed</span>'}</td>`;
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
  if (runBtn) { runBtn.textContent = 'Runningâ€¦'; runBtn.disabled = true; }

  try {
    const res = await fetch('/api/simulate/all', { method: 'POST' });
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    toast(`${data.processed} scenarios processed`, 'ok');
  } catch (e) {
    toast(e.message, 'err');
  } finally {
    allBtns.forEach(b => b.classList.remove('loading'));
    if (runBtn) { runBtn.textContent = 'â–¶\u00A0 Run All Scenarios'; runBtn.disabled = false; }
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
          <p>No recovery events yet. Trigger a scenario â†’</p>
        </div>
      </td>
    </tr>`;
  syncStats({ total_events:0, total_recovered:0, successful:0, failed:0,
    success_rate:0, retries_scheduled:0, renewals_sent:0,
    escalations:0, whatsapp_sent:0, upi_collects:0 });
  toast('Dashboard cleared', 'ok');
}

// â”€â”€ Tab switching â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
function switchTab(name) {
  ['scenarios','custom','upload'].forEach(t => {
    document.getElementById('tab-' + t).classList.toggle('active', t === name);
    document.getElementById('panel-' + t).classList.toggle('hidden', t !== name);
  });
}

// â”€â”€ Custom Event Form â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
async function submitCustom(e) {
  e.preventDefault();
  const btn = document.getElementById('custom-submit-btn');
  btn.textContent = 'Processingâ€¦'; btn.disabled = true;

  const payload = {
    vpa:           document.getElementById('cf-vpa').value.trim(),
    bank:          document.getElementById('cf-bank').value,
    amount:        parseFloat(document.getElementById('cf-amount').value),
    failure_code:  document.getElementById('cf-code').value,
    retry_attempt: parseInt(document.getElementById('cf-retry').value) || 0,
  };

  try {
    const res = await fetch('/api/custom', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || 'Request failed');
    }
    const data = await res.json();
    toast(`Custom event processed â€” ${data.failure_code}`, 'ok');
    document.getElementById('custom-form').reset();
  } catch (err) {
    toast(err.message, 'err');
  } finally {
    btn.textContent = 'â–¶ Run Custom Event'; btn.disabled = false;
  }
}

// â”€â”€ File Upload (drag-drop + browse) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
function dzOver(e) {
  e.preventDefault();
  document.getElementById('drop-zone').classList.add('over');
}
function dzLeave() {
  document.getElementById('drop-zone').classList.remove('over');
}
function dzDrop(e) {
  e.preventDefault();
  dzLeave();
  const file = e.dataTransfer.files[0];
  if (file) parseFile(file);
}
function fileChosen(e) {
  const file = e.target.files[0];
  if (file) parseFile(file);
}

function parseFile(file) {
  const reader = new FileReader();
  reader.onload = ev => {
    try {
      const parsed = JSON.parse(ev.target.result);
      const arr = Array.isArray(parsed) ? parsed : [parsed];
      if (arr.length > 100) {
        toast('Max 100 events per file', 'err'); return;
      }
      uploadedData = arr;
      // Show preview
      document.getElementById('fp-name').textContent = 'ðŸ“„ ' + file.name;
      document.getElementById('fp-count').textContent = `${arr.length} event${arr.length !== 1 ? 's' : ''}`;
      document.getElementById('file-preview').classList.remove('hidden');
      document.getElementById('upload-submit-btn').disabled = false;
      toast(`${arr.length} event(s) loaded from ${file.name}`, 'ok');
    } catch {
      toast('Invalid JSON file', 'err');
      uploadedData = null;
    }
  };
  reader.readAsText(file);
}

async function submitUpload() {
  if (!uploadedData) return;
  const btn = document.getElementById('upload-submit-btn');
  btn.textContent = 'Processingâ€¦'; btn.disabled = true;

  try {
    const res = await fetch('/api/upload', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(uploadedData),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || 'Upload failed');
    }
    const data = await res.json();
    const errs = data.errors?.length || 0;
    toast(
      `${data.processed} processed${errs ? ` Â· ${errs} skipped` : ''}`,
      errs ? 'ok' : 'ok'
    );
    // Reset upload state
    uploadedData = null;
    document.getElementById('file-preview').classList.add('hidden');
    document.getElementById('file-input').value = '';
    btn.disabled = true;
  } catch (err) {
    toast(err.message, 'err');
    btn.disabled = false;
  } finally {
    btn.textContent = 'â–¶\u00A0 Process File';
  }
}

// â”€â”€ Sample JSON download â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
function downloadSample() {
  const sample = [
    { vpa: 'priya@okhdfc',    bank: 'HDFC',     amount: 1499, failure_code: 'U30',  retry_attempt: 0 },
    { vpa: 'arjun@oksbi',    bank: 'SBI',      amount: 599,  failure_code: 'BT01', retry_attempt: 1 },
    { vpa: 'meena@okaxis',   bank: 'Axis',     amount: 2999, failure_code: 'U69',  retry_attempt: 0 },
    { vpa: 'suresh@okicici', bank: 'ICICI',    amount: 799,  failure_code: 'TM',   retry_attempt: 2 },
    { vpa: 'deepa@paytm',    bank: 'Paytm',    amount: 299,  failure_code: 'U13',  retry_attempt: 0 },
  ];
  const blob = new Blob([JSON.stringify(sample, null, 2)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'sample_upi_events.json';
  a.click();
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
  if (n === undefined || n === null) return 'â€”';
  return 'â‚¹' + Number(n).toLocaleString('en-IN', { maximumFractionDigits: 0 });
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

// Explicit exports to window object for inline HTML event handlers
window.switchTab = switchTab;
window.submitCustom = submitCustom;
window.runAll = runAll;
window.runScenario = runScenario;
window.resetAll = resetAll;
window.dzOver = dzOver;
window.dzLeave = dzLeave;
window.dzDrop = dzDrop;
window.fileChosen = fileChosen;
window.submitUpload = submitUpload;
window.downloadSample = downloadSample;
