#!/usr/bin/env python3
"""
Web GUI for cctv_retrieve.py: configure NVR credentials / time window, run
ingestion from NVR storage (ISAPI) and watch live progress, speed and ETA.
Stdlib-only (no Flask available on this host). Binds 0.0.0.0 so any device on
the network can reach it.
"""
import collections
import csv
import io
import ipaddress
import json
import math
import os
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .page import PAGE

from .. import core

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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

    prefetcher = None
    ACTIVE_PREFETCHER = None
    current_prefetched_plans = None

    try:
        with LOCK:
            STATE["phase"] = "checking_credentials"
            log_line(f"Checking credentials on {len(clients)} active NVR(s)...")
        core.validate_nvr_credentials(plan["rows"], clients, cancel_event=CANCEL_EVENT)
        prefetcher = core.WindowPrefetcher(plan["rows"], clients, args, cancel_event=CANCEL_EVENT)
        ACTIVE_PREFETCHER = prefetcher
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
            if prefetcher is not None:
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
        elif path == "/api/inventory/export":
            self._export_inventory(parse_qs(urlparse(self.path).query).get("csv", [""])[0])
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
        elif path == "/api/cameras/toggle-all":
            self._toggle_all_cameras(payload)
        elif path == "/api/inventory":
            self._edit_inventory(payload)
        elif path == "/api/inventory/import":
            self._import_inventory(payload)
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

    def _toggle_all_cameras(self, payload):
        keys = payload.get("keys")
        disabled_value = payload.get("disabled")
        if not isinstance(keys, list) or any(not isinstance(key, str) or "/" not in key for key in keys):
            self._json({"ok": False, "error": "invalid camera keys"}, 400)
            return
        if not isinstance(disabled_value, bool):
            self._json({"ok": False, "error": "disabled must be true or false"}, 400)
            return
        try:
            with LOCK:
                disabled = core.load_disabled()
                if disabled_value:
                    disabled.update(keys)
                else:
                    disabled.difference_update(keys)
                core.save_disabled(disabled)
                log_line(f"{len(set(keys))} cameras {'disabled' if disabled_value else 'enabled'}"
                         + (" (applies from the next run)" if STATE["running"] else ""))
        except (ValueError, OSError) as e:
            self._json({"ok": False, "error": str(e)}, 500)
            return
        self._json({"ok": True, "count": len(set(keys))})

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

    def _export_inventory(self, csv_path):
        path = csv_path.strip() or DEFAULT_CSV
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError as e:
            self._json({"ok": False, "error": f"cannot read CSV: {e}"}, 400)
            return
        filename = (os.path.basename(path) or "camera_inventory.csv").replace('"', "").replace("\r", "").replace("\n", "")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _import_inventory(self, payload):
        csv_path = (payload.get("csv") or "").strip() or DEFAULT_CSV
        content = payload.get("content")
        if not isinstance(content, str) or not content.strip():
            self._json({"ok": False, "error": "choose a non-empty CSV file"}, 400)
            return
        try:
            reader = csv.DictReader(io.StringIO(content.lstrip("\ufeff"), newline=""))
            fields = reader.fieldnames or []
            required = ("camera_ip", "nvr", "channel", "name", "online")
            missing = [column for column in required if column not in fields]
            if missing:
                raise ValueError("CSV is missing required columns: " + ", ".join(missing))
            if len(fields) != len(set(fields)):
                raise ValueError("CSV contains duplicate column names")
            rows = list(reader)
            if not rows:
                raise ValueError("CSV contains no camera rows")
            for index, row in enumerate(rows, 2):
                for column in required:
                    if not (row.get(column) or "").strip():
                        raise ValueError(f"row {index}: '{column}' cannot be blank")
                _ip(row["camera_ip"], f"row {index} camera_ip")
                _ip(row["nvr"], f"row {index} nvr")
                try:
                    core.track_id_from_channel(row["channel"].strip())
                except ValueError as e:
                    raise ValueError(f"row {index}: {e}") from e
                if "online" in fields and (row.get("online") or "").strip().upper() not in ("TRUE", "FALSE"):
                    raise ValueError(f"row {index}: online must be TRUE or FALSE")
            online_keys = [core.camera_key(row) for row in rows if _is_online(row)]
            duplicates = sorted(key for key, count in collections.Counter(online_keys).items() if count > 1)
            if duplicates:
                raise ValueError("duplicate NVR/channel in CSV: " + ", ".join(duplicates))
            with LOCK:
                if STATE["running"]:
                    self._json({"ok": False, "error": "cannot import cameras while a run is in progress"}, 409)
                    return
                write_inventory(csv_path, fields, rows)
                log_line(f"Imported {len(rows)} camera rows from CSV into {csv_path}")
        except (ValueError, OSError, csv.Error) as e:
            self._json({"ok": False, "error": str(e)}, 400)
            return
        self._json({"ok": True, "count": len(rows)})

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
