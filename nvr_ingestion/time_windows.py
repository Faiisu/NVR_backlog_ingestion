"""NVR-local time parsing and retrieval window calculations."""
from datetime import date, datetime, time as dtime, timedelta, timezone

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
