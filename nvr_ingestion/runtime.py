"""Cancellation and subprocess lifecycle helpers."""
import re
import subprocess
import threading

_URL_CREDENTIALS = re.compile(r"(rtsp://)[^/@\s'\"]+@")

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
