"""Explore routes: the /explore page, its /api/explore/* API and artist imports.

The catalog logic lives in explore.py; this blueprint parses requests, applies
the per-endpoint rate-limit buckets and runs the in-memory artist-import jobs.
"""

import logging
import os
import threading
import time

from flask import Blueprint, jsonify, render_template, request

import db
import explore
import logutil
import models
from config import load_config
from utils import (
    BaseNotMountedError,
    _json_object,
    _makedirs_bases_for,
    _resolve_write_base,
    _str_field,
    _validate_target_path,
    check_rate_limit,
    makedirs_safe,
    rate_limit_store,
    sanitize_filename,
    set_permissions,
)
from youtube_import import (
    _ensure_artist_dir_image,
    _execute_playlist_download,
    _release_folder,
    _start_playlist_import,
)

logger = logging.getLogger(__name__)

bp = Blueprint("explore_routes", __name__)


@bp.route("/explore")
def explore_page():
    return render_template("explore.html")


def _explore_error(e):
    return jsonify({"success": False, "message": e.message}), e.status


def _explore_limited(bucket, window=10, max_requests=30):
    client_ip = request.remote_addr or "unknown"
    return not check_rate_limit(
        f"explore_{bucket}:{client_ip}", rate_limit_store,
        window=window, max_requests=max_requests,
    )


def _explore_too_many():
    return jsonify(
        {"success": False, "message": "Too many requests, please slow down"}
    ), 429


def _explore_json(loader, bucket="browse", window=10, max_requests=30):
    if _explore_limited(bucket, window, max_requests):
        return _explore_too_many()
    try:
        return jsonify({"success": True, **loader()})
    except explore.ExploreError as e:
        return _explore_error(e)
    except Exception as e:
        logger.warning("Explore request failed: %s", e)
        return jsonify(
            {"success": False, "message": "YouTube Music is not responding."}
        ), 502


def _explore_home():
    data = explore.home(request.args.get("country", ""))
    if not data["sections"] and not data["moods"]:
        raise explore.ExploreError(
            "YouTube Music is not responding right now. Try again in a minute.",
            502,
        )
    return data


@bp.route("/api/explore/home")
def api_explore_home():
    return _explore_json(_explore_home)


def _explore_charts():
    data = explore.charts(request.args.get("country", ""))
    if not data:
        raise explore.ExploreError("Charts are not available right now.", 502)
    return data


@bp.route("/api/explore/charts")
def api_explore_charts():
    return _explore_json(_explore_charts)


@bp.route("/api/explore/moods")
def api_explore_moods():
    return _explore_json(lambda: {"categories": explore.mood_categories()})


@bp.route("/api/explore/mood/<params>")
def api_explore_mood(params):
    return _explore_json(lambda: {"items": explore.mood_playlists(params)})


@bp.route("/api/explore/artist/<browse_id>")
def api_explore_artist(browse_id):
    return _explore_json(lambda: {"artist": explore.artist(browse_id)})


@bp.route("/api/explore/album/<browse_id>")
def api_explore_album(browse_id):
    return _explore_json(lambda: {"album": explore.album(browse_id)})


@bp.route("/api/explore/playlist/<playlist_id>")
def api_explore_playlist(playlist_id):
    return _explore_json(lambda: {"playlist": explore.playlist(playlist_id)})


@bp.route("/api/explore/search")
def api_explore_search():
    return _explore_json(
        lambda: explore.search(request.args.get("q", "")),
        bucket="search", window=10, max_requests=15,
    )


@bp.route("/api/explore/suggestions")
def api_explore_suggestions():
    return _explore_json(
        lambda: {"suggestions": explore.suggestions(request.args.get("q", ""))},
        bucket="suggest", window=10, max_requests=40,
    )


@bp.route("/api/explore/match")
def api_explore_match():
    return _explore_json(
        lambda: {"match": explore.match(
            request.args.get("kind", ""), request.args.get("id", ""),
            refresh=request.args.get("refresh") == "1",
        )},
        bucket="match", window=10, max_requests=20,
    )


@bp.route("/api/explore/add", methods=["POST"])
def api_explore_add():
    payload = _json_object()
    return _explore_json(
        lambda: {"result": explore.add(payload)},
        bucket="add", window=10, max_requests=5,
    )


@bp.route("/api/explore/import", methods=["POST"])
def api_explore_import():
    if _explore_limited("import", window=10, max_requests=2):
        return _explore_too_many()
    payload = _json_object()
    try:
        plan = explore.import_plan(payload)
    except explore.ExploreError as e:
        return _explore_error(e)
    target = None
    if payload.get("kind") == "album":
        base = _artist_library_base(load_config())
        if base:
            target = os.path.join(
                base, sanitize_filename(plan["artist_name"]) or "Unknown artist",
                _release_folder(plan["album_title"], plan.get("year")),
            )
    return _start_playlist_import(
        plan["artist_name"], plan["album_title"], plan["entries"],
        plan["thumbnail_url"], plan["source_url"],
        release_year=plan.get("year") or "", target_path=target,
    )


_artist_imports = {}
_artist_imports_lock = threading.Lock()


def _artist_library_base(config):
    return (config.get("lidarr_path") or "").strip() or _resolve_write_base(config)


def _artist_import_state(channel_id, **fields):
    with _artist_imports_lock:
        state = _artist_imports.setdefault(channel_id, {})
        state.update(fields)
        return dict(state)


def _run_artist_import(plan, artist_dir, config):
    channel_id = plan["artist_id"]
    artist_name = plan["artist_name"]
    done = failed = 0
    try:
        try:
            makedirs_safe(artist_dir, _makedirs_bases_for(artist_dir, config))
        except (BaseNotMountedError, PermissionError) as exc:
            logger.error("Artist folder unusable: %s", exc)
            _artist_import_state(channel_id, state="failed", message=str(exc)[:200])
            return
        set_permissions(artist_dir)
        _ensure_artist_dir_image(artist_dir, artist_name, [plan.get("artist_image") or ""])
        logutil.milestone(
            logger, "Explore: adding %s from YouTube (%d releases) into %s",
            artist_name, len(plan["releases"]), artist_dir, icon=logutil.ICON_APP,
        )
        for release in plan["releases"]:
            if _artist_import_state(channel_id).get("stop"):
                break
            _artist_import_state(channel_id, current=release["title"])
            try:
                job = explore.release_import(release, artist_name)
            except explore.ExploreError as e:
                failed += 1
                logger.warning(
                    "   Explore: skipped %s — %s (%s)", artist_name, release["title"], e.message,
                )
                _artist_import_state(channel_id, failed=failed)
                continue
            target = os.path.join(artist_dir, _release_folder(job["album_title"], job.get("year")))
            if not _validate_target_path(target, config):
                failed += 1
                _artist_import_state(channel_id, failed=failed)
                continue
            while True:
                if _artist_import_state(channel_id).get("stop"):
                    break
                ran = _execute_playlist_download(
                    artist_name, job["album_title"], job["entries"], target, config,
                    job.get("thumbnail_url") or "", job.get("source_url") or "",
                    job.get("year") or "",
                )
                if ran is not False:
                    break
            done += 1
            _artist_import_state(channel_id, done=done)
        stopped = _artist_import_state(channel_id).get("stop")
        _artist_import_state(
            channel_id, state="stopped" if stopped else "done", current="",
            message=f"{done} of {len(plan['releases'])} releases processed",
        )
    except Exception as e:
        logger.error("Explore artist import failed for %s: %s", artist_name, e)
        _artist_import_state(channel_id, state="failed", message=str(e)[:200])
    finally:
        db.close_db()


def _artist_import_plan(channel_id):
    page = explore.artist(channel_id)
    imported = models.get_imported_youtube_album_titles(page["name"])
    return explore.artist_import_plan(channel_id, already_imported=imported)


def _artist_dir(plan, config):
    base = _artist_library_base(config)
    if not base:
        raise explore.ExploreError("No music library or download path is configured.", 400)
    artist_dir = os.path.join(base, sanitize_filename(plan["artist_name"]) or "Unknown artist")
    if not _validate_target_path(artist_dir, config):
        raise explore.ExploreError("Invalid target path.", 400)
    return artist_dir


def _plan_summary(plan, artist_dir):
    return {
        "artist": plan["artist_name"],
        "mode": plan["mode"],
        "path": artist_dir,
        "total": len(plan["releases"]),
        "releases": [
            {"title": r["title"], "year": r.get("year") or "", "type": r.get("type") or ""}
            for r in plan["releases"]
        ],
        "skipped": plan["skipped"],
    }


@bp.route("/api/explore/import-artist/plan")
def api_explore_import_artist_plan():
    def loader():
        plan = _artist_import_plan(request.args.get("id", ""))
        return _plan_summary(plan, _artist_dir(plan, load_config()))

    return _explore_json(loader, bucket="artist_plan", window=10, max_requests=10)


@bp.route("/api/explore/import-artist", methods=["POST"])
def api_explore_import_artist():
    if _explore_limited("artist_import", window=10, max_requests=2):
        return _explore_too_many()
    data = _json_object()
    channel_id = _str_field(data, "id")
    try:
        plan = _artist_import_plan(channel_id)
        config = load_config()
        artist_dir = _artist_dir(plan, config)
    except explore.ExploreError as e:
        return _explore_error(e)
    if not plan["releases"]:
        return jsonify({
            "success": False,
            "message": (
                f"Everything from {plan['artist_name']} is already imported."
                if plan["skipped"] else
                f"YouTube Music lists nothing to import for {plan['artist_name']}."
            ),
        }), 409
    with _artist_imports_lock:
        current = _artist_imports.get(plan["artist_id"])
        if current and current.get("state") == "running":
            return jsonify({
                "success": False,
                "message": f"{plan['artist_name']} is already being imported.",
            }), 409
        _artist_imports[plan["artist_id"]] = {
            "state": "running", "artist": plan["artist_name"], "path": artist_dir,
            "total": len(plan["releases"]), "done": 0, "failed": 0, "current": "",
            "stop": False, "started": time.time(), "message": "",
        }
    threading.Thread(
        target=_run_artist_import, args=(plan, artist_dir, config),
        daemon=True, name=f"artist-import-{plan['artist_id']}",
    ).start()
    return jsonify({
        "success": True,
        "message": f"Adding {plan['artist_name']} from YouTube: {len(plan['releases'])} releases.",
        **_plan_summary(plan, artist_dir),
    })


@bp.route("/api/explore/import-artist/status")
def api_explore_import_artist_status():
    channel_id = request.args.get("id", "")
    with _artist_imports_lock:
        state = dict(_artist_imports.get(channel_id) or {})
    state.pop("stop", None)
    imported = []
    try:
        imported = models.get_imported_youtube_album_titles(explore.artist(channel_id)["name"])
    except explore.ExploreError as e:
        if e.status == 400:
            return _explore_error(e)
    return jsonify({"success": True, "job": state or None, "imported": len(imported)})


@bp.route("/api/explore/import-artist/stop", methods=["POST"])
def api_explore_import_artist_stop():
    channel_id = _str_field(_json_object(), "id")
    with _artist_imports_lock:
        state = _artist_imports.get(channel_id)
        if not state or state.get("state") != "running":
            return jsonify({"success": False, "message": "No import is running for this artist."}), 404
        state["stop"] = True
    return jsonify({"success": True, "message": "The import stops after the current release."})
