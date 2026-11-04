"""Optional VPN integration through gluetun.

Two independent parts, both off until configured:

* ``vpn_proxy_url`` -- gluetun's built-in HTTP proxy (``HTTPPROXY=on``). Every
  request this app makes *to YouTube* goes through it (yt-dlp, the preview
  stream, the YT Music cover page). Lidarr, Telegram and the web UI keep using
  the normal network, so nothing has to change in the container's networking.
* ``gluetun_control_url`` -- gluetun's control server. The app restarts the VPN
  (stop, then start) to get a new public IP: on request, after every N albums,
  or after YouTube blocked a request. A rotation only ever starts between
  albums, never while something is downloading.

The control server is reached with an API key (``X-API-Key``); its responses
are never echoed back to the browser, only values this module has validated.
"""

import ipaddress
import logging
import threading
import time

import requests
from flask import Blueprint, jsonify, request

from config import SECRET_MASK, load_config, parse_http_url
from utils import (
    InvalidRequestBody,
    _json_object,
    check_rate_limit,
    rate_limit_store,
)

logger = logging.getLogger(__name__)

bp = Blueprint("vpn", __name__)

# A rotation that fails or changes nothing must not be retried in a loop by
# the automatic triggers; a manual rotation ignores this.
AUTO_ROTATE_COOLDOWN = 300
# A block means this address is useless right now, so waiting five minutes
# for the next attempt only burns albums: it gets a shorter cooldown.
BLOCK_ROTATE_COOLDOWN = 60
# After this many rotations in a row that failed, the queue stops waiting for
# one (the VPN is broken: downloading on a refused IP beats not downloading).
MAX_HOLD_FAILURES = 3
STATE_TIMEOUT = 120  # to stop / to start again (a busy NAS took 60 s+ to stop)
IP_TIMEOUT = 30  # for gluetun to report the new public IP once it runs
ROTATE_ATTEMPTS = 2  # restarts when the IP stays unknown
_PUT_TIMEOUT = 30
_SETTLE_SECONDS = 3  # before believing a restart kept the same IP
_POLL_INTERVAL = 2
_CONTROL_TIMEOUT = 5
_STATUS_VALUES = frozenset({
    "running", "stopped", "starting", "stopping", "crashed",
})
# What YouTube answers when it refuses an address (normalised: lower case,
# typographic apostrophes folded to ASCII).
_BLOCK_MARKERS = (
    "confirm you're not a bot",  # not "sign in to confirm": age gates say that too
    "http error 429",
    "too many requests",
    # What yt-dlp reports once YouTube has throttled the whole session.
    "rate-limited by youtube",
)

_state_lock = threading.Lock()
_rotate_lock = threading.Lock()
_state = {
    "albums_since_rotation": 0,
    "block_seen": False,
    "blocks": 0,  # every block ever noted; never reset, only compared
    "last_attempt": 0.0,
    "last_rotation": None,
    "failed_rotations": 0,  # in a row; any success resets it
}


# -- configuration ---------------------------------------------------------

def _cfg_text(cfg, key):
    value = (cfg or {}).get(key)
    return value.strip() if isinstance(value, str) else ""


def proxy_url(cfg=None):
    """The proxy YouTube traffic goes through, or "" when unset."""
    return _cfg_text(cfg if cfg is not None else load_config(), "vpn_proxy_url")


def control_url(cfg=None):
    return _cfg_text(
        cfg if cfg is not None else load_config(), "gluetun_control_url",
    ).rstrip("/")


def ytdlp_opts(cfg=None):
    """Extra yt-dlp options that send its requests through the proxy."""
    url = proxy_url(cfg)
    return {"proxy": url} if url else {}


def requests_proxies(cfg=None):
    """``proxies=`` argument for ``requests`` (None = connect directly)."""
    url = proxy_url(cfg)
    return {"http": url, "https": url} if url else None


# -- gluetun control server ------------------------------------------------

class ControlError(Exception):
    """The control server could not be used; the message is safe to show."""


def _control(method, path, *, url, api_key, body=None, timeout=_CONTROL_TIMEOUT):
    if not url:
        raise ControlError("Gluetun control URL is not set")
    headers = {"Accept": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    session = requests.Session()
    session.trust_env = False  # the control server is on the local network
    try:
        resp = session.request(
            method, url + path, headers=headers, json=body,
            timeout=timeout, allow_redirects=False,
        )
    except (requests.RequestException, UnicodeError) as exc:
        logger.debug("Gluetun control %s %s failed: %s", method, path, exc)
        raise ControlError("Could not reach the gluetun control server") from None
    finally:
        session.close()
    if resp.status_code in (401, 403):
        raise ControlError(
            "Gluetun refused the request: check the API key and that its"
            " role allows this route"
        )
    if resp.status_code == 404:
        raise ControlError(
            "Gluetun does not know this route (is gluetun up to date?)"
        )
    if not 200 <= resp.status_code < 300:
        raise ControlError(f"Gluetun answered HTTP {resp.status_code}")
    try:
        return resp.json() if resp.content else {}
    except ValueError:
        raise ControlError("Gluetun sent an unexpected response") from None


def _creds(cfg, url=None, api_key=None):
    """(control url, api key) from explicit values, else the saved config."""
    return (
        (url if url is not None else control_url(cfg)),
        (api_key if api_key is not None else _cfg_text(cfg, "gluetun_api_key")),
    )


def public_ip(cfg=None, url=None, api_key=None):
    """Current public IP according to gluetun, or None when unknown."""
    cfg = cfg if cfg is not None else load_config()
    url, api_key = _creds(cfg, url, api_key)
    data = _control("GET", "/v1/publicip/ip", url=url, api_key=api_key)
    value = data.get("public_ip") if isinstance(data, dict) else None
    try:
        return str(ipaddress.ip_address(str(value).strip())) if value else None
    except ValueError:
        return None


def vpn_status(cfg=None, url=None, api_key=None):
    cfg = cfg if cfg is not None else load_config()
    url, api_key = _creds(cfg, url, api_key)
    data = _control("GET", "/v1/vpn/status", url=url, api_key=api_key)
    value = str(data.get("status", "")).lower() if isinstance(data, dict) else ""
    return value if value in _STATUS_VALUES else None


def _wait_for(check, timeout, deadline_error):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = check()
        except ControlError:
            result = None
        if result:
            return result
        time.sleep(_POLL_INTERVAL)
    raise ControlError(deadline_error)


def rotating():
    return _rotate_lock.locked()


def rotate(reason="manual", cfg=None, _holding_lock=False):
    """Restart the VPN and report the public IP before and after.

    Blocks until the tunnel is back. Returns ``{"success", "changed",
    "old_ip", "new_ip", "message"}``. A VPN that comes back on the same
    address is a success with ``changed`` False: gluetun may pick the same
    server again. When gluetun cannot report the new address (its one-off
    lookup failed), the VPN is restarted once more; if it still cannot, a
    working proxy makes it a success with ``changed`` None (unconfirmed).

    Whatever goes wrong, the attempt is recorded (so the cooldown applies)
    and a tunnel this call stopped is started again. ``_holding_lock`` is for
    a caller that already took ``_rotate_lock`` and hands it over.
    """
    cfg = cfg if cfg is not None else load_config()
    url, api_key = _creds(cfg)
    if not url:
        if _holding_lock:
            _rotate_lock.release()
        return _result(False, message="Gluetun control URL is not set")
    if not _holding_lock and not _rotate_lock.acquire(blocking=False):
        return _result(False, message="A rotation is already in progress")
    started = time.time()
    tunnel = {"stopped": False}
    old_ip = None
    try:
        try:
            old_ip = public_ip(cfg)
        except ControlError:
            pass  # the PUT below reports a real connection problem
        logger.info("Rotating the VPN IP (%s), current IP: %s", reason, old_ip or "unknown")
        new_ip = None
        try:
            for attempt in range(1, ROTATE_ATTEMPTS + 1):
                new_ip = _restart(cfg, url, api_key, tunnel)
                if new_ip:
                    break
                logger.warning(
                    "The VPN restarted but gluetun cannot tell its public IP"
                    " (attempt %d/%d)", attempt, ROTATE_ATTEMPTS,
                )
        except ControlError as exc:
            logger.warning("VPN rotation failed: %s", exc)
            _start_again(url, api_key, tunnel)
            return _record(_result(False, old_ip=old_ip, message=str(exc)), reason, started)
        except Exception:
            logger.exception("VPN rotation crashed")
            _start_again(url, api_key, tunnel)
            return _record(_result(
                False, old_ip=old_ip,
                message="Unexpected error while changing the IP (see the logs)",
            ), reason, started)
        if not new_ip:
            # The tunnel may well work: gluetun looks the address up once, and
            # that lookup can fail while its DNS is still starting.
            proxy = proxy_url(cfg)
            if proxy and _test_proxy(proxy)["ok"]:
                logger.warning("VPN restarted, new IP unconfirmed; YouTube is reachable")
                return _record(_result(
                    True, changed=None, old_ip=old_ip,
                    message=(
                        "The VPN restarted and YouTube is reachable through"
                        " the proxy, but gluetun could not report the new IP."
                    ),
                ), reason, started)
            message = "The VPN restarted but its public IP is still unknown"
            logger.warning("VPN rotation failed: %s", message)
            return _record(_result(False, old_ip=old_ip, message=message), reason, started)
        if old_ip and new_ip == old_ip:
            # gluetun may still be reporting the address it looked up before
            # the restart: read it once more before saying nothing changed.
            time.sleep(_SETTLE_SECONDS)
            try:
                new_ip = public_ip(cfg) or new_ip
            except ControlError:
                pass
        changed = bool(old_ip and new_ip and old_ip != new_ip)
        if old_ip and new_ip and not changed:
            message = (
                "The VPN restarted but kept the same IP; gluetun picked the"
                " same server. List several locations in its server filter"
                " (SERVER_REGIONS, SERVER_COUNTRIES or SERVER_CITIES,"
                " depending on the provider) to get a different one."
            )
            logger.warning("VPN rotation kept the same IP (%s)", new_ip)
        else:
            message = f"New IP: {new_ip}"
            logger.info("VPN IP rotated: %s -> %s", old_ip or "unknown", new_ip)
        return _record(
            _result(True, changed=changed, old_ip=old_ip, new_ip=new_ip, message=message),
            reason, started,
        )
    except Exception:  # never let a bug here reach the queue thread
        logger.exception("VPN rotation crashed")
        _start_again(url, api_key, tunnel)
        return _record(_result(
            False, old_ip=old_ip,
            message="Unexpected error while changing the IP (see the logs)",
        ), reason, started)
    finally:
        _rotate_lock.release()


def _restart(cfg, url, api_key, tunnel):
    """One stop/start cycle. Returns the new public IP, None when unknown.

    ``tunnel["stopped"]`` is True while this call is holding the VPN down.
    """
    tunnel["stopped"] = True
    _control(
        "PUT", "/v1/vpn/status", url=url, api_key=api_key,
        body={"status": "stopped"}, timeout=_PUT_TIMEOUT,
    )
    _wait_for(
        lambda: vpn_status(cfg, url, api_key) == "stopped", STATE_TIMEOUT,
        "The VPN did not stop in time",
    )
    _control(
        "PUT", "/v1/vpn/status", url=url, api_key=api_key,
        body={"status": "running"}, timeout=_PUT_TIMEOUT,
    )
    tunnel["stopped"] = False
    _wait_for(
        lambda: vpn_status(cfg, url, api_key) == "running", STATE_TIMEOUT,
        "The VPN did not come back in time",
    )
    try:
        return _wait_for(lambda: public_ip(cfg, url, api_key), IP_TIMEOUT, "unknown")
    except ControlError:
        return None


def _start_again(url, api_key, tunnel):
    """Best effort: a failed rotation must not leave the tunnel stopped."""
    if not tunnel.get("stopped"):
        return
    try:
        _control(
            "PUT", "/v1/vpn/status", url=url, api_key=api_key,
            body={"status": "running"}, timeout=_PUT_TIMEOUT,
        )
        tunnel["stopped"] = False
        logger.info("The VPN was started again after the failed rotation")
    except Exception as exc:  # the caller already reports the real failure
        logger.warning("Could not start the VPN again: %s", exc)


def _result(success, changed=False, old_ip=None, new_ip=None, message=""):
    return {
        "success": success, "changed": changed,
        "old_ip": old_ip, "new_ip": new_ip, "message": message,
    }


def _record(result, reason, started):
    with _state_lock:
        _state["last_rotation"] = {
            **result, "reason": reason, "at": started,
            "seconds": round(time.time() - started, 1),
        }
        _state["last_attempt"] = time.time()
        if result["success"]:
            # A failed attempt keeps the triggers: after the cooldown the
            # next idle moment tries again instead of forgetting the block.
            _state["albums_since_rotation"] = 0
            _state["failed_rotations"] = 0
            # A restart that came back on the very same address did not
            # answer the block: it stays pending for the next attempt.
            same_ip = bool(
                result.get("old_ip") and result.get("old_ip") == result.get("new_ip")
            )
            if not same_ip:
                _state["block_seen"] = False
        else:
            _state["failed_rotations"] += 1
    return result


# -- automatic triggers ----------------------------------------------------

def is_block(message):
    """True when ``message`` is YouTube refusing the address (no side effects)."""
    low = str(message or "").lower().replace("’", "'")
    return any(marker in low for marker in _BLOCK_MARKERS)


def note_block(message):
    """Remember that YouTube refused a request (called from the downloader)."""
    if is_block(message):
        if not control_url():
            return False  # nothing to rotate with: do not keep a stale flag
        with _state_lock:
            _state["block_seen"] = True
            _state["blocks"] += 1
        return True
    return False


def blocks_seen():
    """How many blocks ``note_block`` has recorded.

    Take it before a piece of work and again after: a bigger number means
    YouTube refused a request in between. It is a counter rather than the
    ``block_seen`` flag, which stays set until a rotation succeeds.
    """
    with _state_lock:
        return _state["blocks"]


def rotates_on_block(cfg=None):
    """True when a block will be answered with a new IP.

    Only then is a failed track the IP's fault rather than the track's.
    """
    cfg = cfg if cfg is not None else load_config()
    return bool(control_url(cfg)) and bool(cfg.get("vpn_rotate_on_block", True))


def note_album_finished():
    with _state_lock:
        _state["albums_since_rotation"] += 1


def due_reason(cfg=None, now=None):
    """Why an automatic rotation should happen now, or None."""
    cfg = cfg if cfg is not None else load_config()
    if not control_url(cfg):
        return None
    now = time.time() if now is None else now
    with _state_lock:
        since = now - _state["last_attempt"]
        if cfg.get("vpn_rotate_on_block", True) and _state["block_seen"]:
            return "YouTube blocked a request" if since >= BLOCK_ROTATE_COOLDOWN else None
        if since < AUTO_ROTATE_COOLDOWN:
            return None
        every = int(cfg.get("vpn_rotate_every_albums") or 0)
        if every > 0 and _state["albums_since_rotation"] >= every:
            return f"every {every} album(s)"
    return None


def block_pending(cfg=None):
    """True while a block waits for the rotation that will answer it.

    Unlike ``due_reason`` it ignores the cooldown: inside it nothing may start
    on the refused address (an album would only fail on it, and be lost to the
    scheduler's retry window). It lets go after ``MAX_HOLD_FAILURES`` failed
    rotations in a row, so a broken VPN cannot freeze the queue.
    """
    cfg = cfg if cfg is not None else load_config()
    if not rotates_on_block(cfg):
        return False
    with _state_lock:
        return (
            _state["block_seen"]
            and _state["failed_rotations"] < MAX_HOLD_FAILURES
        )


def rotate_if_due(cfg=None, busy=None):
    """Rotate when a trigger fired. Call only while no download is running.

    ``busy`` is asked again once the rotation lock is held (and so
    ``rotating()`` is true, which makes every download entry point refuse): a
    download that started after the caller last looked is seen here, and the
    rotation is left for the next check.
    """
    cfg = cfg if cfg is not None else load_config()
    reason = due_reason(cfg)
    if not reason:
        return None
    if busy is None:
        return rotate(reason, cfg)
    if not _rotate_lock.acquire(blocking=False):
        return None
    if busy():
        _rotate_lock.release()
        return None
    return rotate(reason, cfg, True)


def defer():
    """Count a failed check as an attempt, so the cooldown applies to it."""
    with _state_lock:
        _state["last_attempt"] = time.time()


def reset_state():
    """Forget counters and history (used by tests)."""
    with _state_lock:
        _state.update(
            albums_since_rotation=0, block_seen=False,
            last_attempt=0.0, last_rotation=None, failed_rotations=0,
        )


# -- HTTP API --------------------------------------------------------------

def _json_error(message, status):
    return jsonify({"success": False, "message": message}), status


@bp.route("/api/vpn/status")
def api_vpn_status():
    client_ip = request.remote_addr or "unknown"
    if not check_rate_limit(
        f"vpn_status:{client_ip}", rate_limit_store, window=10, max_requests=30,
    ):
        return _json_error("Too many requests", 429)
    cfg = load_config()
    out = {
        "proxy_configured": bool(proxy_url(cfg)),
        "control_configured": bool(control_url(cfg)),
        "rotating": rotating(),
        "public_ip": None,
        "vpn_status": None,
        "error": "",
        "albums_since_rotation": 0,
        "block_seen": False,
        "last_rotation": None,
    }
    with _state_lock:
        out["albums_since_rotation"] = _state["albums_since_rotation"]
        out["block_seen"] = _state["block_seen"]
        out["last_rotation"] = _state["last_rotation"]
    if out["control_configured"] and not out["rotating"]:
        try:
            out["vpn_status"] = vpn_status(cfg)
            out["public_ip"] = public_ip(cfg)
        except ControlError as exc:
            out["error"] = str(exc)
    return jsonify(out)


@bp.route("/api/vpn/test", methods=["POST"])
def api_vpn_test():
    """Check the proxy and the control server, optionally with unsaved values."""
    client_ip = request.remote_addr or "unknown"
    if not check_rate_limit(
        f"vpn_test:{client_ip}", rate_limit_store, window=10, max_requests=3,
    ):
        return _json_error("Too many requests", 429)
    payload = _json_object()
    cfg = load_config()

    def field(key):
        value = payload.get(key)
        if value is not None and not isinstance(value, str):
            raise InvalidRequestBody(f"{key} must be a string")
        return value.strip() if isinstance(value, str) else None

    proxy, control, api_key = (
        field("proxy_url"), field("control_url"), field("api_key"),
    )
    # A field that is absent, or still shows the mask, means "the saved one".
    if proxy is None or proxy == SECRET_MASK:
        proxy = proxy_url(cfg)
    if control is None:
        control = control_url(cfg)
    try:
        proxy, control = parse_http_url(proxy), parse_http_url(control)
    except ValueError as exc:
        return _json_error(str(exc), 400)
    control = control.rstrip("/")
    # The saved key only ever goes to the saved control server.
    if api_key is None or api_key == SECRET_MASK:
        api_key = (
            _cfg_text(cfg, "gluetun_api_key") if control == control_url(cfg) else ""
        )

    result = {"success": True, "proxy": None, "control": None}
    if proxy:
        result["proxy"] = _test_proxy(proxy)
    if control:
        result["control"] = _test_control(control, api_key)
    if not proxy and not control:
        return _json_error("Nothing to test: set the proxy and/or control URL", 400)
    result["success"] = all(
        part["ok"] for part in (result["proxy"], result["control"]) if part
    )
    return jsonify(result)


def _test_proxy(url):
    try:
        resp = requests.get(
            "https://www.youtube.com/generate_204", timeout=10,
            proxies={"http": url, "https": url}, allow_redirects=False,
        )
    except requests.RequestException as exc:
        logger.debug("VPN proxy test failed: %s", exc)
        return {"ok": False, "message": "Could not reach YouTube through the proxy"}
    if resp.status_code in (200, 204):
        return {"ok": True, "message": "YouTube is reachable through the proxy"}
    if resp.status_code == 407:
        return {"ok": False, "message": "The proxy asked for a username and password"}
    return {"ok": False, "message": f"The proxy answered HTTP {resp.status_code}"}


def _test_control(url, api_key):
    try:
        ip = public_ip(cfg={}, url=url, api_key=api_key)
        status = vpn_status(cfg={}, url=url, api_key=api_key)
    except ControlError as exc:
        return {"ok": False, "message": str(exc), "public_ip": None}
    ok = status == "running"
    return {
        "ok": ok, "public_ip": ip,
        "message": (
            f"Connected, VPN {status or 'status unknown'}"
            + (f", public IP {ip}" if ip else "")
        ),
    }


@bp.route("/api/vpn/rotate", methods=["POST"])
def api_vpn_rotate():
    """Start a rotation in the background (it can take minutes).

    The lock is taken here, before anything else, so ``rotating()`` is true
    from the moment this answers and nothing automatic starts meanwhile; the
    caller follows the outcome with GET /api/vpn/status (``rotating``, then
    ``last_rotation``).
    """
    from processing import any_download_active

    client_ip = request.remote_addr or "unknown"
    if not check_rate_limit(
        f"vpn_rotate:{client_ip}", rate_limit_store, window=30, max_requests=2,
    ):
        return _json_error("Too many requests", 429)
    cfg = load_config()
    if not control_url(cfg):
        return _json_error("Set the gluetun control URL first", 400)
    if not _rotate_lock.acquire(blocking=False):
        return _json_error("The IP is already being changed", 409)
    try:
        if any_download_active():
            _rotate_lock.release()
            return _json_error(
                "A download is in progress. Change the IP when it has finished.", 409,
            )
        threading.Thread(
            target=rotate, args=("manual", cfg, True), daemon=True,
        ).start()
    except Exception:
        if _rotate_lock.locked():
            _rotate_lock.release()
        raise
    return jsonify({"success": True, "started": True}), 202
