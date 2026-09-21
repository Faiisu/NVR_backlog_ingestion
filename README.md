# CCTV Footage Retrieval

Downloads recorded footage (video clips + snapshots) from Hikvision NVR storage over ISAPI, driven by
`camera_n_nvr.csv`.

## How it works
For each enabled online camera:
1. **Search** the NVR's recordings for the window: `POST /ISAPI/ContentMgmt/search` (track = channel
   number x 100 + 1, e.g. `D5` -> `501`). The NVR returns every recording segment that **overlaps** the
   window, not only segments that start inside it.
2. **Download** each overlapping segment in full: `POST /ISAPI/ContentMgmt/download`. This is a file
   transfer at full network speed. The NVR ignores sub-ranges and HTTP `Range`, so a segment is always
   whole (~1 GB per ~70 min per camera).
3. **Default (trim off):** each segment is downloaded and fast-remuxed into a standard `.mp4`
   container (ffmpeg stream copy, no re-encode, ~1-2 s per GB), named by its own start/end time.
4. **Trim on** (GUI checkbox "Trim & join to the exact time range", CLI `--trim`): segments go to
   `output/.segments/`, each is cut to its intersection with the window, the pieces are joined in time
   order with ffmpeg stream copy (no re-encode) into one `.mp4`, and the segments are deleted.
5. Gaps with no recording inside the window are reported as a note in both modes.
6. **Snapshot** (modes `both` / `snapshot`): one frame at the window start. Taken from the trimmed clip
   when trimming, otherwise over RTSP playback (no need to decode a whole segment).

Measured 2026-09-16: one download ~390 Mbps; one NVR tops out ~500 Mbps; 4 parallel downloads from
different NVRs ~800-850 Mbps (the ~1 Gbps link). Defaults: 4 parallel downloads, at most 2 per NVR.
The old RTSP playback method was limited to real time (~2.4 Mbps per camera).

## Time
These NVRs label their **local clock (GMT+7) with a `Z` suffix** in ISAPI and RTSP playback times
(verified against the on-screen clock). Start/end are therefore NVR local time and sent unchanged.
`--tz-offset-hours` (default 7) is only used to compute "now" for `--last-minutes`.

> Footage downloaded before 2026-09-16 17:30 with the old RTSP method was requested 7 hours too early
> (the old code converted local time to UTC).

Trim accuracy (trim on): a clip starts on the keyframe at or just before the requested start, so it can
begin up to one keyframe interval early (observed 0-5 s) and ends at the requested end.

## Requirements
- Python 3.x (stdlib only, no pip packages needed)
- `ffmpeg` on PATH
- Network access to the NVRs on HTTP port 80 (ISAPI) and 554 (RTSP, snapshot-only mode)

## Setup
```
cp config.env.example config.env
# edit config.env with real NVR_USER / NVR_PASSWORD
```

## Usage (CLI)
```
# last 5 minutes: whole recording files + snapshot
python3 cctv_retrieve.py --csv camera_n_nvr.csv --output-dir output

# explicit NVR-local time window, trimmed to exactly that window
python3 cctv_retrieve.py --csv camera_n_nvr.csv \
  --start "2026-09-16 08:00:00" --end "2026-09-16 09:00:00" \
  --mode both --workers 4 --per-nvr 2 --trim --output-dir output

# daily recurring window across dates (e.g. Sept 10, 11, 12 from 10:00 to 22:00)
python3 cctv_retrieve.py --csv camera_n_nvr.csv \
  --start-date 2026-09-10 --end-date 2026-09-12 \
  --daily-start 10:00:00 --daily-end 22:00:00 \
  --mode both --workers 4 --per-nvr 2 --trim --output-dir output
```

## Web GUI
```
python3 webgui.py --host 0.0.0.0 --port 8080
```
Open `http://<host-ip>:8080` from any device on the network. Set NVR credentials, enable/disable
cameras, pick a time window with the date pickers, run, and watch per-camera status, bytes
downloaded, live speed, total size, time remaining and finish time. Stop aborts downloads immediately
and cleans up temporary files.

Currently running persistently on the deployment box via:
```
cd ~/cctv_ingestion && setsid nohup python3 webgui.py --host 0.0.0.0 --port 8080 > webgui.log 2>&1 < /dev/null &
```
**No authentication** is built in: anyone who can reach the host's IP on port 8080 can change NVR
credentials and trigger runs. Only run this on a trusted network. To stop it: `pkill -f '[w]ebgui.py'`.

## Output
```
output/<seg date>/<camera>/<YYYYMMDD_HHMMSS>-<YYYYMMDD_HHMMSS>_<camera>_<channel>.mp4   whole segment (trim off)
output/<date>/<camera>/<YYYYMMDD_HHMMSS>_<camera>_<channel>.mp4                        exact clip (trim on)
output/<date>/<camera>/<YYYYMMDD_HHMMSS>_<camera>_<channel>.jpg                        snapshot at window start
output/logs/run_<YYYYMMDD_HHMMSS>.csv    per-camera OK/FAIL/CANCELLED, files, errors, notes
```
Whole segments are remuxed from the NVR's download format (MPEG-PS) into standard MP4 containers
with ffmpeg stream copy (`-c copy -movflags +faststart` and `-tag:v hvc1` for HEVC), ensuring
compatibility with standard players like Windows Media Player, QuickTime, and browsers. Trimmed
clips are also real MP4, video-only. Cameras sharing a name share a folder; filenames stay unique by channel.

## Notes
- Disabled cameras are kept in `disabled_cameras.json` (keys `<nvr>/<channel>`), separate from the CSV;
  GUI and CLI both skip them.
- Files already in the output folder are inspected during duplicate detection:
  - If a file is already a **genuine MP4 container**, it is skipped and marked as done.
  - If a file exists on disk but is a **legacy non-MP4 container** (e.g. raw MPEG-PS from previous versions), it is **automatically converted in-place to real MP4** without re-downloading from the NVR (~1 s per GB).
  - If in-place conversion of a damaged legacy file fails, the system automatically falls back to re-downloading fresh footage from the NVR.
- **Legacy repair utility (standalone):**
  ```bash
  # Scan and repair all legacy non-MP4 files in output directory:
  python3 cctv_retrieve.py --repair-legacies output

  # Or click "🛠 Scan & Convert Legacy Files" in the Web GUI.
  ```
- **Automated test suite:**
  ```bash
  python3 test_cctv.py
  ```
- A run checks free disk space before downloading each camera (needs ~1.1x the segment sizes).
- Clips are written under a temporary name and renamed when complete, so a failed or stopped run never
  leaves a file that looks finished.
- NVR credentials are stripped from ffmpeg error text before it reaches the GUI, console or logs.
  They are visible in `ps` while an ffmpeg snapshot runs.
- The web GUI refuses to run if the CSV has two online rows with the same NVR + channel.

## Known gap: NVR 172.168.7.2 (channels D6, D9, D12 / "IPdome 01")
The channel mapping is correct (ISAPI input channels 6/9/12 = cameras .200/.203/.211), but ISAPI search
finds **no recordings** on tracks 601/901/1201. Check the recording schedule / storage assignment for
those channels on that NVR.
