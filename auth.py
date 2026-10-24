import hmac
import logging
import os
import secrets
import urllib.parse

from flask import (
    Blueprint,
    jsonify,
    redirect,
    render_template,
    request,
    session,
)
from werkzeug.security import check_password_hash, generate_password_hash

from config import CONFIG_FILE, load_config, update_config
from utils import _json_object, _str_field, check_rate_limit, rate_limit_store

logger = logging.getLogger(__name__)

bp = Blueprint("auth", __name__)

SESSION_DAYS = 30
MIN_PASSWORD_LENGTH = 8
USERNAME_MAX_LENGTH = 64

PUBLIC_PATHS = frozenset({
    "/login", "/api/auth/login", "/manifest.webmanifest", "/sw.js",
    "/favicon.ico", "/health", "/api/health",
})
PUBLIC_PREFIXES = ("/static/", "/api/newznab/", "/api/sabnzbd/")


def _secret_key_path():
    return os.path.join(os.path.dirname(CONFIG_FILE), "secret_key")


def _load_secret_key():
    path = _secret_key_path()
    try:
        with open(path, "rb") as f:
            key = f.read().strip()
        if len(key) >= 32:
            return key
    except OSError:
        pass
    key = secrets.token_hex(32).encode()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key)
    except OSError as e:
        logger.warning(
            "Cannot save the session key (%s); logins end on restart", e,
        )
    return key


def _env_credentials():
    username = (os.getenv("AUTH_USERNAME") or "").strip()
    password = os.getenv("AUTH_PASSWORD") or ""
    if username and password:
        return username, password
    return None


def auth_settings(cfg=None):
    env = _env_credentials()
    if env:
        return {"enabled": True, "username": env[0], "from_env": True}
    cfg = cfg or load_config()
    enabled = (
        cfg.get("auth_enabled") is True
        and bool(cfg.get("auth_username"))
        and bool(cfg.get("auth_password_hash"))
    )
    return {
        "enabled": enabled,
        "username": cfg.get("auth_username") or "",
        "from_env": False,
    }


def _password_ok(username, password, cfg=None):
    env = _env_credentials()
    if env:
        return hmac.compare_digest(username, env[0]) and hmac.compare_digest(
            password.encode(), env[1].encode(),
        )
    cfg = cfg or load_config()
    stored_hash = cfg.get("auth_password_hash") or ""
    if not stored_hash or not hmac.compare_digest(
        username, cfg.get("auth_username") or "",
    ):
        return False
    try:
        return check_password_hash(stored_hash, password)
    except (ValueError, TypeError):
        return False


def _api_key_ok(cfg):
    supplied = (
        request.headers.get("X-Api-Key")
        or request.args.get("apikey")
        or ""
    ).strip()
    expected = (cfg.get("auth_api_key") or "").strip()
    return bool(supplied and expected) and hmac.compare_digest(supplied, expected)


def _session_ok(settings):
    return bool(session.get("user")) and session.get("user") == settings["username"]


def _is_public(path):
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def _wants_html():
    if request.path.startswith("/api/"):
        return False
    return request.method == "GET"


def _safe_next(target):
    if not target or not target.startswith("/") or target.startswith("//"):
        return "/"
    parsed = urllib.parse.urlparse(target)
    if parsed.scheme or parsed.netloc or target.startswith("/login"):
        return "/"
    return target


def require_login():
    if _is_public(request.path):
        return None
    cfg = load_config()
    settings = auth_settings(cfg)
    if not settings["enabled"]:
        return None
    if _session_ok(settings) or _api_key_ok(cfg):
        return None
    if _wants_html():
        nxt = request.full_path if request.query_string else request.path
        return redirect("/login?next=" + urllib.parse.quote(nxt, safe=""))
    response = jsonify({"success": False, "message": "Login required"})
    response.headers["X-Login-Required"] = "1"
    return response, 401


def init_app(app):
    app.secret_key = _load_secret_key()
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=SESSION_DAYS * 86400,
    )
    app.before_request(require_login)
    app.register_blueprint(bp)


@bp.route("/login")
def login_page():
    if not auth_settings()["enabled"]:
        return redirect("/")
    return render_template(
        "login.html", next_url=_safe_next(request.args.get("next", "")),
    )


@bp.route("/api/auth/login", methods=["POST"])
def api_login():
    client_ip = request.remote_addr or "unknown"
    if not check_rate_limit(
        f"login:{client_ip}", rate_limit_store, window=60, max_requests=8,
    ):
        return jsonify({
            "success": False, "message": "Too many attempts, wait a minute",
        }), 429
    data = _json_object()
    username = (_str_field(data, "username") or "").strip()
    password = _str_field(data, "password") or ""
    settings = auth_settings()
    if not settings["enabled"]:
        return jsonify({"success": True, "next": "/"})
    if not _password_ok(username, password):
        logger.warning("Failed login for '%s' from %s", username[:64], client_ip)
        return jsonify({
            "success": False, "message": "Wrong username or password",
        }), 401
    session.clear()
    session["user"] = settings["username"]
    session.permanent = data.get("remember", True) is not False
    return jsonify({
        "success": True, "next": _safe_next(_str_field(data, "next") or ""),
    })


@bp.route("/logout", methods=["POST"])
@bp.route("/api/auth/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"success": True})


@bp.route("/api/auth/status")
def api_auth_status():
    cfg = load_config()
    settings = auth_settings(cfg)
    return jsonify({
        **settings,
        "logged_in": _session_ok(settings),
        "api_key": cfg.get("auth_api_key") or "",
    })


def _validate_credentials(username, password):
    if not username or len(username) > USERNAME_MAX_LENGTH:
        return "Enter a username (up to 64 characters)."
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"The password needs at least {MIN_PASSWORD_LENGTH} characters."
    return None


@bp.route("/api/auth/setup", methods=["POST"])
def api_auth_setup():
    client_ip = request.remote_addr or "unknown"
    if not check_rate_limit(
        f"auth_setup:{client_ip}", rate_limit_store, window=10, max_requests=3,
    ):
        return jsonify({"success": False, "message": "Too many requests"}), 429
    if _env_credentials():
        return jsonify({
            "success": False,
            "message": "Login is set by AUTH_USERNAME / AUTH_PASSWORD",
        }), 409
    data = _json_object()
    username = (_str_field(data, "username") or "").strip()
    password = _str_field(data, "password") or ""
    current = auth_settings()
    if current["enabled"] and not password and username:
        password = None
    else:
        error = _validate_credentials(username, password)
        if error:
            return jsonify({"success": False, "message": error}), 400

    def mutate(cfg):
        cfg["auth_enabled"] = True
        cfg["auth_username"] = username
        if password is not None:
            cfg["auth_password_hash"] = generate_password_hash(password)
        if not cfg.get("auth_api_key"):
            cfg["auth_api_key"] = secrets.token_hex(16)

    update_config(mutate)
    session.clear()
    session["user"] = username
    session.permanent = True
    logger.info("Login enabled for '%s'", username)
    return jsonify({"success": True})


@bp.route("/api/auth/disable", methods=["POST"])
def api_auth_disable():
    if _env_credentials():
        return jsonify({
            "success": False,
            "message": "Login is set by AUTH_USERNAME / AUTH_PASSWORD",
        }), 409

    def mutate(cfg):
        cfg["auth_enabled"] = False

    update_config(mutate)
    logger.info("Login disabled")
    return jsonify({"success": True})


@bp.route("/api/auth/api-key", methods=["POST"])
def api_auth_regenerate_key():
    key = secrets.token_hex(16)

    def mutate(cfg):
        cfg["auth_api_key"] = key

    update_config(mutate)
    return jsonify({"success": True, "api_key": key})
