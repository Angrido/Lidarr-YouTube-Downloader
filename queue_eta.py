"""How long the download queue will take.

The queue holds albums, but the work is tracks: the remaining tracks are the
sum of ``missing_track_count`` (the Lidarr statistic kept in
``missing_albums_cache``) over the queued albums, and an album the cache does
not know counts as the average of the ones it does.

The pace is measured, never configured. Every attempt in ``track_downloads``
counts, successful or not, because the queue drains by tracks tried: a track
that fails still took its turn. Rates are wall-clock: pauses, VPN rotations
and idle time are inside the window, so they lower the pace and lengthen the
estimate, which is what the user will actually wait. Files copied from
another album (``source_format`` 'reused') are not downloads and are left out.

The answer is an interval, not a number. The central value uses the 24 h pace;
the optimistic and pessimistic ends use the quicker and slower quarters of
the last 24 hours and the 7-day pace, whichever are further out. That shows
how much the pace has moved lately, not a statistical confidence interval.

Nothing here writes or starts anything: a read of two tables, cheap enough
for a page that polls it (the result is cached for ``CACHE_TTL`` seconds).
"""
import threading
import time

import models
import vpn
from config import load_config

HOUR = 3600
# Window name -> hours. The 24 h pace is the central value.
WINDOWS = {"1h": 1, "24h": 24, "7d": 168}
HISTORY_HOURS = max(WINDOWS.values())
# Fewer attempts than this in a window and its pace is noise, not a rate.
MIN_ATTEMPTS = 30
# A window or hourly bucket covering less history than this has no pace.
MIN_SPAN = 600
MIN_BUCKET_SPAN = 1800
MIN_HOURLY_BUCKETS = 6
# Used only when no queued album is in the cache (the history average).
DEFAULT_TRACKS_PER_ALBUM = 11
# "The pace has dropped": the last hour is under this share of the 24 h pace.
SLOWDOWN_RATIO = 0.5
# "Most tracks are failing right now": needs a few attempts to mean anything.
HIGH_FAILURE_RATIO = 0.5
RECENT_MIN_ATTEMPTS = 10
CACHE_TTL = 15

_cache_lock = threading.Lock()
_cache = {"at": 0.0, "core": None}


def _percentile(values, fraction):
    """Linear-interpolated percentile of a non-empty list."""
    ordered = sorted(values)
    pos = (len(ordered) - 1) * fraction
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def _window(buckets, first, now, hours):
    attempts = sum(a for b, (a, _) in buckets.items() if b < hours)
    ok = sum(s for b, (_, s) in buckets.items() if b < hours)
    span = min(hours * HOUR, now - first) if first else 0
    return {
        "hours": hours,
        "attempts": attempts,
        "successes": ok,
        "failure_rate": round(1 - ok / attempts, 4) if attempts else None,
        "per_min": (attempts / (span / 60)) if span >= MIN_SPAN else None,
        "enough": span >= MIN_SPAN and attempts >= MIN_ATTEMPTS,
    }


def _hourly_rates(buckets, first, now):
    """Pace of each of the last 24 hours that history covers (0 when idle)."""
    rates = []
    for i in range(WINDOWS["24h"]):
        end = now - i * HOUR
        start = max(now - (i + 1) * HOUR, first) if first else end
        if end - start < MIN_BUCKET_SPAN:
            continue
        rates.append(buckets.get(i, (0, 0))[0] / ((end - start) / 60))
    return rates


def _eta_seconds(tracks, per_min):
    if not per_min or per_min <= 0:
        return None
    return tracks / per_min * 60


def _round(value, digits=2):
    return None if value is None else round(value, digits)


def compute(now=None):
    """The measured part: queue size, pace, failure rate and the interval."""
    now = time.time() if now is None else now
    totals = models.get_queue_track_totals()
    albums, known = totals["albums"], totals["known"]
    avg = totals["tracks"] / known if known else DEFAULT_TRACKS_PER_ALBUM
    estimated = round((albums - known) * avg)
    tracks = totals["tracks"] + estimated

    buckets, first = models.get_attempt_buckets(now, HISTORY_HOURS)
    windows = {n: _window(buckets, first, now, h) for n, h in WINDOWS.items()}
    basis = next((n for n in ("24h", "7d", "1h") if windows[n]["enough"]), None)
    central = windows[basis]["per_min"] if basis else None

    hourly = _hourly_rates(buckets, first, now)
    spread = len(hourly) >= MIN_HOURLY_BUCKETS
    p25 = _percentile(hourly, 0.25) if spread else None
    p75 = _percentile(hourly, 0.75) if spread else None

    eta = None
    if albums and central:
        rates = [central]
        if spread:
            rates += [p25, p75]
        if windows["7d"]["enough"]:
            rates.append(windows["7d"]["per_min"])
        eta = {
            "seconds": _eta_seconds(tracks, central),
            "low": _eta_seconds(tracks, max(rates)),
            # None when a quarter of the hours made no attempt at all: there
            # is no honest upper bound, the page says "at least".
            "high": _eta_seconds(tracks, min(rates)),
        }

    failure = windows[basis]["failure_rate"] if basis else None
    warnings = []
    recent = windows["1h"]
    if (
        central
        and recent["per_min"] is not None
        and recent["per_min"] < central * SLOWDOWN_RATIO
    ):
        warnings.append("slowdown")
    if (
        recent["attempts"] >= RECENT_MIN_ATTEMPTS
        and recent["failure_rate"] is not None
        and recent["failure_rate"] >= HIGH_FAILURE_RATIO
    ):
        warnings.append("high_failures")
    if estimated:
        warnings.append("tracks_estimated")
    if basis and basis != "24h":
        warnings.append("short_history")

    return {
        "generated_at": round(now),
        "queue": {
            "albums": albums,
            "tracks": tracks,
            "tracks_estimated": estimated,
            "avg_tracks": _round(avg, 1),
        },
        "rate": {
            "basis": basis,
            "per_min": _round(central),
            "windows": {
                n: {
                    "attempts": w["attempts"],
                    "successes": w["successes"],
                    "per_min": _round(w["per_min"]),
                }
                for n, w in windows.items()
            },
            "hourly": {
                "buckets": len(hourly),
                "slow": _round(p25),
                "fast": _round(p75),
            },
        },
        "failure_rate": _round(failure, 3),
        "expected_failures": round(tracks * failure) if failure else 0,
        "eta": eta and {k: _round(v, 0) for k, v in eta.items()},
        "confidence": "high" if spread and basis == "24h" else "low",
        "warnings": warnings,
    }


def _held(cfg, restarting):
    """What is stopping the queue from starting anything right now."""
    return {
        "paused": bool(cfg.get("queue_paused")),
        "restarting": bool(restarting),
        "vpn_rotating": vpn.rotating(),
    }


def estimate(now=None, cfg=None, restarting=False):
    """``compute()`` (cached) plus the state the queue is in right now.

    ``state`` is one of ``empty``, ``restarting``, ``paused``, ``vpn_rotating``,
    ``insufficient_data`` or ``ok``. The interval is still
    given while held, as the time left once it runs again.
    """
    cfg = cfg if cfg is not None else load_config()
    clock = time.time() if now is None else now
    with _cache_lock:
        if _cache["core"] is None or not 0 <= clock - _cache["at"] < CACHE_TTL:
            _cache["core"] = compute(clock)
            _cache["at"] = clock
        core = dict(_cache["core"])
    held = _held(cfg, restarting)
    if not core["queue"]["albums"]:
        state = "empty"
    else:
        state = next((k for k in ("restarting", "paused", "vpn_rotating")
                      if held[k]), None)
        if state is None:
            state = "ok" if core["eta"] else "insufficient_data"
    core["state"] = state
    core["held"] = held
    return core


def reset_cache():
    with _cache_lock:
        _cache.update(at=0.0, core=None)
