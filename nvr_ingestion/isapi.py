"""Hikvision ISAPI recording search and streaming download client."""
import http.client
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape as xml_escape

from .runtime import Cancelled, check_cancel
from .time_windows import nvr_time, parse_nvr_time

AUTH_LOCKOUT_WAIT_S = int(os.environ.get("AUTH_LOCKOUT_WAIT_S", 30 * 60))
DOWNLOAD_CHUNK = 1 << 20
DEFAULT_TIMEOUT_S = 30
DEFAULT_PASSWORDS = ["admin", "Mc158806"]
HTTP_PORT = 80
RTSP_PORT = 554
DEFAULT_STREAM_SUFFIX = 1
ISAPI_NS = "{http://www.isapi.org/ver20/XMLSchema}"

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
    def __init__(self, host, user, password=None, timeout=DEFAULT_TIMEOUT_S, port=HTTP_PORT):
        self.base = f"http://{host}:{port}"
        self.host = host
        self.port = int(port)
        self.user = user
        self.timeout = timeout
        if not password:
            self.passwords = list(DEFAULT_PASSWORDS)
        else:
            candidates = [str(p) for p in password] if isinstance(password, (list, tuple)) else [str(password)]
            passwords = []
            for p in candidates + DEFAULT_PASSWORDS:
                if p and p not in passwords:
                    passwords.append(p)
            self.passwords = passwords or list(DEFAULT_PASSWORDS)
        self.active_password = self.passwords[0] if self.passwords else ""
        self.password = self.active_password

    def check_network(self):
        try:
            sock = socket.create_connection((self.host, self.port), timeout=3)
            sock.close()
            return True
        except (socket.error, OSError, TimeoutError):
            return False

    def poll_network_until_connected(self, cancel_event=None, on_status=None, poll_interval=5):
        if self.check_network():
            return
        while True:
            check_cancel(cancel_event)
            if on_status:
                on_status(f"NVR {self.host} unreachable (network down). Polling every {poll_interval}s until connection restored...")
            elapsed = 0.0
            while elapsed < poll_interval:
                check_cancel(cancel_event)
                step = min(0.5, max(0.0, poll_interval - elapsed))
                if step > 0:
                    time.sleep(step)
                elapsed += step
                if step <= 0:
                    break
            check_cancel(cancel_event)
            if self.check_network():
                break
        if on_status:
            on_status(f"Network connection restored to NVR {self.host}. Resuming data retrieval...")

    def wait_auth_lockout(self, cancel_event=None, on_status=None, wait_seconds=None):
        wait_s = wait_seconds if wait_seconds is not None else int(os.environ.get("AUTH_LOCKOUT_WAIT_S", AUTH_LOCKOUT_WAIT_S))
        wait_s = max(0, int(wait_s))
        check_cancel(cancel_event)
        if on_status:
            on_status(f"Login failed for NVR {self.host} (HTTP 401). Waiting {wait_s // 60} minutes before retrying (account lockout cooldown)...")
        for rem in range(wait_s, 0, -1):
            check_cancel(cancel_event)
            time.sleep(1)
            check_cancel(cancel_event)
            remaining = rem - 1
            if remaining > 0 and on_status:
                if remaining % 60 == 0:
                    on_status(f"Login lockout cooldown for NVR {self.host}: {remaining // 60} minutes remaining before retrying (account lockout cooldown)...")
                elif wait_s < 60 and remaining % 10 == 0:
                    on_status(f"Login lockout cooldown for NVR {self.host}: {remaining}s remaining before retrying (account lockout cooldown)...")

    def _open(self, path, body, cancel_event=None, on_status=None):
        data = body.encode() if isinstance(body, str) else body

        while True:
            candidates = [self.active_password] + [p for p in self.passwords if p != self.active_password]
            if not candidates:
                raise IsapiError(f"NVR {self.host} has no password configured")

            for candidate in candidates:
                while True:
                    check_cancel(cancel_event)
                    # A fresh opener per request: urllib's digest handler keeps a retry counter that is not thread-safe.
                    mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
                    mgr.add_password(None, self.base + "/", self.user, candidate)
                    opener = urllib.request.build_opener(urllib.request.HTTPDigestAuthHandler(mgr))
                    req = urllib.request.Request(self.base + path, data, {"Content-Type": "application/xml"})
                    try:
                        resp = opener.open(req, timeout=self.timeout)
                        if candidate != self.active_password:
                            self.active_password = candidate
                            self.password = candidate
                        return resp
                    except urllib.error.HTTPError as e:
                        if e.code == 401:
                            break
                        raise IsapiError(f"NVR {self.host} {path} returned HTTP {e.code}") from e
                    except (urllib.error.URLError, OSError, TimeoutError):
                        check_cancel(cancel_event)
                        self.poll_network_until_connected(cancel_event=cancel_event, on_status=on_status, poll_interval=5)
                        continue

            # When all candidate passwords in self.passwords have been attempted and all returned HTTP 401:
            check_cancel(cancel_event)
            self.wait_auth_lockout(cancel_event=cancel_event, on_status=on_status)
            if self.passwords:
                self.active_password = self.passwords[0]
                self.password = self.active_password

    def search(self, track_id, start, end, cancel_event=None, on_status=None):
        """Recording segments on `track_id` overlapping [start, end), oldest first."""
        search_id = str(uuid.uuid4()).upper()
        segments, position = {}, 0
        while True:
            check_cancel(cancel_event)
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
            with self._open("/ISAPI/ContentMgmt/search", body, cancel_event=cancel_event, on_status=on_status) as resp:
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

    def download(self, segment, dest, on_bytes=None, on_length=None, cancel_event=None, on_status=None):
        """Stream one whole recording segment to `dest` (the NVR ignores sub-ranges and HTTP Range)."""
        body = ('<?xml version="1.0" encoding="UTF-8"?>'
                '<downloadRequest version="1.0" xmlns="http://www.isapi.org/ver20/XMLSchema">'
                f"<playbackURI>{xml_escape(segment.uri)}</playbackURI></downloadRequest>")
        check_cancel(cancel_event)
        tmp = dest + ".part"
        try:
            download_done = False
            while not download_done:
                check_cancel(cancel_event)
                interrupted = False
                with self._open("/ISAPI/ContentMgmt/download", body, cancel_event=cancel_event, on_status=on_status) as resp, open(tmp, "wb") as f:
                    length = resp.headers.get("Content-Length")
                    if on_length and length and length.isdigit():
                        on_length(int(length))
                    while True:
                        check_cancel(cancel_event)
                        try:
                            chunk = resp.read(DOWNLOAD_CHUNK)
                        except (OSError, TimeoutError, http.client.IncompleteRead):
                            check_cancel(cancel_event)
                            self.poll_network_until_connected(cancel_event=cancel_event, on_status=on_status, poll_interval=5)
                            interrupted = True
                            break
                        if not chunk:
                            break
                        f.write(chunk)
                        if on_bytes:
                            on_bytes(len(chunk))
                if not interrupted:
                    download_done = True
            os.replace(tmp, dest)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
