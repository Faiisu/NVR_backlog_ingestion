#!/usr/bin/env python3
"""HTML document served by the web interface."""

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Fieldnote / NVR Ingestion</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#101b1c">
<style>
:root{color-scheme:dark;--ink:#e8eee7;--muted:#96aaa2;--panel:#182526;--panel-2:#1d2d2e;--line:#344646;--ground:#101b1c;--signal:#c7f36a;--signal-ink:#182414;--amber:#f0ad56;--red:#fb766f;--blue:#75d1d4;--mono:ui-monospace,"SFMono-Regular",Menlo,monospace;--sans:Inter,"Avenir Next",Avenir,"Segoe UI",sans-serif}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at 80% -20%,#294141 0,transparent 42%),var(--ground);color:var(--ink);font:15px/1.45 var(--sans)}button,input,select{font:inherit}button{cursor:pointer}.shell{max-width:1440px;margin:auto;padding:28px clamp(16px,3vw,44px) 56px}.masthead{display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid var(--line);padding-bottom:16px;color:var(--muted);font:11px var(--mono);letter-spacing:.11em;text-transform:uppercase}.brand{display:flex;gap:12px;align-items:center;color:var(--ink);font-weight:700}.brandmark{display:grid;place-items:center;width:30px;height:30px;border:1px solid var(--signal);color:var(--signal);font-size:13px}.live-mark{display:inline-flex;align-items:center;gap:8px}.live-mark:before{content:"";width:7px;height:7px;border-radius:50%;background:var(--signal);box-shadow:0 0 12px #c7f36a88}.hero{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:32px;align-items:end;padding:45px 0 34px}.eyebrow{font:11px var(--mono);color:var(--signal);letter-spacing:.15em;text-transform:uppercase}.hero h1{font-size:clamp(38px,6vw,72px);line-height:.98;letter-spacing:-.055em;margin:12px 0 14px;font-weight:650}.hero p{max-width:650px;color:var(--muted);margin:0}.hero-stamp{border-left:1px solid var(--line);padding:4px 0 4px 20px;color:var(--muted);font:11px/1.8 var(--mono);text-transform:uppercase}.hero-stamp b{display:block;color:var(--ink);font-size:16px;font-weight:500}.layout{display:grid;grid-template-columns:minmax(0,1fr) minmax(340px,.68fr);gap:18px}.panel{background:linear-gradient(145deg,#1b2929,#162223);border:1px solid var(--line);padding:22px;min-width:0}.panel-head{display:flex;justify-content:space-between;gap:16px;align-items:flex-start;margin-bottom:20px}.panel-title{margin:0;font-size:18px;font-weight:600;letter-spacing:-.02em}.panel-kicker{margin:4px 0 0;color:var(--muted);font-size:12px}.section-tag{color:var(--signal);font:10px var(--mono);letter-spacing:.1em;text-transform:uppercase}.panel.inventory{grid-row:span 2}.toolbar{display:flex;flex-wrap:wrap;align-items:center;gap:8px}.field{min-width:0}.field label,.field-label{display:block;margin:0 0 6px;color:var(--muted);font:10px var(--mono);letter-spacing:.08em;text-transform:uppercase}.field input,.field select,.ipedit{width:100%;min-height:40px;padding:9px 11px;background:#101a1b;border:1px solid #3b5050;border-radius:2px;color:var(--ink);outline:none}.field input:focus,.field select:focus,.ipedit:focus{border-color:var(--signal);box-shadow:0 0 0 2px #c7f36a22}.field input::placeholder{color:#70827c}.form-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.form-grid.three{grid-template-columns:repeat(3,minmax(0,1fr))}.credentials{display:grid;grid-template-columns:1fr 1fr auto;align-items:end;gap:10px;border-top:1px solid var(--line);margin-top:18px;padding-top:18px}.credentials .field input{min-width:0}.btn{border:1px solid transparent;border-radius:2px;padding:10px 15px;background:var(--signal);color:var(--signal-ink);font-weight:700;font-size:12px;letter-spacing:.02em;transition:filter .15s,transform .15s}.btn:hover{filter:brightness(1.08);transform:translateY(-1px)}.btn:focus-visible{outline:2px solid var(--ink);outline-offset:2px}.btn:disabled{opacity:.42;cursor:not-allowed;transform:none}.btn.secondary{background:#263839;color:var(--ink);border-color:#445758}.btn.ghost{background:transparent;color:var(--ink);border-color:#465859}.btn.stop{background:#4a2728;color:#ffaaa4;border-color:#754240}.btn.small{padding:6px 10px;font-size:10px}.hint{color:var(--muted);font-size:11px}.csv-tools{display:flex;gap:8px;align-items:center}.csv-tools input[type=file]{display:none}.csv-status{min-height:18px;color:var(--signal);font-size:11px;margin-top:8px}.tablewrap{width:100%;overflow:auto;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}table{border-collapse:collapse;width:100%;text-align:left;font-size:12px}th{padding:10px 9px;background:#142021;color:#8fa49c;font:10px var(--mono);letter-spacing:.08em;text-transform:uppercase;white-space:nowrap}td{padding:9px;border-top:1px solid #2a3b3b;vertical-align:middle}td strong{display:block;font-size:12px}td small{display:block;margin-top:2px;color:#82978e;font:10px var(--mono)}td code{color:var(--blue);font:11px var(--mono)}tr.off td{opacity:.48}td .ipedit{min-height:30px;padding:5px 7px;max-width:145px;font:11px var(--mono)}.state-pill{display:inline-block;padding:3px 7px;border:1px solid #4d6650;color:var(--signal);font:9px var(--mono);text-transform:uppercase}.state-off{border-color:#586464;color:#9da9a4}.table-foot{display:flex;justify-content:space-between;gap:12px;padding:10px 0;color:var(--muted);font-size:11px}.nvr-block{margin-top:18px}.subhead{font:10px var(--mono);color:#afc0b8;letter-spacing:.1em;text-transform:uppercase;margin:0 0 8px}.nvr-list{display:flex;flex-wrap:wrap;gap:7px}.nvrrow{display:flex;gap:5px;align-items:center;padding:5px;background:#142021;border:1px solid #2c4040}.nvrrow .hint{padding:0 5px}.add-row{display:grid;grid-template-columns:repeat(4,minmax(0,1fr)) auto;gap:8px;align-items:end;margin-top:15px}.add-row .field input{min-height:36px;padding:7px}.mode-tabs{display:flex;gap:6px;padding:4px;background:#111d1d;border:1px solid var(--line);margin-bottom:15px}.mode-tabs label{flex:1;text-align:center;padding:8px 6px;color:var(--muted);font:10px var(--mono);cursor:pointer}.mode-tabs input{accent-color:var(--signal)}.time-row{display:flex;align-items:center;gap:16px;flex-wrap:wrap;margin:17px 0 12px}.time-row label{display:flex;align-items:center;gap:7px;color:#cbd6cf;font-size:11px;cursor:pointer}.time-row input{accent-color:var(--signal)}.chips{display:flex;align-items:center;flex-wrap:wrap;gap:6px;margin-top:9px}.chip{border:1px solid #435555;background:#223232;color:#cad7ce;border-radius:2px;padding:5px 8px;font:10px var(--mono)}.chip:hover{border-color:var(--signal);color:var(--signal)}.check{display:flex;align-items:center;gap:9px;color:#dce5de;font-size:12px;margin:17px 0}.check input{accent-color:var(--signal)}.run-actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap;border-top:1px solid var(--line);padding-top:16px;margin-top:14px}#runMsg{color:var(--red);font-size:11px}#cfgMsg,#invMsg{font-size:11px;color:var(--signal)}#progressPanel{margin-top:18px}.summary-line{font:12px var(--mono);color:#bdc9c2;margin:0 0 12px;overflow-wrap:anywhere}#warningBanner{display:none;border-left:3px solid var(--amber);background:#3b3021;color:#ffd69b;padding:9px 12px;margin:8px 0;font-size:12px}#progressBar{height:34px;position:relative;background:#111c1c;border:1px solid #394d4c;overflow:hidden}#progressFill{height:100%;width:0;background:repeating-linear-gradient(135deg,#c7f36a,#c7f36a 12px,#b5e65d 12px,#b5e65d 24px);transition:width .45s}#progressText{position:absolute;inset:0;display:grid;place-items:center;color:#102017;font:11px var(--mono);font-weight:700;font-variant-numeric:tabular-nums}#stats{display:flex;flex-wrap:wrap;gap:8px 24px;padding:13px 0;color:var(--muted);font:11px var(--mono);font-variant-numeric:tabular-nums}#stats b{color:var(--ink);font-weight:500}.ok{color:var(--signal);font-weight:600}.fail{color:var(--red);font-weight:600}.cancelled,.auth-warn,.net-warn{color:var(--amber);font-weight:600}.pending{color:var(--muted)}.note{display:block;color:var(--amber);font-size:10px;margin-top:3px}.num{font:11px var(--mono);white-space:nowrap}.files{min-width:200px;color:#8ea39b;font:10px var(--mono);overflow-wrap:anywhere}#log{height:180px;overflow:auto;padding:12px;background:#0c1415;border:1px solid #2a3c3c;color:#a4b6ad;font:10px/1.6 var(--mono);white-space:pre-wrap}.progress-table{margin-top:6px}.progress-table td{padding:8px}.muted-block{color:var(--muted);font-size:11px;margin-top:6px}.divider{height:1px;background:var(--line);margin:16px 0}.daily-hint{margin-top:8px;color:#91a49c;font-size:11px}.single-window,.daily-window{padding-top:2px}.progress-title{display:flex;justify-content:space-between;gap:12px;align-items:center}
.page-nav{display:flex;gap:4px;overflow:auto;border-bottom:1px solid var(--line);margin:-8px 0 20px;padding-bottom:0}.nav-item{display:flex;align-items:center;gap:10px;white-space:nowrap;padding:12px 16px;background:transparent;border:0;border-bottom:2px solid transparent;color:var(--muted);font:12px var(--mono);cursor:pointer}.nav-item span{color:#647871;font-size:9px;letter-spacing:.06em}.nav-item:hover,.nav-item.active{color:var(--ink)}.nav-item.active{border-bottom-color:var(--signal)}.nav-item.active span{color:var(--signal)}.nav-item:focus-visible{outline:2px solid var(--signal);outline-offset:-3px}.app-page{display:none}.app-page.active{display:block}.app-page>.panel{margin-bottom:16px}.bulk-actions{display:flex;justify-content:flex-end;align-items:center;gap:8px;flex-wrap:wrap;margin:4px 0 12px}.bulk-actions .hint{margin-right:auto}
@media(max-width:980px){.panel.inventory{grid-row:auto}.hero{padding-top:34px}.form-grid.three{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:640px){.shell{padding:16px 12px 36px}.masthead{font-size:9px}.hero{display:block;padding:32px 0 24px}.hero-stamp{display:none}.hero h1{font-size:44px}.panel{padding:15px}.credentials{grid-template-columns:1fr}.form-grid,.form-grid.three{grid-template-columns:1fr 1fr}.add-row{grid-template-columns:1fr 1fr}.add-row button{grid-column:span 2}.toolbar{width:100%}.csv-tools{width:100%}.csv-tools .btn{flex:1}.bulk-actions{justify-content:flex-start}.bulk-actions .hint{width:100%}.table-foot{display:block}.table-foot span{display:block;margin:3px 0}.time-row{gap:10px}.panel-head{margin-bottom:14px}.nav-item{padding:11px 12px;font-size:10px}}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{scroll-behavior:auto!important;transition:none!important}}
</style></head>
<body><main class="shell">
<header class="masthead"><div class="brand"><span class="brandmark">NV</span><span>Fieldnote / Retrieval</span></div><span class="live-mark">Local operations console</span></header>
<section class="hero"><div><div class="eyebrow">Camera network · footage operations</div><h1 id="pageTitle">Camera inventory</h1><p id="pageDescription">Manage recorder assignments, camera availability, and the CSV inventory.</p></div><div class="hero-stamp">System / NVR ingest<b>ISAPI · MP4 · Snapshot</b>Time basis / recorder local</div></section>
<nav class="page-nav" aria-label="Main navigation">
<button class="nav-item" data-page="page-retrieve" onclick="showPage('page-retrieve')"><span>RUN</span> Retrieval</button>
<button class="nav-item active" data-page="page-cameras" onclick="showPage('page-cameras')"><span>FLEET</span> Cameras</button>
<button class="nav-item" data-page="page-activity" onclick="showPage('page-activity')"><span>LIVE</span> Activity</button>
<button class="nav-item" data-page="page-settings" onclick="showPage('page-settings')"><span>CFG</span> Settings</button>
</nav>
<section class="app-page active" id="page-cameras">
<section class="panel inventory"><div class="panel-head"><div><div class="section-tag">Inventory / 01</div><h2 class="panel-title">Camera list</h2><p class="panel-kicker">Update recorder assignments and enabled cameras.</p></div><div id="camSummary" class="hint"></div></div>
<div class="toolbar"><div class="field" style="flex:1;min-width:220px"><label for="csv">Inventory CSV path</label><input id="csv" onchange="loadCameras()"></div><div class="csv-tools"><button class="btn secondary" type="button" onclick="exportInventory()">Export CSV ↓</button><button class="btn secondary" type="button" onclick="document.getElementById('csvFile').click()">Import CSV ↑</button><input id="csvFile" type="file" accept=".csv,text/csv" onchange="importInventory(this)"></div></div><div class="bulk-actions"><span class="hint">Apply to every camera in this inventory</span><button class="btn ghost small" type="button" onclick="toggleAllCameras(false)">Enable all</button><button class="btn ghost small" type="button" onclick="toggleAllCameras(true)">Disable all</button></div><div id="csvMsg" class="csv-status" aria-live="polite"></div>
<div class="tablewrap"><table id="invTable"><thead><tr><th>Camera / IP</th><th>NVR address</th><th>Channel</th><th>State</th><th>Action</th></tr></thead><tbody></tbody></table></div><div class="table-foot"><span>Edit NVR address to move one camera; changes save to the selected CSV.</span><span>Disabled cameras are skipped on the next run.</span></div>
<div class="nvr-block"><h3 class="subhead">Recorder addresses</h3><p class="hint">Rename an NVR to update every camera on that recorder.</p><div id="nvrList" class="nvr-list"></div></div>
<div class="divider"></div><h3 class="subhead">Add camera</h3><div class="add-row"><div class="field"><label for="add_nvr">NVR IP</label><input id="add_nvr" placeholder="172.168.6.2" list="nvrOptions"></div><div class="field"><label for="add_channel">Channel</label><input id="add_channel" placeholder="D5"></div><div class="field"><label for="add_camera_ip">Camera IP</label><input id="add_camera_ip" placeholder="172.168.6.15"></div><div class="field"><label for="add_name">Name</label><input id="add_name" placeholder="Optional"></div><button class="btn" onclick="addCamera()">Add</button></div><datalist id="nvrOptions"></datalist><div id="invMsg" class="csv-status" aria-live="polite"></div>
</section>
</section>
<section class="app-page" id="page-retrieve">
<section class="panel"><div class="panel-head"><div><div class="section-tag">Session / 02</div><h2 class="panel-title">Run setup</h2><p class="panel-kicker">Choose output mode and recorder time window.</p></div></div>
<div class="form-grid"><div class="field"><label for="mode">Capture mode</label><select id="mode"><option value="both">Clip + snapshot</option><option value="clip">Clip only</option><option value="snapshot">Snapshot only</option></select></div><div class="field"><label for="last_minutes">Last N minutes</label><input id="last_minutes" type="number" step="0.5" min="0.5"></div></div>
<div class="time-row"><label><input type="radio" name="time_mode_radio" value="single" id="tm_single" checked onchange="switchTimeMode('single')"> One window</label><label><input type="radio" name="time_mode_radio" value="daily" id="tm_daily" onchange="switchTimeMode('daily')"> Repeat by day</label></div>
<div id="singleWindowBlock" class="single-window"><div class="form-grid"><div class="field"><label for="start">Start · recorder local time</label><input id="start" type="datetime-local"></div><div class="field"><label for="end">End · recorder local time</label><input id="end" type="datetime-local"></div></div><div class="chips"><span class="hint">End = start +</span><button type="button" class="chip" onclick="setDuration(1)">1 min</button><button type="button" class="chip" onclick="setDuration(5)">5 min</button><button type="button" class="chip" onclick="setDuration(15)">15 min</button><button type="button" class="chip" onclick="setDuration(30)">30 min</button><button type="button" class="chip" onclick="setDuration(60)">1 hr</button><button type="button" class="chip" onclick="clearWindow()">Clear</button></div></div>
<div id="dailyWindowBlock" class="daily-window" style="display:none"><div class="form-grid"><div class="field"><label for="start_date">Start date</label><input id="start_date" type="date"></div><div class="field"><label for="end_date">End date</label><input id="end_date" type="date"></div><div class="field"><label for="daily_start">Daily start</label><input id="daily_start" type="time"></div><div class="field"><label for="daily_end">Daily end</label><input id="daily_end" type="time"></div></div><div class="chips"><span class="hint">Presets</span><button class="chip" onclick="setDailyPreset('10:00','22:00')">10–22</button><button class="chip" onclick="setDailyPreset('08:00','18:00')">08–18</button><button class="chip" onclick="setDailyPreset('09:00','17:00')">09–17</button><button class="chip" onclick="setDailyPreset('22:00','04:00')">22–04 · overnight</button></div><p class="daily-hint">Each date is processed separately and saved in that date’s output folder.</p></div>
<label class="check"><input id="trim" type="checkbox"> Trim and join to the exact time range</label><div id="trimHint" class="hint"></div><div class="run-actions"><button id="runBtn" class="btn" onclick="startRun()">Start retrieval</button><button id="stopBtn" class="btn stop" onclick="stopRun()" disabled>Stop run</button><button id="repairBtn" class="btn ghost" type="button" onclick="repairLegacies()">Scan &amp; convert legacy files</button><span id="runMsg" role="status"></span></div>
</section>
</section>
<section class="app-page" id="page-settings">
<section class="panel"><div class="panel-head"><div><div class="section-tag">Settings / 04</div><h2 class="panel-title">Transfer and recorder settings</h2><p class="panel-kicker">Tune transfer limits and update NVR credentials.</p></div></div><div class="form-grid three"><div class="field"><label for="workers">Parallel downloads</label><input id="workers" type="number" min="1" max="20"><div class="hint">Four usually fills a 1 Gbps link.</div></div><div class="field"><label for="per_nvr">Downloads per NVR</label><input id="per_nvr" type="number" min="1" max="10"></div><div class="field"><label for="remux_workers">Remux workers</label><input id="remux_workers" type="number" min="1" max="8"></div><div class="field"><label for="timeout">Network timeout · seconds</label><input id="timeout" type="number" min="5" max="600"></div><div class="field"><label for="tz_offset_hours">NVR timezone · hours</label><input id="tz_offset_hours" type="number" step="0.5"><div class="hint">Used for “last N minutes”.</div></div></div><div class="credentials"><div class="field"><label for="nvr_user">NVR username</label><input id="nvr_user" autocomplete="username" placeholder="Leave blank to keep current"></div><div class="field"><label for="nvr_pass">NVR password</label><input id="nvr_pass" type="password" autocomplete="new-password" placeholder="Leave blank to keep current"></div><button class="btn secondary" onclick="saveConfig()">Save credentials</button></div><div id="cfgMsg" aria-live="polite"></div></section>
</section>
<section class="app-page" id="page-activity">
<section class="panel" id="progressPanel"><div class="progress-title"><div><div class="section-tag">Transfer / 03</div><h2 class="panel-title">Live progress</h2></div><div class="hint" id="summary">Idle.</div></div><div id="warningBanner"></div><div id="progressBar"><div id="progressFill"></div><div id="progressText"></div></div><div id="stats"></div><div class="tablewrap progress-table"><table id="camTable"><thead><tr><th>Camera</th><th>NVR</th><th>Channel</th><th>Status</th><th>Downloaded</th><th>Output files</th></tr></thead><tbody></tbody></table></div></section>
<section class="panel" style="margin-top:18px"><div class="panel-head"><div><div class="section-tag">Events / 04</div><h2 class="panel-title">Run log</h2><p class="panel-kicker">Recent system and camera messages.</p></div></div><div id="log" aria-live="polite"></div></section>
</section>
</main>
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

function showPage(pageId){
  document.querySelectorAll('.app-page').forEach(page => page.classList.toggle('active', page.id === pageId));
  document.querySelectorAll('.nav-item').forEach(item => {
    const active = item.dataset.page === pageId;
    item.classList.toggle('active', active);
    item.setAttribute('aria-current', active ? 'page' : 'false');
  });
  const copy = {
    'page-retrieve': ['Retrieval desk', 'Choose a capture mode and recorder time window, then start or stop a retrieval run.'],
    'page-cameras': ['Camera inventory', 'Manage recorder assignments, camera availability, and the CSV inventory.'],
    'page-activity': ['Transfer activity', 'Follow camera transfers, check completion status, and review recent run messages.'],
    'page-settings': ['System settings', 'Tune transfer limits and update the credentials used to connect to recorders.'],
  }[pageId];
  if (copy) {
    document.getElementById('pageTitle').textContent = copy[0];
    document.getElementById('pageDescription').textContent = copy[1];
  }
}

async function loadCameras(){
  const csv = document.getElementById('csv').value;
  const res = await j('/api/cameras?csv=' + encodeURIComponent(csv));
  const tbody = document.querySelector('#invTable tbody');
  const summary = document.getElementById('camSummary');
  if (!res.ok) { cameras = []; tbody.innerHTML = ''; summary.textContent = res.error; return; }
  cameras = res.cameras.sort((a, b) => a.camera_ip.localeCompare(b.camera_ip, undefined, {numeric: true})
    || a.name.localeCompare(b.name, undefined, {numeric: true}));
  const off = cameras.filter(c => c.disabled).length;
  const seen = {}; cameras.forEach(c => seen[c.key] = (seen[c.key] || 0) + 1);
  const dupes = Object.keys(seen).filter(k => seen[k] > 1);
  summary.textContent = `${cameras.length - off} enabled, ${off} disabled — disabled cameras are skipped on the next run`
    + (dupes.length ? ` — WARNING: duplicate NVR/channel in CSV (${dupes.join(', ')}); runs are blocked until fixed` : '');
  tbody.innerHTML = cameras.map((c, i) =>
    `<tr class="${c.disabled ? 'off' : ''}"><td><strong>${esc(c.name)}</strong><small>${esc(c.camera_ip)}</small></td>`
    + `<td><input class="ipedit" value="${esc(c.nvr)}" onchange="setCameraNvr(${i}, this)"></td><td><code>${esc(c.channel)}</code></td>`
    + `<td><span class="state-pill ${c.disabled ? 'state-off' : 'state-on'}">${c.disabled ? 'Disabled' : 'Enabled'}</span></td>`
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

async function toggleAllCameras(disabled){
  if (!cameras.length) return;
  const action = disabled ? 'disable' : 'enable';
  if (!confirm(`${action === 'disable' ? 'Disable' : 'Enable'} all ${cameras.length} cameras in this inventory?`)) return;
  const buttons = document.querySelectorAll('.bulk-actions button');
  buttons.forEach(button => button.disabled = true);
  const status = document.getElementById('csvMsg');
  status.textContent = `${action === 'disable' ? 'Disabling' : 'Enabling'} cameras…`;
  try {
    const res = await post('/api/cameras/toggle-all', {keys: cameras.map(camera => camera.key), disabled});
    if (!res.ok) throw new Error(res.error || 'Could not update camera states');
    status.textContent = `${res.count} cameras ${action}d.`;
    await loadCameras();
  } catch (error) {
    status.textContent = `Could not ${action} cameras: ${error.message}`;
  } finally {
    buttons.forEach(button => button.disabled = false);
  }
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

async function importInventory(input){
  const file = input.files && input.files[0];
  if (!file) return;
  if (!confirm(`Replace the camera list in ${document.getElementById('csv').value} with ${file.name}?`)) { input.value = ''; return; }
  const status = document.getElementById('csvMsg');
  status.textContent = 'Reading CSV…';
  try {
    const content = await file.text();
    const res = await post('/api/inventory/import', {csv: document.getElementById('csv').value, content});
    if (!res.ok) throw new Error(res.error || 'Import failed');
    status.textContent = `Imported ${res.count} camera rows.`;
    await loadCameras();
  } catch (e) {
    status.textContent = `Import failed: ${e.message}`;
  } finally { input.value = ''; }
}

function exportInventory(){
  const csv = document.getElementById('csv').value;
  window.location.href = '/api/inventory/export?csv=' + encodeURIComponent(csv);
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
