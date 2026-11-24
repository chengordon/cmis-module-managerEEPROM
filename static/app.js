/* CMIS Module Manager — Frontend Logic */
'use strict';

// ---------------------------------------------------------------------------
// App state
// ---------------------------------------------------------------------------
const AppState = {
  connected: false,
  currentTab: 'info',
  monitoringInterval: null,
  monitoringIntervalMs: 2000,
  monitoringManual: false,
  // Optional periodic EEPROM read, armed by clicking Read while 烧写空白 is
  // ticked (for unattended blank-chip programming). Never starts on its own.
  eepromAutoReadInterval: null,
  eepromAutoReadIntervalMs: 2000,
  backendsCache: [],   // cached result of /api/backends
};

// Fallback optical power limits (dBm), used only until the module's own
// Page 02h thresholds have been read. Colouring by these when the module
// advertises different limits would contradict both the Thresholds card and
// the module's own alarm flags.
const ALARM_FALLBACK = { TX_LOW: -10, TX_HIGH: 3, RX_LOW: -10, RX_HIGH: 3 };
let _moduleThresholds = null;
let _advertisedApps = [];
// Last successful EEPROM dump, kept so Save .bin / Copy Hex never re-read the bus.
let _lastEeprom = null;
// The image currently shown in the content area AND used by Write:
//   { name, bytes: Uint8Array, start, origin: 'file' | 'chip' }
// A successful Read fills it from the chip; selecting a .bin fills it from
// disk. Keeping ONE source means Write always programs exactly what the user
// sees — there is no hidden second buffer. Nothing is uploaded until Write.
let _eepromImage = null;
// The .bin the operator actually selected via the file picker. Read replaces
// _eepromImage with a chip dump, so this separate handle is what the
// "blank chip → auto-program" path re-reads. Cleared only by a fresh picker
// selection with no file, mirroring the 刷新 button's file source.
let _loadedBinFile = null;

// ---------------------------------------------------------------------------
// Register hover tooltips
//
// Every setting on screen maps to a byte in the module's memory. These build a
// consistent hover string naming the CMIS field, its Page and byte address, and
// what that byte reads right now, so any displayed value can be traced back to
// the register it came from without cross-referencing the spec.
// ---------------------------------------------------------------------------
/** Escape for interpolation into HTML text or a quoted attribute. */
const esc = s => String(s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

const hex8  = v => '0x' + (v & 0xFF).toString(16).toUpperCase().padStart(2, '0');
const bin8  = v => '0b' + (v & 0xFF).toString(2).padStart(8, '0');
const pageName = p => (p === null || p === undefined)
  ? 'Lower Memory'
  : `Page ${p.toString(16).toUpperCase().padStart(2, '0')}h`;

/**
 * @param {object} o
 * @param {string} o.field  CMIS field name, e.g. "AutoSquelchDisableTx"
 * @param {?number} o.page  page number, null/undefined for Lower Memory
 * @param {number} o.addr   byte address
 * @param {number} [o.value] current raw byte
 * @param {number} [o.bit]   bit index within the byte, when the control is one bit
 * @param {string} [o.note]  extra line, e.g. decoded meaning or units
 */
function regTip(o) {
  const lines = [o.field,
    `${pageName(o.page)} · byte ${hex8(o.addr)} hex = ${o.addr} dec`];
  if (o.value !== undefined && o.value !== null && !Number.isNaN(o.value)) {
    const v = o.value & 0xFF;
    lines.push(`Current value: ${hex8(v)} hex = ${bin8(v)} bin = ${v} dec`);
    if (o.bit !== undefined && o.bit !== null) {
      lines.push(`Bit ${o.bit} = ${(o.value >> o.bit) & 1}`);
    }
  }
  if (o.note) lines.push(o.note);
  return lines.join('\n');
}

/**
 * Write, then re-read the panel from the module.
 *
 * A module may reject or clamp a control value (CMIS lets it report
 * ConfigRejected), so the panel must show what the device actually holds
 * afterwards, not what we asked for. Re-reading also refreshes the register
 * tooltips, which quote the current byte.
 */
async function applyAndReload(label, path, body, reload) {
  const res = await apiPost(path, body);
  if (res.status !== 'ok') {
    toast(`Apply failed: ${res.message}`, 'error');
    return res;
  }
  await reload();
  toast(`${label} applied`, 'success');
  return res;
}

/** Multi-byte field (e.g. a 16-byte ASCII string or a 2-byte word). */
function regTipRange(field, page, addr, len, note) {
  const end = addr + len - 1;
  const lines = [field,
    `${pageName(page)} · bytes ${hex8(addr)}-${hex8(end)} hex = ${addr}-${end} dec`];
  if (note) lines.push(note);
  return lines.join('\n');
}

// ---------------------------------------------------------------------------
// API helpers
// ---------------------------------------------------------------------------
async function apiFetch(method, path, body) {
  const opts = {
    method,
    headers: { 'Content-Type': 'application/json' },
  };
  if (body !== undefined) opts.body = JSON.stringify(body);
  try {
    const resp = await fetch(path, opts);
    return await resp.json();
  } catch (e) {
    return { status: 'error', message: e.message };
  }
}

const apiGet  = (path)       => apiFetch('GET',  path);
const apiPost = (path, body) => apiFetch('POST', path, body);

// ---------------------------------------------------------------------------
// Toast notifications
// ---------------------------------------------------------------------------
function toast(message, type = 'info', durationMs = 3000) {
  const container = document.getElementById('toast-container');
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  el.textContent = message;
  container.appendChild(el);
  setTimeout(() => el.remove(), durationMs);
}

// ---------------------------------------------------------------------------
// Display preferences
// ---------------------------------------------------------------------------
// Everything lands on <html>: the post-update screen replaces document.body
// wholesale, and anything parked there would go with it. The same key is read
// by the inline bootstrap in index.html, which applies all of this before the
// first paint; this module only handles later changes.
const PREFS_KEY = 'cmis.ui';
const PREFS_DEFAULT = Object.freeze({
  v: 1, theme: 'midnight', scale: 1, scaleAuto: true, fontStep: 0, fontSans: 'system',
});

// innerWidth is CSS pixels, so Windows DPI scaling and browser zoom are already
// folded in - it measures usable workspace, not pixel density. Multiplying by
// devicePixelRatio on top would double-count and blow up a 125%-scaled laptop.
// Only ever scales up: a small viewport means a small screen, and shrinking the
// text there is backwards.
function autoScale(width) {
  if (width >= 3000) return 1.5;
  if (width >= 2400) return 1.35;
  if (width >= 1900) return 1.15;
  return 1;
}

function loadPrefs() {
  try {
    const raw = JSON.parse(localStorage.getItem(PREFS_KEY) || 'null');
    if (raw && typeof raw === 'object') return { ...PREFS_DEFAULT, ...raw };
  } catch (e) { /* corrupt preferences must not cost the user the whole UI */ }
  return { ...PREFS_DEFAULT };
}

function savePrefs(prefs) {
  try {
    localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
  } catch (e) {
    toast('Could not save display settings in this browser', 'error');
  }
}

function applyPrefs(prefs) {
  const root = document.documentElement;
  root.setAttribute('data-theme', prefs.theme);
  root.setAttribute('data-font-sans', prefs.fontSans);
  // The unit is not optional: calc(10px + 2) is invalid at computed-value time,
  // which would silently collapse every font-size to its inherited value.
  root.style.setProperty('--fs-step', prefs.fontStep + 'px');
  root.style.setProperty(
    '--ui-zoom', prefs.scaleAuto ? autoScale(window.innerWidth) : prefs.scale);
}

function initSettings() {
  const dialog = document.getElementById('settings-dialog');
  const theme = document.getElementById('set-theme');
  const scale = document.getElementById('set-scale');
  const step = document.getElementById('set-fontstep');
  const font = document.getElementById('set-font');
  if (!dialog) return;

  let prefs = loadPrefs();

  const toForm = () => {
    theme.value = prefs.theme;
    scale.value = prefs.scaleAuto ? 'auto' : String(prefs.scale);
    step.value = String(prefs.fontStep);
    font.value = prefs.fontSans;
  };
  const commit = () => { applyPrefs(prefs); savePrefs(prefs); };

  applyPrefs(prefs);
  toForm();

  // Applied live, saved immediately: a single-user local tool has no reason to
  // make someone confirm what they can already see.
  theme.addEventListener('change', () => { prefs.theme = theme.value; commit(); });
  font.addEventListener('change', () => { prefs.fontSans = font.value; commit(); });
  step.addEventListener('change', () => { prefs.fontStep = parseInt(step.value, 10); commit(); });
  scale.addEventListener('change', () => {
    prefs.scaleAuto = scale.value === 'auto';
    if (!prefs.scaleAuto) prefs.scale = parseFloat(scale.value);
    commit();
  });

  document.getElementById('btn-settings-reset')?.addEventListener('click', () => {
    prefs = { ...PREFS_DEFAULT };
    commit();
    toForm();
  });

  document.getElementById('btn-settings')?.addEventListener('click', () => {
    toForm();
    dialog.showModal();
  });

  // Clicking the backdrop lands on the dialog element itself, not the form.
  dialog.addEventListener('click', (e) => { if (e.target === dialog) dialog.close(); });
}

// ---------------------------------------------------------------------------
// Connection panel
// ---------------------------------------------------------------------------

// Bench default. /api/backends lists the four mock profiles first; without an
// explicit default the browser would open on mock_coherent, which is useless
// on a programming bench with a CH341A adapter plugged in.
const DEFAULT_BACKEND = 'ch341';

async function loadBackends() {
  const sel = document.getElementById('sel-backend');
  if (!sel) return;

  // Remember the currently selected value so a manual "Refresh Backends"
  // keeps the user's choice. On first load this is the HTML placeholder
  // (ch341), which doubles as the default selection.
  const previousValue = sel.value;

  const res = await apiGet('/api/backends');
  if (res.status !== 'ok') { toast('Failed to load backends', 'error'); return; }

  // Cache backends list for later use (e.g. backend info area)
  AppState.backendsCache = res.data || [];

  sel.innerHTML = '';
  for (const b of AppState.backendsCache) {
    const opt = document.createElement('option');
    opt.value = b.name;
    opt.textContent = `${b.name}${b.available ? '' : ' (unavailable)'}`;
    opt.disabled = !b.available;
    sel.appendChild(opt);
  }

  // Prefer the current selection, then the bench default, then the first
  // available backend. Only available entries are selectable — a disabled
  // option would silently leave the dropdown with no value.
  const availableName = name =>
    AppState.backendsCache.find(b => b.name === name && b.available)?.name;
  const target = availableName(previousValue) || availableName(DEFAULT_BACKEND)
    || AppState.backendsCache.find(b => b.available)?.name;
  if (target) {
    sel.value = target;
  } else if (AppState.backendsCache.some(b => b.name === DEFAULT_BACKEND)) {
    // Nothing is available: still show the default so Connect reports a
    // meaningful backend name instead of an empty string.
    sel.value = DEFAULT_BACKEND;
  }
}

async function connectModule() {
  const backend = document.getElementById('sel-backend').value;
  const bus     = parseInt(document.getElementById('inp-bus').value, 10) || 0;
  const addrStr = document.getElementById('inp-address').value.trim() || '0xA0';
  const rawAddr = parseHexOrDec(addrStr);
  if (!Number.isFinite(rawAddr) || rawAddr < 0 || rawAddr > 0xFF) {
    toast(`Invalid I2C address: ${addrStr}`, 'error');
    return;
  }
  // The UI speaks 8-bit addresses (0xA0); the API/driver speak 7-bit (0x50).
  const address = uiAddressTo7Bit(rawAddr);

  setConnectBusy(true);
  const res = await apiPost('/api/connect', { backend, bus, address });
  setConnectBusy(false);

  if (res.status !== 'ok') {
    toast(`Connect failed: ${res.message}`, 'error', 5000);
    return;
  }
  AppState.connected = true;
  updateConnectionUI(true, `${backend}  bus:${bus}  addr:${format8BitAddress(address)}`);
  updateBackendInfoArea(backend);
  // Keep the EEPROM tab's base address in step with the connected device.
  const eepBase = document.getElementById('eep-base');
  if (eepBase) eepBase.value = format8BitAddress(address);
  toast('Connected', 'success');
  // Auto-load info tab
  switchTab('info');
  loadInfo();
}

async function disconnectModule() {
  await apiGet('/api/disconnect');
  AppState.connected = false;
  // The next module advertises its own limits and its own Applications.
  _moduleThresholds = null;
  _advertisedApps = [];
  stopMonitoring();
  stopEepromAutoRead();   // halt periodic blank-chip reads on disconnect
  updateConnectionUI(false, '');
  clearBackendInfoArea();
  toast('Disconnected', 'info');
  clearTabContent();
  // Switch back to the info tab so the active tab matches the enabled state
  switchTab('info');
}

function setConnectBusy(busy) {
  document.getElementById('btn-connect').disabled = busy;
  document.getElementById('btn-connect').innerHTML = busy
    ? '<span class="spinner"></span> Connecting…'
    : 'Connect';
}

function updateConnectionUI(connected, label) {
  const dot  = document.getElementById('status-dot');
  const text = document.getElementById('status-text');
  const btnConn  = document.getElementById('btn-connect');
  const btnDisc  = document.getElementById('btn-disconnect');

  dot.className  = `status-dot ${connected ? 'connected' : 'disconnected'}`;
  text.textContent = connected ? `Connected — ${label}` : 'Disconnected';
  btnConn.disabled = connected;
  btnDisc.disabled = !connected;

  // Enable/disable tabs
  document.querySelectorAll('.tab-btn[data-tab]').forEach(btn => {
    btn.disabled = !connected;
  });
  updateEepromWriteButton();
}

function updateBackendInfoArea(backendName) {
  const area = document.getElementById('backend-info-area');
  if (!area) return;

  const info = AppState.backendsCache.find(b => b.name === backendName);
  if (!info) {
    area.innerHTML = `<span style="color:var(--text)">Connected to <strong>${backendName}</strong></span>`;
    return;
  }

  const availBadge = info.available
    ? '<span class="badge badge-ok">available</span>'
    : '<span class="badge badge-off">unavailable</span>';

  const descHtml = info.description
    ? `<div style="margin-top:6px;color:var(--text)">${info.description}</div>`
    : '';

  area.innerHTML =
    `<div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap">` +
    `  <strong style="color:var(--text)">${info.name}</strong>${availBadge}` +
    `</div>` +
    descHtml;
}

function clearBackendInfoArea() {
  const area = document.getElementById('backend-info-area');
  if (area) {
    area.innerHTML = 'Select a backend and connect to view info.';
  }
}

function clearTabContent() {
  const tableIds = [
    'tbl-info', 'tbl-monitoring', 'tbl-datapath', 'tbl-flags', 'tbl-thresholds',
    'tbl-ber', 'tbl-snr', 'tbl-counters', 'tbl-laser', 'tbl-apps', 'tbl-control',
    'tbl-prbs-host-gen', 'tbl-prbs-media-gen', 'tbl-prbs-host-chk', 'tbl-prbs-media-chk',
  ];
  tableIds.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.innerHTML = '';
  });
  const dump = document.getElementById('hex-dump');
  if (dump) dump.textContent = '';
  // A dump from the previous module must not stay downloadable after a
  // reconnect — that would silently save the old chip's contents.
  _lastEeprom = null;
  const eepDump = document.getElementById('eep-dump');
  if (eepDump) eepDump.textContent = '';
  const eepSummary = document.getElementById('eep-summary');
  if (eepSummary) eepSummary.textContent = '';
  const eepStatus = document.getElementById('eep-status');
  if (eepStatus) eepStatus.textContent = 'Ready.';
  ['btn-eep-export', 'btn-eep-copy'].forEach(id => {
    const b = document.getElementById(id);
    if (b) b.disabled = true;
  });
  // A chip readback belongs to the part that was disconnected, so it must not
  // be writable on the next one. A .bin template loaded from disk stays.
  if (_eepromImage && _eepromImage.origin === 'chip') _eepromImage = null;
  // 确认 / Confirm is a user preference ("ask before writing"), not a one-shot
  // arm flag, so keep its state across reconnects. Only an open dialog is
  // dismissed — a write that was never confirmed must not survive a reconnect.
  const eepDlg = document.getElementById('eep-confirm-dialog');
  if (eepDlg?.open && typeof eepDlg.close === 'function') eepDlg.close();
  updateEepromWriteButton();
  updateEepromPreview();
  const laserCaps = document.getElementById('laser-caps');
  if (laserCaps) laserCaps.innerHTML = '';

  // The Output Controls and Loopback checkboxes live in cells the table sweep
  // above does not touch, and the summary line keeps its last reading. Without
  // this, reconnecting to a different module briefly shows the previous one's
  // squelch settings, temperature and voltage as if they were the new module's.
  ['sq', 'sf', 'od', 'rd'].forEach(p => {
    for (let i = 0; i < 8; i++) {
      const td = document.getElementById(`${p}-td-${i}`);
      if (td) td.innerHTML = '';
    }
  });
  ['mso', 'msi', 'hso', 'hsi'].forEach(p => {
    for (let i = 0; i < 8; i++) {
      const td = document.getElementById(`lb-${p}-${i}`);
      if (td) td.innerHTML = '';
    }
  });
  const summary = document.getElementById('monitor-summary');
  if (summary) summary.innerHTML = '';
  clearMonitoringStale();
}

// ---------------------------------------------------------------------------
// Tab switching
// ---------------------------------------------------------------------------
function switchTab(name) {
  if (!AppState.connected && name !== 'info') return;

  // Stop monitoring if leaving that tab
  if (AppState.currentTab === 'monitoring' && name !== 'monitoring') {
    stopMonitoring();
  }

  AppState.currentTab = name;

  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));

  const btn   = document.querySelector(`.tab-btn[data-tab="${name}"]`);
  const panel = document.getElementById(`tab-${name}`);
  if (btn)   btn.classList.add('active');
  if (panel) panel.classList.add('active');

  // Auto-load data for tab
  if (name === 'info')        loadInfo();
  if (name === 'monitoring')  startMonitoring();
  // The AppSelect dropdown is built from the advertised Applications, so they
  // must be in hand before the DataPath table renders.
  if (name === 'datapath') {
    loadModuleControl();
    loadSquelch();
    loadApplications().then(loadDatapath);
  }
  // Load the whole Diagnostics tab, not half of it: BER, SNR, counters and
  // laser tuning used to sit empty until each card's own refresh was clicked,
  // so half the page showed live data next to placeholders or values from
  // before a reset.
  if (name === 'diagnostics') {
    loadLoopback(); loadPrbs(); loadBer(); loadSnr(); loadCounters(); loadLaser();
  }
}

// ---------------------------------------------------------------------------
// Module Info tab
// ---------------------------------------------------------------------------
async function loadInfo() {
  if (!AppState.connected) return;

  const [infoRes, statusRes] = await Promise.all([
    apiGet('/api/module/info'),
    apiGet('/api/module/status'),
  ]);

  const tbody = document.getElementById('tbl-info');
  if (!tbody) return;

  if (infoRes.status !== 'ok') { toast(`Info error: ${infoRes.message}`, 'error'); return; }
  if (statusRes.status !== 'ok') { toast(`Status error: ${statusRes.message}`, 'error'); return; }

  // Strings here come straight out of module EEPROM, so escape them rather
  // than trusting a device to hand back clean ASCII.
  const d = Object.fromEntries(Object.entries(infoRes.data)
    .map(([k, v]) => [k, typeof v === 'string' ? esc(v) : v]));
  const s = Object.fromEntries(Object.entries(statusRes.data)
    .map(([k, v]) => [k, typeof v === 'string' ? esc(v) : v]));

  // [field, value, page, address, cmis_definition]
  const rows = [
    ['Module Type',     d.module_type,                                                            'Lower', '0x00',        'SFF-8024 Identifier (Table 8-5)'],
    ['Module ID',       `0x${(d.module_id||0).toString(16).toUpperCase().padStart(2,'0')}`,      'Lower', '0x00',        'Identifier byte (raw hex)'],
    ['CMIS Revision',   d.cmis_revision,                                                          'Lower', '0x01',        'Upper nibble=major, lower=minor (0x53=5.3)'],
    ['Memory Model',    d.memory_model,                                                           'Lower', '0x02[7]',     '0=Paged, 1=Flat'],
    ['Media Type',      d.media_type,                                                             'Lower', '0x55',        'Media type code (Table 8-21)'],
    ['Module State',    s.module_state,                                                           'Lower', '0x03[3:1]',   'Current state machine (Table 8-7)'],
    ['Vendor Name',     d.vendor_name,                                                            '00h',   '0x81–0x90',   'Vendor Name, 16-byte ASCII'],
    ['Vendor OUI',      d.vendor_oui,                                                             '00h',   '0x91–0x93',   'IEEE OUI (3 bytes hex)'],
    ['Vendor P/N',      d.vendor_pn,                                                              '00h',   '0x94–0xA3',   'Vendor Part Number, 16-byte ASCII'],
    ['Vendor Rev',      d.vendor_rev,                                                             '00h',   '0xA4–0xA5',   'Vendor Revision, 2-byte ASCII'],
    ['Vendor S/N',      d.vendor_sn,                                                              '00h',   '0xA6–0xB5',   'Vendor Serial Number, 16-byte ASCII'],
    ['Date Code',       d.date_code,                                                              '00h',   '0xB6–0xBD',   'Date Code, 8-byte ASCII (YYMMDDLL)'],
    ['CLEI Code',       d.clei_code || '(none)',                                                  '00h',   '0xBE–0xC7',   'CLEI Code, 10-byte ASCII'],
    ['Power Class',     `Class ${d.power_class}`,                                                 '00h',   '0xC8[7:5]',   'Module Power Class (1–8)'],
    ['Max Power',       `${d.max_power_w} W`,                                                     '00h',   '0xC9',        'Maximum Power Consumption (×0.25 W)'],
    ['Cable Length',    d.cable_length_m === 0 ? '— (transceiver)' : `${d.cable_length_m} m`,    '00h',   '0xCA',        '[7:6]=mult, [5:0]=base (m)'],
    ['Connector',       `${d.connector_type} (0x${(d.connector_code||0).toString(16).toUpperCase().padStart(2,'0')})`, '00h', '0xCB', 'SFF-8024 Connector Type (Table 4-3)'],
    ['Media Interface', `${d.media_if_tech} (0x${(d.media_if_tech_code||0).toString(16).toUpperCase().padStart(2,'0')})`, '00h', '0xD4', 'Media Interface Technology (Table 8-40)'],
    ['Host Lanes',      d.lanes_detail ? `${d.host_lanes} <span style="color:var(--text-muted);font-size:var(--fs-xs)">(${d.lanes_detail})</span>` : `${d.host_lanes}`,  'Lower', '0x56+', 'Max concurrent host lanes across all AppDescriptors'],
    ['Media Lanes',     `${d.media_lanes}`, 'Lower', '0x56+', 'Max concurrent media lanes'],
    ['FW Revision',     d.fw_revision,                                                            'Lower', '0x27–0x28',   'Module Active Firmware Major.Minor'],
    ['HW Revision',     d.hw_revision,                                                            '01h',   '0x82–0x83',   'Hardware Revision Major.Minor'],
    ['Temperature',     `${s.temperature_c?.toFixed(2)} °C`,                                     'Lower', '0x0E–0x0F',   'Module Temperature (s16/256)'],
    ['Supply Voltage',  `${s.voltage_v?.toFixed(4)} V`,                                          'Lower', '0x10–0x11',   'Supply Voltage (u16 × 100 µV)'],
    ['Alarms',          s.alarm_active ? '<span class="text-danger">Active</span>' : '<span class="text-success">None</span>', 'Lower', '0x08–0x0D', 'Module-Level Flags'],
  ];

  tbody.innerHTML = rows.map(([k, v, pg, addr, def]) => {
    const plain = String(v).replace(/<[^>]*>/g, '').replace(/\s+/g, ' ').trim();
    const tip = esc([
      k,
      `${pg === 'Lower' ? 'Lower Memory' : 'Page ' + pg} · byte ${addr}`,
      `Current: ${plain}`,
      def,
    ].join('\n'));
    return `<tr title="${tip}">
      <td style="color:var(--text-muted);width:140px">${k}</td>
      <td>${v}</td>
      <td class="td-page">${pg}</td>
      <td class="td-addr">${addr}</td>
      <td class="td-def">${def}</td>
    </tr>`;
  }).join('');
}

// ---------------------------------------------------------------------------
// Monitoring tab
// ---------------------------------------------------------------------------
function startMonitoring() {
  stopMonitoring();
  loadThresholds();
  loadMonitoring();
  if (AppState.monitoringManual) return;
  AppState.monitoringInterval = setInterval(loadMonitoring, AppState.monitoringIntervalMs);
}

function stopMonitoring() {
  if (AppState.monitoringInterval) {
    clearInterval(AppState.monitoringInterval);
    AppState.monitoringInterval = null;
  }
}

// ---------------------------------------------------------------------------
// EEPROM periodic read ("烧写空白" arm)
// ---------------------------------------------------------------------------
/**
 * Start re-reading the EEPROM every 2 s, for unattended blank-chip
 * programming. Only called from an explicit Read click while the 烧写空白
 * checkbox is ticked — it never self-starts. stopEepromAutoRead() is idempotent
 * so repeated Read clicks never stack multiple timers.
 */
function startEepromAutoRead() {
  stopEepromAutoRead();
  AppState.eepromAutoReadInterval = setInterval(() => {
    const cb = document.getElementById('eep-blank-program');
    // Self-heal: unticking the box or disconnecting stops the loop even if the
    // change/disconnect handlers did not run for some reason.
    if (!cb?.checked || !AppState.connected) { stopEepromAutoRead(); return; }
    // No re-entrancy: a read (which may also program a blank chip) holds the
    // Read button disabled until it finishes; skip ticks that land mid-flight.
    const btn = document.getElementById('btn-eep-read');
    if (btn?.disabled) return;
    eepromRead();
  }, AppState.eepromAutoReadIntervalMs);
}

function stopEepromAutoRead() {
  if (AppState.eepromAutoReadInterval) {
    clearInterval(AppState.eepromAutoReadInterval);
    AppState.eepromAutoReadInterval = null;
  }
}

// ---------------------------------------------------------------------------
// Update check
//
// Only ever triggered by the button: this tool runs on isolated lab networks
// and must not reach out to GitHub on its own.
// ---------------------------------------------------------------------------
async function checkForUpdate() {
  const btn = document.getElementById('btn-check-update');
  if (!btn) return;
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Checking…';

  const res = await apiGet('/api/update/check');
  btn.disabled = false;

  if (res.status !== 'ok') {
    btn.textContent = label;
    toast(res.message, 'error', 9000);
    return;
  }

  const d = res.data;
  if (!d.update_available) {
    btn.textContent = label;
    btn.classList.remove('update-available');
    toast(`Already on the newest version (v${d.current_version})`, 'success');
    return;
  }

  btn.textContent = `↑ v${d.latest_version}`;
  btn.classList.add('update-available');

  const mb = (d.asset_size / 1048576).toFixed(1);
  if (!d.can_self_update) {
    // Running from source: replacing an executable is meaningless here.
    toast(`v${d.latest_version} is available. Running from source — `
          + 'use git pull to upgrade.', 'success', 9000);
    window.open(d.release_url, '_blank', 'noopener');
    return;
  }

  const ok = confirm(
    `A newer version is available.\n\n`
    + `Installed: v${d.current_version}\nAvailable: v${d.latest_version}  (${mb} MB)\n\n`
    + `Download and install it now?\n\n`
    + `The tool closes once the files are replaced, then you start `
    + `CMIS_Module_Manager.exe again — one double-click.\n\n`
    + `Disconnect from the module first if a measurement is running.`);
  if (!ok) return;

  btn.disabled = true;
  btn.textContent = 'Updating…';
  toast(`Downloading v${d.latest_version} (${mb} MB)…`, 'success', 20000);

  const applied = await apiPost('/api/update/apply', {});
  if (applied.status !== 'ok') {
    btn.disabled = false;
    btn.textContent = `↑ v${d.latest_version}`;
    toast(applied.message, 'error', 12000);
    return;
  }

  const outcome = await _followUpdateProgress(btn);
  if (outcome.state === 'ready') {
    await _waitForNewVersion(applied.data.version);
  } else {
    btn.disabled = false;
    btn.textContent = `↑ v${d.latest_version}`;
    toast(outcome.message || 'The update did not complete', 'error', 12000);
  }
}

/**
 * Report the background download until it finishes, one way or the other.
 *
 * Sixteen megabytes takes seconds on a good link and the better part of an
 * hour on a slow one, and without a number on screen those two look identical
 * - the second one looks like a hang, and the natural response is to kill the
 * tool. So the button carries the percentage, and the source that won the
 * speed probe is named once it is known.
 */
async function _followUpdateProgress(btn) {
  let announced = '';
  for (;;) {
    await new Promise(r => setTimeout(r, 1000));
    let p;
    try {
      const r = await fetch('/api/update/progress', { cache: 'no-store' });
      p = (await r.json()).data;
    } catch (e) {
      // The old build exits as soon as the swap is handed over, so the poll
      // failing is the expected end of a successful update, not a fault.
      return { state: 'ready' };
    }
    if (p.state === 'ready' || p.state === 'error') return p;

    if (p.state === 'downloading' && p.total) {
      const pct = Math.floor(p.done / p.total * 100);
      btn.textContent = `Updating… ${pct}%`;
      if (p.source && p.source !== announced) {
        announced = p.source;
        toast(`Downloading from ${p.source}`, 'success', 6000);
      }
    } else {
      btn.textContent = p.state === 'probing' ? 'Picking a source…'
                      : p.state === 'verifying' ? 'Verifying…'
                      : p.state === 'installing' ? 'Installing…'
                      : 'Updating…';
    }
  }
}

/**
 * Hold the page while the install is swapped, then reload into the new build.
 *
 * The updated instance is started with the browser suppressed, so this tab is
 * the one the user keeps looking at - it has to come back on its own rather
 * than ask them to relaunch anything.
 */
async function _waitForNewVersion(expected) {
  // Say what to do straight away. Spinning for a minute first, on the chance
  // the process relaunches itself, just looks like the tool has hung — the
  // files are already updated by this point and one double-click finishes it.
  document.body.innerHTML =
    `<div style="display:flex;align-items:center;justify-content:center;`
    + `height:100%;font-family:var(--font-sans);text-align:center;padding:24px">`
    + `<div style="max-width:520px">`
    + `<div style="font-size:3em;line-height:1">✓</div>`
    + `<h2 style="color:var(--success-fg);margin:12px 0 4px">Updated to v${esc(expected)}</h2>`
    + `<p style="color:var(--accent-fg);font-size:var(--fs-h1);margin:18px 0 6px">`
    + `Start <b>CMIS_Module_Manager.exe</b> again to continue.</p>`
    + `<p style="color:var(--text-muted);line-height:1.7;font-size:var(--fs-md)">`
    + `The new version is already installed — the old console window has closed. `
    + `This page reconnects on its own if the tool comes back up.<br>`
    + `A record of the update is in <code>update.log</code> next to the exe.</p>`
    + `</div></div>`;

  // Keep watching quietly: if the relaunch did work, the user should not have
  // to do anything after all.
  for (let i = 0; i < 60; i++) {
    await new Promise(r => setTimeout(r, 1000));
    try {
      const r = await fetch('/api/version', { cache: 'no-store' });
      const j = await r.json();
      if (j.status === 'ok' && j.data.version === expected) {
        location.reload();
        return;
      }
    } catch (e) { /* expected while the old instance is gone */ }
  }
}

/**
 * Optical power limits to colour by: the module's own advertised alarm
 * thresholds when they have been read, otherwise the generic fallback.
 *
 * A module can advertise anything - mock_dr8 alarms Tx low at -8.0 dBm, not
 * -10 - so colouring by a fixed pair made the monitoring table, the Thresholds
 * card and the module's Lane Flags give three different answers about the same
 * lane.
 */
function _powerLimits() {
  const t = _moduleThresholds;
  if (!t) return ALARM_FALLBACK;
  const num = (v, fb) => (typeof v === 'number' && Number.isFinite(v)) ? v : fb;
  return {
    TX_LOW:  num(t.tx_power_low_alarm_dbm,  ALARM_FALLBACK.TX_LOW),
    TX_HIGH: num(t.tx_power_high_alarm_dbm, ALARM_FALLBACK.TX_HIGH),
    RX_LOW:  num(t.rx_power_low_alarm_dbm,  ALARM_FALLBACK.RX_LOW),
    RX_HIGH: num(t.rx_power_high_alarm_dbm, ALARM_FALLBACK.RX_HIGH),
  };
}

/** Say plainly that what is on screen is no longer live. */
function markMonitoringStale(reason) {
  const el = document.getElementById('monitor-stale');
  if (el) {
    el.innerHTML = `⚠ Readings below are STALE — last refresh failed: `
      + `${esc(reason || 'unknown error')}`;
    el.style.display = '';
  }
  document.getElementById('tbl-monitoring')?.closest('table')
    ?.classList.add('stale-data');
  document.getElementById('tbl-flags')?.closest('table')
    ?.classList.add('stale-data');
}

function clearMonitoringStale() {
  const el = document.getElementById('monitor-stale');
  if (el) el.style.display = 'none';
  document.querySelectorAll('.stale-data')
    .forEach(node => node.classList.remove('stale-data'));
}

async function loadMonitoring() {
  if (!AppState.connected) return;

  const [monRes, statusRes, flagsRes] = await Promise.all([
    apiGet('/api/module/monitoring'),
    apiGet('/api/module/status'),
    apiGet('/api/module/flags'),
  ]);

  // Any failure here would otherwise leave the previous reading on screen while
  // the page still looks live - the operator would read minutes-old power
  // levels, or worse, all-green lane flags, as the current state.
  if (monRes.status !== 'ok') {
    toast(`Monitoring error: ${monRes.message}`, 'error');
    markMonitoringStale(monRes.message);
    stopMonitoring();  // halt auto-refresh so a dead server doesn't spam toasts
    return;
  }
  if (statusRes.status !== 'ok') {
    toast(`Status error: ${statusRes.message}`, 'error');
    markMonitoringStale(statusRes.message);
    return;
  }
  if (flagsRes.status === 'ok') {
    renderFlags(flagsRes.data.lanes);
    clearMonitoringStale();
  } else {
    toast(`Lane flags error: ${flagsRes.message}`, 'error');
    markMonitoringStale(flagsRes.message);
  }

  const s = statusRes.data;
  const summaryEl = document.getElementById('monitor-summary');
  if (summaryEl) {
    const tempClass = s.temperature_c > 70 ? 'text-danger' : s.temperature_c > 60 ? 'text-warning' : 'text-success';
    summaryEl.innerHTML =
      `<span class="${tempClass}">Temp: ${s.temperature_c?.toFixed(2)} °C</span>` +
      `&ensp;|&ensp;<span>Voltage: ${s.voltage_v?.toFixed(4)} V</span>` +
      (s.alarm_active ? `&ensp;|&ensp;<span class="text-danger">⚠ Alarm Active</span>` : '');
  }

  const tbody = document.getElementById('tbl-monitoring');
  if (!tbody) return;

  const lanes = monRes.data.lanes;
  const allActivated = lanes.length > 0 && lanes.every(l => l.datapath_state === 'Activated');
  const dotEl = document.getElementById('monitor-all-activated-dot');
  if (dotEl) dotEl.style.display = allActivated ? 'inline-block' : 'none';

  tbody.innerHTML = lanes.map(lane => {
    const txDbm = lane.tx_power_dbm;
    const rxDbm = lane.rx_power_dbm;
    const lim = _powerLimits();
    const txCls = txDbm < lim.TX_LOW ? 'alarm-low' : txDbm > lim.TX_HIGH ? 'alarm-high' : '';
    const rxCls = rxDbm < lim.RX_LOW ? 'alarm-low' : rxDbm > lim.RX_HIGH ? 'alarm-high' : '';
    const stateClass = lane.datapath_state === 'Activated'
      ? 'state-activated' : lane.datapath_state === 'Init'
      ? 'state-init' : 'state-deactivated';

    const cfgStatus = lane.config_status || '—';
    // A rejected configuration is a failure, so it must not share the muted
    // grey used for "this lane is simply not in use".
    const cfgClass = cfgStatus === 'ConfigSuccess' ? 'state-activated'
                   : cfgStatus === 'ConfigInProgress' ? 'state-init'
                   : cfgStatus.startsWith('ConfigRejected') ? 'flag-active'
                   : 'state-deactivated';
    return `<tr>
      <td>${lane.lane}</td>
      <td class="${txCls}">${lane.tx_power_uw.toFixed(1)} µW<br><small>${txDbm.toFixed(2)} dBm</small></td>
      <td>${lane.tx_bias_ma.toFixed(3)} mA</td>
      <td class="${rxCls}">${lane.rx_power_uw.toFixed(1)} µW<br><small>${rxDbm.toFixed(2)} dBm</small></td>
      <td class="${stateClass}">${lane.datapath_state}</td>
      <td class="${cfgClass}">${cfgStatus}</td>
    </tr>`;
  }).join('');
}

function setRefreshInterval(ms) {
  AppState.monitoringIntervalMs = ms;
  // Restart monitoring if we are currently on the monitoring tab
  // (covers both the case where interval was running and the case
  //  where the user switched back from "Manual" mode)
  if (AppState.connected && AppState.currentTab === 'monitoring') {
    startMonitoring();
  }
}

// ---------------------------------------------------------------------------
// DataPath config tab
// ---------------------------------------------------------------------------
async function loadDatapath() {
  if (!AppState.connected) return;

  const res = await apiGet('/api/module/datapath');
  if (res.status !== 'ok') { toast(`DataPath error: ${res.message}`, 'error'); return; }

  const tbody = document.getElementById('tbl-datapath');
  if (!tbody) return;

  const d = res.data;
  tbody.innerHTML = d.lanes.map(lane => {
    const i = lane.lane - 1;
    // Offer only the Applications this module advertises. Listing 0-15 let
    // the user pick a code the module never announced, which it then rejects
    // in ConfigStatus - and AppSelCode 0 means "no application", not App 0.
    const opts = _advertisedApps.length
      ? _advertisedApps.map(a =>
          `<option value="${a.app_sel}" ${lane.app_select === a.app_sel ? 'selected' : ''}>`
          + `App ${a.app_sel} — ${hex8(a.host_if_id)}/${hex8(a.media_if_id)} `
          + `${a.host_lanes}H/${a.media_lanes}M</option>`)
      : Array.from({length: 15}, (_, i) =>
          `<option value="${i + 1}" ${lane.app_select === i + 1 ? 'selected' : ''}>App ${i + 1}</option>`);
    // A lane can sit on a code the module no longer advertises; keep it
    // visible rather than silently snapping the dropdown to another value.
    if (lane.app_select && !_advertisedApps.some(a => a.app_sel === lane.app_select)) {
      opts.unshift(`<option value="${lane.app_select}" selected>`
                   + `App ${lane.app_select} — not advertised</option>`);
    }
    const appOpts = opts.join('');

    // DPConfigLane is one byte per lane; the rest are one bit per lane.
    const tipApp = regTip({
      field: `DPConfigLane${lane.lane}`, page: 0x10, addr: 0x91 + i,
      note: `AppSelCode = ${lane.app_select} (bits 7-4); DataPathID bits 3-1, ExplicitControl bit 0`,
    });
    const tipTx = regTip({
      field: `OutputDisableTx${lane.lane}`, page: 0x10, addr: 0x82,
      value: d.tx_disable_mask, bit: i,
      note: lane.tx_enable ? 'Checked = Tx output enabled (disable bit clear)'
                           : 'Unchecked = Tx output disabled (disable bit set)',
    });
    const tipTxPol = regTip({
      field: `InputPolarityFlipTx${lane.lane}`, page: 0x10, addr: 0x81,
      value: d.tx_polarity_flip_mask, bit: i,
      note: lane.tx_polarity_flip ? 'Host-side input polarity flipped' : 'No input polarity flip',
    });
    const tipRxPol = regTip({
      field: `OutputPolarityFlipRx${lane.lane}`, page: 0x10, addr: 0x89,
      value: d.rx_polarity_flip_mask, bit: i,
      note: lane.rx_polarity_flip ? 'Host-side output polarity flipped' : 'No output polarity flip',
    });
    const tipDeinit = regTip({
      field: `DPDeinitLane${lane.lane}`, page: 0x10, addr: 0x80,
      value: d.dp_deinit_mask, bit: i,
      note: lane.dp_deinit ? 'Data Path held de-initialised' : 'Data Path released for operation',
    });

    return `<tr class="datapath-lane-row">
      <td>Lane ${lane.lane}</td>
      <td title="${esc(tipApp)}"><select id="app-sel-${lane.lane}" class="app-select-input" title="${esc(tipApp)}">${appOpts}</select></td>
      <td title="${esc(tipTx)}"><input type="checkbox" id="tx-en-${lane.lane}" title="${esc(tipTx)}" ${lane.tx_enable ? 'checked' : ''}></td>
      <td title="${esc(tipTxPol)}"><input type="checkbox" id="tx-pol-${lane.lane}" title="${esc(tipTxPol)}" ${lane.tx_polarity_flip ? 'checked' : ''}></td>
      <td title="${esc(tipRxPol)}"><input type="checkbox" id="rx-pol-${lane.lane}" title="${esc(tipRxPol)}" ${lane.rx_polarity_flip ? 'checked' : ''}></td>
      <td title="${esc(tipDeinit)}">${lane.dp_deinit ? '<span class="text-warning">Deinit</span>' : '<span class="text-success">Active</span>'}</td>
    </tr>`;
  }).join('');
}

async function applyDatapath() {
  const app_select = [];
  let tx_disable_mask = 0, tx_pol_mask = 0, rx_pol_mask = 0;

  for (let i = 1; i <= 8; i++) {
    const appSel = document.getElementById(`app-sel-${i}`);
    const txEn   = document.getElementById(`tx-en-${i}`);
    if (!appSel || !txEn) continue;
    app_select.push(parseInt(appSel.value, 10));
    if (!txEn.checked) tx_disable_mask |= (1 << (i - 1));
    if (document.getElementById(`tx-pol-${i}`)?.checked) tx_pol_mask |= (1 << (i - 1));
    if (document.getElementById(`rx-pol-${i}`)?.checked) rx_pol_mask |= (1 << (i - 1));
  }

  const res = await apiPost('/api/module/datapath', {
    tx_disable_mask,
    app_select,
    tx_polarity_flip_mask: tx_pol_mask,
    rx_polarity_flip_mask: rx_pol_mask,
    apply: true,
  });

  if (res.status !== 'ok') {
    toast(`Apply failed: ${res.message}`, 'error');
    return;
  }
  // ApplyDPInit restarts the Data Path state machines, so give the module a
  // moment before reading back what it settled on.
  await new Promise(r => setTimeout(r, 300));
  await loadDatapath();
  await loadSquelch();

  // The write itself succeeding says nothing about whether the module accepted
  // the configuration - it reports that per lane in ConfigStatus.
  const mon = await apiGet('/api/module/monitoring');
  const rejected = mon.status === 'ok'
    ? mon.data.lanes.filter(l => (l.config_status || '').startsWith('ConfigRejected'))
    : [];
  if (rejected.length) {
    const detail = rejected.map(l => `L${l.lane}: ${l.config_status}`).join(', ');
    toast(`Module rejected the configuration — ${detail}`, 'error', 8000);
  } else {
    toast('DataPath configuration applied', 'success');
  }
}

// ---------------------------------------------------------------------------
// Module Control panel (DataPath tab)
// ---------------------------------------------------------------------------
async function loadModuleControl() {
  if (!AppState.connected) return;
  const res = await apiGet('/api/module/control');
  if (res.status !== 'ok') { toast(`Control error: ${res.message}`, 'error'); return; }
  const d = res.data;
  const tbody = document.getElementById('tbl-control');
  if (!tbody) return;

  const yes = '<span class="text-success">●</span>';
  const no  = '<span style="color:var(--text-muted)">○</span>';

  const ctrlTip = (field, bit, note) =>
    esc(regTip({ field, page: null, addr: 0x1A, value: d.raw, bit, note }));

  tbody.innerHTML = `
    <tr title="${ctrlTip('SoftwareReset', 3, d.software_reset ? 'Reset in progress' : 'Idle; writing 1 restarts the module')}">
      <td>Software Reset</td>
      <td>${d.software_reset ? yes : no}</td>
      <td class="td-addr">0x1A[3]</td>
      <td><button class="btn-danger btn-sm" id="btn-mod-reset">Reset Module</button></td>
    </tr>
    <tr title="${ctrlTip('LowPwrRequestSW', 4, d.low_pwr_request_sw ? 'Module held in low power' : 'Module allowed to reach high power')}">
      <td>Low Power Request (SW)</td>
      <td>${d.low_pwr_request_sw ? yes : no}</td>
      <td class="td-addr">0x1A[4]</td>
      <td>
        <button class="btn-secondary btn-sm" id="btn-mod-lp">Enter LowPwr</button>
        <button class="btn-secondary btn-sm" id="btn-mod-hp">Exit LowPwr</button>
      </td>
    </tr>
    <tr title="${ctrlTip('LowPwrAllowRequestHW', 6, d.low_pwr_allow_request_hw ? 'Hardware LPMode pin honoured' : 'Hardware LPMode pin ignored')}">
      <td>Allow LowPwrRequestHW</td>
      <td>${d.low_pwr_allow_request_hw ? yes : no}</td>
      <td class="td-addr">0x1A[6]</td>
      <td>—</td>
    </tr>
    <tr title="${ctrlTip('SquelchMethodSelect', 5, d.squelch_method_select ? 'Squelch on average power (Pav)' : 'Squelch on modulation amplitude (OMA)')}">
      <td>Squelch Method</td>
      <td>${d.squelch_method_select ? 'Pav' : 'OMA'}</td>
      <td class="td-addr">0x1A[5]</td>
      <td>—</td>
    </tr>`;

  document.getElementById('btn-mod-reset')?.addEventListener('click', async () => {
    if (!confirm('Software-reset the module? All DataPaths will reinitialize.')) return;
    const r = await apiPost('/api/module/control', { action: 'reset' });
    toast(r.status === 'ok' ? 'Module reset issued' : `Reset failed: ${r.message}`,
          r.status === 'ok' ? 'success' : 'error');
    if (r.status !== 'ok') return;
    // A reset reinitialises every Data Path, so refresh the whole tab rather
    // than leaving stale values and tooltips behind.
    setTimeout(() => {
      loadModuleControl(); loadDatapath(); loadSquelch(); loadApplications();
    }, 400);
  });
  document.getElementById('btn-mod-lp')?.addEventListener('click', async () => {
    const r = await apiPost('/api/module/control', { action: 'low_power' });
    toast(r.status === 'ok' ? 'LowPwr requested' : `Failed: ${r.message}`,
          r.status === 'ok' ? 'success' : 'error');
    setTimeout(loadModuleControl, 200);
  });
  document.getElementById('btn-mod-hp')?.addEventListener('click', async () => {
    const r = await apiPost('/api/module/control', { action: 'high_power' });
    toast(r.status === 'ok' ? 'High power requested' : `Failed: ${r.message}`,
          r.status === 'ok' ? 'success' : 'error');
    setTimeout(loadModuleControl, 200);
  });
}

// ---------------------------------------------------------------------------
// Application Descriptors (DataPath tab)
// ---------------------------------------------------------------------------
async function loadApplications() {
  if (!AppState.connected) return;
  const res = await apiGet('/api/module/applications');
  if (res.status !== 'ok') { toast(`Apps error: ${res.message}`, 'error'); return; }
  const tbody = document.getElementById('tbl-apps');
  if (!tbody) return;

  const apps = res.data.applications;
  // The DataPath AppSelect dropdown offers exactly these.
  _advertisedApps = Array.isArray(apps) ? apps : [];
  if (!apps || apps.length === 0) {
    tbody.innerHTML = '<tr><td colspan="6" class="placeholder-text">No applications advertised.</td></tr>';
    return;
  }
  tbody.innerHTML = apps.map(a => {
    const hostHex = `0x${a.host_if_id.toString(16).toUpperCase().padStart(2,'0')}`;
    const mediaHex = `0x${a.media_if_id.toString(16).toUpperCase().padStart(2,'0')}`;
    // Prefix the bitmap: a bare "00000001" reads as decimal one to anyone
    // scanning the table.
    const assignBin = '0b' + a.host_lane_assign_mask.toString(2).padStart(8, '0');
    const lanesTip = `Host Lane Assignment: ${assignBin} bin = `
      + `${hex8(a.host_lane_assign_mask)} hex = ${a.host_lane_assign_mask} dec\n`
      + 'Bit n set = this Application may start on host lane n+1';
    return `<tr>
      <td title="AppSelCode ${a.app_sel} (dec), 1-15">${a.app_sel}</td>
      <td title="Host Interface ID ${hostHex} hex = ${a.host_if_id} dec (SFF-8024)">${hostHex}</td>
      <td title="Media Interface ID ${mediaHex} hex = ${a.media_if_id} dec">${mediaHex}</td>
      <td title="Host lane count (dec)">${a.host_lanes || '—'}</td>
      <td title="Media lane count (dec)">${a.media_lanes || '—'}</td>
      <td title="${esc(lanesTip)}"><code>${assignBin}</code></td>
    </tr>`;
  }).join('');
}

// ---------------------------------------------------------------------------
// SNR (Diagnostics tab)
// ---------------------------------------------------------------------------
async function loadSnr() {
  if (!AppState.connected) return;
  const res = await apiGet('/api/module/snr');
  const tbody = document.getElementById('tbl-snr');
  if (!tbody) return;
  if (res.status !== 'ok') {
    tbody.innerHTML = `<tr><td colspan="9" class="placeholder-text">SNR not available: ${res.message}</td></tr>`;
    return;
  }
  const fmt = (v) => (v == null ? '—' : v.toFixed(2));
  const hostCells = res.data.host_snr_db.map(v => `<td>${fmt(v)}</td>`).join('');
  const mediaCells = res.data.media_snr_db.map(v => `<td>${fmt(v)}</td>`).join('');
  tbody.innerHTML =
    `<tr><td style="color:var(--text-muted)">Host<span class="reg-badge">14h/0xC0+16 sel=0x06</span></td>${hostCells}</tr>` +
    `<tr><td style="color:var(--text-muted)">Media<span class="reg-badge">14h/0xC0+48 sel=0x06</span></td>${mediaCells}</tr>`;
}

// ---------------------------------------------------------------------------
// Raw Register tab
// ---------------------------------------------------------------------------

function parseHexOrDec(s) {
  s = (s || '').trim();
  if (s.startsWith('0x') || s.startsWith('0X')) return parseInt(s, 16);
  return parseInt(s, 10);
}

// ---------------------------------------------------------------------------
// I2C address representation
// ---------------------------------------------------------------------------
// The UI shows the 8-bit address seen on the bus (0xA0 = 7-bit slave 0x50 << 1,
// write direction). The API and the I2C drivers take the 7-bit form, so every
// value entered in the UI is shifted right once before it is sent. A bare
// 7-bit value (<= 0x7F, e.g. a legacy '0x50') is accepted unchanged so old
// bookmarks keep working.
function uiAddressTo7Bit(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n < 0 || n > 0xFF) return NaN;
  return n > 0x7F ? n >> 1 : n;
}

/** 7-bit slave address -> 8-bit display string, e.g. 0x50 -> '0xA0'. */
function format8BitAddress(addr7) {
  const n = Number(addr7);
  if (!Number.isFinite(n)) return '0x??';
  return '0x' + ((n << 1) & 0xFF).toString(16).toUpperCase().padStart(2, '0');
}

async function rawRead() {
  const page    = parseHexOrDec(document.getElementById('raw-page').value);
  const address = parseHexOrDec(document.getElementById('raw-address').value);
  const length  = parseInt(document.getElementById('raw-length').value, 10) || 1;

  const res = await apiPost('/api/register/read', { page, address, length });
  const dumpEl = document.getElementById('hex-dump');

  if (res.status !== 'ok') {
    dumpEl.textContent = `Error: ${res.message}`;
    toast(`Read error: ${res.message}`, 'error');
    return;
  }

  dumpEl.textContent = formatHexDump(res.data.data, address);
}

async function rawWrite() {
  const page    = parseHexOrDec(document.getElementById('raw-page').value);
  const address = parseHexOrDec(document.getElementById('raw-address').value);
  const dataStr = document.getElementById('raw-data').value.trim();

  if (!dataStr) { toast('Enter data bytes (space-separated hex)', 'error'); return; }

  const data = dataStr.split(/\s+/).map(h => parseInt(h, 16)).filter(v => !isNaN(v));
  if (!data.length) { toast('Invalid hex data', 'error'); return; }

  const res = await apiPost('/api/register/write', { page, address, data });
  const dumpEl = document.getElementById('hex-dump');

  if (res.status === 'ok') {
    // The write returning ok only means the I2C transfer completed. Read the
    // bytes back so the user sees what the module actually holds - writes to
    // read-only or protected registers are silently discarded.
    const back = await apiPost('/api/register/read', { page, address, length: data.length });
    if (back.status === 'ok') {
      const got = back.data.data;
      const same = got.length === data.length && got.every((b, i) => b === data[i]);
      dumpEl.textContent =
        `Wrote ${data.length} byte(s) to page 0x${page.toString(16).toUpperCase()} `
        + `addr 0x${address.toString(16).toUpperCase().padStart(2,'0')}\n`
        + `read back:\n${formatHexDump(got, address)}`;
      toast(same ? `Written and verified ${data.length} byte(s)`
                 : 'Write completed but the module reports different values — '
                   + 'the register may be read-only or the value was clamped',
            same ? 'success' : 'error', same ? 3000 : 8000);
    } else {
      dumpEl.textContent = `Wrote ${data.length} byte(s); read-back failed: ${back.message}`;
      toast(`Written, but read-back failed: ${back.message}`, 'error', 6000);
    }
  } else {
    dumpEl.textContent = `Error: ${res.message}`;
    toast(`Write error: ${res.message}`, 'error');
  }
}

/**
 * Render a classic 16-bytes-per-row hex dump.
 * @param {number[]} byteArray bytes to render
 * @param {number} baseAddr    address of byteArray[0]
 * @param {number} [addrWidth] hex digits in the address column; 2 for a
 *                             256-byte page, 3/4 for a 1024-byte EEPROM dump
 */
function formatHexDump(byteArray, baseAddr, addrWidth = 2) {
  const ROW = 16;
  // Header states the radix: the body is bare 2-digit groups with no 0x, so
  // without it a row of "00 01 10" is ambiguous.
  let out = 'addr (hex) :    bytes (hex)                            ascii\n';
  for (let i = 0; i < byteArray.length; i += ROW) {
    const slice = byteArray.slice(i, i + ROW);
    const addrStr = `0x${(baseAddr + i).toString(16).toUpperCase().padStart(addrWidth, '0')}`;
    const hex = slice.map(b => b.toString(16).toUpperCase().padStart(2, '0')).join(' ').padEnd(ROW * 3 - 1, ' ');
    const asc = slice.map(b => (b >= 32 && b < 127) ? String.fromCharCode(b) : '.').join('');
    out += `${addrStr}: ${hex}  ${asc}\n`;
  }
  return out || '(no data)';
}

// ---------------------------------------------------------------------------
// EEPROM Bulk Read tab — up to 1024 bytes
// 8-bit bus addresses (7-bit slave << 1):
// 24C02 = 1 × 256 B @ 0xA0 · 24C04 = 2 × 256 B @ 0xA0..0xA2 ·
// 24C08 = 4 × 256 B @ 0xA0..0xA6
// ---------------------------------------------------------------------------
// Deliberately not auto-loaded by switchTab(): a full dump is 32+ I2C
// transactions, so it must only run when the user explicitly asks for it.

// Mirrors EEPROM_PROFILES in app.py — keep the two in sync when adding a chip.
// The API response is authoritative for the actual read; this table only
// drives the UI defaults (length, chunk) and the information panel.
const EEPROM_CHIPS = {
  '24c02': {
    name: '24C02', voltage: '3.3V', type: 'I2C', vendor: 'GENERIC',
    bits: '2 Kbits', capacity: 256, page: 8, blocks: 1,
    addr: '0xA0 (1 × 256 B)',
  },
  '24c04': {
    name: '24C04', voltage: '3.3V', type: 'I2C', vendor: 'GENERIC',
    bits: '4 Kbits', capacity: 512, page: 16, blocks: 2,
    addr: '0xA0–0xA2 (2 × 256 B)',
  },
  '24c08': {
    name: '24C08', voltage: '3.3V', type: 'I2C', vendor: 'GENERIC',
    bits: '8 Kbits', capacity: 1024, page: 16, blocks: 4,
    addr: '0xA0–0xA6 (4 × 256 B)',
  },
};

/**
 * Apply a chip profile to the panel: information, defaults and stale data.
 * @param {string} chipKey key of EEPROM_CHIPS
 * @param {boolean} [announce] show the selection in the status line
 */
function applyEepromChip(chipKey, announce = false) {
  const chip = EEPROM_CHIPS[chipKey];
  if (!chip) return;

  const set = (id, value) => {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
  };
  set('eep-ci-name', `${chip.name} [${chip.voltage}]`);
  set('eep-ci-type', chip.type);
  set('eep-ci-bits', chip.bits);
  set('eep-ci-vendor', chip.vendor);
  set('eep-ci-size', `${chip.capacity} Bytes`);
  set('eep-ci-page', `${chip.page} Bytes`);
  set('eep-ci-addr', chip.addr);

  const startEl  = document.getElementById('eep-start');
  const lengthEl = document.getElementById('eep-length');
  const chunkEl  = document.getElementById('eep-chunk');
  if (startEl) startEl.value = '0x000';
  if (lengthEl) {
    lengthEl.max = String(chip.capacity);
    lengthEl.value = String(chip.capacity);
  }
  // Sequential reads are not limited by the write-page size, but starting each
  // transfer on a page boundary is the safest setting for the CH341 DLLs.
  if (chunkEl) chunkEl.value = String(chip.page);

  // A dump from another chip must never stay downloadable, but switching
  // chips must not blank an image that still fits the new capacity.
  const image = _eepromImage;
  eepromClear();
  if (image && image.start + image.bytes.length <= chip.capacity) {
    // loadSn=false: keep a manually edited Serial Number across the redraw.
    setEepromImage(image, false);
    const summaryEl = document.getElementById('eep-summary');
    if (summaryEl) summaryEl.textContent =
      `${image.name} · ${image.bytes.length} bytes · ${eepromOriginLabel(image)}`
      + ` · kept after switching to ${chip.name}`;
  } else if (image) {
    toast(`${image.name} (${image.bytes.length} B) does not fit ${chip.name}`
          + ` (${chip.capacity} B) — image discarded`, 'info', 6000);
  }
  updateEepromWriteButton();
  updateEepromPreview();
  if (announce) {
    const status = document.getElementById('eep-status');
    if (status) {
      status.textContent =
        `${chip.name} selected — ${chip.capacity} B, ${chip.page} B page, ${chip.addr}`;
    }
  }
}

// ---------------------------------------------------------------------------
// Serial number (12 printable ASCII bytes at 0x000–0x00B) + BIN image programming
// ---------------------------------------------------------------------------
// The serial is plain ASCII text, e.g. "CAD211193882", stored as 12 bytes in
// the EEPROM. Selecting a .bin loads those bytes into the field; every Write
// that is actually sent advances the selected digit run by one so no two
// boards share a S/N; letters on either side of the run never change.
const EEPROM_SERIAL_START = 0x000;
const EEPROM_SERIAL_LENGTH = 12;
const EEPROM_SERIAL_RANGE = '0x000–0x00B';
const EEPROM_SERIAL_DEFAULT = 'CAD211193882';
const EEPROM_SN_KEY = 'cmis.eeprom.sn.ascii';   // persisted 12-character ASCII

// Consumable-chip "Auto update": remaining-capacity percent is BE16 at
// 0x09C–0x09D, 0x0064 = 100% (fresh chip). Mirrors app.py; keep both in sync.
const CONSUMPTION_OFFSET = 0x09C;
const CONSUMPTION_FULL_PERCENT = 0x64;

const hexEepromSerial = bytes => Array.from(bytes)
  .map(b => b.toString(16).toUpperCase().padStart(2, '0')).join(' ');

/** 'CAD211193882' -> the 12 ASCII codes actually stored in the EEPROM. */
const asciiEepromSerial = text => Uint8Array.from(
  Array.from(String(text), ch => ch.charCodeAt(0)));

/** 12 ASCII bytes -> text; null when the field holds non-printable data. */
function decodeEepromSerial(bytes) {
  if (!bytes || bytes.length !== EEPROM_SERIAL_LENGTH) return null;
  const vals = Array.from(bytes);
  if (vals.some(b => b < 0x21 || b > 0x7E)) return null;
  return String.fromCharCode(...vals);
}

/** Validate the editable field and return its 12 EEPROM bytes. */
function parseEepromSerial(text) {
  const s = String(text ?? '');
  if (s.length !== EEPROM_SERIAL_LENGTH || /[^\x21-\x7E]/.test(s)) {
    throw new Error(`Serial Number must be ${EEPROM_SERIAL_LENGTH} printable `
                  + `ASCII characters, e.g. ${EEPROM_SERIAL_DEFAULT}`);
  }
  return asciiEepromSerial(s);
}

/**
 * Resolve which characters of the S/N may change. Production S/Ns can end in
 * letters ('CAA2127891EB'), so the digits are not always a trailing run.
 * Empty/'auto' picks the last contiguous digit run (which may be followed by
 * letters, e.g. 'CAA2127891EB'); an explicit 0-based inclusive range
 * ('3-9', '3:9', or a single '9') pins it. Letters on either side stay put.
 * Mirrors _resolve_serial_pos() in app.py; keep both in sync.
 */
function resolveEepromSnPos(value, text) {
  const raw = String(value ?? '').trim().toLowerCase();
  const str = String(text ?? '');
  if (raw === '' || raw === 'auto' || raw === '自动') {
    // Last contiguous digit run, NOT necessarily at the end: S/Ns such as
    // 'CAA2127891EB' carry a letter suffix after the digits.
    let j = str.length;
    while (j > 0 && !(str[j - 1] >= '0' && str[j - 1] <= '9')) j--;
    if (j === 0) return null;
    let i = j;
    while (i > 0 && str[i - 1] >= '0' && str[i - 1] <= '9') i--;
    return [i, j - 1];
  }
  let loS = raw, hiS = raw;
  for (const sep of ['-', '–', ':', '~']) {
    if (raw.includes(sep)) { [loS, hiS] = raw.split(sep); break; }
  }
  const lo = Number(loS.trim()), hi = Number(hiS.trim());
  if (!Number.isInteger(lo) || !Number.isInteger(hi)
      || lo < 0 || hi < lo || hi >= str.length) {
    throw new Error(`递增位置必须是 0-based 区间，如 3-9（Serial Number 共 ${str.length} 字符）`);
  }
  const part = str.slice(lo, hi + 1);
  if (!/^[0-9]+$/.test(part)) {
    throw new Error(`递增位置 ${lo}-${hi} 只能覆盖数字，当前是 "${part}"`);
  }
  return [lo, hi];
}

/** Raw 递增位置 value from the UI ('' = auto-detect). */
function eepromSnPos() {
  return document.getElementById('eep-sn-pos')?.value ?? '';
}

/** Show the auto-detected range as a hint without overwriting a manual range. */
function updateEepromSnPosHint() {
  const el = document.getElementById('eep-sn-pos');
  const sn = document.getElementById('eep-serial')?.value;
  if (!el || !sn) return;
  let range = null;
  try { range = resolveEepromSnPos('', sn); } catch (e) { /* no digits */ }
  el.placeholder = range ? `自动 (${range[0]}-${range[1]})` : '自动 (无数字段)';
}

/**
 * Increment only the selected digit run, keeping its width; letters are never
 * touched. Overflow wraps inside the run. Mirrors _advance_serial_text().
 */
function advanceSerialText(text, pos = '') {
  const s = String(text ?? '');
  let range;
  try { range = resolveEepromSnPos(pos, s); }
  catch (e) { range = resolveEepromSnPos('', s); }  // defensive: server validates too
  if (!range) return s;
  const [lo, hi] = range;
  const digits = s.slice(lo, hi + 1);
  const n = (Number(digits) + 1) % (10 ** digits.length);
  return s.slice(0, lo) + String(n).padStart(digits.length, '0') + s.slice(hi + 1);
}

const formatEepromSerial = text => String(text ?? '');

function _readEepromSn() {
  try {
    const s = localStorage.getItem(EEPROM_SN_KEY);
    parseEepromSerial(s);
    return s;
  } catch (e) {
    return EEPROM_SERIAL_DEFAULT;   // missing/invalid storage: seed a known value
  }
}

function _saveEepromSn(text) {
  try { localStorage.setItem(EEPROM_SN_KEY, String(text)); } catch (e) { /* blocked */ }
}

function _showEepromSn(text) {
  const shown = String(text);
  const el = document.getElementById('eep-serial');
  if (el) el.value = shown;
  _saveEepromSn(shown);
  updateEepromPreview();
  return shown;
}

/** Seed the S/N field from the persisted value on page load. */
function initEepromSerial() {
  _showEepromSn(_readEepromSn());
}

/**
 * Should a Write advance the Serial Number? The 自动递增 checkbox defaults to
 * ticked (the historical behaviour); unticked leaves the S/N unchanged so the
 * operator can program several boards with one S/N and advance with Next only.
 */
function eepromSnAutoIncrement() {
  const el = document.getElementById('eep-sn-auto');
  return el ? !!el.checked : true;   // no control → keep legacy behaviour
}

/** Trailing-decimal +1, mirroring _advance_serial_text() in app.py. */
function advanceEepromSerial() {
  let text = _readEepromSn();
  try {
    const v = document.getElementById('eep-serial')?.value;
    parseEepromSerial(v);
    text = String(v);
  } catch (e) { /* invalid manual entry: fall back to the stored value */ }
  return _showEepromSn(advanceSerialText(text, eepromSnPos()));
}

function nextEepromSerial() { return advanceEepromSerial(); }

/** Load ASCII bytes 0x000–0x00B of the shown image into the Serial Number box. */
function loadEepromSerialFromImage(bytes) {
  const text = decodeEepromSerial(bytes);
  if (text === null) return null;
  return _showEepromSn(text);
}

/** Provenance of the image currently displayed in the content area. */
function eepromOriginLabel(image) {
  return image.origin === 'chip' ? 'read from chip' : 'loaded from file';
}

/** True when the shown bytes actually contain absolute 0x000–0x00B. */
function imageCoversSerial(image) {
  return !!image && image.start <= EEPROM_SERIAL_START
      && image.start + image.bytes.length
         >= EEPROM_SERIAL_START + EEPROM_SERIAL_LENGTH;
}

/**
 * Make `image` the single source for the content area, Save/Copy and Write,
 * then render it. Both Read and a successful file selection go through here.
 *
 * Why one source: Read used to show chip data in the content area while Write
 * stayed disabled because it demanded a .bin. A production bench reads a
 * golden part and programs the next boards from that dump, so the dump itself
 * must be the writable image. Loading 0x000–0x00B here also satisfies
 * "Serial Number comes from the content area" for both sources.
 *
 * @param {{name: string, bytes: Uint8Array, start: number, origin: string}} image
 * @param {boolean} [loadSn] copy 0x000–0x00B into the editable S/N field
 */
function setEepromImage(image, loadSn = true) {
  _eepromImage = image;
  const chip = EEPROM_CHIPS[document.getElementById('eep-chip')?.value];
  _lastEeprom = {
    start: image.start,
    bytes: Array.from(image.bytes),
    chip: chip ? chip.name : 'dump',
  };
  const dumpEl = document.getElementById('eep-dump');
  if (dumpEl) dumpEl.textContent = formatHexDump(_lastEeprom.bytes, image.start, 3);
  document.getElementById('btn-eep-export').disabled = false;
  document.getElementById('btn-eep-copy').disabled = false;
  if (loadSn && imageCoversSerial(image)) {
    const at = EEPROM_SERIAL_START - image.start;
    loadEepromSerialFromImage(image.bytes.subarray(
      at, at + EEPROM_SERIAL_LENGTH));
  }
  updateEepromWriteButton();
  updateEepromPreview();
}

/** Copy of the shown image with 0x000–0x00B replaced by the Serial Number. */
function patchedEepromImage() {
  const image = _eepromImage;
  if (!image) return null;
  const out = new Uint8Array(image.bytes);   // never mutate the source image
  if (imageCoversSerial(image)) {
    const sn = parseEepromSerial(document.getElementById('eep-serial')?.value);
    out.set(sn, EEPROM_SERIAL_START - image.start);
  }
  return out;
}

function updateEepromPreview() {
  updateEepromSnPosHint();
  const el = document.getElementById('eep-write-preview');
  if (!el) return;
  if (!_eepromImage) {
    el.textContent = 'Read the chip or load a .bin image — the content area '
                   + `becomes the Write source (S/N patched at ${EEPROM_SERIAL_RANGE}).`;
    return;
  }
  try {
    const bytes = patchedEepromImage();
    const head = Array.from(bytes.slice(0, 16))
      .map(b => b.toString(16).toUpperCase().padStart(2, '0')).join(' ');
    const sn = imageCoversSerial(_eepromImage)
      ? (decodeEepromSerial(bytes.slice(0, EEPROM_SERIAL_LENGTH)) || '(non-ASCII bytes)')
      : 'outside this range';
    const at = `0x${_eepromImage.start.toString(16).toUpperCase().padStart(3, '0')}`;
    el.textContent = `${eepromOriginLabel(_eepromImage)} · ${bytes.length} bytes`
                   + `  |  ${at}: ${head} …  |  S/N ${sn} → ${EEPROM_SERIAL_RANGE}`;
  } catch (e) {
    el.textContent = `Serial Number: ${e.message}`;
  }
}

/**
 * Read a File object into the EEPROM content area. Shared by the file-picker
 * change handler and the 刷新 button, so both paths render identically.
 * @returns {Promise<boolean>} true when the file was loaded successfully.
 */
async function loadEepromFile(file) {
  const info = document.getElementById('eep-file-info');
  const dumpEl = document.getElementById('eep-dump');
  const summaryEl = document.getElementById('eep-summary');
  const statusEl = document.getElementById('eep-status');
  const refreshBtn = document.getElementById('btn-eep-file-refresh');
  if (refreshBtn) refreshBtn.disabled = !file;
  _loadedBinFile = file || null;
  _eepromImage = null;
  if (!file) {
    if (info) info.textContent = 'No .bin file loaded.';
    if (dumpEl) dumpEl.textContent = 'Results will appear here…';
    if (summaryEl) summaryEl.textContent = '';
    if (statusEl) statusEl.textContent = 'Ready.';
    _lastEeprom = null;
    document.getElementById('btn-eep-export').disabled = true;
    document.getElementById('btn-eep-copy').disabled = true;
    updateEepromWriteButton();
    updateEepromPreview();
    return false;
  }
  try {
    const buf = await file.arrayBuffer();
    const image = {
      name: file.name, bytes: new Uint8Array(buf),
      start: 0, origin: 'file',
    };
    const n = image.bytes.length;
    if (info) info.textContent = `${file.name} · ${n} bytes`;
    if (n < 1) throw new Error('.bin file is empty');
    // A successful load immediately fills the content area, loads
    // 0x000–0x00B into the editable S/N and enables Write — same path as Read.
    setEepromImage(image);
    const chip = EEPROM_CHIPS[document.getElementById('eep-chip')?.value];
    const sn = document.getElementById('eep-serial')?.value;
    if (summaryEl) summaryEl.textContent =
      `${file.name} · ${n} bytes · loaded from file (not read from chip)`
      + (chip ? ` · ${chip.name} capacity ${chip.capacity} B` : '')
      + ` · S/N ${sn}`;
    if (statusEl) statusEl.textContent = `Loaded ${file.name} — ${n} bytes.`;
    if (chip && n > chip.capacity) {
      toast(`${chip.name} holds only ${chip.capacity} bytes; the image has ${n}`,
            'error', 6000);
    }
    return true;
  } catch (e) {
    _eepromImage = null;
    _lastEeprom = null;
    if (info) info.textContent = `Could not use ${file.name}: ${e.message}`;
    toast(`Could not use ${file.name}: ${e.message}`, 'error', 6000);
    return false;
  } finally {
    updateEepromWriteButton();
    updateEepromPreview();
  }
}

async function handleEepromFile(event) {
  await loadEepromFile(event.target.files?.[0]);
}

/** 刷新: re-read the already-selected file into the content area. */
async function refreshEepromFile() {
  const input = document.getElementById('eep-file');
  const file = input?.files?.[0];
  if (!file) {
    toast('没有选中的 .bin 文件，请先选择文件。', 'error');
    return;
  }
  const ok = await loadEepromFile(file);
  if (!ok) return;
  const statusEl = document.getElementById('eep-status');
  if (statusEl) statusEl.textContent = `已重新读取 ${file.name} — ${file.size} bytes.`;
  toast(`已刷新 ${file.name}`, 'success');
}

function updateEepromWriteButton(busy = false) {
  const btn = document.getElementById('btn-eep-write');
  if (!btn) return;
  const chip = EEPROM_CHIPS[document.getElementById('eep-chip')?.value];
  const image = _eepromImage;
  const bytes = image && image.bytes;
  // Either a Read dump or a loaded .bin is enough — Write programs whichever
  // one the content area currently shows.
  const unusable = !bytes || bytes.length < 1 ||
                   (chip && image.start + bytes.length > chip.capacity);
  btn.disabled = busy || !AppState.connected || unusable;
  btn.title = !AppState.connected ? 'Connect to a device first'
            : unusable ? 'Read the chip or load a .bin image first'
            : eepromWriteNeedsConfirm()
              ? 'Write — a confirmation dialog will appear first'
              : 'Write immediately — no confirmation dialog';
}

function bytesToBase64(bytes) {
  let bin = '';
  const CHUNK = 0x8000;             // spread/apply argument limit on large blobs
  for (let i = 0; i < bytes.length; i += CHUNK) {
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
  }
  return btoa(bin);
}

// ---------------------------------------------------------------------------
// Write confirmation — optional in-page dialog anchored at the Write button
// ---------------------------------------------------------------------------
// Chrome draws window.confirm() at the top of the viewport and no page script
// can move it, which means a long mouse trip on a programming bench. A modal
// <dialog> keeps the top layer, backdrop and Escape-to-cancel, but lets us put
// "确定" a few pixels above the button that was just clicked.

/**
 * Is the "确认 / Confirm" checkbox ticked? It is an OPT-IN confirmation
 * switch, not a write gate: unticked means Write programs immediately with no
 * dialog and no toast; ticked means ask first via the in-page dialog.
 */
function eepromWriteNeedsConfirm() {
  return !!document.getElementById('eep-arm-write')?.checked;
}
// Legacy name kept for bench scripts / earlier harnesses. Not a write gate.
function eepromWriteArmed() { return eepromWriteNeedsConfirm(); }

function _eepDialogZoom() {
  try {
    const z = parseFloat(getComputedStyle(document.documentElement).zoom);
    return Number.isFinite(z) && z > 0 ? z : 1;
  } catch (e) {
    return 1;                       // non-browser harness / unsupported property
  }
}

/**
 * Show the confirmation with 确定 / 取消 immediately next to btn-eep-write.
 * Preferred placement is ABOVE the button: the action row is the dialog's last
 * row, so OK ends up just above the cursor. If the viewport has no room above,
 * the dialog drops below and a CSS class moves the action row back to the top.
 * @returns {Promise<boolean>} true only when the OK button was used; Escape,
 *          backdrop clicks and Cancel all resolve false.
 */
function openEepromConfirmDialog(message) {
  return new Promise(resolve => {
    const dlg = document.getElementById('eep-confirm-dialog');
    const text = document.getElementById('eep-confirm-text');
    if (text) text.textContent = message;
    if (!dlg || typeof dlg.showModal !== 'function') {
      // Last-resort fallback for a browser without <dialog>: still confirm.
      resolve(!!window.confirm(message));
      return;
    }

    const onClose = () => {
      dlg.removeEventListener?.('close', onClose);
      resolve(dlg.returnValue === 'ok');
    };
    dlg.addEventListener('close', onClose);
    dlg.returnValue = '';           // Escape/backdrop leave it empty → cancel
    dlg.showModal();

    // Measure AFTER showModal(): a closed dialog has no layout box. rect/box
    // are real viewport pixels; the dialog carries --ui-zoom, so its CSS
    // top/left must be divided by that zoom to land on those same pixels.
    const anchor = document.getElementById('btn-eep-write');
    const rect = anchor?.getBoundingClientRect?.();
    const zoom = _eepDialogZoom();
    const box = dlg.getBoundingClientRect?.() || { width: 460, height: 200 };
    const vw = Number.isFinite(window.innerWidth) ? window.innerWidth : 1920;
    const vh = Number.isFinite(window.innerHeight) ? window.innerHeight : 1080;
    if (rect) {
      const left = Math.max(8, Math.min(rect.left - 10, vw - box.width - 8));
      // Prefer ABOVE: 确定 is in the last row, so it lands a few pixels above
      // the button the user just clicked. Fall back to BELOW only when the
      // dialog cannot fit above; then eep-confirm-below puts OK back on top.
      let top = rect.top - box.height - 8;
      let below = false;
      if (top < 8) {
        below = true;
        top = rect.bottom + 8;
      }
      top = Math.max(8, Math.min(top, vh - box.height - 8));
      dlg.classList?.toggle?.('eep-confirm-below', below);
      dlg.style.left = `${Math.round(left / zoom)}px`;
      dlg.style.top = `${Math.round(top / zoom)}px`;
    } else {
      // No layout box (unsupported DOM): centre it, still below the viewport
      // top where a native confirm() would have sat.
      dlg.classList?.remove?.('eep-confirm-below');
      dlg.style.left = `${Math.round((vw - box.width) / 2 / zoom)}px`;
      dlg.style.top = `${Math.round(vh / 3 / zoom)}px`;
    }
    // Focus starts on OK: Enter confirms without moving the mouse at all.
    document.getElementById('eep-confirm-ok')?.focus?.();
  });
}

/**
 * Confirmation seam: production uses the in-page dialog, while the Node VM
 * harness (or a scripted bench tool) can inject window.__eepromConfirm.
 */
async function confirmEepromWrite(message) {
  if (typeof window.__eepromConfirm === 'function') {
    return !!(await window.__eepromConfirm(message));
  }
  return openEepromConfirmDialog(message);
}

/**
 * Program the content-area image into the EEPROM. The server is authoritative
 * for the serial patch, page splitting and tWR wait; this function only
 * prepares the image, optionally asks for confirmation and reports the outcome.
 */
async function eepromWrite() {
  // 确认 / Confirm is opt-in: unticked → program immediately, with no dialog
  // and no pop-up of any kind ("直接下发"). Ticked → ask first, and only a
  // confirmed click reaches the server / consumes a Serial Number.
  const needsConfirm = eepromWriteNeedsConfirm();
  if (!AppState.connected) { toast('Connect to a device first', 'error'); return; }
  const image = _eepromImage;
  if (!image) { toast('Read the chip or choose a .bin image first', 'error'); return; }

  const chip      = document.getElementById('eep-chip').value;
  const profile   = EEPROM_CHIPS[chip];
  const base      = uiAddressTo7Bit(parseHexOrDec(document.getElementById('eep-base').value));
  const verify    = !!document.getElementById('eep-verify')?.checked;
  const snEl      = document.getElementById('eep-serial');
  const statusEl  = document.getElementById('eep-status');
  const dumpEl    = document.getElementById('eep-dump');
  const summaryEl = document.getElementById('eep-summary');

  if (!profile) { toast('Select a supported EEPROM chip', 'error'); return; }
  if (Number.isNaN(base)) { toast('Enter a valid base address', 'error'); return; }

  let bytes;
  try { bytes = patchedEepromImage(); }
  catch (e) { toast(e.message, 'error'); return; }
  // Reject a bad 递增位置 before anything is sent — a write is irreversible.
  if (imageCoversSerial(image)) {
    try { resolveEepromSnPos(eepromSnPos(), snEl.value); }
    catch (e) { toast(e.message, 'error'); return; }
  }
  if (!bytes.length) {
    toast('The content area holds no bytes to write', 'error');
    return;
  }
  if (image.start + bytes.length > profile.capacity) {
    toast(`${profile.name} holds only ${profile.capacity} bytes; the image ends at `
          + `${image.start + bytes.length}`, 'error');
    return;
  }

  // A partial dump starting above 0x000 has no S/N bytes: write it verbatim
  // and tell the server not to patch (it would otherwise reject the request).
  const coversSN = imageCoversSerial(image);
  // Show the editable field, not the old bytes from the dump: this is exactly
  // what patchedEepromImage() patched and what the server will receive.
  const sn = coversSN
    ? snEl.value
    : `(unchanged — dump starts at 0x${image.start.toString(16).toUpperCase().padStart(3, '0')})`;
  const addr = `0x${base.toString(16).toUpperCase().padStart(2, '0')}`;
  const startHex = `0x${image.start.toString(16).toUpperCase().padStart(3, '0')}`;
  const message =
    `Write ${bytes.length} bytes from ${eepromOriginLabel(image)} `
    + `("${image.name}", start ${startHex}) to ${profile.name} at ${addr}?\n\n`
    + `Serial Number ${sn} replaces bytes ${EEPROM_SERIAL_RANGE}.\n`
    + `This OVERWRITES the EEPROM and cannot be undone.\n`
    + `Never use this on a live CMIS optical module.`;
  if (needsConfirm && !(await confirmEepromWrite(message))) return;

  updateEepromWriteButton(true);
  statusEl.textContent = `Writing ${bytes.length} byte(s)…`;
  // Keep the image visible in the content area while programming.
  summaryEl.textContent = `Writing ${image.name} … S/N ${sn}`;

  try {
    const payload = {
      confirm: true, chip, start: image.start, base_address: base,
      patch_serial: coversSN, verify,
      serial_pos: eepromSnPos(),
      data_b64: bytesToBase64(bytes),
    };
    if (coversSN) payload.serial_number = snEl.value;
    const res = await apiPost('/api/eeprom/write', payload);
    if (res.status !== 'ok') {
      statusEl.textContent = 'Write failed.';
      summaryEl.textContent = `Write failed: ${res.message}`;
      toast(`EEPROM write error: ${res.message}`, 'error', 8000);
      return;
    }

    const d = res.data;
    const segs = d.segments.map(s =>
      `0x${s.address.toString(16).toUpperCase().padStart(2, '0')}:${s.count}B`).join('  ');
    const result = d.verified === null ? 'verify skipped'
      : (d.verified ? 'verified' : `${d.mismatch_count} mismatch(es)`);
    const range = `0x${d.start.toString(16).toUpperCase().padStart(3, '0')}`
      + `–0x${(d.end - 1).toString(16).toUpperCase().padStart(3, '0')}`;
    summaryEl.textContent =
      `${d.chip_name} · wrote ${d.length} bytes ${range} · S/N ${sn}`
      + ` · ${d.transactions} page writes (≤${d.page_size} B) · ${d.write_delay_ms} ms tWR`
      + ` · ${result} · ${segs} · sha256:${d.image_sha256_16}`;
    // The content area keeps showing exactly the image that was programmed —
    // not a "write complete" placeholder and not the previous chip dump.
    dumpEl.textContent = formatHexDump(Array.from(bytes), image.start, 3);
    _lastEeprom = { start: image.start, bytes: Array.from(bytes), chip: d.chip_name };
    document.getElementById('btn-eep-export').disabled = false;
    document.getElementById('btn-eep-copy').disabled = false;
    // Direct mode deliberately stays silent: the status line, summary and hex
    // dump report the result without any floating prompt after the write.
    if (needsConfirm) {
      toast(`Wrote ${d.length} bytes to ${d.chip_name}`, 'success');
    }
    statusEl.textContent = d.verified === false
      ? 'Write complete, but verification FAILED — re-read to check.'
      : `Write complete (${result}).`;
    if (d.verified === false) {
      toast(`Verify failed at ${d.mismatch_count} byte(s) — check wiring and re-read`,
            'error', 8000);
    }
  } finally {
    updateEepromWriteButton(false);
    // Tick 自动递增 → exactly one increment per Write actually sent (with or
    // without a confirmation dialog), success or failure: a partly programmed
    // part must never share its S/N with the next. Unticked → the S/N stays
    // put; the operator advances it explicitly with the Next button.
    if (eepromSnAutoIncrement()) {
      const next = advanceEepromSerial();
      statusEl.textContent += `  ·  Next S/N: ${next}`;
    }
  }
}

/**
 * Physically erase the whole EEPROM: write 0xFF to every byte of the selected
 * chip. Unlike eepromClear() (which only empties the UI content area), this is
 * an irreversible hardware write. Because it destroys all data it ALWAYS asks
 * for confirmation, independent of the 确认 checkbox — the operator cannot
 * silently wipe a part. The serial number is never patched, changed or
 * consumed: a blank chip has no product S/N.
 */
async function eepromErase() {
  if (!AppState.connected) { toast('Connect to a device first', 'error'); return; }
  const chip      = document.getElementById('eep-chip').value;
  const profile   = EEPROM_CHIPS[chip];
  const base      = uiAddressTo7Bit(parseHexOrDec(document.getElementById('eep-base').value));
  const statusEl  = document.getElementById('eep-status');
  const summaryEl = document.getElementById('eep-summary');
  if (!profile) { toast('Select a supported EEPROM chip', 'error'); return; }
  if (Number.isNaN(base)) { toast('Enter a valid base address', 'error'); return; }

  const addr = `0x${base.toString(16).toUpperCase().padStart(2, '0')}`;
  const message =
    `ERASE the entire ${profile.name} at ${addr}?\n\n`
    + `All ${profile.capacity} bytes will be written to 0xFF (blank).\n`
    + `This is PHYSICAL and CANNOT be undone — every byte, including the `
    + `Serial Number, is destroyed.\n`
    + `Never do this on a live CMIS optical module.`;
  // Mandatory confirmation: deliberately ignore the 确认 switch here, so this
  // destructive operation can never run silently just because it is unticked.
  if (!(await confirmEepromWrite(message))) return;

  const bytes = new Uint8Array(profile.capacity).fill(0xFF);
  updateEepromWriteButton(true);
  statusEl.textContent = `Erasing ${profile.name} (${profile.capacity} × 0xFF)…`;
  summaryEl.textContent = `Erasing entire ${profile.name} to 0xFF…`;
  try {
    // patch_serial:false is essential — the backend must write the raw 0xFF
    // bytes verbatim, not stamp the editable S/N into 0x000–0x00B.
    const res = await apiPost('/api/eeprom/write', {
      confirm: true, chip, start: 0, base_address: base,
      patch_serial: false, verify: !!document.getElementById('eep-verify')?.checked,
      data_b64: bytesToBase64(bytes),
    });
    if (res.status !== 'ok') {
      statusEl.textContent = 'Erase failed.';
      summaryEl.textContent = `Erase failed: ${res.message}`;
      toast(`Erase error: ${res.message}`, 'error', 8000);
      return;
    }
    const d = res.data;
    const result = d.verified === null ? 'verify skipped'
      : (d.verified ? 'verified' : `${d.mismatch_count} mismatch(es)`);
    // Content area shows the exact image that went onto the chip. loadSn=false
    // keeps the editable S/N untouched — 0xFF is not a serial number.
    setEepromImage({
      name: `${profile.name} erased`, bytes, start: 0, origin: 'chip',
    }, false);
    statusEl.textContent = `Erased ${d.chip_name}: ${d.length} bytes → 0xFF (${result}).`;
    summaryEl.textContent =
      `${d.chip_name} · erased ${d.length} bytes to 0xFF · ${d.transactions} page writes`
      + ` · ${result} · sha256:${d.image_sha256_16}`;
    toast(`已清空 ${d.chip_name} · ${d.length} B → 0xFF`, 'success');
    if (d.verified === false) {
      toast(`Verify failed at ${d.mismatch_count} byte(s) — re-read to check`,
            'error', 8000);
    }
  } catch (e) {
    statusEl.textContent = `Erase failed: ${e.message || e}`;
    toast(`Erase error: ${e.message || e}`, 'error', 8000);
  } finally {
    // No S/N increment: erasing is not programming a new board.
    updateEepromWriteButton(false);
  }
}

/**
 * Blank-chip auto-program: an erased 24Cxx reads back 0xFF at 0x000–0x00B.
 * When the operator already loaded a .bin (whose first 12 bytes supplied the
 * S/N in the box), a Read that finds an erased part programs that bin with the
 * S/N advanced by one — silently, no dialog and no second increment.
 *
 * @param {object} d /api/eeprom/read response data (carries `blank_chip`)
 * @param {number} base 7-bit base address already resolved by eepromRead()
 */
async function autoProgramBlankChip(d, base) {
  const statusEl  = document.getElementById('eep-status');
  const summaryEl = document.getElementById('eep-summary');
  const snEl      = document.getElementById('eep-serial');
  const chip      = document.getElementById('eep-chip').value;
  const profile   = EEPROM_CHIPS[chip];
  const file      = _loadedBinFile;
  if (!profile || !file) return;

  // Validate EVERYTHING before consuming an S/N. A half-armed state (bad file,
  // short image, illegal increment range) must not burn a serial number.
  let binBytes;
  try {
    binBytes = new Uint8Array(await file.arrayBuffer());
  } catch (e) {
    statusEl.textContent = `空白芯片自动烧写失败：无法读取 ${file.name} (${e.message || e})`;
    toast(`无法读取 ${file.name}`, 'error', 6000);
    return;
  }
  if (binBytes.length < EEPROM_SERIAL_LENGTH) {
    statusEl.textContent = `空白芯片自动烧写跳过：${file.name} 不足 `
                         + `${EEPROM_SERIAL_LENGTH} 字节，无法确定序列号`;
    return;
  }
  if (binBytes.length > profile.capacity) {
    statusEl.textContent = `空白芯片自动烧写跳过：${file.name} (${binBytes.length} B) `
                         + `超过 ${profile.name} 容量 ${profile.capacity} B`;
    return;
  }
  let currentSn;
  try {
    currentSn = String(snEl?.value ?? '');
    parseEepromSerial(currentSn);
    resolveEepromSnPos(eepromSnPos(), currentSn);
  } catch (e) {
    statusEl.textContent = `空白芯片自动烧写跳过：序列号无效 (${e.message})`;
    toast('空白芯片自动烧写跳过：序列号无效', 'error', 6000);
    return;
  }

  // Advance once, then send that value down with the .bin. This path must not
  // delegate to the manual write handler: that would ask for confirmation and
  // increment the S/N a second time.
  const nextSn = advanceEepromSerial();
  const binSn = decodeEepromSerial(binBytes.subarray(0, EEPROM_SERIAL_LENGTH));
  statusEl.textContent = `空白芯片自动烧写：写入 ${file.name}，序列号 ${nextSn}…`;
  summaryEl.textContent = `空白芯片 (0x000–0x00B 全 0xFF)：${file.name}`
                        + ` · ${binBytes.length} B · 序列号 ${currentSn} → ${nextSn}`;

  try {
    // Backend patches the advanced S/N into a COPY of the raw bin and runs its
    // full validation (ASCII, range, serial_pos, page/boundary splitting).
    const payload = {
      confirm: true, chip, start: 0, base_address: base,
      patch_serial: true, serial_number: nextSn, serial_pos: eepromSnPos(),
      verify: !!document.getElementById('eep-verify')?.checked,
      data_b64: bytesToBase64(binBytes),
    };
    const res = await apiPost('/api/eeprom/write', payload);
    if (res.status !== 'ok') {
      statusEl.textContent = `空白芯片自动烧写失败：${res.message}`;
      toast(`空白芯片自动烧写失败：${res.message}`, 'error', 8000);
      return;
    }
    // The content area must show exactly what went onto the chip: the bin
    // image with the advanced S/N at 0x000–0x00B — not the erased dump.
    const programmed = new Uint8Array(binBytes);
    programmed.set(parseEepromSerial(nextSn), EEPROM_SERIAL_START);
    setEepromImage({
      name: `${file.name} (auto-programmed)`,
      bytes: programmed, start: 0, origin: 'chip',
    }, false);
    const result = res.data.verified === null ? 'verify skipped'
      : (res.data.verified ? 'verified' : `${res.data.mismatch_count} mismatch(es)`);
    statusEl.textContent = `空白芯片已自动烧写：${res.data.length} B`
                         + ` · 序列号 ${nextSn} · ${result}`;
    summaryEl.textContent = `空白芯片：${file.name} · ${res.data.length} B`
      + ` · 序列号 ${currentSn} → ${nextSn} · ${result}`
      + ` · ${res.data.transactions} 次页写入`;
    toast(`空白芯片已自动烧写 · 序列号 ${nextSn}`, 'success');
  } catch (e) {
    statusEl.textContent = `空白芯片自动烧写失败：${e.message || e}`;
    toast(`空白芯片自动烧写失败：${e.message || e}`, 'error', 8000);
  }
}

/**
 * "Auto update": after a successful Read, if the S/N matched the product rule
 * and the consumption field was not 100%, repair that field in the dump and
 * write the image back silently — no confirmation dialog and, crucially, no
 * S/N increment (we are fixing the *same* chip, not programming the next one).
 *
 * @param {object} d the /api/eeprom/read response data (carries the analysis)
 * @param {number} base 7-bit base address already resolved by eepromRead()
 */
async function autoUpdateConsumption(d, base) {
  const statusEl  = document.getElementById('eep-status');
  const summaryEl = document.getElementById('eep-summary');
  const chip      = document.getElementById('eep-chip').value;
  const profile   = EEPROM_CHIPS[chip];
  const image     = _eepromImage;
  if (!image || !profile) return;

  const rel = CONSUMPTION_OFFSET - image.start;
  if (rel < 0 || rel + 1 >= image.bytes.length) {
    statusEl.textContent = 'Auto update skipped: dump does not cover 0x09C–0x09D.';
    return;
  }
  const oldPercent = d.consumption_percent;
  const before = [image.bytes[rel], image.bytes[rel + 1]];

  // Patch a copy first: the source image must stay intact if the write fails.
  const fixed = new Uint8Array(image.bytes);
  fixed[rel] = 0x00;
  fixed[rel + 1] = CONSUMPTION_FULL_PERCENT;
  const hexBefore = before.map(b => b.toString(16).toUpperCase().padStart(2, '0')).join(' ');

  summaryEl.textContent = `Auto update: 0x09C–0x09D ${hexBefore} (${oldPercent}%)`
                        + ` → 00 64 (100%) — writing back…`;
  statusEl.textContent = 'Auto update: writing 100% consumption…';
  try {
    const payload = {
      confirm: true, chip, start: image.start, base_address: base,
      patch_serial: false, verify: !!document.getElementById('eep-verify')?.checked,
      data_b64: bytesToBase64(fixed),
    };
    const res = await apiPost('/api/eeprom/write', payload);
    if (res.status !== 'ok') {
      statusEl.textContent = `Auto update FAILED: ${res.message}`;
      toast(`Auto update write error: ${res.message}`, 'error', 8000);
      return;
    }
    // Keep the content area showing exactly what is now on the chip.
    setEepromImage({
      name: image.name, bytes: fixed, start: image.start, origin: 'chip',
    }, false);
    const result = res.data.verified === null ? 'verify skipped'
      : (res.data.verified ? 'verified' : `${res.data.mismatch_count} mismatch(es)`);
    summaryEl.textContent = `Auto update: 0x09C–0x09D ${hexBefore} (${oldPercent}%)`
      + ` → 00 64 (100%) · ${res.data.length} bytes written, ${result}`
      + ` · S/N unchanged (${document.getElementById('eep-serial')?.value || ''})`;
    statusEl.textContent = 'Auto update complete: consumption set to 100%.';
    toast('Auto update: consumption set to 100%', 'success');
  } catch (e) {
    statusEl.textContent = `Auto update FAILED: ${e.message || e}`;
    toast(`Auto update error: ${e.message || e}`, 'error', 8000);
  }
}

async function eepromRead() {
  const chip    = document.getElementById('eep-chip').value;
  const start   = parseHexOrDec(document.getElementById('eep-start').value);
  const length  = parseInt(document.getElementById('eep-length').value, 10);
  const chunk   = parseInt(document.getElementById('eep-chunk').value, 10);
  const base    = uiAddressTo7Bit(parseHexOrDec(document.getElementById('eep-base').value));
  const profile = EEPROM_CHIPS[chip];

  if (!profile) { toast('Select a supported EEPROM chip', 'error'); return; }
  if ([start, length, chunk, base].some(v => Number.isNaN(v))) {
    toast('Enter valid hex/dec values for address, length and chunk', 'error');
    return;
  }
  if (length > profile.capacity) {
    toast(`${profile.name} holds only ${profile.capacity} bytes`, 'error');
    return;
  }

  const btn       = document.getElementById('btn-eep-read');
  const dumpEl    = document.getElementById('eep-dump');
  const statusEl  = document.getElementById('eep-status');
  const summaryEl = document.getElementById('eep-summary');

  btn.disabled = true;
  // While "Reading…" is on screen there is no valid image to program. Keep the
  // previous image in memory, but disable Write until the read resolves.
  _lastEeprom = null;
  updateEepromWriteButton(true);
  document.getElementById('btn-eep-export').disabled = true;
  document.getElementById('btn-eep-copy').disabled = true;
  statusEl.textContent = `Reading ${length} byte(s)…`;
  dumpEl.textContent = 'Reading…';
  try {
    const res = await apiPost('/api/eeprom/read', {
      start, length, chunk, chip, base_address: base,
      serial_pos: eepromSnPos(),
    });
    if (res.status !== 'ok') {
      statusEl.textContent = 'Read failed.';
      summaryEl.textContent = `Read failed: ${res.message}`;
      toast(`EEPROM read error: ${res.message}`, 'error', 6000);
      // Keep the content area and the Write source consistent: restore the
      // previous image (if any) instead of leaving an error where data was.
      if (_eepromImage) setEepromImage(_eepromImage, false);
      else dumpEl.textContent = `Error: ${res.message}`;
      return;
    }

    const d = res.data;
    const chipName = d.chip_name || profile.name;
    // Requirement: a successful Read enables Write AND copies the 12 ASCII bytes
    // at 0x000–0x00B of the content area into the Serial Number box. The dump
    // itself becomes the image Write programs, so "read a golden part →
    // program the next boards" needs no .bin file at all.
    const startHex = `0x${d.start.toString(16).toUpperCase().padStart(3, '0')}`;
    setEepromImage({
      name: `${chipName} readback ${startHex}-${d.length}B`,
      bytes: Uint8Array.from(d.data),
      start: d.start,
      origin: 'chip',
    });

    const segs = d.segments.map(s =>
      `0x${s.address.toString(16).toUpperCase().padStart(2, '0')}:${s.count}B`).join('  ');
    summaryEl.textContent =
      `${chipName} · ${d.capacity} B chip · ${d.page_size} B page · ${d.blocks} block(s)`
      + ` | ${d.length} bytes · 0x${d.start.toString(16).toUpperCase().padStart(3, '0')}`
      + `–0x${(d.end - 1).toString(16).toUpperCase().padStart(3, '0')}`
      + ` · ${d.transactions} I2C transfers (≤${d.chunk}B each) · ${segs}`
      + (d.serial_bytes ? ` · S/N ${document.getElementById('eep-serial')?.value} loaded` : '');
    statusEl.textContent = 'Read complete. Write is enabled with this dump.';
    toast(`Read ${d.length} bytes from ${chipName} — Write enabled`, 'success');

    // Blank-chip auto-program takes priority and is mutually exclusive with
    // "Auto update": an erased part (0xFF S/N) is not a product S/N, so the
    // consumption repair must never run on it. Both paths write only after a
    // successful read and only from explicit flags in the read response.
    // This one is additionally gated by the opt-in "烧写空白" checkbox — a
    // Read never writes anything unless the operator explicitly armed it.
    const blankEl = document.getElementById('eep-blank-program');
    if (blankEl?.checked && d.blank_chip === true && _loadedBinFile) {
      await autoProgramBlankChip(d, base);
    } else {
      // Optional "Auto update": only when the checkbox is ticked AND the dump
      // passed the product S/N rule AND consumption is not already 100%.
      const autoEl = document.getElementById('eep-auto-update');
      if (autoEl?.checked && d.serial_rule_ok === true
          && d.consumption_needs_fix === true) {
        await autoUpdateConsumption(d, base);
      }
    }
  } finally {
    btn.disabled = false;
    updateEepromWriteButton(false);
  }
}

function eepromExport() {
  if (!_lastEeprom) return;
  const { start, bytes, chip } = _lastEeprom;
  const blob = new Blob([new Uint8Array(bytes)], { type: 'application/octet-stream' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  const tag = `0x${start.toString(16).toUpperCase().padStart(3, '0')}-${bytes.length}B`;
  a.href = url;
  a.download = `eeprom_${chip || 'dump'}_${tag}.bin`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  toast(`Saved ${bytes.length} bytes`, 'success');
}

async function eepromCopyHex() {
  if (!_lastEeprom) return;
  const text = _lastEeprom.bytes
    .map(b => b.toString(16).toUpperCase().padStart(2, '0')).join(' ');
  try {
    await navigator.clipboard.writeText(text);
    toast('Hex copied to clipboard', 'success');
  } catch (e) {
    // Clipboard API needs a secure context. Fall back to selecting the dump
    // that is already on screen so Ctrl+C still works.
    const range = document.createRange();
    range.selectNodeContents(document.getElementById('eep-dump'));
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    toast('Clipboard blocked — dump selected, press Ctrl+C', 'error', 6000);
  }
}

function eepromClear() {
  _lastEeprom = null;
  // An empty content area must not leave a hidden image that Write could
  // program: Read / file selection is the only way to arm a write again.
  _eepromImage = null;
  document.getElementById('eep-dump').textContent = 'Results will appear here…';
  document.getElementById('eep-summary').textContent = '';
  document.getElementById('eep-status').textContent = 'Ready.';
  document.getElementById('btn-eep-export').disabled = true;
  document.getElementById('btn-eep-copy').disabled = true;
  updateEepromWriteButton();
  updateEepromPreview();
}

// ---------------------------------------------------------------------------
// PRBS pattern names
// ---------------------------------------------------------------------------
const PRBS_PATTERNS = [
  'PRBS31Q','PRBS31','PRBS23Q','PRBS23','PRBS15Q','PRBS15',
  'PRBS13Q','PRBS13','PRBS9Q','PRBS9','PRBS7Q','PRBS7','SSPRQ',
];

/**
 * Render a module-reported BER.
 *
 * A zero F16 word means the module counted no errors, not that it measured a
 * bound. This used to render as a fixed "less than" figure with no basis - the
 * format bottoms out around 1e-24 - which is the sort of number that ends up
 * quoted in a test report as if it had been measured.
 */
function formatBer(ber) {
  if (ber == null || !isFinite(ber)) return '—';
  if (ber === 0) {
    return '<span title="Module reports no errors. This is a zero count, not a '
         + 'measured limit — the achievable floor depends on how many bits the '
         + 'gate accumulated.">0</span>';
  }
  return ber.toExponential(2);
}

// ---------------------------------------------------------------------------
// Flags (Monitoring tab)
// ---------------------------------------------------------------------------
function renderFlags(lanes) {
  const tbody = document.getElementById('tbl-flags');
  if (!tbody || !lanes) return;

  tbody.innerHTML = lanes.map(lane => {
    function flagCell(val, isAlarm) {
      if (val) {
        return isAlarm
          ? '<span class="flag-active">&#9632; ALARM</span>'
          : '<span class="flag-warn">&#9650; WARN</span>';
      }
      return '<span class="flag-ok">&#9679;</span>';
    }
    const txFault   = flagCell(lane.tx_fault, true);
    const txLos     = flagCell(lane.tx_los, true);
    const txCdrLol  = flagCell(lane.tx_cdr_lol, true);
    const rxLos     = flagCell(lane.rx_los, true);
    const rxCdrLol  = flagCell(lane.rx_cdr_lol, true);

    const anyAlarm = lane.tx_power_high_alarm || lane.tx_power_low_alarm ||
                     lane.tx_bias_high_alarm  || lane.tx_bias_low_alarm  ||
                     lane.rx_power_high_alarm || lane.rx_power_low_alarm;
    const anyWarn  = lane.tx_power_high_warn  || lane.tx_power_low_warn  ||
                     lane.tx_bias_high_warn   || lane.tx_bias_low_warn   ||
                     lane.rx_power_high_warn  || lane.rx_power_low_warn;
    const summary = anyAlarm
      ? '<span class="flag-active">&#9632; Alarm</span>'
      : anyWarn
      ? '<span class="flag-warn">&#9650; Warn</span>'
      : '<span class="flag-ok">&#9679;</span>';

    return `<tr>
      <td>${lane.lane}</td>
      <td>${txFault}</td>
      <td>${txLos}</td>
      <td>${txCdrLol}</td>
      <td>${rxLos}</td>
      <td>${rxCdrLol}</td>
      <td>${summary}</td>
    </tr>`;
  }).join('');
}

// ---------------------------------------------------------------------------
// Thresholds (Monitoring tab)
// ---------------------------------------------------------------------------
async function loadThresholds() {
  if (!AppState.connected) return;
  const res = await apiGet('/api/module/thresholds');
  if (res.status !== 'ok') { toast(`Thresholds error: ${res.message}`, 'error'); return; }
  const d = res.data;
  _moduleThresholds = d;   // drives the monitoring table's alarm colouring
  const tbody = document.getElementById('tbl-thresholds');
  if (!tbody) return;

  // [label, page/addr, ha, la, hw, lw]
  const rows = [
    ['Temperature (°C)', '02h / 0x80–0x87', d.temp_high_alarm, d.temp_low_alarm, d.temp_high_warn, d.temp_low_warn],
    ['Vcc (V)',          '02h / 0x88–0x8F', d.vcc_high_alarm,  d.vcc_low_alarm,  d.vcc_high_warn,  d.vcc_low_warn],
    ['Tx Power (dBm)',   '02h / 0xB0–0xB7', d.tx_power_high_alarm_dbm, d.tx_power_low_alarm_dbm, d.tx_power_high_warn_dbm, d.tx_power_low_warn_dbm],
    ['Tx Bias (mA)',     '02h / 0xB8–0xBF', d.tx_bias_high_alarm_ma,   d.tx_bias_low_alarm_ma,   d.tx_bias_high_warn_ma,   d.tx_bias_low_warn_ma],
    ['Rx Power (dBm)',   '02h / 0xC0–0xC7', d.rx_power_high_alarm_dbm, d.rx_power_low_alarm_dbm, d.rx_power_high_warn_dbm, d.rx_power_low_warn_dbm],
  ];

  tbody.innerHTML = rows.map(([label, reg, ha, la, hw, lw]) =>
    `<tr>
      <td style="color:var(--text-muted)">${label}</td>
      <td class="td-addr">${reg}</td>
      <td class="ha">${ha}</td>
      <td class="la">${la}</td>
      <td class="hw">${hw}</td>
      <td class="lw">${lw}</td>
    </tr>`
  ).join('');
}

// ---------------------------------------------------------------------------
// Squelch / Output Controls (DataPath tab)
// ---------------------------------------------------------------------------
function _mkCheckbox(id) {
  return `<input type="checkbox" id="${id}">`;
}

function _populateBitmaskRow(prefix, mask, meta) {
  for (let i = 0; i < 8; i++) {
    const td = document.getElementById(`${prefix}-td-${i}`);
    if (!td) continue;
    td.innerHTML = _mkCheckbox(`${prefix}-cb-${i}`);
    const cb = document.getElementById(`${prefix}-cb-${i}`);
    cb.checked = !!((mask >> i) & 1);
    if (meta) {
      const tip = regTip({
        field: `${meta.field}${i + 1}`,
        page: meta.page, addr: meta.addr, value: mask, bit: i,
        note: ((mask >> i) & 1) ? meta.onNote : meta.offNote,
      });
      cb.title = tip;
      td.title = tip;
    }
  }
}

function _readBitmaskRow(prefix) {
  let mask = 0;
  for (let i = 0; i < 8; i++) {
    const cb = document.getElementById(`${prefix}-cb-${i}`);
    if (cb && cb.checked) mask |= (1 << i);
  }
  return mask;
}

async function loadSquelch() {
  if (!AppState.connected) return;
  const res = await apiGet('/api/module/squelch');
  if (res.status !== 'ok') { toast(`Squelch error: ${res.message}`, 'error'); return; }
  _populateBitmaskRow('sq', res.data.tx_squelch_disable, {
    field: 'AutoSquelchDisableTx', page: 0x10, addr: 0x83,
    onNote: 'Auto-squelch controller disabled for this lane',
    offNote: 'Auto-squelch controller enabled for this lane' });
  _populateBitmaskRow('sf', res.data.tx_squelch_force, {
    field: 'OutputSquelchForceTx', page: 0x10, addr: 0x84,
    onNote: 'Tx output squelch forced on', offNote: 'Tx output not force-squelched' });
  _populateBitmaskRow('od', res.data.rx_output_disable, {
    field: 'OutputDisableRx', page: 0x10, addr: 0x8A,
    onNote: 'Rx output disabled', offNote: 'Rx output enabled' });
  _populateBitmaskRow('rd', res.data.rx_squelch_disable, {
    field: 'AutoSquelchDisableRx', page: 0x10, addr: 0x8B,
    onNote: 'Auto-squelch controller disabled for this lane',
    offNote: 'Auto-squelch controller enabled for this lane' });
}

async function applySquelch() {
  return applyAndReload('Output controls', '/api/module/squelch', {
    tx_squelch_disable: _readBitmaskRow('sq'),
    tx_squelch_force:   _readBitmaskRow('sf'),
    rx_output_disable:  _readBitmaskRow('od'),
    rx_squelch_disable: _readBitmaskRow('rd'),
  }, loadSquelch);
}

// ---------------------------------------------------------------------------
// Loopback (Diagnostics tab)
// ---------------------------------------------------------------------------
function _populateLoopbackRow(prefix, mask, meta) {
  for (let i = 0; i < 8; i++) {
    const td = document.getElementById(`lb-${prefix}-${i}`);
    if (!td) continue;
    td.innerHTML = _mkCheckbox(`lb-cb-${prefix}-${i}`);
    const cb = document.getElementById(`lb-cb-${prefix}-${i}`);
    cb.checked = !!((mask >> i) & 1);
    if (meta) {
      const tip = regTip({
        field: `${meta.field}Lane${i + 1}`,
        page: 0x13, addr: meta.addr, value: mask, bit: i,
        note: ((mask >> i) & 1) ? 'Loopback engaged on this lane' : 'Normal non-loopback operation',
      });
      cb.title = tip;
      td.title = tip;
    }
  }
}

function _readLoopbackRow(prefix) {
  let mask = 0;
  for (let i = 0; i < 8; i++) {
    const cb = document.getElementById(`lb-cb-${prefix}-${i}`);
    if (cb && cb.checked) mask |= (1 << i);
  }
  return mask;
}

async function loadLoopback() {
  if (!AppState.connected) return;
  const res = await apiGet('/api/module/loopback');
  if (res.status !== 'ok') { toast(`Loopback error: ${res.message}`, 'error'); return; }
  _populateLoopbackRow('mso', res.data.media_side_output,
    { field: 'MediaSideOutputLoopbackEnable', addr: 0xB4 });
  _populateLoopbackRow('msi', res.data.media_side_input,
    { field: 'MediaSideInputLoopbackEnable', addr: 0xB5 });
  _populateLoopbackRow('hso', res.data.host_side_output,
    { field: 'HostSideOutputLoopbackEnable', addr: 0xB6 });
  _populateLoopbackRow('hsi', res.data.host_side_input,
    { field: 'HostSideInputLoopbackEnable', addr: 0xB7 });
}

async function applyLoopback() {
  return applyAndReload('Loopback configuration', '/api/module/loopback', {
    media_side_output: _readLoopbackRow('mso'),
    media_side_input:  _readLoopbackRow('msi'),
    host_side_output:  _readLoopbackRow('hso'),
    host_side_input:   _readLoopbackRow('hsi'),
  }, loadLoopback);
}

// ---------------------------------------------------------------------------
// PRBS (Diagnostics tab)
// ---------------------------------------------------------------------------
function _renderPrbsTable(tbodyId, block, lolMask, base, side) {
  const tbody = document.getElementById(tbodyId);
  if (!tbody) return;
  const isChecker = (lolMask !== undefined);
  // Field names follow CMIS 5.3 Tables 8-109/8-111/8-113/8-115: each block is
  // 8 bytes from `base` — Enable, DataInvert, SwapSymbolBits, Pre/PostFECEnable,
  // then 4 PatternSelect bytes holding two 4-bit lane selectors each.
  const role = isChecker ? 'Checker' : 'Generator';
  const fecName = isChecker ? 'PostFECEnable' : 'PreFECEnable';
  const tip = (suffix, off, mask, i, note) => esc(regTip({
    field: `${side}Side${role}${suffix}Lane${i + 1}`, page: 0x13, addr: base + off,
    value: mask, bit: i, note,
  }));
  tbody.innerHTML = Array.from({length: 8}, (_, i) => {
    const en  = !!((block.enable_mask    >> i) & 1);
    const inv = !!((block.invert_mask    >> i) & 1);
    const sw  = !!((block.byte_swap_mask >> i) & 1);
    const fec = !!((block.fec_mask       >> i) & 1);
    const pattern = block.patterns[i] || 0;
    const patOpts = PRBS_PATTERNS.map((name, idx) =>
      `<option value="${idx}" ${pattern === idx ? 'selected' : ''}>${name}</option>`
    ).join('');
    let lolCell = '';
    if (isChecker) {
      const lol = !!((lolMask >> i) & 1);
      lolCell = `<td>${lol ? '<span class="flag-active">LOL</span>' : '<span class="flag-ok">●</span>'}</td>`;
    }
    // Lane i's 4-bit pattern selector sits in the low or high nibble of
    // pattern byte base+4+(i>>1).
    // Lane i's 4-bit PatternSelect sits in byte base+4+(i>>1); odd lanes
    // (Lane 1, 3, 5, 7) occupy bits 3-0, even lanes bits 7-4.
    const patAddr = base + 4 + (i >> 1);
    const patTip = esc(regTip({
      field: `${side}Side${role}PatternSelectLane${i + 1}`, page: 0x13, addr: patAddr,
      note: `Bits ${(i % 2) ? '7-4' : '3-0'} = ${pattern} (${PRBS_PATTERNS[pattern] || '?'})`,
    }));
    const tEn  = tip('Enable', 0, block.enable_mask, i,
                     en ? `${role} running on this lane` : `${role} stopped on this lane`);
    const tInv = tip('DataInvert', 1, block.invert_mask, i,
                     inv ? 'Pattern data inverted' : 'Pattern data not inverted');
    const tSw  = tip('SwapSymbolBits', 2, block.byte_swap_mask, i,
                     sw ? 'Symbol bit order swapped' : 'Normal symbol bit order');
    const tFec = tip(fecName, 3, block.fec_mask, i,
                     fec ? 'Applied at the FEC-coded side' : 'Applied at the raw side');

    return `<tr>
      <td>L${i+1}</td>
      <td title="${tEn}"><input type="checkbox" id="${tbodyId}-en-${i}" title="${tEn}" ${en  ? 'checked' : ''}></td>
      <td title="${tInv}"><input type="checkbox" id="${tbodyId}-inv-${i}" title="${tInv}" ${inv ? 'checked' : ''}></td>
      <td title="${tSw}"><input type="checkbox" id="${tbodyId}-sw-${i}" title="${tSw}" ${sw  ? 'checked' : ''}></td>
      <td title="${tFec}"><input type="checkbox" id="${tbodyId}-fec-${i}" title="${tFec}" ${fec ? 'checked' : ''}></td>
      <td title="${patTip}"><select class="app-select-input" id="${tbodyId}-pat-${i}" title="${patTip}">${patOpts}</select></td>
      ${lolCell}
    </tr>`;
  }).join('');
}

function _readPrbsSection(tbodyId) {
  let en = 0, inv = 0, sw = 0, fec = 0;
  const patterns = [];
  for (let i = 0; i < 8; i++) {
    if (document.getElementById(`${tbodyId}-en-${i}`)?.checked)  en  |= (1 << i);
    if (document.getElementById(`${tbodyId}-inv-${i}`)?.checked) inv |= (1 << i);
    if (document.getElementById(`${tbodyId}-sw-${i}`)?.checked)  sw  |= (1 << i);
    if (document.getElementById(`${tbodyId}-fec-${i}`)?.checked) fec |= (1 << i);
    const sel = document.getElementById(`${tbodyId}-pat-${i}`);
    patterns.push(sel ? parseInt(sel.value, 10) : 0);
  }
  return { enable_mask: en, invert_mask: inv, byte_swap_mask: sw, fec_mask: fec, patterns };
}

async function loadPrbs() {
  if (!AppState.connected) return;
  const res = await apiGet('/api/module/prbs');
  if (res.status !== 'ok') { toast(`PRBS error: ${res.message}`, 'error'); return; }
  const d = res.data;
  _renderPrbsTable('tbl-prbs-host-gen',  d.host_gen,  undefined, 0x90, 'Host');
  _renderPrbsTable('tbl-prbs-media-gen', d.media_gen, undefined, 0x98, 'Media');
  _renderPrbsTable('tbl-prbs-host-chk',  d.host_chk,  d.host_chk_lol_mask,  0xA0, 'Host');
  _renderPrbsTable('tbl-prbs-media-chk', d.media_chk, d.media_chk_lol_mask, 0xA8, 'Media');
}

async function applyPrbs() {
  return applyAndReload('PRBS configuration', '/api/module/prbs', {
    host_gen:  _readPrbsSection('tbl-prbs-host-gen'),
    media_gen: _readPrbsSection('tbl-prbs-media-gen'),
    host_chk:  _readPrbsSection('tbl-prbs-host-chk'),
    media_chk: _readPrbsSection('tbl-prbs-media-chk'),
  }, loadPrbs);
}

// ---------------------------------------------------------------------------
// BER (Diagnostics tab)
// ---------------------------------------------------------------------------
async function loadBer() {
  if (!AppState.connected) return;
  const res = await apiGet('/api/module/ber');
  const tbody = document.getElementById('tbl-ber');
  if (!tbody) return;
  if (res.status !== 'ok') { toast(`BER error: ${res.message}`, 'error'); return; }
  const hostCells = res.data.lanes.map(l => `<td>${formatBer(l.host_ber)}</td>`).join('');
  const mediaCells = res.data.lanes.map(l => `<td>${formatBer(l.media_ber)}</td>`).join('');
  tbody.innerHTML =
    `<tr><td style="color:var(--text-muted)">Host<span class="reg-badge">14h/0xC0</span></td>${hostCells}</tr>` +
    `<tr><td style="color:var(--text-muted)">Media<span class="reg-badge">14h/0xD0</span></td>${mediaCells}</tr>`;
}

// ---------------------------------------------------------------------------
// Error / Bit Counters (Diagnostics tab)
// ---------------------------------------------------------------------------
async function loadCounters() {
  if (!AppState.connected) return;
  const res = await apiGet('/api/module/counters');
  const tbody = document.getElementById('tbl-counters');
  if (!tbody) return;
  if (res.status !== 'ok') { toast(`Counters error: ${res.message}`, 'error'); return; }
  const lanes = res.data.lanes;
  const fmtCount = (v) => v != null ? v.toLocaleString() : '—';
  // While the checker has lost pattern sync the counters keep accumulating but
  // mean nothing, so say so rather than rendering a number that reads like a
  // measurement.
  const fmtBer = (v, psl) => psl ? '<span class="flag-active">no sync</span>'
                           : (v != null && v > 0 ? v.toExponential(2) : '—');
  const cell = (l, psl, html) =>
    `<td${psl ? ' class="text-muted" title="Pattern sync lost on this lane — counts are not a valid measurement"' : ''}>${html}</td>`;

  const row = (side, field, fmt) => lanes.map(l =>
    cell(l, l[`${side}_psl`], fmt(l[`${side}_${field}`]))).join('');

  const hostErrCells  = row('host', 'error_count', fmtCount);
  const hostBitCells  = row('host', 'total_bits', fmtCount);
  const hostBerCells  = lanes.map(l => cell(l, l.host_psl, fmtBer(l.host_ber, l.host_psl))).join('');
  const mediaErrCells = row('media', 'error_count', fmtCount);
  const mediaBitCells = row('media', 'total_bits', fmtCount);
  const mediaBerCells = lanes.map(l => cell(l, l.media_psl, fmtBer(l.media_ber, l.media_psl))).join('');

  const anyPsl = lanes.some(l => l.host_psl || l.media_psl);
  const note = anyPsl
    ? `<tr><td colspan="9" class="flag-active" style="font-size:var(--fs-xs)">`
      + `⚠ Pattern sync lost on one or more lanes (14h counter bit 0). Their error `
      + `and bit counts are not a valid BER measurement.</td></tr>`
    : '';

  tbody.innerHTML = `
    <tr><td style="color:var(--text-muted)">Host Errors</td>${hostErrCells}</tr>
    <tr><td style="color:var(--text-muted)">Host Total Bits</td>${hostBitCells}</tr>
    <tr><td style="color:var(--text-muted)">Host BER (calc)</td>${hostBerCells}</tr>
    <tr><td style="color:var(--text-muted)">Media Errors</td>${mediaErrCells}</tr>
    <tr><td style="color:var(--text-muted)">Media Total Bits</td>${mediaBitCells}</tr>
    <tr><td style="color:var(--text-muted)">Media BER (calc)</td>${mediaBerCells}</tr>
    ${note}`;
}

// ---------------------------------------------------------------------------
// Laser Tuning (Diagnostics tab)
// ---------------------------------------------------------------------------
let _laserData = null;

async function loadLaser() {
  if (!AppState.connected) return;
  const res = await apiGet('/api/module/laser');
  const tbody = document.getElementById('tbl-laser');
  const capsEl = document.getElementById('laser-caps');
  if (!tbody) return;
  if (res.status !== 'ok') {
    tbody.innerHTML = `<tr><td colspan="7" class="placeholder-text">Laser tuning not available: ${res.message}</td></tr>`;
    return;
  }
  _laserData = res.data;
  const d = res.data;

  // Non-tunable module: Page 04h reads all zeros → no grids advertised
  if (!d.grids_supported || d.grids_supported.length === 0) {
    if (capsEl) {
      capsEl.innerHTML = '<span style="color:var(--muted)">Not a tunable laser module (Media Interface Technology is not C-band/L-band); Page 04h tuning capabilities not advertised.</span>';
    }
    tbody.innerHTML = '<tr><td colspan="7" class="placeholder-text">Non-tunable module — no Page 04h/12h data.</td></tr>';
    return;
  }

  if (capsEl) {
    capsEl.innerHTML =
      `Grids: <b>${d.grids_supported.join(', ')}</b> | ` +
      `Fine Tuning: <b>${d.fine_tuning_supported ? 'Yes' : 'No'}</b>` +
      (d.fine_tuning_supported ? ` (res ${d.fine_resolution_ghz} GHz, range ${d.fine_range_ghz[0]}..${d.fine_range_ghz[1]} GHz)` : '') +
      ` | Power: <b>${d.power_range_dbm[0]}..${d.power_range_dbm[1]} dBm</b>`;
  }

  const gridOpts = (cur) => [
    [5, '100 GHz'], [4, '50 GHz'], [3, '25 GHz'], [7, '75 GHz'],
    [6, '33 GHz'], [2, '12.5 GHz'], [1, '6.25 GHz'], [0, '3.125 GHz'], [8, '150 GHz'],
  ].map(([code, name]) =>
    `<option value="${code}" ${cur === code ? 'selected' : ''}>${name}</option>`
  ).join('');

  tbody.innerHTML = d.lanes.map(l => {
    const lockIcon = l.wavelength_locked
      ? '<span class="flag-ok">Locked</span>'
      : l.tuning_in_progress
      ? '<span class="flag-warn">Tuning...</span>'
      : '<span class="flag-active">Unlocked</span>';
    const i = l.lane - 1;
    // Page 12h: 1 byte/lane grid, then S16 per lane for channel, fine offset
    // and target power; frequency and status are read-only feedback.
    const tipGrid = esc(regTip({
      field: `GridSpacingTx${l.lane}`, page: 0x12, addr: 0x80 + i,
      note: `Bits 7-4 = ${l.grid_code} (${l.grid}); bit 0 FineTuningEnableTx = `
          + `${l.fine_tuning_enabled ? 1 : 0} (bits 3-1 reserved)`,
    }));
    const tipCh = esc(regTipRange(`ChannelNumberTx${l.lane}`, 0x12, 0x88 + i * 2, 2,
      `S16 channel number, current ${l.channel}`));
    const tipFt = esc(regTipRange(`FineTuningOffsetTx${l.lane}`, 0x12, 0x98 + i * 2, 2,
      `S16 in units of 0.001 GHz, current ${l.fine_offset_ghz} GHz`));
    const tipFreq = esc(regTipRange(`CurrentLaserFrequencyTx${l.lane}`, 0x12, 0xA8 + i * 4, 4,
      `U32 in units of 0.001 GHz, current ${l.frequency_thz.toFixed(6)} THz (read-only)`));
    const tipPwr = esc(regTipRange(`TargetOutputPowerTx${l.lane}`, 0x12, 0xC8 + i * 2, 2,
      `S16 in units of 0.01 dBm, current ${l.target_power_dbm} dBm`));
    const tipStat = esc(regTip({
      field: `TuningInProgressTx${l.lane} / WavelengthUnlockedTx${l.lane}`,
      page: 0x12, addr: 0xDE + i,
      note: `Bit 1 TuningInProgressTx = ${l.tuning_in_progress ? 1 : 0}; `
          + `bit 0 WavelengthUnlockedTx = ${l.wavelength_locked ? 0 : 1} (bits 7-2 reserved)`,
    }));

    return `<tr>
      <td>${l.lane}</td>
      <td title="${tipGrid}"><select class="app-select-input" id="laser-grid-${l.lane}" title="${tipGrid}">${gridOpts(l.grid_code)}</select></td>
      <td title="${tipCh}"><input type="number" id="laser-ch-${l.lane}" title="${tipCh}" value="${l.channel}" style="width:70px" class="raw-data-input"></td>
      <td title="${tipFt}"><input type="number" id="laser-ft-${l.lane}" title="${tipFt}" value="${l.fine_offset_ghz}" step="0.001" style="width:80px" class="raw-data-input"></td>
      <td style="font-family:var(--font-mono)" title="${tipFreq}">${l.frequency_thz.toFixed(6)}</td>
      <td title="${tipPwr}"><input type="number" id="laser-pwr-${l.lane}" title="${tipPwr}" value="${l.target_power_dbm}" step="0.01" style="width:70px" class="raw-data-input"></td>
      <td title="${tipStat}">${lockIcon}</td>
    </tr>`;
  }).join('');
}

async function applyLaser() {
  const lanes = [];
  for (let i = 1; i <= 8; i++) {
    const gridSel = document.getElementById(`laser-grid-${i}`);
    const chInput = document.getElementById(`laser-ch-${i}`);
    const ftInput = document.getElementById(`laser-ft-${i}`);
    const pwrInput = document.getElementById(`laser-pwr-${i}`);
    if (!gridSel) continue;
    const fineOffset = parseFloat(ftInput.value);
    lanes.push({
      lane: i,
      grid_code: parseInt(gridSel.value, 10),
      channel: parseInt(chInput.value, 10) || 0,
      fine_tuning_enabled: !Number.isNaN(fineOffset) && fineOffset !== 0,
      fine_offset_ghz: Number.isNaN(fineOffset) ? 0 : fineOffset,
      target_power_dbm: parseFloat(pwrInput.value) || 0,
    });
  }
  const res = await apiPost('/api/module/laser', { lanes });
  if (res.status === 'ok') {
    toast('Laser tuning applied', 'success');
    setTimeout(loadLaser, 300);
  } else {
    toast(`Apply failed: ${res.message}`, 'error');
  }
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
document.addEventListener('DOMContentLoaded', () => {
  // Display settings — wired first so the controls work even before a module
  // is connected, which is exactly when someone needs to fix an unreadable UI.
  initSettings();

  // Load backends
  loadBackends();

  // Connect / Disconnect buttons
  document.getElementById('btn-connect').addEventListener('click', connectModule);
  document.getElementById('btn-disconnect').addEventListener('click', disconnectModule);
  document.getElementById('btn-refresh-backends').addEventListener('click', loadBackends);

  // Tab buttons
  document.querySelectorAll('.tab-btn[data-tab]').forEach(btn => {
    btn.addEventListener('click', () => {
      if (!btn.disabled) switchTab(btn.dataset.tab);
    });
  });

  // Info refresh
  document.getElementById('btn-refresh-info')?.addEventListener('click', loadInfo);
  document.getElementById('btn-check-update')?.addEventListener('click', checkForUpdate);

  // Monitoring interval selector
  document.getElementById('sel-refresh-interval')?.addEventListener('change', e => {
    const v = e.target.value;
    if (v === 'manual') {
      AppState.monitoringManual = true;
      stopMonitoring();
    } else {
      AppState.monitoringManual = false;
      setRefreshInterval(parseInt(v, 10));
    }
  });
  document.getElementById('btn-refresh-monitor')?.addEventListener('click', loadMonitoring);

  // DataPath apply
  document.getElementById('btn-apply-datapath')?.addEventListener('click', applyDatapath);
  document.getElementById('btn-refresh-datapath')?.addEventListener('click', loadDatapath);

  // Thresholds
  document.getElementById('btn-refresh-thresholds')?.addEventListener('click', loadThresholds);

  // Squelch / output controls
  document.getElementById('btn-refresh-squelch')?.addEventListener('click', loadSquelch);
  document.getElementById('btn-apply-squelch')?.addEventListener('click', applySquelch);

  // Loopback
  document.getElementById('btn-refresh-loopback')?.addEventListener('click', loadLoopback);
  document.getElementById('btn-apply-loopback')?.addEventListener('click', applyLoopback);

  // PRBS
  document.getElementById('btn-refresh-prbs')?.addEventListener('click', loadPrbs);
  document.getElementById('btn-apply-prbs')?.addEventListener('click', applyPrbs);

  // BER, SNR, Counters, Laser
  document.getElementById('btn-refresh-ber')?.addEventListener('click', loadBer);
  document.getElementById('btn-refresh-snr')?.addEventListener('click', loadSnr);
  document.getElementById('btn-refresh-counters')?.addEventListener('click', loadCounters);
  document.getElementById('btn-refresh-laser')?.addEventListener('click', loadLaser);
  document.getElementById('btn-apply-laser')?.addEventListener('click', applyLaser);

  // Module Control & Applications
  document.getElementById('btn-refresh-control')?.addEventListener('click', loadModuleControl);
  document.getElementById('btn-refresh-apps')?.addEventListener('click', loadApplications);

  // Raw register read/write
  document.getElementById('btn-raw-read')?.addEventListener('click', rawRead);
  document.getElementById('btn-raw-write')?.addEventListener('click', rawWrite);

  // EEPROM read/write — chip selection, serial number, .bin image, Write
  const eepChipSel = document.getElementById('eep-chip');
  eepChipSel?.addEventListener('change', () => applyEepromChip(eepChipSel.value, true));
  // Seed the panel with the defaults of the initially selected chip (24C08).
  if (eepChipSel) applyEepromChip(eepChipSel.value);
  initEepromSerial();
  document.getElementById('btn-eep-read')?.addEventListener('click', () => {
    eepromRead();
    // Arm periodic reads only from an explicit Read click with 烧写空白 ticked.
    if (document.getElementById('eep-blank-program')?.checked) {
      startEepromAutoRead();
    }
  });
  // Unticking 烧写空白 stops the periodic read immediately.
  document.getElementById('eep-blank-program')?.addEventListener('change', e => {
    if (!e.target.checked) stopEepromAutoRead();
  });
  document.getElementById('btn-eep-export')?.addEventListener('click', eepromExport);
  document.getElementById('btn-eep-copy')?.addEventListener('click', eepromCopyHex);
  document.getElementById('btn-eep-clear')?.addEventListener('click', eepromClear);
  document.getElementById('btn-eep-erase')?.addEventListener('click', eepromErase);
  document.getElementById('eep-file')?.addEventListener('change', handleEepromFile);
  document.getElementById('btn-eep-file-refresh')?.addEventListener('click', refreshEepromFile);
  document.getElementById('eep-serial')?.addEventListener('input', updateEepromPreview);
  document.getElementById('btn-eep-sn-next')?.addEventListener('click', nextEepromSerial);
  document.getElementById('btn-eep-write')?.addEventListener('click', eepromWrite);
  // Unticking 确认 while a confirmation is open cancels it; the button title
  // tracks the chosen mode without disabling the button.
  document.getElementById('eep-arm-write')?.addEventListener('change', e => {
    updateEepromWriteButton();
    if (!e.target.checked) {
      const dlg = document.getElementById('eep-confirm-dialog');
      if (dlg?.open && typeof dlg.close === 'function') dlg.close();
    }
  });
  // A backdrop click cancels; Escape is handled natively by <dialog>.
  const eepConfirmDlg = document.getElementById('eep-confirm-dialog');
  eepConfirmDlg?.addEventListener('click', e => {
    if (e.target === eepConfirmDlg && eepConfirmDlg.open &&
        typeof eepConfirmDlg.close === 'function') eepConfirmDlg.close();
  });

  // Set initial UI state
  updateConnectionUI(false, '');
});
