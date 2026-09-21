#!/usr/bin/env python3
"""
Download recorded CCTV footage from Hikvision NVR storage over ISAPI.

For each camera: search the NVR's recordings for the requested window
(/ISAPI/ContentMgmt/search) and download every overlapping recording segment at
full network speed (/ISAPI/ContentMgmt/download), remuxed into real MP4 containers
(ffmpeg stream copy, no re-encode). With --trim the segments are instead cut and joined
into one clip covering exactly the window (ffmpeg stream copy, no re-encode).

Times sent to the NVR are its own local clock time. These NVRs label local time
with a 'Z' suffix (verified 2026-09-16 against the on-screen clock), so requested
local times are passed through unchanged.

Output layout:
  <output_dir>/<YYYY-MM-DD>/<camera>/<seg start>-<seg end>_<camera>_<channel>.mp4   whole NVR segments (default)
  <output_dir>/<YYYY-MM-DD>/<camera>/<YYYYMMDD_HHMMSS>_<camera>_<channel>.mp4       trimmed clip (--trim)
  <output_dir>/<YYYY-MM-DD>/<camera>/<YYYYMMDD_HHMMSS>_<camera>_<channel>.jpg       snapshot at window start
"""
import argparse
import collections
import concurrent.futures
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape as xml_escape

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_ENV_PATH = os.path.join(BASE_DIR, "config.env")
DISABLED_PATH = os.path.join(BASE_DIR, "disabled_cameras.json")

HTTP_PORT = 80
RTSP_PORT = 554
DEFAULT_STREAM_SUFFIX = 1  # main stream sub-index appended to channel number
DEFAULT_WORKERS = 4        # measured: 4 parallel downloads from different NVRs saturate the ~1 Gbps link
DEFAULT_PER_NVR = 2        # measured: one NVR tops out around 500 Mbps total
DEFAULT_TIMEOUT_S = 30
DOWNLOAD_CHUNK = 1 << 20
ISAPI_NS = "{http://www.isapi.org/ver20/XMLSchema}"
SEGMENTS_DIRNAME = ".segments"
STALE_SEGMENTS_S = 6 * 3600


# ---------------------------------------------------------------- config / inventory

def load_config_env(path):
    cfg = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    return cfg


def load_online_rows(csv_path):
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        return [r for r in csv.DictReader(f) if (r.get("online") or "").strip().upper() == "TRUE"]


def load_disabled(path=DISABLED_PATH):
    """Camera keys ('<nvr>/<channel>') switched off by the user; kept apart from the inventory CSV."""
    try:
        with open(path, encoding="utf-8") as f:
            keys = json.load(f)
    except FileNotFoundError:
        return set()
    except ValueError as e:
        raise ValueError(f"{os.path.basename(path)} is not valid JSON; fix or delete it") from e
    return {k for k in keys if isinstance(k, str)}


def save_disabled(keys, path=DISABLED_PATH):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sorted(keys), f, indent=1)
    os.replace(tmp, path)


def camera_key(row):
    return f"{(row.get('nvr') or '').strip()}/{(row.get('channel') or '').strip()}"


def track_id_from_channel(channel):
    """Map inventory channel codes like 'D5', 'D35' to NVR track IDs (501, 3501, ...)."""
    m = re.match(r"^[A-Za-z]*(\d+)$", channel.strip())
    if not m:
        raise ValueError(f"Cannot parse channel code: {channel!r}")
    return int(m.group(1)) * 100 + DEFAULT_STREAM_SUFFIX


def safe_name(s):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s.strip())


# ---------------------------------------------------------------- time window (NVR local clock)

def nvr_time(dt):
    """NVR-local naive datetime -> ISAPI string. The 'Z' is how these NVRs label their local clock."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_nvr_time(s):
    clean = s.strip().rstrip("Z").split(".")[0]
    return datetime.strptime(clean, "%Y-%m-%dT%H:%M:%S")


def resolve_window(start, end, last_minutes, tz_offset_hours):
    """Return (start, end) as naive datetimes on the NVR's local clock. Raises ValueError on bad input.

    start/end are already local ('YYYY-MM-DD HH:MM:SS'); tz_offset_hours is only needed to turn
    'now' into NVR-local time for the last-N-minutes mode.
    """
    if bool(start) != bool(end):
        raise ValueError("start and end must be given together (or both left blank to use last N minutes)")
    if start:
        try:
            start_dt = datetime.strptime(start, "%Y-%m-%d %H:%M:%S")
            end_dt = datetime.strptime(end, "%Y-%m-%d %H:%M:%S")
        except ValueError as e:
            raise ValueError("start/end must be in format YYYY-MM-DD HH:MM:SS") from e
    else:
        if not 0 < last_minutes <= 7 * 24 * 60:
            raise ValueError("last minutes must be greater than 0 and at most 7 days")
        end_dt = (datetime.now(timezone.utc) + timedelta(hours=tz_offset_hours)).replace(tzinfo=None, microsecond=0)
        start_dt = end_dt - timedelta(minutes=last_minutes)
    if end_dt <= start_dt:
        raise ValueError("end must be after start")
    return start_dt, end_dt


def parse_time_str(s):
    if isinstance(s, dtime):
        return s
    s = str(s).strip()
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(s, fmt).time()
        except ValueError:
            pass
    raise ValueError(f"Invalid time format: {s!r}, expected HH:MM or HH:MM:SS")


def parse_date_str(s):
    if isinstance(s, date):
        return s
    return datetime.strptime(str(s).strip(), "%Y-%m-%d").date()


def resolve_daily_windows(start_date_str, end_date_str, start_time_str, end_time_str):
    """Generate a list of (start_dt, end_dt) for each day in [start_date, end_date].
    Supports both same-day windows (e.g. 10:00 to 22:00) and overnight windows (e.g. 22:00 to 04:00).
    """
    d_start = parse_date_str(start_date_str)
    d_end = parse_date_str(end_date_str)
    if d_end < d_start:
        raise ValueError("end_date must be on or after start_date")
    if (d_end - d_start).days > 90:
        raise ValueError("date range cannot exceed 90 days")
    t_start = parse_time_str(start_time_str)
    t_end = parse_time_str(end_time_str)

    windows = []
    curr = d_start
    overnight = t_start >= t_end
    while curr <= d_end:
        w_start = datetime.combine(curr, t_start)
        w_end = datetime.combine(curr + timedelta(days=1 if overnight else 0), t_end)
        windows.append((w_start, w_end))
        curr += timedelta(days=1)
    return windows


def resolve_windows(start=None, end=None, last_minutes=5, tz_offset_hours=7,
                    start_date=None, end_date=None, daily_start=None, daily_end=None):
    """Return list of (start_dt, end_dt) tuples on the NVR local clock.
    If daily parameters are given, returns a window per day. Otherwise returns [(start, end)].
    """
    if start_date or end_date or daily_start or daily_end:
        if not (start_date and end_date and daily_start and daily_end):
            raise ValueError("start-date, end-date, daily-start, and daily-end must all be provided together")
        return resolve_daily_windows(start_date, end_date, daily_start, daily_end)
    s, e = resolve_window(start, end, last_minutes, tz_offset_hours)
    return [(s, e)]


def output_base(row, start_dt, output_dir):
    """<output_dir>/<YYYY-MM-DD>/<camera>/<YYYYMMDD_HHMMSS>_<camera>_<channel> (NVR local time, no extension)."""
    cam = safe_name(row.get("name") or "cam")
    folder = os.path.join(output_dir, start_dt.strftime("%Y-%m-%d"), cam)
    return os.path.join(folder, f"{start_dt:%Y%m%d_%H%M%S}_{cam}_{safe_name(row['channel'])}")


def raw_segment_path(row, segment, output_dir):
    """<output_dir>/<YYYY-MM-DD>/<camera>/<segment start>-<segment end>_<camera>_<channel>.mp4

    The segment is remuxed from the NVR's download format into a standard MP4 container
    with ffmpeg stream copy (no re-encode); it plays in Windows Media Player, VLC, and browsers.
    """
    cam = safe_name(row.get("name") or "cam")
    folder = os.path.join(output_dir, segment.start.strftime("%Y-%m-%d"), cam)
    return os.path.join(folder, f"{segment.start:%Y%m%d_%H%M%S}-{segment.end:%Y%m%d_%H%M%S}"
                                f"_{cam}_{safe_name(row['channel'])}.mp4")


# ---------------------------------------------------------------- cancellation / ffmpeg

class Cancelled(Exception):
    pass


class ProcRegistry:
    """Tracks live ffmpeg subprocesses so a run can be cancelled mid-flight."""

    def __init__(self):
        self._procs = set()
        self._lock = threading.Lock()

    def register(self, proc):
        with self._lock:
            self._procs.add(proc)

    def unregister(self, proc):
        with self._lock:
            self._procs.discard(proc)

    def kill_all(self):
        with self._lock:
            procs = list(self._procs)
        for p in procs:
            try:
                p.kill()
            except Exception:
                pass


_URL_CREDENTIALS = re.compile(r"(rtsp://)[^/@\s'\"]+@")


def redact(text):
    """ffmpeg echoes input URLs in its errors; strip user:password before it reaches logs or the GUI."""
    return _URL_CREDENTIALS.sub(r"\1***@", text)


def check_cancel(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise Cancelled()


def run_ffmpeg(cmd, timeout, proc_registry=None, cancel_event=None):
    check_cancel(cancel_event)
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        return False, "ffmpeg binary not found on PATH"
    if proc_registry is not None:
        proc_registry.register(proc)
    # Cancel may have fired between the check above and register(); kill_all() would have missed this proc.
    if cancel_event is not None and cancel_event.is_set():
        proc.kill()
    try:
        _, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return False, f"ffmpeg timed out after {timeout}s"
    finally:
        if proc_registry is not None:
            proc_registry.unregister(proc)
    if proc.returncode != 0:
        check_cancel(cancel_event)
        return False, redact(err.decode(errors="replace"))[-2000:]
    return True, ""


# ---------------------------------------------------------------- ISAPI client

@dataclass
class Segment:
    start: datetime
    end: datetime
    uri: str
    name: str
    size: int


class IsapiError(Exception):
    pass


class NvrClient:
    def __init__(self, host, user, password, timeout=DEFAULT_TIMEOUT_S, port=HTTP_PORT):
        self.base = f"http://{host}:{port}"
        self.host = host
        self.user = user
        self.password = password
        self.timeout = timeout

    def _open(self, path, body):
        # A fresh opener per request: urllib's digest handler keeps a retry counter that is not thread-safe.
        mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        mgr.add_password(None, self.base + "/", self.user, self.password)
        opener = urllib.request.build_opener(urllib.request.HTTPDigestAuthHandler(mgr))
        req = urllib.request.Request(self.base + path, body.encode(), {"Content-Type": "application/xml"})
        try:
            return opener.open(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise IsapiError(f"NVR {self.host} rejected the username/password (HTTP 401)") from e
            raise IsapiError(f"NVR {self.host} {path} returned HTTP {e.code}") from e
        except (urllib.error.URLError, OSError) as e:
            raise IsapiError(f"cannot reach NVR {self.host}: {getattr(e, 'reason', e)}") from e

    def search(self, track_id, start, end):
        """Recording segments on `track_id` overlapping [start, end), oldest first."""
        search_id = str(uuid.uuid4()).upper()
        segments, position = {}, 0
        while True:
            body = (
                '<?xml version="1.0" encoding="UTF-8"?><CMSearchDescription>'
                f"<searchID>{search_id}</searchID>"
                f"<trackList><trackID>{track_id}</trackID></trackList>"
                f"<timeSpanList><timeSpan><startTime>{nvr_time(start)}</startTime>"
                f"<endTime>{nvr_time(end)}</endTime></timeSpan></timeSpanList>"
                f"<maxResults>50</maxResults><searchResultPosition>{position}</searchResultPosition>"
                "<metadataList><metadataDescriptor>//recordType.meta.std-cgi.com</metadataDescriptor></metadataList>"
                "</CMSearchDescription>"
            )
            with self._open("/ISAPI/ContentMgmt/search", body) as resp:
                root = ET.fromstring(resp.read())
            status = (root.findtext(ISAPI_NS + "responseStatusStrg") or "").upper()
            items = list(root.iter(ISAPI_NS + "searchMatchItem"))
            for item in items:
                uri = item.findtext(f"{ISAPI_NS}mediaSegmentDescriptor/{ISAPI_NS}playbackURI")
                if not uri:
                    continue
                size = re.search(r"[?&]size=(\d+)", uri)
                name = re.search(r"[?&]name=([^&]+)", uri)
                t_start = item.findtext(f"{ISAPI_NS}timeSpan/{ISAPI_NS}startTime")
                t_end = item.findtext(f"{ISAPI_NS}timeSpan/{ISAPI_NS}endTime")
                if not t_start or not t_end:
                    continue  # malformed item; skip rather than crash
                seg = Segment(
                    start=parse_nvr_time(t_start),
                    end=parse_nvr_time(t_end),
                    uri=uri, name=name.group(1) if name else uri, size=int(size.group(1)) if size else 0,
                )
                if seg.end > start and seg.start < end:
                    segments[seg.name] = seg
            if status != "MORE" or not items:
                break
            position += len(items)
        return sorted(segments.values(), key=lambda s: s.start)

    def download(self, segment, dest, on_bytes=None, on_length=None, cancel_event=None):
        """Stream one whole recording segment to `dest` (the NVR ignores sub-ranges and HTTP Range)."""
        body = ('<?xml version="1.0" encoding="UTF-8"?>'
                '<downloadRequest version="1.0" xmlns="http://www.isapi.org/ver20/XMLSchema">'
                f"<playbackURI>{xml_escape(segment.uri)}</playbackURI></downloadRequest>")
        check_cancel(cancel_event)
        tmp = dest + ".part"
        try:
            with self._open("/ISAPI/ContentMgmt/download", body) as resp, open(tmp, "wb") as f:
                length = resp.headers.get("Content-Length")
                if on_length and length and length.isdigit():
                    on_length(int(length))
                while True:
                    check_cancel(cancel_event)
                    try:
                        chunk = resp.read(DOWNLOAD_CHUNK)
                    except (OSError, TimeoutError) as e:
                        raise IsapiError(f"download from {self.host} interrupted: {e}") from e
                    if not chunk:
                        break
                    f.write(chunk)
                    if on_bytes:
                        on_bytes(len(chunk))
            os.replace(tmp, dest)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass


# ---------------------------------------------------------------- per-camera work

@dataclass
class CameraPlan:
    row: dict
    key: str
    track_id: int = 0
    segments: list = None
    error: str = None
    existing: set = None      # names of segments already saved in output folder as real MP4 (trim off)
    legacy: set = None        # names of segments saved on disk as legacy non-MP4 (trim off)
    clip_exists: bool = False  # trimmed clip for this window already in output folder as real MP4 (trim on)
    legacy_clip: bool = False # trimmed clip exists on disk but is legacy non-MP4 (trim on)

    @property
    def expected_bytes(self):
        if self.clip_exists or self.legacy_clip:
            return 0
        already_on_disk = (self.existing or set()) | (self.legacy or set())
        return sum(s.size for s in self.segments or [] if s.name not in already_on_disk)


def plan_camera(row, client, start, end, mode):
    plan = CameraPlan(row=row, key=camera_key(row))
    try:
        plan.track_id = track_id_from_channel(row["channel"])
    except ValueError as e:
        plan.error = str(e)
        return plan
    if mode == "snapshot":
        plan.segments = []
        return plan
    try:
        plan.segments = client.search(plan.track_id, start, end)
    except (IsapiError, ET.ParseError, ValueError) as e:
        plan.error = f"search: {e}"
        return plan
    if not plan.segments:
        plan.error = f"no recording on NVR for track {plan.track_id} in {nvr_time(start)} - {nvr_time(end)}"
    return plan


def covered_seconds(plan, start, end):
    intervals = []
    for s in (plan.segments or []):
        s_clamped, e_clamped = max(start, s.start), min(end, s.end)
        if e_clamped > s_clamped:
            intervals.append((s_clamped, e_clamped))
    if not intervals:
        return 0.0
    intervals.sort()
    merged = [intervals[0]]
    for cur_s, cur_e in intervals[1:]:
        prev_s, prev_e = merged[-1]
        if cur_s <= prev_e:
            merged[-1] = (prev_s, max(prev_e, cur_e))
        else:
            merged.append((cur_s, cur_e))
    return sum((e - s).total_seconds() for s, e in merged)


def file_done(path):
    # Outputs are written under a temp name and renamed when complete, so a non-empty final file is finished.
    return os.path.isfile(path) and os.path.getsize(path) > 0


def is_real_mp4(path):
    """Check whether `path` is a genuine MP4 container (ISO BMFF).
    Legacy files downloaded directly from Hikvision NVR are MPEG-PS streams named .mp4;
    they start with IMKH/HKMI or MPEG-PS pack headers (00 00 01 BA) instead of an 'ftyp' or 'moov' box.
    """
    if not os.path.isfile(path) or os.path.getsize(path) < 16:
        return False
    try:
        with open(path, "rb") as f:
            header = f.read(16)
        return len(header) >= 8 and header[4:8] in (b"ftyp", b"moov")
    except OSError:
        return False


def media_duration(path):
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                              "-of", "default=nw=1:nk=1", path], capture_output=True, text=True, timeout=30)
        return float(out.stdout.strip())
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def mark_existing(plan, args, start, end):
    """Record which outputs of this plan are already in the output folder, so they aren't downloaded again.
    Also inspects existing files: genuine MP4 files are skipped, while legacy non-MP4 files (e.g. from
    older runs) are flagged so they can be converted in-place without re-downloading from NVR.
    """
    if plan.error or not plan.segments:
        return
    if args.trim:
        # Trimmed clips are named by window start only, so also check the length matches this window
        # (a clip can start up to one keyframe interval early).
        clip = output_base(plan.row, start, args.output_dir) + ".mp4"
        if file_done(clip):
            dur, covered = media_duration(clip), covered_seconds(plan, start, end)
            if dur is not None and covered - 2 <= dur <= covered + 10:
                if is_real_mp4(clip):
                    plan.clip_exists = True
                else:
                    plan.legacy_clip = True
        if plan.clip_exists or plan.legacy_clip:
            return

    plan.existing = set()
    plan.legacy = set()
    for s in plan.segments:
        p = raw_segment_path(plan.row, s, args.output_dir)
        if file_done(p):
            if is_real_mp4(p):
                plan.existing.add(s.name)
            else:
                plan.legacy.add(s.name)


def trim_parts(plan, start, end, seg_paths):
    """(segment file, offset into it, duration) for each piece of the window covered by a recording.
    Deduplicates overlapping segments so footage is not repeated in the trimmed clip.
    """
    parts = []
    last_end = start
    for seg in (plan.segments or []):
        s = max(start, seg.start, last_end)
        e = min(end, seg.end)
        if e > s:
            parts.append((seg_paths[seg.name], (s - seg.start).total_seconds(), (e - s).total_seconds()))
            last_end = e
    return parts


def probe_video_codec(path):
    """Return video codec name (e.g. 'h264', 'hevc') or empty string if undetectable."""
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                              "-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1", path],
                             capture_output=True, text=True, timeout=15)
        return out.stdout.strip().lower()
    except (OSError, subprocess.SubprocessError):
        return ""


def probe_audio_codec(path):
    """Return audio codec name (e.g. 'aac', 'pcm_mulaw') or empty string if no audio / undetectable."""
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0",
                              "-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1", path],
                             capture_output=True, text=True, timeout=15)
        return out.stdout.strip().lower()
    except (OSError, subprocess.SubprocessError):
        return ""


# Audio codecs that can be stream-copied into an MP4 container without re-encoding.
_MP4_NATIVE_AUDIO = {"aac", "mp3", "ac3", "eac3", "alac", "opus", "flac"}


def _audio_args(audio_codec):
    """Return ffmpeg arguments to include audio.  Stream-copy if the codec is
    MP4-native, otherwise re-encode to AAC (handles G.711 µ-law/A-law and other
    non-MP4 codecs from Hikvision NVR).  Returns empty list when there is no audio."""
    if not audio_codec:
        return []
    args = ["-map", "0:a?"]
    if audio_codec in _MP4_NATIVE_AUDIO:
        args.extend(["-c:a", "copy"])
    else:
        args.extend(["-c:a", "aac", "-b:a", "128k"])
    return args


def remux_to_mp4(src_path, out_path, proc_registry, cancel_event):
    """Stream-copy one raw recording segment (.ps) into a standard MP4 container.
    Audio is included when present: MP4-native codecs (AAC, MP3, …) are stream-copied,
    others (G.711 µ-law/A-law from Hikvision NVR) are re-encoded to AAC.
    Written to a temp name first so a failure never leaves a file that looks complete."""
    tmp_out = out_path + ".part.mp4"
    timeout = 600
    codec = probe_video_codec(src_path)
    audio = probe_audio_codec(src_path)
    cmd = ["ffmpeg", "-y", "-i", src_path, "-map", "0:v:0", "-c:v", "copy"]
    cmd.extend(_audio_args(audio))
    if codec in ("hevc", "h265"):
        cmd.extend(["-tag:v", "hvc1"])
    cmd.extend(["-movflags", "+faststart", tmp_out])
    try:
        ok, err = run_ffmpeg(cmd, timeout, proc_registry, cancel_event)
        if not ok:
            return False, err
        os.replace(tmp_out, out_path)
        return True, ""
    finally:
        if os.path.exists(tmp_out):
            try:
                os.remove(tmp_out)
            except OSError:
                pass


def repair_legacy_file(path, proc_registry=None, cancel_event=None):
    """If `path` is not a genuine MP4 container (e.g. legacy Hikvision MPEG-PS),
    remux it in-place to a standard MP4 container.
    Returns (ok: bool, message: str).
    """
    if is_real_mp4(path):
        return True, "already genuine MP4"
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        return False, "file does not exist or is empty"
    ok, err = remux_to_mp4(path, path, proc_registry, cancel_event)
    if not ok:
        return False, err
    return True, "converted to genuine MP4"


def scan_and_repair_legacies(root_dir, proc_registry=None, cancel_event=None, on_file=None):
    """Walk `root_dir` to find any .mp4 files that are legacy non-MP4 containers (MPEG-PS),
    and remux them in-place to genuine MP4 containers.
    Returns dict(total=..., legacy=..., repaired=..., failed=..., errors=[...]).
    """
    stats = {"total": 0, "legacy": 0, "repaired": 0, "failed": 0, "errors": []}
    if not os.path.exists(root_dir):
        return stats

    mp4_files = []
    for dirpath, dirnames, filenames in os.walk(root_dir):
        dirnames[:] = [d for d in dirnames if d != SEGMENTS_DIRNAME and not d.startswith(".")]
        for fname in filenames:
            if fname.lower().endswith(".mp4") and not fname.lower().endswith(".part.mp4"):
                mp4_files.append(os.path.join(dirpath, fname))

    stats["total"] = len(mp4_files)
    for i, path in enumerate(sorted(mp4_files), 1):
        check_cancel(cancel_event)
        if not file_done(path):
            continue
        if not is_real_mp4(path):
            stats["legacy"] += 1
            if on_file:
                on_file(i, len(mp4_files), path, "converting")
            try:
                ok, err = repair_legacy_file(path, proc_registry, cancel_event)
            except Cancelled:
                raise
            except Exception as exc:
                ok, err = False, str(exc)
            if ok:
                stats["repaired"] += 1
                if on_file:
                    on_file(i, len(mp4_files), path, "ok")
            else:
                stats["failed"] += 1
                stats["errors"].append(f"{path}: {err}")
                if on_file:
                    on_file(i, len(mp4_files), path, f"failed: {err}")
        else:
            if on_file:
                on_file(i, len(mp4_files), path, "already_mp4")
    return stats


def cut_clip(parts, out_path, proc_registry, cancel_event, work_dir):
    """Stream-copy the covered pieces into one mp4.  Audio is included when present
    (re-encoded to AAC if the source codec isn't MP4-native).
    Written to a temp name first so a failure never leaves a file that looks complete."""
    if not parts:
        return False, "no recording parts covered the window"
    tmp_out = out_path + ".part.mp4"
    timeout = 600 + int(sum(p[2] for p in parts) / 10)
    try:
        if len(parts) == 1:
            src, offset, dur = parts[0]
            codec = probe_video_codec(src)
            audio = probe_audio_codec(src)
            cmd = ["ffmpeg", "-y", "-ss", f"{offset:.3f}", "-i", src, "-t", f"{dur:.3f}",
                   "-map", "0:v:0", "-c:v", "copy"]
            cmd.extend(_audio_args(audio))
            if codec in ("hevc", "h265"):
                cmd.extend(["-tag:v", "hvc1"])
            cmd.extend(["-movflags", "+faststart", tmp_out])
            ok, err = run_ffmpeg(cmd, timeout, proc_registry, cancel_event)
            if not ok:
                return False, err
        else:
            pieces = []
            codec = ""
            audio = ""
            for src, _, _ in parts:
                if not codec:
                    codec = probe_video_codec(src)
                if not audio:
                    audio = probe_audio_codec(src)
                if codec and audio:
                    break
            for i, (src, offset, dur) in enumerate(parts):
                piece = os.path.join(work_dir, f"piece{i}.ts")
                # MPEG-TS intermediate: include audio if present (re-encode to AAC if not MP4/TS-native).
                piece_cmd = ["ffmpeg", "-y", "-ss", f"{offset:.3f}", "-i", src, "-t", f"{dur:.3f}",
                             "-map", "0:v:0", "-c:v", "copy"]
                if audio:
                    piece_cmd.extend(["-map", "0:a?"])
                    if audio in {"aac", "mp3", "ac3"}:
                        piece_cmd.extend(["-c:a", "copy"])
                    else:
                        piece_cmd.extend(["-c:a", "aac", "-b:a", "128k"])
                piece_cmd.extend(["-f", "mpegts", piece])
                ok, err = run_ffmpeg(piece_cmd, timeout, proc_registry, cancel_event)
                if not ok:
                    return False, err
                pieces.append(piece)
                # Free disk space: if src is a temporary file in work_dir and not needed in subsequent parts, remove it
                if os.path.dirname(os.path.abspath(src)) == os.path.abspath(work_dir) and all(p[0] != src for p in parts[i + 1:]):
                    try:
                        os.remove(src)
                    except OSError:
                        pass
            concat_list_path = os.path.join(work_dir, "concat_list.txt")
            with open(concat_list_path, "w", encoding="utf-8") as f:
                for piece in pieces:
                    safe_path = os.path.abspath(piece).replace("\\", "/").replace("'", "'\\''")
                    f.write(f"file '{safe_path}'\n")
            cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list_path, "-map", "0:v:0", "-c:v", "copy"]
            if audio:
                cmd.extend(["-map", "0:a?", "-c:a", "copy"])
            if codec in ("hevc", "h265"):
                cmd.extend(["-tag:v", "hvc1"])
            cmd.extend(["-movflags", "+faststart", tmp_out])
            ok, err = run_ffmpeg(cmd, timeout, proc_registry, cancel_event)
            if not ok:
                return False, err
        os.replace(tmp_out, out_path)
        return True, ""
    finally:
        if os.path.exists(tmp_out):
            try:
                os.remove(tmp_out)
            except OSError:
                pass


def snapshot_from_clip(clip_path, out_path, proc_registry, cancel_event):
    # Take the frame from the trimmed mp4, which starts on a keyframe; seeking inside the NVR's raw
    # MPEG-PS file can land mid-GOP and decode as a grey smear.
    return run_ffmpeg(["ffmpeg", "-y", "-i", clip_path, "-frames:v", "1", "-q:v", "2", out_path],
                      120, proc_registry, cancel_event)


def snapshot_via_rtsp(host, track_id, start, user, password, out_path, timeout_s, proc_registry, cancel_event):
    """Snapshot-only mode: one frame over RTSP playback instead of downloading a whole segment."""
    stamp = start.strftime("%Y%m%dT%H%M%SZ")
    stop = (start + timedelta(seconds=10)).strftime("%Y%m%dT%H%M%SZ")
    q_user = urllib.parse.quote(str(user), safe="")
    q_password = urllib.parse.quote(str(password), safe="")
    url = f"rtsp://{q_user}:{q_password}@{host}:{RTSP_PORT}/Streaming/tracks/{track_id}?starttime={stamp}&endtime={stop}"
    return run_ffmpeg(["ffmpeg", "-y", "-rtsp_transport", "tcp", "-timeout", str(timeout_s * 1_000_000),
                       "-i", url, "-frames:v", "1", "-q:v", "2", out_path],
                      timeout_s + 30, proc_registry, cancel_event)


def process_camera(plan, args, start, end, client, run_dir, callbacks, proc_registry, cancel_event):
    row = plan.row
    result = {"name": row.get("name"), "nvr": row["nvr"].strip(), "channel": row["channel"].strip(),
              "ok": True, "errors": [], "notes": [], "files": []}
    cb = callbacks

    def report(stage, **info):
        cb.stage(plan.key, stage, row, errors=list(result["errors"]), notes=list(result["notes"]),
                 files=list(result["files"]), **info)

    work_dir = os.path.join(run_dir, safe_name(plan.key))
    try:
        check_cancel(cancel_event)
        base = output_base(row, start, args.output_dir)
        os.makedirs(os.path.dirname(base), exist_ok=True)

        if args.mode == "snapshot":
            if file_done(base + ".jpg"):
                result["files"].append(base + ".jpg")
                result["notes"].append("snapshot already in output folder, skipped")
                report("ok")
                return result
            report("snapshot")
            ok, err = snapshot_via_rtsp(client.host, plan.track_id, start, args.user, args.password,
                                        base + ".jpg", args.timeout, proc_registry, cancel_event)
            if ok:
                result["files"].append(base + ".jpg")
            else:
                result["ok"] = False
                result["errors"].append(f"snapshot: {err}")
            report("ok" if result["ok"] else "fail")
            return result

        # Both modes use work_dir as scratch for downloading temp segments.
        os.makedirs(work_dir, exist_ok=True)
        need = plan.expected_bytes
        disk_check_dir = args.output_dir
        free = shutil.disk_usage(disk_check_dir).free
        headroom = 2.2 if getattr(args, "trim", False) else 1.2
        if need * headroom > free:
            raise IsapiError(f"not enough disk space: need ~{need * headroom / 1e9:.1f} GB, {free / 1e9:.1f} GB free")

        existing = plan.existing or set()
        legacy = plan.legacy or set()
        if existing:
            result["notes"].append(f"{len(existing)} of {len(plan.segments)} recording file(s) "
                                   "already in output folder (MP4), skipped")
        if legacy:
            result["notes"].append(f"detected {len(legacy)} legacy recording file(s) on disk (converting in-place)")

        if plan.clip_exists:
            result["notes"].append("clip already in output folder (MP4), skipped")
        elif plan.legacy_clip:
            result["notes"].append("clip detected as legacy non-MP4 (converting in-place)")
            report("remuxing")
            ok, err = repair_legacy_file(base + ".mp4", proc_registry, cancel_event)
            if ok:
                result["notes"].append(f"converted legacy clip to MP4: {os.path.basename(base + '.mp4')}")
                plan.clip_exists = True
            else:
                result["notes"].append(f"failed to convert legacy clip ({err}), regenerating clip")
                try:
                    os.remove(base + ".mp4")
                except OSError:
                    pass

        seg_paths, lengths = {}, {s.name: s.size for s in plan.segments if s.name not in existing and s.name not in legacy}
        for seg in [] if plan.clip_exists else plan.segments:
            temp_ps = os.path.join(work_dir, f"{safe_name(seg.name)}.ps")
            dest_final = raw_segment_path(row, seg, args.output_dir)
            if not args.trim:
                os.makedirs(os.path.dirname(dest_final), exist_ok=True)

            if seg.name in existing:
                seg_paths[seg.name] = dest_final
                if not args.trim and dest_final not in result["files"]:
                    result["files"].append(dest_final)
                continue

            if not args.trim and seg.name in legacy:
                report("remuxing")
                ok, err = repair_legacy_file(dest_final, proc_registry, cancel_event)
                if ok:
                    if dest_final not in result["files"]:
                        result["files"].append(dest_final)
                    result["notes"].append(f"converted legacy file to MP4: {os.path.basename(dest_final)}")
                    seg_paths[seg.name] = dest_final
                    continue
                else:
                    result["notes"].append(f"repair legacy file failed ({err}), re-downloading: {os.path.basename(dest_final)}")
                    try:
                        os.remove(dest_final)
                    except OSError:
                        pass
                    lengths[seg.name] = seg.size

            if args.trim and file_done(dest_final):
                if is_real_mp4(dest_final):
                    seg_paths[seg.name] = dest_final
                    continue
                if seg.name in legacy:
                    report("remuxing")
                    ok, err = repair_legacy_file(dest_final, proc_registry, cancel_event)
                    if ok:
                        result["notes"].append(f"converted legacy file to MP4: {os.path.basename(dest_final)}")
                        seg_paths[seg.name] = dest_final
                        continue
                    result["notes"].append(f"repair legacy file failed ({err}), re-downloading: {os.path.basename(dest_final)}")
                else:
                    result["notes"].append(f"corrupted segment found ({os.path.basename(dest_final)}), re-downloading")
                try:
                    os.remove(dest_final)
                except OSError:
                    pass
                lengths[seg.name] = seg.size

            def on_length(n, name=seg.name):
                lengths[name] = n
                report("downloading", expected_bytes=sum(lengths.values()))

            report("downloading", expected_bytes=sum(lengths.values()))
            client.download(seg, temp_ps, on_bytes=lambda n: cb.bytes(plan.key, n), on_length=on_length,
                            cancel_event=cancel_event)

            if args.trim:
                seg_paths[seg.name] = temp_ps
            else:
                report("remuxing")
                ok, err = remux_to_mp4(temp_ps, dest_final, proc_registry, cancel_event)
                if not ok:
                    result["ok"] = False
                    result["errors"].append(f"remux: {err}")
                    report("fail")
                    return result
                if dest_final not in result["files"]:
                    result["files"].append(dest_final)
                try:
                    os.remove(temp_ps)
                except OSError:
                    pass

        covered = covered_seconds(plan, start, end)
        wanted = (end - start).total_seconds()
        if covered < wanted - 2:
            result["notes"].append(f"recording covers {covered:.0f}s of {wanted:.0f}s requested (gaps on NVR)")

        if plan.clip_exists:
            if (base + ".mp4") not in result["files"]:
                result["files"].append(base + ".mp4")
        elif args.trim:
            parts = trim_parts(plan, start, end, seg_paths)
            report("cutting")
            ok, err = cut_clip(parts, base + ".mp4", proc_registry, cancel_event, work_dir)
            if ok:
                if (base + ".mp4") not in result["files"]:
                    result["files"].append(base + ".mp4")
            else:
                result["ok"] = False
                result["errors"].append(f"clip: {err}")

        if args.mode == "both" and result["ok"] and file_done(base + ".jpg"):
            result["files"].append(base + ".jpg")
        elif args.mode == "both" and result["ok"]:
            report("snapshot")
            if args.trim:
                ok, err = snapshot_from_clip(base + ".mp4", base + ".jpg", proc_registry, cancel_event)
            else:
                # Raw segments start long before the window; RTSP playback gives an exact frame quickly.
                ok, err = snapshot_via_rtsp(client.host, plan.track_id, start, args.user, args.password,
                                            base + ".jpg", args.timeout, proc_registry, cancel_event)
            if ok:
                result["files"].append(base + ".jpg")
            else:
                # Snapshot failure is a warning in 'both' mode — the clip was already saved successfully.
                result["notes"].append(f"snapshot failed (clip saved): {redact(err)[:200]}")
    except Cancelled:
        result["ok"] = False
        result["errors"].append("cancelled")
        report("cancelled")
        return result
    except IsapiError as e:
        result["ok"] = False
        result["errors"].append(str(e))
    except Exception as e:
        # One camera's unexpected error must not abort the batch or its run log.
        result["ok"] = False
        result["errors"].append(f"internal error: {redact(str(e))}")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    report("ok" if result["ok"] else "fail")
    return result


class Callbacks:
    """Progress hooks; the CLI uses these no-op defaults, the web GUI overrides them."""

    def stage(self, key, stage, row, **info):
        pass

    def bytes(self, key, n):
        pass


def prepare_segments_dir(output_dir):
    """Per-run scratch dir for downloaded segments; removes leftovers from runs that died long ago."""
    root = os.path.join(output_dir, SEGMENTS_DIRNAME)
    os.makedirs(root, exist_ok=True)
    now = time.time()
    for name in os.listdir(root):
        path = os.path.join(root, name)
        try:
            if now - os.path.getmtime(path) > STALE_SEGMENTS_S:
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    os.remove(path)
        except OSError:
            pass
    run_dir = os.path.join(root, f"run_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}")
    os.makedirs(run_dir)
    return run_dir


def run_batch(rows, args, start, end, callbacks=None, cancel_event=None, proc_registry=None):
    """Search every camera's recordings, then download and trim with at most `args.workers` downloads
    overall and `args.per_nvr` per NVR. Shared by the CLI and the web GUI."""
    callbacks = callbacks or Callbacks()
    proc_registry = proc_registry or ProcRegistry()
    clients = {}
    for row in rows:
        host = row["nvr"].strip()
        clients.setdefault(host, NvrClient(host, args.user, args.password, args.timeout))

    for row in rows:
        callbacks.stage(camera_key(row), "searching", row)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        plans = list(pool.map(lambda r: plan_camera(r, clients[r["nvr"].strip()], start, end, args.mode), rows))
        list(pool.map(lambda p: mark_existing(p, args, start, end), plans))

    results = []
    pending = []
    for plan in plans:
        if plan.error:
            results.append({"name": plan.row.get("name"), "nvr": plan.row["nvr"].strip(),
                            "channel": plan.row["channel"].strip(), "ok": False,
                            "errors": [plan.error], "notes": [], "files": []})
            callbacks.stage(plan.key, "fail", plan.row, errors=[plan.error], notes=[], files=[])
        else:
            callbacks.stage(plan.key, "queued", plan.row, expected_bytes=plan.expected_bytes)
            pending.append(plan)

    run_dir = prepare_segments_dir(args.output_dir)
    try:
        active = collections.Counter()
        running = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            while pending or running:
                if cancel_event is not None and cancel_event.is_set():
                    for plan in pending:
                        results.append({"name": plan.row.get("name"), "nvr": plan.row["nvr"].strip(),
                                        "channel": plan.row["channel"].strip(), "ok": False,
                                        "errors": ["cancelled"], "notes": [], "files": []})
                        callbacks.stage(plan.key, "cancelled", plan.row, errors=["cancelled"], notes=[], files=[])
                    pending = []
                # Start the next camera whose NVR still has a free download slot.
                nxt = next((p for p in pending if active[p.row["nvr"].strip()] < args.per_nvr), None)
                if nxt is not None and len(running) < args.workers:
                    pending.remove(nxt)
                    host = nxt.row["nvr"].strip()
                    active[host] += 1
                    fut = pool.submit(process_camera, nxt, args, start, end, clients[host], run_dir,
                                      callbacks, proc_registry, cancel_event)
                    running[fut] = host
                    continue
                if not running:
                    break  # pending is also empty (loop condition); shouldn't reach here but be safe
                done, _ = concurrent.futures.wait(running, timeout=0.5,
                                                  return_when=concurrent.futures.FIRST_COMPLETED)
                for fut in done:
                    active[running.pop(fut)] -= 1
                    results.append(fut.result())
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
    return results


def run_windows(rows, args, windows, callbacks=None, cancel_event=None, proc_registry=None, on_window=None):
    """Run ingestion sequentially across multiple (start, end) windows (e.g. daily recurring windows)."""
    all_results = []
    for idx, (start, end) in enumerate(windows, 1):
        if cancel_event is not None and cancel_event.is_set():
            break
        if on_window:
            on_window(idx, len(windows), start, end)
        results = run_batch(rows, args, start, end, callbacks, cancel_event, proc_registry)
        all_results.extend(results)
    return all_results


def write_run_log(results, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["name", "nvr", "channel", "status", "files", "errors", "notes"])
        for r in results:
            status = "OK" if r["ok"] else ("CANCELLED" if r["errors"] == ["cancelled"] else "FAIL")
            w.writerow([r["name"], r["nvr"], r["channel"], status, "; ".join(r["files"]),
                        "; ".join(r["errors"]), "; ".join(r.get("notes", []))])


# ---------------------------------------------------------------- CLI

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", default="camera_n_nvr.csv", help="Camera/NVR inventory CSV")
    ap.add_argument("--start", help="NVR local start time 'YYYY-MM-DD HH:MM:SS' (default: now - --last-minutes)")
    ap.add_argument("--end", help="NVR local end time 'YYYY-MM-DD HH:MM:SS' (default: now)")
    ap.add_argument("--last-minutes", type=float, default=5,
                    help="If --start/--end omitted, fetch the last N minutes up to now (default 5)")
    ap.add_argument("--tz-offset-hours", type=float, default=float(os.environ.get("TZ_OFFSET_HOURS", "7")),
                    help="NVR clock timezone vs UTC, used only to compute 'now' for --last-minutes (default 7)")
    ap.add_argument("--start-date", help="Start date 'YYYY-MM-DD' for daily recurring window")
    ap.add_argument("--end-date", help="End date 'YYYY-MM-DD' for daily recurring window")
    ap.add_argument("--daily-start", help="Daily start time 'HH:MM[:SS]' (e.g. 10:00:00)")
    ap.add_argument("--daily-end", help="Daily end time 'HH:MM[:SS]' (e.g. 22:00:00)")
    ap.add_argument("--mode", choices=["snapshot", "clip", "both"], default="both")
    ap.add_argument("--output-dir", default="output")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="Max parallel downloads overall")
    ap.add_argument("--per-nvr", type=int, default=DEFAULT_PER_NVR, help="Max parallel downloads per NVR")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S, help="Network stall timeout (s)")
    ap.add_argument("--trim", action="store_true",
                    help="Trim and join the downloaded segments into one clip covering exactly the window "
                         "(default: save the NVR's recording files as-is)")
    ap.add_argument("--user", default=None, help="NVR username (default from config.env / env NVR_USER)")
    ap.add_argument("--password", default=None, help="NVR password (default from config.env / env NVR_PASSWORD)")
    ap.add_argument("--repair-legacies", nargs="?", const="", default=None,
                    help="Scan output directory (or specified folder) and convert all legacy non-MP4 files in-place to genuine MP4")
    args = ap.parse_args()

    if args.repair_legacies is not None:
        target_dir = args.repair_legacies or args.output_dir
        if not os.path.exists(target_dir):
            sys.exit(f"Directory not found: {target_dir}")
        print(f"Scanning '{target_dir}' for legacy non-MP4 files...")
        def on_repair_file(idx, total, path, status):
            if status == "converting":
                print(f"  [{idx}/{total}] Converting: {path}...", end="", flush=True)
            elif status == "ok":
                print(" OK")
            elif status.startswith("failed:"):
                print(f" FAIL ({status})")
        stats = scan_and_repair_legacies(target_dir, on_file=on_repair_file)
        print(f"\nDone: scanned {stats['total']} file(s). Found {stats['legacy']} legacy file(s): "
              f"{stats['repaired']} converted to genuine MP4, {stats['failed']} failed.")
        sys.exit(0 if stats["failed"] == 0 else 1)

    cfg = load_config_env(CONFIG_ENV_PATH)
    args.user = args.user or os.environ.get("NVR_USER") or cfg.get("NVR_USER") or "admin"
    args.password = args.password or os.environ.get("NVR_PASSWORD") or cfg.get("NVR_PASSWORD") or "admin"

    try:
        windows = resolve_windows(
            start=args.start, end=args.end, last_minutes=args.last_minutes, tz_offset_hours=args.tz_offset_hours,
            start_date=args.start_date, end_date=args.end_date, daily_start=args.daily_start, daily_end=args.daily_end,
        )
    except ValueError as e:
        sys.exit(str(e))

    rows = load_online_rows(args.csv)
    dupes = sorted(k for k, n in collections.Counter(camera_key(r) for r in rows).items() if n > 1)
    if dupes:
        sys.exit("Duplicate NVR/channel rows in CSV: " + ", ".join(dupes))
    disabled = load_disabled()
    skipped = [r for r in rows if camera_key(r) in disabled]
    rows = [r for r in rows if camera_key(r) not in disabled]
    if skipped:
        print(f"Skipping {len(skipped)} disabled camera(s): " +
              ", ".join(f"{r['name']} ({camera_key(r)})" for r in skipped))
    if not rows:
        sys.exit("No enabled online cameras found in CSV")

    if len(windows) == 1:
        print(f"Window (NVR local time): {windows[0][0]} -> {windows[0][1]}")
    else:
        print(f"Daily Recurring Windows: {len(windows)} day(s) "
              f"({windows[0][0].strftime('%Y-%m-%d')} to {windows[-1][0].strftime('%Y-%m-%d')}, "
              f"daily {windows[0][0].strftime('%H:%M:%S')} -> {windows[0][1].strftime('%H:%M:%S')})")
    print(f"Cameras: {len(rows)} (mode={args.mode}, workers={args.workers}, per NVR={args.per_nvr}, "
          f"{'trim to window' if args.trim else 'whole recording files'})")

    class ConsoleCallbacks(Callbacks):
        def stage(self, key, stage, row, **info):
            if stage in ("ok", "fail", "cancelled") or (stage == "queued" and info.get("expected_bytes")):
                extra = f" ({info['expected_bytes'] / 1e9:.2f} GB to download)" if stage == "queued" else ""
                print(f"  {row.get('name')} [{key}] {stage}{extra}", flush=True)

    def on_window(idx, total, start, end):
        if total > 1:
            print(f"\n--- Day {idx}/{total}: {start} -> {end} ---", flush=True)

    t0 = time.time()
    results = run_windows(rows, args, windows, ConsoleCallbacks(), on_window=on_window)

    ok_count = sum(1 for r in results if r["ok"])
    print(f"\nDone in {time.time() - t0:.0f}s: {ok_count}/{len(results)} succeeded")
    for r in results:
        status = "OK" if r["ok"] else "FAIL"
        print(f"  [{status}] {r['name']} nvr={r['nvr']} ch={r['channel']}"
              + ("" if r["ok"] else f" -> {'; '.join(r['errors'])}")
              + (f" ({'; '.join(r['notes'])})" if r["notes"] else ""))

    log_path = os.path.join(args.output_dir, "logs", f"run_{datetime.now():%Y%m%d_%H%M%S}.csv")
    write_run_log(results, log_path)
    print(f"Log written to {log_path}")
    sys.exit(0 if ok_count == len(results) else 1)


if __name__ == "__main__":
    main()
