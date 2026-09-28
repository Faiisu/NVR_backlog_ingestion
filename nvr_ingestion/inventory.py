"""Inventory, credentials-file, and camera naming helpers."""
import csv
import json
import os
import re

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DISABLED_PATH = os.path.join(BASE_DIR, "disabled_cameras.json")
DEFAULT_STREAM_SUFFIX = 1

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
