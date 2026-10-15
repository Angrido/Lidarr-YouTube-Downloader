import logging
import sqlite3

import pytest

import scheduler


class _StopLoop(BaseException):
    pass


def test_run_scheduler_survives_job_exception(monkeypatch, caplog):
    calls = []

    def fake_run_pending():
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        raise _StopLoop()

    monkeypatch.setattr(scheduler.schedule, "run_pending", fake_run_pending)
    monkeypatch.setattr(scheduler.time, "sleep", lambda s: None)
    with caplog.at_level(logging.ERROR, logger="scheduler"):
        with pytest.raises(_StopLoop):
            scheduler.run_scheduler()
    assert len(calls) == 2
    assert any(
        "database is locked" in (r.exc_text or "") or r.exc_info
        for r in caplog.records
    )


def test_scheduled_check_failure_keeps_job_scheduled(monkeypatch):
    import datetime

    import processing

    def broken():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setitem(processing.download_process, "active", False)
    monkeypatch.setattr(scheduler, "load_config", lambda: {})
    monkeypatch.setattr(scheduler, "get_missing_albums", broken)
    runner = scheduler.schedule.Scheduler()
    job = runner.every(60).minutes.do(scheduler.scheduled_check)
    job.next_run = datetime.datetime.now() - datetime.timedelta(seconds=1)
    runner.run_pending()
    assert job.next_run > datetime.datetime.now()


def test_auto_download_off_notifies_only_new_albums(monkeypatch):
    import processing

    albums = [
        {"id": 1, "missingTrackCount": 3},
        {"id": 2, "missingTrackCount": 1},
    ]
    sent = []
    monkeypatch.setitem(processing.download_process, "active", False)
    monkeypatch.setattr(scheduler, "_last_reported_ids", set())
    monkeypatch.setattr(
        scheduler,
        "load_config",
        lambda: {
            "scheduler_auto_download": False,
            "scheduler_retry_after_hours": 0,
        },
    )
    monkeypatch.setattr(scheduler, "get_missing_albums", lambda: list(albums))
    monkeypatch.setattr(
        scheduler.models, "get_history_album_ids_since", lambda ts: set()
    )
    monkeypatch.setattr(
        scheduler.models, "get_attempted_album_ids_since", lambda ts: set()
    )
    monkeypatch.setattr(scheduler.models, "get_queue", lambda: [])
    monkeypatch.setattr(
        scheduler, "send_notifications", lambda msg, **kw: sent.append(msg)
    )

    scheduler._scheduled_check()
    scheduler._scheduled_check()
    assert sent == ["Scheduler: Found 2 missing albums (Auto-DL Disabled)"]

    albums.append({"id": 3, "missingTrackCount": 2})
    scheduler._scheduled_check()
    assert sent[1:] == [
        "Scheduler: Found 1 missing albums (Auto-DL Disabled)"
    ]
