#!/usr/bin/env python3
"""
Web GUI for cctv_retrieve.py: configure NVR credentials / time window, run
ingestion from NVR storage (ISAPI) and watch live progress, speed and ETA.
Stdlib-only (no Flask available on this host). Binds 0.0.0.0 so any device on
the network can reach it.
"""
import collections
import csv
import ipaddress
import json
import math
import os
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import cctv_retrieve as core

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CSV = os.path.join(BASE_DIR, "camera_n_nvr.csv")
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
UI_STATE_PATH = os.path.join(BASE_DIR, "webgui_state.json")
MODES = ("both", "snapshot", "clip")
UI_DEFAULTS = {"csv": DEFAULT_CSV, "mode": "both", "workers": core.DEFAULT_WORKERS,
               "per_nvr": core.DEFAULT_PER_NVR, "remux_workers": core.DEFAULT_REMUX_WORKERS,
               "timeout": core.DEFAULT_TIMEOUT_S,
               "tz_offset_hours": 7, "last_minutes": 5, "start": "", "end": "", "trim": False,
               "time_mode": "single", "start_date": "", "end_date": "", "daily_start": "10:00", "daily_end": "22:00"}
FINAL_STAGES = ("ok", "fail", "cancelled")
SPEED_WINDOW_S = 5
ETA_WINDOW_S = 20

LOCK = threading.RLock()
CANCEL_EVENT = threading.Event()
REGISTRY = core.ProcRegistry()
ACTIVE_PREFETCHER = None


def fresh_state():
    return {
        "running": False, "cancelling": False, "phase": None,
        "started_at": None, "finished_at": None, "window": None, "mode": None,
        "total": 0, "done": 0, "ok": 0, "fail": 0,
        "current_window": 1, "total_windows": 1,
        "cameras": {},   # key -> {name, nvr, channel, stage, ok, error, note, files, bytes, expected}
        "log": [], "log_file": None, "error": None,
        # Transfer stats, refreshed once a second by stats_loop().
        "bytes": 0, "expected_bytes": 0, "rate_bps": 0, "avg_bps": None,
        "elapsed_s": 0, "eta_s": None, "fraction": 0,
        "window_bytes": 0, "window_expected_bytes": 0,
    }


STATE = fresh_state()
# Per-run bookkeeping the browser doesn't need; guarded by LOCK like STATE.
RUN = {}


def load_ui_defaults():
    d = dict(UI_DEFAULTS)
    if os.path.exists(UI_STATE_PATH):
        try:
            with open(UI_STATE_PATH, encoding="utf-8") as f:
                saved = json.load(f)
            d.update({k: v for k, v in saved.items() if k in UI_DEFAULTS})
        except (OSError, ValueError):
            pass
    # A saved CSV path that no longer exists (moved project, mistyped path) would leave the camera list empty.
    if not os.path.isfile(str(d["csv"])):
        d["csv"] = DEFAULT_CSV
    return d


def save_ui_defaults(d):
    tmp = UI_STATE_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.replace(tmp, UI_STATE_PATH)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise


def log_line(msg):
    with LOCK:
        STATE["log"].append(f"[{datetime.now():%H:%M:%S}] {msg}")
        STATE["log"] = STATE["log"][-300:]


def short_error(err):
    """ffmpeg stderr is long; keep the 'snapshot:'/'clip:'/'remux:' prefix plus its last meaningful line."""
    prefix, sep, body = err.partition(": ")
    if not (sep and prefix in ("snapshot", "clip", "remux")):
        prefix, body = None, err
    lines = [l.strip() for l in body.splitlines() if l.strip()]
    last = lines[-1] if lines else body.strip()
    return f"{prefix}: {last}" if prefix else last


def rel(path):
    return os.path.relpath(path, OUTPUT_DIR)


class GuiCallbacks(core.Callbacks):
    def stage(self, key, stage, _row=None, errors=(), notes=(), files=(), expected_bytes=None):
        with LOCK:
            cam = STATE["cameras"].get(key)
            if cam is None:
                return  # key not tracked in this run (should not happen); ignore silently
            prev_stage = cam.get("stage")
            cam["stage"] = stage
            if errors or stage in FINAL_STAGES:
                cam["error"] = "; ".join(short_error(e) for e in errors) or None
            if notes or stage in FINAL_STAGES:
                cam["note"] = "; ".join(notes) or None
            if files:
                for p in files:
                    rp = rel(p)
                    if rp not in cam["files"]:
                        cam["files"].append(rp)
            if expected_bytes is not None:
                cam["expected"] = expected_bytes
            if stage == "downloading":
                STATE["phase"] = "downloading"
            if prev_stage == "searching" and stage in ("queued", "fail", "cancelled"):
                RUN["searched"] = RUN.get("searched", 0) + 1
                if RUN["searched"] == STATE.get("cams_count", STATE["total"]):
                    if STATE["phase"] == "searching":
                        STATE["phase"] = "downloading"
                    log_line(f"Search done: {sum(c['expected'] for c in STATE['cameras'].values()) / 1e9:.2f} GB "
                             "of recordings to download")
            if stage in FINAL_STAGES:
                STATE["done"] += 1
                cam["ok"] = stage == "ok"
                STATE["ok" if stage == "ok" else "fail"] += 1
                log_line(f"{cam['name']} ({cam['nvr']} {cam['channel']}) -> {stage}"
                         + (f": {cam['error']}" if cam["error"] and stage != "cancelled" else "")
                         + (f" ({cam['note']})" if cam["note"] else ""))

    def bytes(self, key, n):
        with LOCK:
            cam = STATE["cameras"].get(key)
            if cam is None:
                return
            cam["bytes"] += n
            RUN["downloaded"] = RUN.get("downloaded", 0) + n


def update_stats():
    now = time.monotonic()
    samples = RUN.get("samples")
    if samples is None:
        samples = collections.deque()
        RUN["samples"] = samples
    samples.append((now, RUN.get("downloaded", 0)))
    while len(samples) > 1 and now - samples[0][0] > ETA_WINDOW_S:
        samples.popleft()

    def rate_over(window):
        if not samples:
            return 0
        old = next(((t, b) for t, b in samples if now - t <= window), samples[0])
        return (RUN.get("downloaded", 0) - old[1]) / (now - old[0]) if now > old[0] else 0

    cams = STATE["cameras"].values()
    cur_win_expected = sum(max(c["expected"], c["bytes"]) for c in cams if c["stage"] not in ("fail", "cancelled") or c["bytes"])
    cur_win_remaining = sum(max(c["expected"] - c["bytes"], 0) for c in cams if c["stage"] not in FINAL_STAGES)
    cur_win_bytes = sum(c["bytes"] for c in cams)

    total_windows = STATE.get("total_windows", 1)
    idx = STATE.get("current_window", 1)

    if total_windows > 1:
        history = RUN.get("window_expected_history") or []
        known_expected = sum(history) + cur_win_expected
        avg_expected = known_expected / max(idx, 1) if known_expected > 0 else 0
        projected_total_expected = max(round(avg_expected * total_windows), RUN.get("downloaded", 0))
        STATE["expected_bytes"] = projected_total_expected
        STATE["window_bytes"] = cur_win_bytes
        STATE["window_expected_bytes"] = cur_win_expected
        remaining_future = max(0, total_windows - idx) * avg_expected
        total_remaining = cur_win_remaining + remaining_future
    else:
        STATE["expected_bytes"] = max(cur_win_expected, RUN.get("downloaded", 0))
        STATE["window_bytes"] = cur_win_bytes
        STATE["window_expected_bytes"] = cur_win_expected
        total_remaining = cur_win_remaining

    STATE["bytes"] = RUN.get("downloaded", 0)
    STATE["rate_bps"] = rate_over(SPEED_WINDOW_S)
    t0 = RUN.get("t0", now)
    STATE["elapsed_s"] = round(now - t0)

    if STATE.get("mode") == "snapshot":
        STATE["eta_s"] = None
        fraction = STATE["done"] / max(STATE["total"], 1)
    else:
        eta_rate = rate_over(ETA_WINDOW_S)
        waiting = STATE["phase"] == "searching" or STATE.get("cancelling", False)
        STATE["eta_s"] = None if waiting or eta_rate <= 0 else round(total_remaining / eta_rate)
        if total_windows > 1:
            cams_count = STATE.get("cams_count") or len(cams) or 1
            if cur_win_expected:
                win_fraction = cur_win_bytes / cur_win_expected
            else:
                completed_in_window = STATE["done"] - (idx - 1) * cams_count
                win_fraction = max(0.0, completed_in_window / cams_count)
            fraction = ((idx - 1) + min(win_fraction, 1.0)) / total_windows
        else:
            fraction = RUN.get("downloaded", 0) / STATE["expected_bytes"] if STATE["expected_bytes"] else 0
    # Never move backwards (e.g. when a segment turns out larger than the search reported).
    STATE["fraction"] = max(STATE["fraction"], min(fraction, 0.99))


def stats_loop(finished):
    while not finished.wait(1):
        with LOCK:
            # do_run may have finalized the stats while this thread waited for the lock.
            if finished.is_set():
                return
            update_stats()


CSV_FIELDS = ["camera_ip", "nvr", "channel", "name", "manage_port", "online", "remark"]


def _ip(value, label):
    v = (value or "").strip() if isinstance(value, str) else ""
    try:
        ipaddress.ip_address(v)
    except ValueError as e:
        raise ValueError(f"{label} is not a valid IP address: {v!r}") from e
    return v


def read_inventory(csv_path):
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        return list(reader.fieldnames or CSV_FIELDS), list(reader)


def write_inventory(csv_path, fields, rows):
    tmp = csv_path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, csv_path)


def _is_online(row):
    return (row.get("online") or "").strip().upper() == "TRUE"


def edit_inventory(payload):
    """Apply one NVR/camera edit to the inventory CSV. Caller holds LOCK. Returns a log message."""
    csv_path = (payload.get("csv") or "").strip() or DEFAULT_CSV
    action = payload.get("action")
    fields, rows = read_inventory(csv_path)
    for col in ("camera_ip", "nvr", "channel", "name"):
        if col not in fields:
            raise ValueError(f"CSV has no '{col}' column")
    renamed = {}  # old camera key -> new camera key, to carry over enabled/disabled state

    if action == "rename_nvr":
        old, new = _ip(payload.get("old"), "Current NVR IP"), _ip(payload.get("new"), "New NVR IP")
        targets = [r for r in rows if (r.get("nvr") or "").strip() == old]
        if not targets:
            raise ValueError(f"no cameras on NVR {old}")
        for r in targets:
            renamed[core.camera_key(r)] = f"{new}/{r['channel'].strip()}"
            r["nvr"] = new
        msg = f"NVR {old} -> {new} ({len(targets)} cameras)"
    elif action == "set_camera_nvr":
        key, cam_ip = payload.get("key"), (payload.get("camera_ip") or "").strip()
        new = _ip(payload.get("nvr"), "NVR IP")
        targets = [r for r in rows if _is_online(r) and core.camera_key(r) == key]
        if len(targets) != 1:
            raise ValueError("camera not found in CSV (reload the page)")
        r = targets[0]
        if cam_ip:
            r["camera_ip"] = cam_ip
        else:
            cam_ip = (r.get("camera_ip") or "").strip()
        renamed[key] = f"{new}/{r['channel'].strip()}"
        r["nvr"] = new
        msg = f"Camera {r.get('name')} ({cam_ip}) NVR {key.split('/')[0]} -> {new}"
    elif action == "add_camera":
        channel = (payload.get("channel") or "").strip()
        core.track_id_from_channel(channel)  # raises ValueError on a bad code
        r = dict.fromkeys(fields, "")
        r.update({"camera_ip": _ip(payload.get("camera_ip"), "Camera IP"), "nvr": _ip(payload.get("nvr"), "NVR IP"),
                  "channel": channel.upper(), "name": (payload.get("name") or "").strip() or channel.upper()})
        if "manage_port" in fields:
            r["manage_port"] = "8000"
        if "online" in fields:
            r["online"] = "TRUE"
        rows.append(r)
        msg = f"Added camera {r['name']} ({r['camera_ip']}) on NVR {r['nvr']} channel {r['channel']}"
    else:
        raise ValueError("unknown action")

    keys = [core.camera_key(r) for r in rows if _is_online(r)]
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    if dupes:
        raise ValueError("would create duplicate NVR/channel: " + ", ".join(dupes))

    write_inventory(csv_path, fields, rows)
    disabled = core.load_disabled()
    moved = {renamed[k] for k in disabled if k in renamed}
    if moved:
        core.save_disabled((disabled - set(renamed)) | moved)
    return msg


def _param(payload, key, cast, label):
    v = payload.get(key)
    if v is None or (isinstance(v, str) and v.strip() == ""):
        return UI_DEFAULTS[key]
    try:
        n = cast(v)
    except (TypeError, ValueError, OverflowError) as e:
        raise ValueError(f"{label} is not a valid number: {v!r}") from e
    if not math.isfinite(n):
        raise ValueError(f"{label} is not a valid number: {v!r}")
    return n


def plan_run(payload):
    """Validate the request synchronously so bad input is reported before anything starts."""
    mode = payload.get("mode") or "both"
    if mode not in MODES:
        raise ValueError(f"invalid mode: {mode!r}")
    workers = _param(payload, "workers", int, "Parallel downloads")
    if not 1 <= workers <= 20:
        raise ValueError("Parallel downloads must be between 1 and 20")
    per_nvr = _param(payload, "per_nvr", int, "Downloads per NVR")
    if not 1 <= per_nvr <= 10:
        raise ValueError("Downloads per NVR must be between 1 and 10")
    remux_workers = _param(payload, "remux_workers", int, "Remux workers")
    if not 1 <= remux_workers <= 8:
        raise ValueError("Remux workers must be between 1 and 8")
    timeout = _param(payload, "timeout", int, "Network timeout")
    if not 5 <= timeout <= 600:
        raise ValueError("Network timeout must be between 5 and 600 seconds")
    tz = _param(payload, "tz_offset_hours", float, "NVR timezone")
    if not -12 <= tz <= 14:
        raise ValueError("NVR timezone must be between -12 and 14")
    last_minutes = _param(payload, "last_minutes", float, "Last N minutes")
    trim = payload.get("trim") is True
    time_mode = (payload.get("time_mode") or "single").strip()
    start_date = (payload.get("start_date") or "").strip()
    end_date = (payload.get("end_date") or "").strip()
    daily_start = (payload.get("daily_start") or "").strip()
    daily_end = (payload.get("daily_end") or "").strip()
    start = (payload.get("start") or "").strip()
    end = (payload.get("end") or "").strip()
    csv_path = (payload.get("csv") or "").strip() or DEFAULT_CSV

    if time_mode == "daily":
        if not (start_date and end_date and daily_start and daily_end):
            raise ValueError("Start date, end date, daily start time, and daily end time are all required for daily recurring mode")
        windows = core.resolve_daily_windows(start_date, end_date, daily_start, daily_end)
    else:
        start_dt, end_dt = core.resolve_window(start, end, last_minutes, tz)
        windows = [(start_dt, end_dt)]
    try:
        rows = core.load_online_rows(csv_path)
    except OSError as e:
        raise ValueError(f"cannot read CSV: {e}") from e
    if not rows:
        raise ValueError("no online cameras in CSV")
    # Progress and Disable are keyed by NVR/channel, so two rows with the same pair would be merged.
    dupes = sorted(k for k, n in collections.Counter(core.camera_key(r) for r in rows).items() if n > 1)
    if dupes:
        raise ValueError("duplicate NVR/channel rows in CSV: " + ", ".join(dupes))
    disabled = core.load_disabled()
    rows = [r for r in rows if core.camera_key(r) not in disabled]
    if not rows:
        raise ValueError("all cameras are disabled")

    cfg = core.load_config_env(core.CONFIG_ENV_PATH)

    class Args:
        pass
    args = Args()
    args.user = os.environ.get("NVR_USER") or cfg.get("NVR_USER") or "admin"
    args.password = os.environ.get("NVR_PASSWORD") or cfg.get("NVR_PASSWORD") or core.DEFAULT_PASSWORDS
    args.mode = mode
    args.output_dir = OUTPUT_DIR
    args.workers = workers
    args.per_nvr = per_nvr
    args.remux_workers = remux_workers
    args.timeout = timeout
    args.trim = trim

    ui = {"csv": csv_path, "mode": mode, "workers": workers, "per_nvr": per_nvr,
          "remux_workers": remux_workers, "timeout": timeout,
          "tz_offset_hours": tz, "last_minutes": last_minutes, "start": start, "end": end, "trim": trim,
          "time_mode": time_mode, "start_date": start_date, "end_date": end_date,
          "daily_start": daily_start, "daily_end": daily_end}
    return {"rows": rows, "args": args, "windows": windows, "ui": ui}


def do_run(plan):
    global ACTIVE_PREFETCHER
    CANCEL_EVENT.clear()
    finished = threading.Event()
    threading.Thread(target=stats_loop, args=(finished,), daemon=True).start()
    windows = plan["windows"]
    total_windows = len(windows)
    all_results = []

    args = plan.get("args")
    if args is None:
        class DummyArgs:
            pass
        args = DummyArgs()
        args.user = "admin"
        args.password = core.DEFAULT_PASSWORDS
        args.timeout = core.DEFAULT_TIMEOUT_S
        args.workers = core.DEFAULT_WORKERS
        args.per_nvr = core.DEFAULT_PER_NVR
        args.remux_workers = core.DEFAULT_REMUX_WORKERS
        args.mode = "both"
        args.output_dir = OUTPUT_DIR
        args.trim = False

    clients = {}
    for r in plan["rows"]:
        host = r["nvr"].strip()
        clients.setdefault(host, core.NvrClient(host, getattr(args, "user", "admin"),
                                                getattr(args, "password", core.DEFAULT_PASSWORDS),
                                                getattr(args, "timeout", core.DEFAULT_TIMEOUT_S)))

    prefetcher = core.WindowPrefetcher(plan["rows"], clients, args, cancel_event=CANCEL_EVENT)
    ACTIVE_PREFETCHER = prefetcher
    current_prefetched_plans = None

    try:
        for idx, (w_start, w_end) in enumerate(windows, 1):
            if CANCEL_EVENT.is_set():
                break
            with LOCK:
                if total_windows > 1:
                    STATE["window"] = f"[{idx}/{total_windows}] {w_start} -> {w_end} (NVR local time)"
                    log_line(f"=== Starting Day {idx}/{total_windows}: {w_start} -> {w_end} ===")
                else:
                    STATE["window"] = f"{w_start} -> {w_end} (NVR local time)"
                STATE["current_window"] = idx
                STATE["total_windows"] = total_windows

                if idx == 1 or not current_prefetched_plans:
                    STATE["phase"] = "searching"
                    RUN["searched"] = 0
                else:
                    STATE["phase"] = "downloading"
                    RUN["searched"] = len(current_prefetched_plans)

                for row in plan["rows"]:
                    k = core.camera_key(row)
                    if k not in STATE["cameras"]:
                        STATE["cameras"][k] = {
                            "name": row.get("name"), "nvr": row.get("nvr", "").strip(),
                            "channel": row.get("channel", "").strip(),
                            "stage": "searching", "ok": None, "error": None, "note": None,
                            "files": [], "bytes": 0, "expected": 0,
                        }
                    cam = STATE["cameras"][k]
                    if current_prefetched_plans and k in current_prefetched_plans:
                        p = current_prefetched_plans[k]
                        cam["stage"] = "fail" if p.error else "queued"
                        cam["expected"] = p.expected_bytes
                    else:
                        cam["stage"] = "searching"
                        cam["expected"] = 0
                    cam["ok"] = None
                    cam["error"] = None
                    cam["note"] = None
                    cam["bytes"] = 0

            # If there is a next window (idx < total_windows), trigger background prefetch
            if idx < total_windows:
                next_w_start, next_w_end = windows[idx]
                prefetcher.start_prefetch(next_w_start, next_w_end)
                log_line(f"Pre-fetching Day {idx+1}/{total_windows} ({next_w_start.strftime('%Y-%m-%d')})...")

            w_results = core.run_batch(
                plan["rows"],
                args,
                w_start,
                w_end,
                callbacks=GuiCallbacks(),
                cancel_event=CANCEL_EVENT,
                proc_registry=REGISTRY,
                prefetched_plans=current_prefetched_plans,
                clients=clients,
            )
            with LOCK:
                cams = STATE["cameras"].values()
                win_expected = sum(max(c["expected"], c["bytes"]) for c in cams if c["stage"] not in ("fail", "cancelled") or c["bytes"])
                RUN.setdefault("window_expected_history", []).append(win_expected)
                RUN["completed_windows_bytes"] = RUN.get("downloaded", 0)
            if total_windows > 1:
                for r in w_results:
                    r.setdefault("notes", []).insert(0, f"[{w_start.strftime('%Y-%m-%d')}]")
            all_results.extend(w_results)

            if CANCEL_EVENT.is_set():
                break

            # When window K finishes, retrieve pre-fetched plans for next window
            if idx < total_windows:
                current_prefetched_plans = prefetcher.get_prefetched()
                with LOCK:
                    next_idx = idx + 1
                    next_start, next_end = windows[idx]
                    STATE["window"] = f"[{next_idx}/{total_windows}] {next_start} -> {next_end} (NVR local time)"
                    # Transition STATE["window"] without blanking STATE["phase"] to "searching"

        log_path = os.path.join(OUTPUT_DIR, "logs", f"run_{datetime.now():%Y%m%d_%H%M%S}.csv")
        core.write_run_log(all_results, log_path)
        with LOCK:
            STATE["log_file"] = rel(log_path)
            log_line("Run " + ("stopped" if CANCEL_EVENT.is_set() else "finished") + f"; log: {rel(log_path)}")
    except Exception as e:
        with LOCK:
            STATE["error"] = str(e)
            log_line(f"ERROR: {e}")
    finally:
        try:
            prefetcher.cancel()
        except Exception:
            pass
        ACTIVE_PREFETCHER = None
        finished.set()
        with LOCK:
            update_stats()
            STATE["avg_bps"] = STATE["bytes"] / STATE["elapsed_s"] if STATE["elapsed_s"] else None
            STATE["rate_bps"] = 0
            STATE["eta_s"] = None
            STATE["fraction"] = 1 if STATE["done"] == STATE["total"] else STATE["done"] / max(STATE["total"], 1)
            STATE["running"] = False
            STATE["cancelling"] = False
            STATE["phase"] = None
            STATE["finished_at"] = datetime.now().isoformat(timespec="seconds")


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


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # keep stdout clean; state log covers app-level events

    def _send(self, body, content_type, code=200):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(json.dumps(obj).encode(), "application/json", code)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self._send(PAGE.encode(), "text/html; charset=utf-8")
        elif path == "/api/state":
            with LOCK:
                body = json.dumps(STATE).encode()
            self._send(body, "application/json")
        elif path == "/api/defaults":
            self._json(load_ui_defaults())
        elif path == "/api/cameras":
            self._list_cameras(parse_qs(urlparse(self.path).query).get("csv", [""])[0])
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw or b"{}")
            if not isinstance(payload, dict):
                raise ValueError
        except ValueError:
            self._json({"ok": False, "error": "invalid JSON body"}, 400)
            return

        if path == "/api/config":
            self._save_config(payload)
        elif path == "/api/run":
            self._start_run(payload)
        elif path == "/api/cancel":
            self._cancel()
        elif path == "/api/cameras/toggle":
            self._toggle_camera(payload)
        elif path == "/api/inventory":
            self._edit_inventory(payload)
        elif path == "/api/repair-legacies":
            self._repair_legacies(payload)
        else:
            self._json({"error": "not found"}, 404)

    def _repair_legacies(self, payload):
        target_dir = (payload.get("dir") or OUTPUT_DIR).strip()
        global REGISTRY
        with LOCK:
            if STATE["running"]:
                self._json({"ok": False, "error": "Cannot repair files while an ingestion run is in progress"}, 409)
                return
            if not os.path.isdir(target_dir):
                self._json({"ok": False, "error": f"Directory not found: {target_dir}"}, 400)
                return
            STATE["running"] = True
            STATE["cancelling"] = False
            STATE["phase"] = "repairing"
            CANCEL_EVENT.clear()
            REGISTRY = core.ProcRegistry()
        try:
            log_line(f"Scanning for legacy non-MP4 files in {target_dir}...")
            def on_repair(idx, total, path, status):
                if status == "ok":
                    log_line(f"[{idx}/{total}] Repaired legacy file: {os.path.basename(path)}")
                elif status.startswith("failed:"):
                    log_line(f"[{idx}/{total}] Failed to repair {os.path.basename(path)}: {status}")
            stats = core.scan_and_repair_legacies(target_dir, proc_registry=REGISTRY, cancel_event=CANCEL_EVENT, on_file=on_repair)
            log_line(f"Legacy scan complete: {stats['repaired']} file(s) converted to MP4, {stats['failed']} failed.")
            self._json({"ok": True, "stats": stats})
        except core.Cancelled:
            log_line("Legacy file repair stopped by user")
            self._json({"ok": False, "error": "cancelled"})
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 500)
        finally:
            with LOCK:
                STATE["running"] = False
                STATE["cancelling"] = False
                STATE["phase"] = None

    def _list_cameras(self, csv_path):
        try:
            rows = core.load_online_rows(csv_path.strip() or DEFAULT_CSV)
        except OSError as e:
            self._json({"ok": False, "error": f"cannot read CSV: {e}"}, 400)
            return
        try:
            disabled = core.load_disabled()   # pure file I/O — no need to hold LOCK
        except ValueError as e:
            self._json({"ok": False, "error": str(e)}, 500)
            return
        cams = [{"key": core.camera_key(r), "name": r.get("name"), "nvr": r["nvr"].strip(),
                 "channel": r["channel"].strip(), "camera_ip": (r.get("camera_ip") or "").strip(),
                 "disabled": core.camera_key(r) in disabled} for r in rows]
        self._json({"ok": True, "cameras": cams})

    def _toggle_camera(self, payload):
        key = payload.get("key")
        if not isinstance(key, str) or "/" not in key:
            self._json({"ok": False, "error": "invalid camera key"}, 400)
            return
        try:
            with LOCK:
                disabled = core.load_disabled()
                if payload.get("disabled"):
                    disabled.add(key)
                else:
                    disabled.discard(key)
                core.save_disabled(disabled)
                log_line(f"Camera {key} {'disabled' if payload.get('disabled') else 'enabled'}"
                         + (" (applies from the next run)" if STATE["running"] else ""))
        except (ValueError, OSError) as e:
            self._json({"ok": False, "error": str(e)}, 500)
            return
        self._json({"ok": True})

    def _edit_inventory(self, payload):
        try:
            with LOCK:
                if STATE["running"]:
                    self._json({"ok": False, "error": "cannot edit cameras while a run is in progress"}, 409)
                    return
                log_line(edit_inventory(payload))
        except (ValueError, OSError) as e:
            self._json({"ok": False, "error": str(e)}, 400)
            return
        self._json({"ok": True})

    def _save_config(self, payload):
        user = (payload.get("user") or "").strip()
        password = payload.get("password") or ""
        if any(c in user + password for c in "\r\n"):
            self._json({"ok": False, "error": "credentials cannot contain line breaks"}, 400)
            return
        tmp = core.CONFIG_ENV_PATH + ".tmp"
        try:
            cfg = core.load_config_env(core.CONFIG_ENV_PATH)
            if user:
                cfg["NVR_USER"] = user
            if password:
                cfg["NVR_PASSWORD"] = password
            with open(tmp, "w", encoding="utf-8") as f:
                for k, v in cfg.items():
                    f.write(f"{k}={v}\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, core.CONFIG_ENV_PATH)
            self._json({"ok": True})
        except OSError as e:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            self._json({"ok": False, "error": str(e)}, 500)

    def _start_run(self, payload):
        try:
            plan = plan_run(payload)
        except ValueError as e:
            self._json({"ok": False, "error": str(e)}, 400)
            return

        global STATE, REGISTRY
        with LOCK:
            # Check and claim under one lock so two quick clicks can't start two runs.
            if STATE["running"]:
                self._json({"ok": False, "error": "a run is already in progress"}, 409)
                return
            args = plan["args"]
            windows = plan["windows"]
            total_windows = len(windows)
            cams_count = len(plan["rows"])
            STATE = fresh_state()
            if total_windows == 1:
                win_str = f"{windows[0][0]} -> {windows[0][1]} (NVR local time)"
            else:
                win_str = f"{total_windows} day(s): {windows[0][0].date()} to {windows[-1][0].date()} (daily {windows[0][0].strftime('%H:%M:%S')} -> {windows[0][1].strftime('%H:%M:%S')})"
            STATE.update({
                "running": True, "phase": "searching", "mode": args.mode,
                "started_at": datetime.now().isoformat(timespec="seconds"),
                "window": win_str,
                "total": cams_count * total_windows,
                "cams_count": cams_count,
                "current_window": 1,
                "total_windows": total_windows,
            })
            RUN.clear()
            RUN.update({
                "t0": time.monotonic(), "downloaded": 0, "searched": 0, "samples": collections.deque(),
                "completed_windows_bytes": 0, "window_expected_history": [],
            })
            for row in plan["rows"]:
                STATE["cameras"][core.camera_key(row)] = {
                    "name": row.get("name"), "nvr": row["nvr"].strip(), "channel": row["channel"].strip(),
                    "stage": "searching", "ok": None, "error": None, "note": None, "files": [],
                    "bytes": 0, "expected": 0,
                }
            update_stats()
            log_line(f"Starting run: {cams_count} cameras, {total_windows} window(s), mode={args.mode}, "
                     f"parallel={args.workers}, per NVR={args.per_nvr}, remux_workers={args.remux_workers}, "
                     + ("trim & join to exact window" if args.trim else "whole recording files"))
            CANCEL_EVENT.clear()
            REGISTRY = core.ProcRegistry()

        try:
            save_ui_defaults(plan["ui"])
        except OSError:
            pass  # non-fatal — UI defaults will be saved on the next successful run
        threading.Thread(target=do_run, args=(plan,), daemon=True).start()
        self._json({"ok": True})

    def _cancel(self):
        ok, err = _cancel()
        if not ok:
            self._json({"ok": False, "error": err}, 409)
            return
        self._json({"ok": True})


def _cancel():
    global ACTIVE_PREFETCHER
    with LOCK:
        if not STATE["running"]:
            return False, "no run in progress"
        STATE["cancelling"] = True
        log_line("Stop requested: aborting downloads, skipping queued cameras")
        CANCEL_EVENT.set()
        prefetcher = ACTIVE_PREFETCHER
        registry = REGISTRY
    if prefetcher is not None:
        try:
            prefetcher.cancel()
        except Exception:
            pass
    registry.kill_all()
    return True, None


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0", help="bind address (0.0.0.0 = reachable from any device on the network)")
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"CCTV ingestion web GUI on http://{args.host}:{args.port}  (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
