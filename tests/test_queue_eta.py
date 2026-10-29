"""Tests for queue_eta: the time left on the download queue."""

import time

import pytest

import db
import models
import queue_eta

NOW = 1_800_000_000.0
HOUR = 3600


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", str(tmp_path / "test.db"))
    db.init_db()
    queue_eta.reset_cache()
    yield
    queue_eta.reset_cache()
    db.close_db()


def seed_attempts(per_hour, hours, start_hour=0, ok=1.0, source_format="",
                  now=NOW):
    """``per_hour`` attempts in each of ``hours`` hourly buckets back from
    ``now`` (bucket 0 = the last hour), ``ok`` of them successful."""
    rows = []
    for h in range(start_hour, start_hour + hours):
        for i in range(per_hour):
            ts = now - h * HOUR - (i + 0.5) * HOUR / per_hour
            rows.append((
                1, "A", "A", f"T{h}-{i}", i + 1,
                1 if i < round(per_hour * ok) else 0, ts, source_format,
            ))
    conn = db.get_db()
    conn.executemany(
        "INSERT INTO track_downloads (album_id, album_title, artist_name,"
        " track_title, track_number, success, timestamp, source_format)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    conn.commit()


def queue_album(album_id, missing=None, status="queued"):
    """Queue an album; ``missing`` None leaves it out of the cache."""
    models.enqueue_album(album_id)
    conn = db.get_db()
    if status != "queued":
        conn.execute("UPDATE download_queue SET status = ? WHERE album_id = ?",
                     (status, album_id))
    if missing is not None:
        conn.execute(
            "INSERT INTO missing_albums_cache (album_id, title, track_count,"
            " missing_track_count, synced_at) VALUES (?, 'T', ?, ?, 0)",
            (album_id, missing, missing))
    conn.commit()


class TestQueueTracks:
    def test_empty_queue(self):
        core = queue_eta.compute(NOW)
        assert core["queue"]["albums"] == 0
        assert core["queue"]["tracks"] == 0
        assert core["eta"] is None

    def test_sums_missing_tracks_of_queued_albums(self):
        queue_album(1, 10)
        queue_album(2, 4)
        q = queue_eta.compute(NOW)["queue"]
        assert q["albums"] == 2
        assert q["tracks"] == 14
        assert q["tracks_estimated"] == 0

    def test_unknown_album_counts_as_the_average(self):
        queue_album(1, 10)
        queue_album(2, 20)
        queue_album(3)  # not in the cache
        core = queue_eta.compute(NOW)
        assert core["queue"]["tracks"] == 45
        assert core["queue"]["tracks_estimated"] == 15
        assert core["queue"]["avg_tracks"] == 15.0
        assert "tracks_estimated" in core["warnings"]

    def test_nothing_in_the_cache_uses_the_default(self):
        queue_album(1)
        queue_album(2)
        q = queue_eta.compute(NOW)["queue"]
        assert q["tracks"] == 2 * queue_eta.DEFAULT_TRACKS_PER_ALBUM

    def test_zero_missing_is_treated_as_unknown(self):
        queue_album(1, 12)
        queue_album(2, 0)
        assert queue_eta.compute(NOW)["queue"]["tracks"] == 24

    def test_the_downloading_album_is_not_counted(self):
        queue_album(1, 10)
        queue_album(2, 30, status="downloading")
        q = queue_eta.compute(NOW)["queue"]
        assert (q["albums"], q["tracks"]) == (1, 10)


class TestRates:
    def test_windows_count_attempts_and_wall_clock(self):
        seed_attempts(60, 48)  # 1 a minute, two days
        r = queue_eta.compute(NOW)["rate"]
        assert r["windows"]["1h"]["attempts"] == 60
        assert r["windows"]["24h"]["attempts"] == 24 * 60
        assert r["windows"]["7d"]["attempts"] == 48 * 60
        assert r["basis"] == "24h"
        assert r["per_min"] == pytest.approx(1.0, abs=0.01)
        # 48 h of history spread over a 7-day window: the pace is per
        # minute of history, not per minute of the window
        assert r["windows"]["7d"]["per_min"] == pytest.approx(1.0, abs=0.01)

    def test_idle_time_inside_the_window_lowers_the_pace(self):
        seed_attempts(60, 12, start_hour=12)  # busy, then nothing for 12 h
        seed_attempts(60, 12, start_hour=24)
        r = queue_eta.compute(NOW)["rate"]
        assert r["windows"]["24h"]["per_min"] == pytest.approx(0.5, abs=0.01)

    def test_copied_files_are_not_downloads(self):
        seed_attempts(60, 24)
        seed_attempts(30, 24, source_format="reused")
        r = queue_eta.compute(NOW)["rate"]
        assert r["windows"]["24h"]["attempts"] == 24 * 60

    def test_failures_are_attempts_and_set_the_failure_rate(self):
        queue_album(1, 100)
        seed_attempts(60, 24, ok=0.75)
        core = queue_eta.compute(NOW)
        assert core["rate"]["per_min"] == pytest.approx(1.0, abs=0.01)
        assert core["failure_rate"] == 0.25
        assert core["expected_failures"] == 25

    def test_history_shorter_than_the_window(self):
        seed_attempts(60, 3)  # started three hours ago
        r = queue_eta.compute(NOW)["rate"]
        assert r["windows"]["24h"]["per_min"] == pytest.approx(1.0, abs=0.02)

    def test_buckets_are_counted_back_from_now(self):
        seed_attempts(5, 2)
        buckets, first = models.get_attempt_buckets(NOW, 168)
        assert buckets == {0: (5, 5), 1: (5, 5)}
        assert first == pytest.approx(NOW - 2 * HOUR + 0.5 * HOUR / 5)

    def test_rows_older_than_the_history_are_ignored(self):
        seed_attempts(5, 1, start_hour=200)
        assert models.get_attempt_buckets(NOW, 168)[0] == {}


class TestEta:
    def test_central_value_and_interval(self):
        queue_album(1, 600)
        seed_attempts(60, 24)  # 1 / min
        eta = queue_eta.compute(NOW)["eta"]
        assert eta["seconds"] == pytest.approx(600 * 60, abs=60)
        assert eta["low"] <= eta["seconds"] <= eta["high"]

    def test_interval_follows_the_slow_and_fast_hours(self):
        queue_album(1, 1200)
        seed_attempts(30, 12)           # slow half: 0.5 / min
        seed_attempts(90, 12, start_hour=12)  # fast half: 1.5 / min
        core = queue_eta.compute(NOW)
        assert core["rate"]["per_min"] == pytest.approx(1.0, abs=0.01)
        eta = core["eta"]
        assert eta["high"] > eta["seconds"] > eta["low"]
        assert eta["high"] == pytest.approx(1200 / 0.5 * 60, rel=0.05)
        assert eta["low"] == pytest.approx(1200 / 1.5 * 60, rel=0.05)

    def test_a_better_week_stretches_the_optimistic_end(self):
        queue_album(1, 1000)
        seed_attempts(30, 24)                       # 0.5 / min today
        seed_attempts(120, 24 * 6, start_hour=24)   # 2 / min before
        core = queue_eta.compute(NOW)
        week = core["rate"]["windows"]["7d"]["per_min"]
        assert week > core["rate"]["per_min"]
        assert core["eta"]["low"] == pytest.approx(1000 / week * 60, rel=0.01)

    def test_idle_quarter_of_the_day_has_no_upper_bound(self):
        queue_album(1, 100)
        seed_attempts(60, 12)
        # the 12 older hours have nothing at all
        seed_attempts(60, 1, start_hour=30)
        core = queue_eta.compute(NOW)
        assert core["eta"]["high"] is None
        assert core["eta"]["seconds"] is not None

    def test_too_little_data_gives_no_estimate(self):
        queue_album(1, 100)
        seed_attempts(5, 1)
        core = queue_eta.compute(NOW)
        assert core["rate"]["basis"] is None
        assert core["eta"] is None

    def test_short_history_is_flagged_low_confidence(self):
        queue_album(1, 100)
        seed_attempts(60, 3)
        core = queue_eta.compute(NOW)
        assert core["eta"] is not None
        assert core["confidence"] == "low"

    def test_a_full_day_is_high_confidence(self):
        queue_album(1, 100)
        seed_attempts(60, 30)
        assert queue_eta.compute(NOW)["confidence"] == "high"

    def test_seven_day_pace_stands_in_for_an_idle_day(self):
        queue_album(1, 100)
        seed_attempts(60, 24 * 3, start_hour=30)  # nothing in the last 30 h
        core = queue_eta.compute(NOW)
        assert core["rate"]["basis"] == "7d"
        assert "short_history" in core["warnings"]


class TestWarnings:
    def test_slowdown_when_the_last_hour_falls_behind(self):
        queue_album(1, 100)
        seed_attempts(60, 23, start_hour=1)
        seed_attempts(20, 1)
        assert "slowdown" in queue_eta.compute(NOW)["warnings"]

    def test_no_slowdown_at_a_steady_pace(self):
        queue_album(1, 100)
        seed_attempts(60, 24)
        assert "slowdown" not in queue_eta.compute(NOW)["warnings"]

    def test_high_failures_in_the_last_hour(self):
        queue_album(1, 100)
        seed_attempts(60, 23, start_hour=1)
        seed_attempts(40, 1, ok=0.25)
        assert "high_failures" in queue_eta.compute(NOW)["warnings"]

    def test_a_few_failures_are_not_a_warning(self):
        queue_album(1, 100)
        seed_attempts(60, 23, start_hour=1)
        seed_attempts(4, 1, ok=0.0)
        assert "high_failures" not in queue_eta.compute(NOW)["warnings"]


class TestState:
    @pytest.fixture(autouse=True)
    def _vpn_idle(self, monkeypatch):
        monkeypatch.setattr(queue_eta.vpn, "rotating", lambda: False)

    def busy_queue(self):
        queue_album(1, 100)
        seed_attempts(60, 24)

    def test_empty(self):
        out = queue_eta.estimate(NOW, cfg={})
        assert out["state"] == "empty"

    def test_ok(self):
        self.busy_queue()
        out = queue_eta.estimate(NOW, cfg={})
        assert out["state"] == "ok"
        assert out["held"] == {"paused": False, "restarting": False,
                               "vpn_rotating": False}

    def test_paused_still_gives_the_time_left_once_resumed(self):
        self.busy_queue()
        out = queue_eta.estimate(NOW, cfg={"queue_paused": True})
        assert out["state"] == "paused"
        assert out["eta"]["seconds"] is not None

    def test_vpn_rotating(self, monkeypatch):
        self.busy_queue()
        monkeypatch.setattr(queue_eta.vpn, "rotating", lambda: True)
        assert queue_eta.estimate(NOW, cfg={})["state"] == "vpn_rotating"

    def test_restart_pending(self):
        self.busy_queue()
        out = queue_eta.estimate(NOW, cfg={"queue_paused": True}, restarting=True)
        assert out["state"] == "restarting"

    def test_insufficient_data(self):
        queue_album(1, 100)
        assert queue_eta.estimate(NOW, cfg={})["state"] == "insufficient_data"


class TestCache:
    def test_repeat_calls_read_the_database_once(self, monkeypatch):
        queue_album(1, 100)
        seed_attempts(60, 24)
        calls = []
        real = models.get_queue_track_totals
        monkeypatch.setattr(models, "get_queue_track_totals",
                            lambda: calls.append(1) or real())
        queue_eta.estimate(NOW, cfg={})
        queue_eta.estimate(NOW + 5, cfg={})
        assert len(calls) == 1
        queue_eta.estimate(NOW + queue_eta.CACHE_TTL + 1, cfg={})
        assert len(calls) == 2

    def test_held_state_is_never_stale(self):
        queue_album(1, 100)
        seed_attempts(60, 24)
        assert queue_eta.estimate(NOW, cfg={})["state"] == "ok"
        assert queue_eta.estimate(NOW + 1, cfg={"queue_paused": True})["state"] == "paused"

    def test_a_cached_result_is_not_shared_with_callers(self):
        queue_album(1, 100)
        seed_attempts(60, 24)
        queue_eta.estimate(NOW, cfg={})["state"] = "tampered"
        assert queue_eta.estimate(NOW + 1, cfg={})["state"] == "ok"


class TestPercentile:
    def test_interpolates(self):
        assert queue_eta._percentile([1, 2, 3, 4], 0.25) == pytest.approx(1.75)
        assert queue_eta._percentile([1, 2, 3, 4], 0.75) == pytest.approx(3.25)

    def test_single_value(self):
        assert queue_eta._percentile([7], 0.25) == 7


class TestSpeed:
    def test_a_large_history_is_cheap(self):
        """The queue page polls this; ~60 k rows must stay far under a second."""
        queue_album(1, 100)
        seed_attempts(350, 168)  # 58 800 rows over 7 days
        queue_eta.compute(NOW)  # warm the page cache
        start = time.perf_counter()
        queue_eta.compute(NOW)
        assert time.perf_counter() - start < 1.0


class TestRoute:
    @pytest.fixture()
    def client(self, tmp_path, monkeypatch):
        monkeypatch.setattr("config.CONFIG_FILE", str(tmp_path / "config.json"))
        monkeypatch.setenv("DOWNLOAD_PATH", str(tmp_path / "downloads"))
        monkeypatch.setenv("LIDARR_URL", "http://localhost:8686")
        monkeypatch.setenv("LIDARR_API_KEY", "test-key")
        monkeypatch.setattr(queue_eta.vpn, "rotating", lambda: False)
        from app import app
        from utils import rate_limit_store
        rate_limit_store.clear()
        app.config["TESTING"] = True  # nosemgrep
        with app.test_client() as c:
            yield c
        rate_limit_store.clear()

    def test_shape(self, client):
        queue_album(1, 120)
        seed_attempts(60, 24, now=time.time())
        resp = client.get("/api/queue/eta")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["state"] == "ok"
        assert data["queue"]["tracks"] == 120
        assert data["eta"]["low"] <= data["eta"]["seconds"] <= data["eta"]["high"]
        assert set(data["rate"]["windows"]) == {"1h", "24h", "7d"}

    def test_empty_queue(self, client):
        data = client.get("/api/queue/eta").get_json()
        assert data["state"] == "empty"
        assert data["eta"] is None

    def test_rate_limited(self, client):
        codes = [client.get("/api/queue/eta").status_code for _ in range(12)]
        assert codes[0] == 200
        assert codes[-1] == 429

    def test_stats_are_unchanged(self, client):
        data = client.get("/api/stats").get_json()
        assert set(data) == {"in_queue", "queued", "active",
                             "downloaded_today", "queue_paused"}
