"""HTML document served by the web interface."""

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>CCTV Ingestion</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{color-scheme:light dark}
body{font-family:system-ui,sans-serif;max-width:1080px;margin:20px auto;padding:0 16px;background:#fafafa;color:#111}
@media (prefers-color-scheme:dark){body{background:#161616;color:#eee}input,select{background:#222;color:#eee;border-color:#444}
th,td{border-color:#333!important}}
h1{font-size:1.3rem}
fieldset{border:1px solid #ccc;border-radius:8px;margin-bottom:16px;padding:12px 16px}
legend{font-weight:600;padding:0 6px}
label{display:block;margin:8px 0 2px;font-size:.85rem;opacity:.8}
input,select{padding:6px 8px;border-radius:6px;border:1px solid #ccc;font-size:.9rem;max-width:100%;box-sizing:border-box}
.row{display:flex;gap:16px;flex-wrap:wrap}
.row > div{flex:1;min-width:140px}
.row input,.row select{width:100%}
.hint{font-size:.75rem;opacity:.6;margin-top:2px}
button{padding:8px 18px;border-radius:6px;border:0;background:#2563eb;color:#fff;font-size:.9rem;cursor:pointer}
button.stop{background:#dc2626}
button:disabled{opacity:.5;cursor:not-allowed}
#progressBar{position:relative;height:24px;background:#ddd;border-radius:6px;overflow:hidden;margin:10px 0 6px}
#progressFill{height:100%;background:#16a34a;width:0%;transition:width .5s}
#progressText{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:.8rem;font-weight:600;color:#111;font-variant-numeric:tabular-nums}
#stats{display:flex;flex-wrap:wrap;gap:6px 18px;font-size:.85rem;margin-bottom:10px;font-variant-numeric:tabular-nums}
#stats b{font-weight:600}
@media (prefers-color-scheme:dark){#progressBar{background:#333}#progressText{color:#eee}}
.tablewrap{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:.85rem}
th,td{border:1px solid #ddd;padding:4px 8px;text-align:left;vertical-align:top}
td.files{font-family:monospace;font-size:.75rem;word-break:break-all}
td.num{font-variant-numeric:tabular-nums;white-space:nowrap}
.ok{color:#16a34a;font-weight:600}
.fail{color:#dc2626;font-weight:600}
.cancelled{color:#d97706;font-weight:600}
.pending{opacity:.6}
.auth-warn{color:#ea580c;font-weight:600}
.net-warn{color:#d97706;font-weight:600}
#warningBanner{display:none;background:#fef3c7;color:#92400e;border:1px solid #f59e0b;padding:8px 12px;border-radius:6px;margin:8px 0;font-size:.85rem;font-weight:600}
@media (prefers-color-scheme:dark){#warningBanner{background:#451a03;color:#fde68a;border-color:#b45309}}
.note{display:block;font-weight:400;color:#d97706;font-size:.75rem}
#runMsg{margin-left:10px;font-size:.85rem;color:#dc2626}
button.small{padding:3px 10px;font-size:.8rem}
label.check{display:flex;align-items:center;gap:8px;margin-top:14px;font-size:.9rem;opacity:1;cursor:pointer}
label.check input{width:auto;margin:0}
.chips{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin-top:8px}
button.chip{padding:4px 12px;font-size:.8rem;background:#e5e7eb;color:#111;border-radius:999px}
button.chip.clear{background:transparent;color:inherit;border:1px solid #ccc}
@media (prefers-color-scheme:dark){button.chip{background:#333;color:#eee}button.chip.clear{border-color:#444}}
button.enable{background:#16a34a}
button.disable{background:#6b7280}
h3{font-size:.95rem;margin:16px 0 4px}
input.ipedit{width:9.5em;padding:3px 6px;font-size:.85rem}
.nvrrow{margin-top:6px}
tr.off td{opacity:.45}
tr.off td:last-child{opacity:1}
#log{height:180px;overflow:auto;background:#111;color:#c9c9c9;font:.78rem/1.3 monospace;padding:8px;border-radius:6px}
</style></head>
<body>
<h1>CCTV Footage Ingestion</h1>

<fieldset>
<legend>Config</legend>
<div class="row">
  <div><label>NVR username</label><input id="nvr_user" placeholder="(unchanged if blank)"></div>
  <div><label>NVR password</label><input id="nvr_pass" type="password" placeholder="(unchanged if blank)"></div>
  <div><label>CSV path</label><input id="csv" onchange="loadCameras()"></div>
</div>
<button onclick="saveConfig()">Save credentials</button>
<span id="cfgMsg" style="margin-left:10px;font-size:.85rem"></span>
</fieldset>

<fieldset>
<legend>Cameras</legend>
<div id="camSummary" class="hint" style="margin-bottom:8px"></div>
<div class="tablewrap">
<table id="invTable"><thead><tr><th>Camera</th><th>NVR IP</th><th>Channel</th><th>Camera IP</th><th>Enabled</th><th></th></tr></thead><tbody></tbody></table>
</div>
<div class="hint">Edit a camera's NVR IP in the table and press Enter to move just that camera. Changes are written to the CSV.</div>

<h3>NVR servers</h3>
<div class="hint">Change an NVR's IP to update every camera recorded on it.</div>
<div id="nvrList"></div>

<h3>Add camera</h3>
<div class="row">
  <div><label>NVR IP</label><input id="add_nvr" placeholder="172.168.6.2" list="nvrOptions"></div>
  <div><label>Channel</label><input id="add_channel" placeholder="D5"></div>
  <div><label>Camera IP</label><input id="add_camera_ip" placeholder="172.168.6.15"></div>
  <div><label>Name</label><input id="add_name" placeholder="(defaults to channel)"></div>
</div>
<datalist id="nvrOptions"></datalist>
<div style="margin-top:10px"><button onclick="addCamera()">Add camera</button><span id="invMsg" style="margin-left:10px;font-size:.85rem"></span></div>
</fieldset>

<fieldset>
<legend>Run</legend>
<div class="row">
  <div><label>Mode</label>
    <select id="mode"><option value="both">clip + snapshot</option><option value="clip">clip only</option><option value="snapshot">snapshot only</option></select>
  </div>
  <div><label>Parallel downloads</label><input id="workers" type="number" min="1" max="20">
    <div class="hint">cameras downloading at once (4 ≈ full 1 Gbps link)</div></div>
  <div><label>Downloads per NVR</label><input id="per_nvr" type="number" min="1" max="10">
    <div class="hint">one NVR tops out ~500 Mbps; 2 is best</div></div>
  <div><label>Remux Workers</label><input id="remux_workers" type="number" min="1" max="8">
    <div class="hint">(Max parallel ffmpeg packaging processes)</div></div>
  <div><label>Network timeout (s)</label><input id="timeout" type="number" min="5" max="600"></div>
  <div><label>NVR timezone (hrs)</label><input id="tz_offset_hours" type="number" step="0.5">
    <div class="hint">7 = Thailand; only used for "last N minutes"</div></div>
</div>
<div style="margin:14px 0 8px; display:flex; gap:20px; align-items:center;">
  <label style="margin:0; font-weight:600; cursor:pointer;"><input type="radio" name="time_mode_radio" value="single" id="tm_single" checked onchange="switchTimeMode('single')"> Single continuous window / Last N min</label>
  <label style="margin:0; font-weight:600; cursor:pointer;"><input type="radio" name="time_mode_radio" value="daily" id="tm_daily" onchange="switchTimeMode('daily')"> Daily recurring (แยกวันและเวลา เช่น 10,11,12 เวลา 10:00-22:00)</label>
</div>

<div id="singleWindowBlock">
  <div class="row">
    <div><label>Last N minutes (used if start/end blank)</label><input id="last_minutes" type="number" step="0.5" min="0.5"></div>
    <div><label>Start (NVR local time)</label><input id="start" type="datetime-local"></div>
    <div><label>End (NVR local time)</label><input id="end" type="datetime-local"></div>
  </div>
  <div class="chips">
    <span class="hint">End = Start +</span>
    <button type="button" class="chip" onclick="setDuration(1)">1 min</button>
    <button type="button" class="chip" onclick="setDuration(5)">5 min</button>
    <button type="button" class="chip" onclick="setDuration(15)">15 min</button>
    <button type="button" class="chip" onclick="setDuration(30)">30 min</button>
    <button type="button" class="chip" onclick="setDuration(60)">1 hr</button>
    <button type="button" class="chip clear" onclick="clearWindow()">Clear (use last N minutes)</button>
  </div>
</div>

<div id="dailyWindowBlock" style="display:none">
  <div class="row">
    <div><label>Start Date</label><input id="start_date" type="date"></div>
    <div><label>End Date</label><input id="end_date" type="date"></div>
    <div><label>Daily Start Time</label><input id="daily_start" type="time"></div>
    <div><label>Daily End Time</label><input id="daily_end" type="time"></div>
  </div>
  <div class="chips">
    <span class="hint">Presets:</span>
    <button type="button" class="chip" onclick="setDailyPreset('10:00','22:00')">10:00 - 22:00</button>
    <button type="button" class="chip" onclick="setDailyPreset('08:00','18:00')">08:00 - 18:00</button>
    <button type="button" class="chip" onclick="setDailyPreset('09:00','17:00')">09:00 - 17:00</button>
    <button type="button" class="chip" onclick="setDailyPreset('22:00','04:00')">22:00 - 04:00 (ข้ามคืน)</button>
  </div>
  <div class="hint" style="margin-top:6px;">
    ระบบจะประมวลผลแยกทีละวัน บันทึกลงโฟลเดอร์ของวันนั้นๆ (เช่น output/2026-09-10/, output/2026-09-11/) และข้ามช่วงกลางคืนที่ไม่ต้องการ
  </div>
</div>
<label class="check"><input id="trim" type="checkbox"> Trim &amp; join to the exact time range</label>
<div class="hint" id="trimHint"></div>
<div style="margin-top:12px;display:flex;align-items:center;gap:8px;flex-wrap:wrap">
  <button id="runBtn" onclick="startRun()">Run ingestion</button>
  <button id="stopBtn" class="stop" onclick="stopRun()" disabled>Stop</button>
  <button id="repairBtn" type="button" class="chip" onclick="repairLegacies()">🛠 Scan &amp; Convert Legacy Files</button>
  <span id="runMsg"></span>
</div>
</fieldset>

<fieldset>
<legend>Progress</legend>
<div id="summary">Idle.</div>
<div id="warningBanner"></div>
<div id="progressBar"><div id="progressFill"></div><div id="progressText"></div></div>
<div id="stats"></div>
<div class="tablewrap">
<table id="camTable"><thead><tr><th>Camera</th><th>NVR</th><th>Channel</th><th>Status</th><th>Downloaded</th><th>Output</th></tr></thead><tbody></tbody></table>
</div>
</fieldset>

<fieldset>
<legend>Log</legend>
<div id="log"></div>
</fieldset>

<script>
const FIELDS = ['csv','mode','workers','per_nvr','remux_workers','timeout','tz_offset_hours','last_minutes','start','end',
                'start_date','end_date','daily_start','daily_end'];
const TRIM_HINT = {
  false: 'Off: downloads whole recording segments and packages them into real MP4 (~1 GB per ~70 min per camera). '
       + 'Output: output/<date>/<camera>/<file start>-<file end>_<camera>_<channel>.mp4',
  true:  'On: downloads those files to a temp folder, cuts and joins them into one clip covering exactly the range, then deletes the originals. '
       + 'Output: output/<date>/<camera>/<start>_<camera>_<channel>.mp4',
};
function updateTrimHint(){ document.getElementById('trimHint').textContent = TRIM_HINT[document.getElementById('trim').checked]; }
const STAGE_LABEL = {searching:'searching recordings…', queued:'queued', downloading:'downloading…',
                     remuxing:'packaging mp4…', cutting:'trimming…', snapshot:'snapshot…',
                     auth_wait:'waiting 30 min (login failed, lockout cooldown)…',
                     network_wait:'network down, reconnecting…'};
let busy = false;

function switchTimeMode(mode){
  const isDaily = mode === 'daily';
  const rDaily = document.getElementById('tm_daily');
  const rSingle = document.getElementById('tm_single');
  if (rDaily) rDaily.checked = isDaily;
  if (rSingle) rSingle.checked = !isDaily;
  const sw = document.getElementById('singleWindowBlock');
  const dw = document.getElementById('dailyWindowBlock');
  if (sw) sw.style.display = isDaily ? 'none' : 'block';
  if (dw) dw.style.display = isDaily ? 'block' : 'none';
}

function setDailyPreset(s, e){
  document.getElementById('daily_start').value = s;
  document.getElementById('daily_end').value = e;
}

async function j(url, opts){ const r = await fetch(url, opts); return await r.json(); }
function post(url, body){ return j(url, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body||{})}); }
function esc(s){ return String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

// Picker value is "YYYY-MM-DDTHH:MM[:SS]"; the server expects "YYYY-MM-DD HH:MM:SS".
function pickerToServer(v){ if (!v) return ''; const s = v.replace('T', ' '); return s.length === 16 ? s + ':00' : s; }
function serverToPicker(v){ return v ? v.replace(' ', 'T').slice(0, 16) : ''; }
function pad(n){ return String(n).padStart(2, '0'); }
function dateToPicker(d){ return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`; }

function setDuration(minutes){
  const startEl = document.getElementById('start');
  const endEl = document.getElementById('end');
  if (!startEl.value) {
    // No start picked yet: window ends now, starts N minutes earlier.
    const now = new Date(); now.setSeconds(0, 0);
    endEl.value = dateToPicker(now);
    startEl.value = dateToPicker(new Date(now.getTime() - minutes * 60000));
    return;
  }
  endEl.value = dateToPicker(new Date(new Date(startEl.value).getTime() + minutes * 60000));
}

function clearWindow(){
  document.getElementById('start').value = '';
  document.getElementById('end').value = '';
}

async function loadDefaults(){
  const d = await j('/api/defaults');
  FIELDS.forEach(f => {
    if (d[f] === undefined) return;
    const el = document.getElementById(f);
    if (!el) return;
    el.value = (f === 'start' || f === 'end') ? serverToPicker(d[f]) : d[f];
  });
  document.getElementById('trim').checked = d.trim === true;
  switchTimeMode(d.time_mode || 'single');
  updateTrimHint();
}

let cameras = [];

async function loadCameras(){
  const csv = document.getElementById('csv').value;
  const res = await j('/api/cameras?csv=' + encodeURIComponent(csv));
  const tbody = document.querySelector('#invTable tbody');
  const summary = document.getElementById('camSummary');
  if (!res.ok) { cameras = []; tbody.innerHTML = ''; summary.textContent = res.error; return; }
  cameras = res.cameras;
  const off = cameras.filter(c => c.disabled).length;
  const seen = {}; cameras.forEach(c => seen[c.key] = (seen[c.key] || 0) + 1);
  const dupes = Object.keys(seen).filter(k => seen[k] > 1);
  summary.textContent = `${cameras.length - off} enabled, ${off} disabled — disabled cameras are skipped on the next run`
    + (dupes.length ? ` — WARNING: duplicate NVR/channel in CSV (${dupes.join(', ')}); runs are blocked until fixed` : '');
  tbody.innerHTML = cameras.map((c, i) =>
    `<tr class="${c.disabled ? 'off' : ''}"><td>${esc(c.name)}</td>`
    + `<td><input class="ipedit" value="${esc(c.nvr)}" onchange="setCameraNvr(${i}, this)"></td><td>${esc(c.channel)}</td>`
    + `<td>${esc(c.camera_ip)}</td><td>${c.disabled ? 'no' : 'yes'}</td>`
    + `<td><button class="small ${c.disabled ? 'enable' : 'disable'}" onclick="toggleCamera(${i}, this)">`
    + `${c.disabled ? 'Enable' : 'Disable'}</button></td></tr>`).join('');
  const nvrs = {};
  cameras.forEach(c => nvrs[c.nvr] = (nvrs[c.nvr] || 0) + 1);
  const ips = Object.keys(nvrs).sort();
  document.getElementById('nvrList').innerHTML = ips.map((ip, i) =>
    `<div class="nvrrow"><input class="ipedit" id="nvr_${i}" value="${esc(ip)}" data-old="${esc(ip)}">`
    + ` <button class="small" onclick="renameNvr(${i})">Save</button>`
    + ` <span class="hint">${nvrs[ip]} camera${nvrs[ip] > 1 ? 's' : ''}</span></div>`).join('');
  document.getElementById('nvrOptions').innerHTML = ips.map(ip => `<option value="${esc(ip)}">`).join('');
}

async function editInventory(body){
  body.csv = document.getElementById('csv').value;
  const res = await post('/api/inventory', {...body});
  document.getElementById('invMsg').textContent = '';
  if (!res.ok) alert('Error: ' + res.error);
  await loadCameras();
  return res.ok;
}

function setCameraNvr(i, input){
  const c = cameras[i];
  if (input.value.trim() === c.nvr) return;
  editInventory({action: 'set_camera_nvr', key: c.key, camera_ip: c.camera_ip, nvr: input.value.trim()});
}

function renameNvr(i){
  const input = document.getElementById('nvr_' + i);
  const old = input.dataset.old, nv = input.value.trim();
  if (nv === old) return;
  if (!confirm(`Change NVR ${old} to ${nv} for all its cameras?`)) { input.value = old; return; }
  editInventory({action: 'rename_nvr', old: old, new: nv});
}

async function addCamera(){
  const get = id => document.getElementById(id).value.trim();
  const ok = await editInventory({action: 'add_camera', nvr: get('add_nvr'), channel: get('add_channel'),
                                  camera_ip: get('add_camera_ip'), name: get('add_name')});
  if (ok) {
    ['add_channel', 'add_camera_ip', 'add_name'].forEach(id => document.getElementById(id).value = '');
    document.getElementById('invMsg').textContent = 'Added.';
  }
}

async function toggleCamera(i, btn){
  btn.disabled = true;
  const c = cameras[i];
  const res = await post('/api/cameras/toggle', {key: c.key, disabled: !c.disabled});
  if (!res.ok) alert('Error: ' + res.error);
  await loadCameras();
}

async function saveConfig(){
  const body = {user: document.getElementById('nvr_user').value, password: document.getElementById('nvr_pass').value};
  const res = await post('/api/config', body);
  document.getElementById('cfgMsg').textContent = res.ok ? 'Saved.' : ('Error: ' + res.error);
  document.getElementById('nvr_pass').value = '';
}

async function startRun(){
  const btn = document.getElementById('runBtn');
  const msg = document.getElementById('runMsg');
  btn.disabled = true; busy = true; msg.textContent = '';
  const body = {};
  FIELDS.forEach(f => {
    const el = document.getElementById(f);
    if (el) body[f] = el.value;
  });
  body.start = pickerToServer(body.start);
  body.end = pickerToServer(body.end);
  body.trim = document.getElementById('trim').checked;
  body.time_mode = document.getElementById('tm_daily').checked ? 'daily' : 'single';
  try {
    const res = await post('/api/run', body);
    if (!res.ok) msg.textContent = res.error;
  } finally { busy = false; poll(); }
}

async function stopRun(){
  document.getElementById('stopBtn').disabled = true;
  await post('/api/cancel');
  poll();
}

async function repairLegacies(){
  if (!confirm('Scan output folder for legacy non-MP4 files and convert them to genuine MP4 in-place?')) return;
  const btn = document.getElementById('repairBtn');
  const orig = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Scanning & Converting…';
  try {
    const res = await post('/api/repair-legacies', {});
    if (res.ok) {
      alert(`Legacy scan complete!\nScanned: ${res.stats.total} file(s)\nLegacy found: ${res.stats.legacy}\nConverted to genuine MP4: ${res.stats.repaired}\nFailed: ${res.stats.failed}`);
    } else {
      alert('Error: ' + res.error);
    }
  } catch(e) {
    alert('Request failed: ' + e);
  } finally {
    btn.disabled = false;
    btn.textContent = orig;
    poll();
  }
}

function statusCell(c){
  if (c.ok === true) return ['ok', 'OK'];
  if (c.stage === 'cancelled') return ['cancelled', 'stopped'];
  if (c.stage === 'auth_wait') return ['auth-warn', '⚠️ Auth Lockout (Waiting 30m)'];
  if (c.stage === 'network_wait') return ['net-warn', '⚡ Network Down (Reconnecting…)'];
  if (c.ok === false) return ['fail', 'FAIL' + (c.error ? ': ' + c.error : '')];
  return ['pending', STAGE_LABEL[c.stage] || c.stage];
}

function fmtRate(bps){
  const bits = (bps || 0) * 8;
  return bits >= 1e6 ? (bits / 1e6).toFixed(1) + ' Mbps' : (bits / 1e3).toFixed(0) + ' kbps';
}
function fmtBytes(b){
  b = b || 0;
  if (b >= 1e9) return (b / 1e9).toFixed(2) + ' GB';
  if (b >= 1e6) return (b / 1e6).toFixed(1) + ' MB';
  return (b / 1e3).toFixed(0) + ' KB';
}
function fmtDur(sec){
  sec = Math.max(0, Math.round(sec));
  const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), ss = sec % 60;
  return h ? `${h}:${pad(m)}:${pad(ss)}` : `${m}:${pad(ss)}`;
}

function renderProgress(s){
  const cams = Object.values(s.cameras || {});
  const authCount = cams.filter(c => c.stage === 'auth_wait').length;
  const netCount = cams.filter(c => c.stage === 'network_wait').length;
  const banner = document.getElementById('warningBanner');
  if (banner) {
    if (authCount || netCount) {
      banner.style.display = 'block';
      banner.textContent = `⚠️ Warning: ${authCount ? authCount + ' camera(s) waiting 30m auth cooldown. ' : ''}${netCount ? netCount + ' camera(s) waiting for network reconnect.' : ''}`.trim();
    } else {
      banner.style.display = 'none';
    }
  }
  const pct = Math.round(100 * (s.fraction || 0));
  document.getElementById('progressFill').style.width = pct + '%';
  const text = document.getElementById('progressText');
  const stats = document.getElementById('stats');
  const snapshotOnly = s.mode === 'snapshot';
  if (s.running) {
    let eta;
    if (s.cancelling) eta = 'stopping…';
    else if (s.phase === 'checking_credentials') eta = 'checking NVR credentials…';
    else if (s.phase === 'repairing') eta = 'repairing legacy files…';
    else if (s.phase === 'searching') eta = 'searching recordings…';
    else if (snapshotOnly) eta = `${s.done}/${s.total} cameras`;
    else eta = s.eta_s == null ? 'estimating…' : '~' + fmtDur(s.eta_s) + ' left';
    text.textContent = `${pct}% · ↓ ${fmtRate(s.rate_bps)} · ${eta}`;
    const finishAt = (!s.cancelling && s.eta_s != null)
      ? new Date(Date.now() + s.eta_s * 1000).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'})
      : '—';
    const dayStats = (!snapshotOnly && s.total_windows > 1 && s.window_expected_bytes > 0)
      ? `<span>Day ${s.current_window}/${s.total_windows}: <b>${fmtBytes(s.window_bytes)}</b> of <b>${fmtBytes(s.window_expected_bytes)}</b></span>`
      : '';
    stats.innerHTML = `<span>Speed <b>${fmtRate(s.rate_bps)}</b></span>`
      + (snapshotOnly ? '' : `<span>Downloaded <b>${fmtBytes(s.bytes)}</b> of <b>${s.phase === 'searching' && s.expected_bytes === 0 ? '…' : (s.total_windows > 1 ? '~' : '') + fmtBytes(s.expected_bytes)}</b></span>`)
      + dayStats
      + `<span>Elapsed <b>${fmtDur(s.elapsed_s)}</b></span>`
      + (snapshotOnly ? '' : `<span>Remaining <b>${s.eta_s == null || s.cancelling ? '—' : '~' + fmtDur(s.eta_s)}</b></span>`
      + `<span>Finish at <b>${finishAt}</b></span>`);
  } else if (s.finished_at) {
    text.textContent = `${pct}% · done in ${fmtDur(s.elapsed_s)}`;
    stats.innerHTML = `<span>Average speed <b>${fmtRate(s.avg_bps)}</b></span>`
      + `<span>Downloaded <b>${fmtBytes(s.bytes)}</b></span>`
      + `<span>Took <b>${fmtDur(s.elapsed_s)}</b></span>`;
  } else {
    text.textContent = '';
    stats.innerHTML = '';
  }
}

async function poll(){
  const s = await j('/api/state');
  if (!busy) {
    document.getElementById('runBtn').disabled = s.running;
    const repairBtn = document.getElementById('repairBtn');
    if (repairBtn) repairBtn.disabled = s.running;
  }
  document.getElementById('stopBtn').disabled = !s.running || s.cancelling;
  renderProgress(s);
  const head = s.cancelling ? 'Stopping… ' : s.running ? (s.phase === 'repairing' ? 'Repairing: ' : 'Running: ') : (s.finished_at ? 'Finished: ' : 'Idle. ');
  document.getElementById('summary').textContent =
    head + `${s.done}/${s.total} done, ${s.ok} ok, ${s.fail} fail/stopped`
    + (s.window ? ' — ' + s.window : '') + (s.log_file ? ' — log: output/' + s.log_file : '')
    + (s.error ? ' — ERROR: ' + s.error : '');
  const rows = Object.values(s.cameras).sort((a,b) => (a.name||'').localeCompare(b.name||'') || (a.channel||'').localeCompare(b.channel||''));
  document.querySelector('#camTable tbody').innerHTML = rows.map(c => {
    const [cls, label] = statusCell(c);
    const dl = c.expected ? `${fmtBytes(c.bytes)} / ${fmtBytes(Math.max(c.expected, c.bytes))}` : (c.bytes ? fmtBytes(c.bytes) : '');
    return `<tr><td>${esc(c.name)}</td><td>${esc(c.nvr)}</td><td>${esc(c.channel)}</td>`
      + `<td class="${cls}">${esc(label)}${c.note ? `<span class="note">${esc(c.note)}</span>` : ''}</td>`
      + `<td class="num">${dl}</td>`
      + `<td class="files">${(c.files||[]).map(esc).join('<br>')}</td></tr>`;
  }).join('');
  const logDiv = document.getElementById('log');
  const atBottom = logDiv.scrollTop + logDiv.clientHeight >= logDiv.scrollHeight - 4;
  logDiv.innerHTML = s.log.map(esc).join('<br>');
  if (atBottom) logDiv.scrollTop = logDiv.scrollHeight;
}

document.getElementById('trim').addEventListener('change', updateTrimHint);
loadDefaults().then(loadCameras);
poll();
setInterval(poll, 1000);
</script>
</body></html>
"""
