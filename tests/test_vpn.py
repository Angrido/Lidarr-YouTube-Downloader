"""Tests for the gluetun VPN integration (proxy + IP rotation).

A small HTTP server stands in for gluetun's control server, so rotation runs
end to end (real requests, real polling) without a VPN.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock, patch

import pytest

import config
import vpn
from db import close_db, init_db


class FakeGluetun:
    """Just enough of gluetun's control server: public IP and VPN status."""

    def __init__(self, ips=("10.0.0.1", "10.0.0.2", "10.0.0.3"), api_key="secret"):
        self.ips = list(ips)
        self.ip_index = 0
        self.api_key = api_key
        self.status = "running"
        self.requests = []  # (method, path, api key header, body)
        self.rotate_on_start = True
        self.put_status_code = 200
        self.blank_ip_starts = 0  # starts that come back without a public IP
        self.blank_ip = False
        self.fail_running_times = 0  # PUT running answers 500 this many times
        self.ignore_stop = False  # PUT stopped is accepted but nothing stops
        self.stale_reads = 0  # reads right after a start still give the old IP
        handler = self._make_handler()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True,
        ).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def _make_handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, code, payload=None, headers=None):
                body = json.dumps(payload).encode() if payload is not None else b""
                self.send_response(code)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _handle(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw) if raw else None
                key = self.headers.get("X-API-Key")
                fake.requests.append((method, self.path, key, body))
                if fake.api_key and key != fake.api_key:
                    return self._reply(401, {"error": "unauthorized"})
                if method == "GET" and self.path == "/v1/publicip/ip":
                    ip = "" if fake.blank_ip else fake.ips[fake.ip_index]
                    if fake.stale_reads > 0 and not fake.blank_ip:
                        fake.stale_reads -= 1
                        ip = fake.ips[(fake.ip_index - 1) % len(fake.ips)]
                    return self._reply(200, {"public_ip": ip})
                if method == "GET" and self.path == "/v1/vpn/status":
                    return self._reply(200, {"status": fake.status})
                if method == "PUT" and self.path == "/v1/vpn/status":
                    if fake.put_status_code != 200:
                        return self._reply(fake.put_status_code, {"error": "no"})
                    if body["status"] == "stopped" and fake.ignore_stop:
                        return self._reply(200, {"outcome": "stopping"})
                    if body["status"] == "running" and fake.fail_running_times > 0:
                        fake.fail_running_times -= 1
                        return self._reply(500, {"error": "boom"})
                    fake.status = body["status"]
                    if body["status"] == "running":
                        if fake.rotate_on_start:
                            fake.ip_index = (fake.ip_index + 1) % len(fake.ips)
                        fake.blank_ip = fake.blank_ip_starts > 0
                        fake.blank_ip_starts = max(0, fake.blank_ip_starts - 1)
                    return self._reply(200, {"outcome": fake.status})
                return self._reply(404, {"error": "not found"})

            def do_GET(self):
                self._handle("GET")

            def do_PUT(self):
                self._handle("PUT")

        return Handler


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr("config.CONFIG_FILE", str(tmp_path / "config.json"))
    monkeypatch.setattr("db.DB_PATH", str(tmp_path / "test.db"))
    for var in ("VPN_PROXY_URL", "GLUETUN_CONTROL_URL", "GLUETUN_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(vpn, "_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(vpn, "STATE_TIMEOUT", 3)
    monkeypatch.setattr(vpn, "IP_TIMEOUT", 0.3)
    monkeypatch.setattr(vpn, "_SETTLE_SECONDS", 0)
    init_db()
    vpn.reset_state()
    yield
    vpn.reset_state()
    close_db()


@pytest.fixture()
def gluetun():
    fake = FakeGluetun()
    yield fake
    fake.close()


def _set(**values):
    config.update_config(lambda cfg: cfg.update(values))


def _configure(gluetun, **extra):
    _set(gluetun_control_url=gluetun.url, gluetun_api_key=gluetun.api_key, **extra)


@pytest.fixture()
def client():
    from app import app

    app.config["TESTING"] = True  # nosemgrep
    from utils import rate_limit_store

    rate_limit_store.clear()
    with app.test_client() as c:
        yield c
    rate_limit_store.clear()


# -- proxy options -----------------------------------------------------------

class TestProxyOptions:
    def test_nothing_by_default(self):
        assert vpn.proxy_url() == ""
        assert vpn.ytdlp_opts() == {}
        assert vpn.requests_proxies() is None

    def test_proxy_reaches_ytdlp_and_requests(self):
        _set(vpn_proxy_url="http://gluetun:8888")
        assert vpn.ytdlp_opts() == {"proxy": "http://gluetun:8888"}
        assert vpn.requests_proxies() == {
            "http": "http://gluetun:8888", "https": "http://gluetun:8888",
        }

    def test_common_ytdlp_options_use_the_proxy(self):
        import downloader

        assert "proxy" not in downloader._build_common_opts()
        _set(vpn_proxy_url="http://gluetun:8888")
        assert downloader._build_common_opts()["proxy"] == "http://gluetun:8888"
        # album browsing is built on the same options
        assert downloader._ytdl_for_album_browse("android")["proxy"] == (
            "http://gluetun:8888"
        )

    def test_preview_stream_is_fetched_through_the_proxy(self, client):
        """googlevideo URLs are bound to the IP that resolved them."""
        import app as app_module

        _set(vpn_proxy_url="http://gluetun:8888")
        upstream = MagicMock(headers={}, status_code=200)
        upstream.iter_content.return_value = iter([b"x"])
        with patch("requests.get", return_value=upstream) as get:
            app_module._proxy_audio_stream(
                "https://rr1.googlevideo.com/a", {}, None,
            )
        assert get.call_args.kwargs["proxies"] == {
            "http": "http://gluetun:8888", "https": "http://gluetun:8888",
        }

    def test_preview_stream_goes_direct_without_a_proxy(self, client):
        import app as app_module

        upstream = MagicMock(headers={}, status_code=200)
        upstream.iter_content.return_value = iter([b"x"])
        with patch("requests.get", return_value=upstream) as get:
            app_module._proxy_audio_stream(
                "https://rr1.googlevideo.com/a", {}, None,
            )
        assert get.call_args.kwargs["proxies"] is None


# -- configuration ------------------------------------------------------------

class TestConfig:
    @pytest.mark.parametrize("key", ["vpn_proxy_url", "gluetun_control_url"])
    @pytest.mark.parametrize("raw,expected", [
        ("http://gluetun:8888", "http://gluetun:8888"),
        ("  https://vpn.lan:8443/  ", "https://vpn.lan:8443/"),
        ("", ""),
        (None, ""),
    ])
    def test_valid_urls(self, key, raw, expected):
        assert config.coerce_config_value(key, raw) == expected

    def test_the_proxy_url_may_carry_a_login(self):
        url = "http://user:pw@gluetun:8888"
        assert config.coerce_config_value("vpn_proxy_url", url) == url

    @pytest.mark.parametrize("raw", [
        "http://user:pw@gluetun:8000", "http://user@gluetun:8000",
        "http://:pw@gluetun:8000",
    ])
    def test_the_control_url_must_not_carry_credentials(self, raw):
        """It is not a masked secret: GET /api/config would show it."""
        with pytest.raises(ValueError):
            config.coerce_config_value("gluetun_control_url", raw)

    @pytest.mark.parametrize("raw", ["abc\u20ac", "a b", "k\u00e9y", "x" * 300, 5])
    def test_the_api_key_must_be_plain_ascii(self, raw):
        with pytest.raises(ValueError):
            config.coerce_config_value("gluetun_api_key", raw)

    def test_a_good_api_key_and_an_empty_one(self):
        assert config.coerce_config_value("gluetun_api_key", " Key_123-ok ") == "Key_123-ok"
        assert config.coerce_config_value("gluetun_api_key", "") == ""

    def test_the_album_interval_is_bounded(self):
        assert config.coerce_config_value("vpn_rotate_every_albums", 1000) == 1000
        with pytest.raises(ValueError):
            config.coerce_config_value("vpn_rotate_every_albums", 1001)

    def test_an_invalid_stored_value_warns_once(self, tmp_path, caplog):
        """Without a config file load_config() runs uncached on every call."""
        config._warned_invalid.discard("vpn_proxy_url")
        (tmp_path / "config.json").unlink(missing_ok=True)
        with patch.dict("os.environ", {"VPN_PROXY_URL": "gluetun:8888"}):
            with caplog.at_level("WARNING"):
                for _ in range(3):
                    config.invalidate_config_cache()
                    assert config.load_config()["vpn_proxy_url"] == ""
        assert caplog.text.count("Ignoring invalid vpn_proxy_url") == 1

    @pytest.mark.parametrize("key", ["vpn_proxy_url", "gluetun_control_url"])
    @pytest.mark.parametrize("raw", [
        "gluetun:8888", "socks5://gluetun:1080", "ftp://x", "http://",
        "http://a b", "http://h:99999", "http://h?x=1", "http://h#f", 7,
    ])
    def test_invalid_urls(self, key, raw):
        with pytest.raises(ValueError):
            config.coerce_config_value(key, raw)

    def test_rotate_every_albums(self):
        assert config.coerce_config_value("vpn_rotate_every_albums", "5") == 5
        assert config.coerce_config_value("vpn_rotate_every_albums", 0) == 0
        for bad in (-1, "x", True, 1.5):
            with pytest.raises(ValueError):
                config.coerce_config_value("vpn_rotate_every_albums", bad)

    def test_defaults(self):
        cfg = config.load_config()
        assert cfg["vpn_proxy_url"] == ""
        assert cfg["gluetun_control_url"] == ""
        assert cfg["gluetun_api_key"] == ""
        assert cfg["vpn_rotate_every_albums"] == 0
        assert cfg["vpn_rotate_on_block"] is True

    def test_environment(self, monkeypatch):
        monkeypatch.setenv("VPN_PROXY_URL", "http://gluetun:8888")
        monkeypatch.setenv("GLUETUN_CONTROL_URL", "http://gluetun:8000")
        monkeypatch.setenv("GLUETUN_API_KEY", "abc")
        cfg = config.load_config()
        assert cfg["vpn_proxy_url"] == "http://gluetun:8888"
        assert cfg["gluetun_control_url"] == "http://gluetun:8000"
        assert cfg["gluetun_api_key"] == "abc"

    def test_bad_stored_values_fall_back(self, tmp_path):
        (tmp_path / "config.json").write_text(json.dumps({
            "vpn_proxy_url": "javascript:alert(1)",
            "gluetun_control_url": "http://user:pw@gluetun:8000",
            "gluetun_api_key": "bad\u20ackey",
            "vpn_rotate_every_albums": -4,
        }))
        config.invalidate_config_cache()
        cfg = config.load_config()
        assert cfg["vpn_proxy_url"] == ""
        assert cfg["gluetun_control_url"] == ""
        assert cfg["gluetun_api_key"] == ""
        assert cfg["vpn_rotate_every_albums"] == 0

    def test_secrets_are_masked_and_round_trip(self, client):
        _set(vpn_proxy_url="http://u:pw@gluetun:8888", gluetun_api_key="abc",
             gluetun_control_url="http://gluetun:8000")
        public = client.get("/api/config").get_json()
        assert public["vpn_proxy_url"] == config.SECRET_MASK
        assert public["gluetun_api_key"] == config.SECRET_MASK
        assert public["gluetun_control_url"] == "http://gluetun:8000"
        export = client.get("/api/config/export").get_data(as_text=True)
        assert "pw@gluetun" not in export and "abc" not in export
        # saving the page with the mask still in the fields keeps the secrets
        resp = client.post("/api/config", json={
            "vpn_proxy_url": config.SECRET_MASK,
            "gluetun_api_key": config.SECRET_MASK,
        })
        assert resp.status_code == 200
        assert config.load_config()["vpn_proxy_url"] == "http://u:pw@gluetun:8888"
        assert config.load_config()["gluetun_api_key"] == "abc"

    def test_api_rejects_a_bad_url(self, client):
        resp = client.post("/api/config", json={"vpn_proxy_url": "socks5://x:1"})
        assert resp.status_code == 400


# -- control server ------------------------------------------------------------

class TestControlServer:
    def test_public_ip_and_status(self, gluetun):
        _configure(gluetun)
        assert vpn.public_ip() == "10.0.0.1"
        assert vpn.vpn_status() == "running"

    def test_sends_the_api_key(self, gluetun):
        _configure(gluetun)
        vpn.public_ip()
        assert gluetun.requests[-1][2] == "secret"

    def test_wrong_key_is_a_clear_error(self, gluetun):
        _set(gluetun_control_url=gluetun.url, gluetun_api_key="wrong")
        with pytest.raises(vpn.ControlError, match="API key"):
            vpn.public_ip()

    def test_unknown_route(self, gluetun):
        _configure(gluetun)
        with pytest.raises(vpn.ControlError, match="up to date"):
            vpn._control("GET", "/v1/nope", url=gluetun.url, api_key="secret")

    def test_an_odd_key_is_a_control_error_not_a_crash(self, gluetun):
        """http.client cannot send a non latin-1 header: UnicodeEncodeError."""
        with pytest.raises(vpn.ControlError):
            vpn._control("GET", "/v1/publicip/ip", url=gluetun.url, api_key="k\u20ac")

    def test_unreachable_server(self):
        _set(gluetun_control_url="http://127.0.0.1:9", gluetun_api_key="k")
        with pytest.raises(vpn.ControlError, match="Could not reach"):
            vpn.public_ip()

    def test_garbage_ip_is_ignored(self, gluetun):
        _configure(gluetun)
        gluetun.ips = ["<script>alert(1)</script>"]
        assert vpn.public_ip() is None

    def test_unknown_status_value_is_ignored(self, gluetun):
        _configure(gluetun)
        gluetun.status = "<b>pwned</b>"
        assert vpn.vpn_status() is None


# -- rotation --------------------------------------------------------------------

class TestRotate:
    def test_new_ip(self, gluetun):
        _configure(gluetun)
        result = vpn.rotate("manual")
        assert result["success"] and result["changed"]
        assert (result["old_ip"], result["new_ip"]) == ("10.0.0.1", "10.0.0.2")
        puts = [r for r in gluetun.requests if r[0] == "PUT"]
        assert [p[3] for p in puts] == [{"status": "stopped"}, {"status": "running"}]
        assert vpn.rotating() is False

    def test_same_ip_is_reported_honestly(self, gluetun):
        _configure(gluetun)
        gluetun.rotate_on_start = False
        result = vpn.rotate("manual")
        assert result["success"] is True
        assert result["changed"] is False
        assert "same IP" in result["message"]

    def test_wrong_key_fails_without_restarting_anything(self, gluetun):
        _set(gluetun_control_url=gluetun.url, gluetun_api_key="wrong")
        result = vpn.rotate("manual")
        assert result["success"] is False and "API key" in result["message"]
        assert not [r for r in gluetun.requests if r[0] == "PUT" and r[2] == "secret"]

    def test_vpn_that_never_comes_back(self, gluetun, monkeypatch):
        _configure(gluetun)
        monkeypatch.setattr(vpn, "STATE_TIMEOUT", 0.3)
        # PUT {"status": "running"} is accepted but the tunnel never comes up
        with patch.object(vpn, "vpn_status", lambda *a, **k: "stopped"):
            result = vpn.rotate("manual")
        assert result["success"] is False
        assert "did not" in result["message"]
        assert gluetun.requests  # the control server was contacted

    def test_restarts_again_when_gluetun_cannot_tell_the_ip(self, gluetun):
        """Its one-off IP lookup can fail while its DNS is still starting."""
        _configure(gluetun)
        gluetun.blank_ip_starts = 1
        result = vpn.rotate("manual")
        assert result["success"] and result["changed"]
        assert result["new_ip"] == "10.0.0.3"  # second restart, second IP
        puts = [r[3] for r in gluetun.requests if r[0] == "PUT"]
        assert puts == [{"status": "stopped"}, {"status": "running"}] * 2

    def test_unknown_ip_with_a_working_proxy_is_unconfirmed(self, gluetun):
        _configure(gluetun, vpn_proxy_url="http://gluetun:8888")
        gluetun.blank_ip_starts = 9
        with patch.object(vpn, "_test_proxy", return_value={"ok": True, "message": ""}):
            result = vpn.rotate("manual")
        assert result["success"] is True
        assert result["changed"] is None and result["new_ip"] is None
        assert "could not report" in result["message"]

    def test_unknown_ip_with_a_dead_proxy_fails(self, gluetun):
        _configure(gluetun, vpn_proxy_url="http://gluetun:8888")
        gluetun.blank_ip_starts = 9
        with patch.object(vpn, "_test_proxy", return_value={"ok": False, "message": ""}):
            result = vpn.rotate("manual")
        assert result["success"] is False
        assert "still unknown" in result["message"]

    def test_unknown_ip_without_a_proxy_fails(self, gluetun):
        _configure(gluetun)
        gluetun.blank_ip_starts = 9
        result = vpn.rotate("manual")
        assert result["success"] is False
        assert "still unknown" in result["message"]

    def test_a_failed_rotation_keeps_the_triggers(self, gluetun):
        """The block must not be forgotten: the next idle moment retries."""
        _set(gluetun_control_url=gluetun.url, gluetun_api_key="wrong")
        vpn.note_album_finished()
        vpn.note_block("HTTP Error 429: Too Many Requests")
        assert vpn.rotate("YouTube blocked a request")["success"] is False
        assert vpn._state["block_seen"] is True
        assert vpn._state["albums_since_rotation"] == 1
        assert vpn._state["last_attempt"] > 0
        later = vpn._state["last_attempt"] + vpn.AUTO_ROTATE_COOLDOWN + 1
        assert vpn.due_reason(now=later) == "YouTube blocked a request"

    def test_the_tunnel_is_started_again_when_the_restart_fails(self, gluetun):
        """A failure after PUT stopped must not leave the proxy dead."""
        _configure(gluetun)
        gluetun.fail_running_times = 1  # the first PUT running answers 500
        result = vpn.rotate("manual")
        assert result["success"] is False
        assert gluetun.status == "running"
        puts = [r[3] for r in gluetun.requests if r[0] == "PUT"]
        assert puts == [{"status": "stopped"}, {"status": "running"}, {"status": "running"}]

    def test_a_stop_that_never_happens_fails_without_leaving_anything_down(self, gluetun, monkeypatch):
        _configure(gluetun)
        monkeypatch.setattr(vpn, "STATE_TIMEOUT", 0.3)
        gluetun.ignore_stop = True
        result = vpn.rotate("manual")
        assert result["success"] is False and "did not stop" in result["message"]
        assert gluetun.status == "running"

    def test_an_unexpected_error_is_recorded_and_does_not_escape(self, gluetun):
        """Anything else must not reach the queue thread, and must cool down."""
        _configure(gluetun)
        vpn.note_block("HTTP Error 429: Too Many Requests")
        with patch.object(vpn, "_restart", side_effect=RuntimeError("boom")):
            result = vpn.rotate("YouTube blocked a request")
        assert result["success"] is False
        assert "Unexpected error" in result["message"]
        assert "boom" not in result["message"]
        assert vpn._state["last_attempt"] > 0
        assert vpn._state["block_seen"] is True  # kept for the next attempt
        assert vpn.due_reason() is None  # cooling down

    def test_an_unexpected_error_restarts_a_tunnel_it_stopped(self, gluetun):
        _configure(gluetun)

        def stop_then_crash(cfg, url, api_key, tunnel):
            vpn._control("PUT", "/v1/vpn/status", url=url, api_key=api_key,
                         body={"status": "stopped"})
            tunnel["stopped"] = True
            raise RuntimeError("boom")

        with patch.object(vpn, "_restart", side_effect=stop_then_crash):
            vpn.rotate("manual")
        assert gluetun.status == "running"

    def test_a_stale_ip_read_is_not_taken_for_the_same_ip(self, gluetun):
        """The first read after a start can still be the old address."""
        _configure(gluetun)
        gluetun.stale_reads = 1
        result = vpn.rotate("manual")
        assert result["success"] and result["changed"] is True
        assert result["new_ip"] == "10.0.0.2"

    def test_not_configured(self):
        result = vpn.rotate("manual")
        assert result["success"] is False
        assert "control URL" in result["message"]

    def test_only_one_rotation_at_a_time(self, gluetun):
        _configure(gluetun)
        assert vpn._rotate_lock.acquire(blocking=False)
        try:
            assert vpn.rotating() is True
            result = vpn.rotate("manual")
            assert result["success"] is False and "already" in result["message"]
        finally:
            vpn._rotate_lock.release()

    def test_a_rotation_resets_the_triggers_and_is_remembered(self, gluetun):
        _configure(gluetun)
        vpn.note_album_finished()
        vpn.note_block("HTTP Error 429: Too Many Requests")
        vpn.rotate("manual")
        assert vpn._state["albums_since_rotation"] == 0
        assert vpn._state["block_seen"] is False
        last = vpn._state["last_rotation"]
        assert last["reason"] == "manual" and last["new_ip"] == "10.0.0.2"


# -- automatic triggers -----------------------------------------------------------

class TestTriggers:
    @pytest.mark.parametrize("message", [
        "ERROR: [youtube] x: Sign in to confirm you're not a bot",
        "Sign in to confirm you’re not a bot",
        "HTTP Error 429: Too Many Requests",
        "unable to download: too many requests",
    ])
    def test_block_messages(self, message):
        _set(gluetun_control_url="http://gluetun:8000")
        assert vpn.note_block(message) is True

    @pytest.mark.parametrize("message", [
        "Video unavailable", "Requested format is not available",
        "HTTP Error 403: Forbidden", "", None,
    ])
    def test_other_errors_are_not_blocks(self, message):
        _set(gluetun_control_url="http://gluetun:8000")
        assert vpn.note_block(message) is False
        assert vpn.due_reason() is None

    @pytest.mark.parametrize("message", [
        "Sign in to confirm your age. This video may be inappropriate for some users.",
        "ERROR: [youtube] x: Sign in to confirm your age",
    ])
    def test_an_age_gate_is_not_a_block(self, message):
        """It also says "sign in to confirm": a new address would not help."""
        _set(gluetun_control_url="http://gluetun:8000")
        assert vpn.note_block(message) is False
        assert vpn.due_reason() is None

    def test_a_block_without_a_control_server_leaves_no_stale_flag(self):
        assert vpn.note_block("HTTP Error 429") is False
        _set(gluetun_control_url="http://gluetun:8000")
        assert vpn.due_reason() is None

    def test_rotates_after_a_block(self):
        _set(gluetun_control_url="http://gluetun:8000")
        vpn.note_block("HTTP Error 429: Too Many Requests")
        assert vpn.due_reason() == "YouTube blocked a request"

    def test_block_trigger_can_be_turned_off(self):
        _set(gluetun_control_url="http://gluetun:8000", vpn_rotate_on_block=False)
        vpn.note_block("HTTP Error 429: Too Many Requests")
        assert vpn.due_reason() is None

    def test_rotates_every_n_albums(self):
        _set(gluetun_control_url="http://gluetun:8000", vpn_rotate_every_albums=3)
        for _ in range(2):
            vpn.note_album_finished()
            assert vpn.due_reason() is None
        vpn.note_album_finished()
        assert vpn.due_reason() == "every 3 album(s)"

    def test_zero_means_never(self):
        _set(gluetun_control_url="http://gluetun:8000", vpn_rotate_every_albums=0)
        for _ in range(50):
            vpn.note_album_finished()
        assert vpn.due_reason() is None

    def test_nothing_without_a_control_server(self):
        _set(vpn_rotate_every_albums=1)
        vpn.note_album_finished()
        assert vpn.due_reason() is None

    def test_cooldown_stops_a_rotation_loop(self, gluetun):
        _configure(gluetun, vpn_rotate_every_albums=1)
        vpn.note_album_finished()
        assert vpn.rotate_if_due()["success"]
        vpn.note_album_finished()
        vpn.note_block("HTTP Error 429: Too Many Requests")
        assert vpn.due_reason() is None  # still cooling down
        assert vpn.due_reason(now=vpn._state["last_attempt"] + vpn.AUTO_ROTATE_COOLDOWN + 1) \
            == "YouTube blocked a request"

    def test_a_failed_rotation_also_cools_down(self):
        _set(gluetun_control_url="http://127.0.0.1:9", vpn_rotate_every_albums=1)
        vpn.note_album_finished()
        assert vpn.rotate_if_due()["success"] is False
        vpn.note_album_finished()
        assert vpn.due_reason() is None


    def test_each_block_is_counted(self):
        _set(gluetun_control_url="http://gluetun:8000")
        before = vpn.blocks_seen()
        vpn.note_block("HTTP Error 429: Too Many Requests")
        vpn.note_block("Sign in to confirm you\u2019re not a bot")
        vpn.note_block("Video unavailable")  # not a block
        assert vpn.blocks_seen() == before + 2

    def test_blocks_are_not_counted_without_a_control_server(self):
        before = vpn.blocks_seen()
        vpn.note_block("HTTP Error 429: Too Many Requests")
        assert vpn.blocks_seen() == before

    def test_the_count_survives_a_reset(self):
        """A run compares it before and after: it only ever goes up."""
        _set(gluetun_control_url="http://gluetun:8000")
        vpn.note_block("HTTP Error 429: Too Many Requests")
        before = vpn.blocks_seen()
        vpn.reset_state()
        assert vpn.blocks_seen() == before

    @pytest.mark.parametrize("values,expected", [
        ({}, False),
        ({"gluetun_control_url": "http://gluetun:8000"}, True),
        ({"gluetun_control_url": "http://gluetun:8000", "vpn_rotate_on_block": False}, False),
        ({"vpn_rotate_on_block": True}, False),
    ])
    def test_a_block_is_answered_with_a_new_ip_only_when_configured(self, values, expected):
        _set(**values)
        assert vpn.rotates_on_block() is expected


class TestRotateIfDueBusyCheck:
    """The dispatcher looked, then took the lock: a download may have started."""

    def test_busy_is_asked_with_the_lock_held(self, gluetun):
        _configure(gluetun, vpn_rotate_every_albums=1)
        vpn.note_album_finished()
        seen = []

        def busy():
            seen.append(vpn.rotating())  # downloads refuse to start from here
            return True

        assert vpn.rotate_if_due(busy=busy) is None
        assert seen == [True]
        assert not vpn.rotating()
        assert not [r for r in gluetun.requests if r[0] == "PUT"]
        assert vpn.due_reason() == "every 1 album(s)"  # still due next time

    def test_rotates_when_nothing_is_running(self, gluetun):
        _configure(gluetun, vpn_rotate_every_albums=1)
        vpn.note_album_finished()
        result = vpn.rotate_if_due(busy=lambda: False)
        assert result["success"] and result["changed"]
        assert not vpn.rotating()

    def test_a_rotation_already_running_is_left_alone(self, gluetun):
        _configure(gluetun, vpn_rotate_every_albums=1)
        vpn.note_album_finished()
        assert vpn._rotate_lock.acquire(blocking=False)
        try:
            assert vpn.rotate_if_due(busy=lambda: False) is None
        finally:
            vpn._rotate_lock.release()
        assert not [r for r in gluetun.requests if r[0] == "PUT"]

    def test_nothing_due_asks_nothing(self):
        def busy():
            raise AssertionError("asked although no rotation is due")

        assert vpn.rotate_if_due(busy=busy) is None


# -- the queue -------------------------------------------------------------------------

class TestQueueIntegration:
    @pytest.fixture()
    def processing(self, monkeypatch):
        import processing

        monkeypatch.setattr(processing, "automatic_downloads_held", lambda: False)
        return processing

    def test_rotates_before_the_next_album_when_idle(self, processing, monkeypatch):
        calls = []
        monkeypatch.setattr(processing.vpn, "due_reason", lambda cfg=None: "x")
        monkeypatch.setattr(processing.vpn, "rotate_if_due", lambda cfg=None, busy=None: calls.append(1))
        monkeypatch.setattr(processing, "any_download_active", lambda: False)

        def boom():
            raise AssertionError("an album was dispatched during the rotation")

        monkeypatch.setattr(processing.models, "get_queue", boom)
        processing._dispatch_next_from_queue()
        assert calls == [1]

    def test_never_rotates_during_a_download(self, processing, monkeypatch):
        calls = []
        monkeypatch.setattr(processing.vpn, "due_reason", lambda cfg=None: "x")
        monkeypatch.setattr(processing.vpn, "rotate_if_due", lambda cfg=None, busy=None: calls.append(1))
        monkeypatch.setattr(processing, "any_download_active", lambda: True)
        monkeypatch.setattr(processing.models, "get_queue", lambda: [])
        processing._dispatch_next_from_queue()
        assert calls == []

    def test_rotates_when_idle_even_with_an_empty_queue(self, processing, monkeypatch):
        """A playlist import after a block does not go through the queue."""
        calls = []
        monkeypatch.setattr(processing.vpn, "due_reason", lambda cfg=None: "x")
        monkeypatch.setattr(processing.vpn, "rotate_if_due", lambda cfg=None, busy=None: calls.append(1))
        monkeypatch.setattr(processing, "any_download_active", lambda: False)
        monkeypatch.setattr(processing.models, "get_queue", lambda: [])
        processing._dispatch_next_from_queue()
        assert calls == [1]

    def test_nothing_due_means_no_rotation(self, processing, monkeypatch):
        calls = []
        monkeypatch.setattr(processing.vpn, "due_reason", lambda cfg=None: None)
        monkeypatch.setattr(processing.vpn, "rotate_if_due", lambda cfg=None, busy=None: calls.append(1))
        monkeypatch.setattr(processing, "any_download_active", lambda: False)
        monkeypatch.setattr(processing.models, "get_queue", lambda: [])
        processing._dispatch_next_from_queue()
        assert calls == []

    def test_automatic_downloads_wait_while_rotating(self):
        import processing

        assert processing.automatic_downloads_held() is False
        assert vpn._rotate_lock.acquire(blocking=False)
        try:
            assert processing.automatic_downloads_held() is True
        finally:
            vpn._rotate_lock.release()

    def test_a_due_rotation_lets_running_downloads_drain(self, processing, monkeypatch):
        """With several jobs running, nothing new may start while one is due."""
        calls = []
        monkeypatch.setattr(processing.vpn, "due_reason", lambda cfg=None: "x")
        monkeypatch.setattr(processing.vpn, "rotate_if_due", lambda cfg=None, busy=None: calls.append(1))
        monkeypatch.setattr(processing, "any_download_active", lambda: True)

        def boom():
            raise AssertionError("a new album was started while a rotation is due")

        monkeypatch.setattr(processing.models, "get_queue", boom)
        processing._dispatch_next_from_queue()
        assert calls == []

    def test_a_crashing_rotation_never_takes_the_queue_down(self, processing, monkeypatch):
        monkeypatch.setattr(processing.vpn, "due_reason", lambda cfg=None: "x")
        monkeypatch.setattr(processing, "any_download_active", lambda: False)

        def crash(cfg=None, busy=None):
            raise RuntimeError("boom")

        monkeypatch.setattr(processing.vpn, "rotate_if_due", crash)
        processing._dispatch_next_from_queue()  # must not raise
        assert vpn._state["last_attempt"] > 0  # and must not retry every tick


class TestInlineDownload:
    """The failed-tracks retry downloads inside its request and holds no
    slot: a rotation or a restart must still wait for it."""

    @pytest.fixture()
    def processing(self):
        import processing

        return processing

    def test_counts_as_active_and_is_released(self, processing):
        assert processing.any_download_active() is False
        with processing.inline_download():
            assert processing.any_download_active() is True
            with processing.inline_download():  # two retries at once
                pass
            assert processing.any_download_active() is True
        assert processing.any_download_active() is False

    def test_is_released_when_the_download_raises(self, processing):
        with pytest.raises(RuntimeError):
            with processing.inline_download():
                raise RuntimeError("boom")
        assert processing.any_download_active() is False

    def test_the_dispatcher_hands_over_the_busy_check(self, processing, monkeypatch):
        seen = {}
        monkeypatch.setattr(processing, "automatic_downloads_held", lambda: False)
        monkeypatch.setattr(processing.vpn, "due_reason", lambda cfg=None: "x")
        monkeypatch.setattr(
            processing.vpn, "rotate_if_due",
            lambda cfg=None, busy=None: seen.update(busy=busy),
        )
        monkeypatch.setattr(processing.models, "get_queue", lambda: [])
        processing._dispatch_next_from_queue()
        assert seen["busy"] is processing.any_download_active

    def test_a_due_rotation_waits_for_it(self, processing, monkeypatch):
        calls = []
        monkeypatch.setattr(processing, "automatic_downloads_held", lambda: False)
        monkeypatch.setattr(processing.vpn, "due_reason", lambda cfg=None: "x")
        monkeypatch.setattr(
            processing.vpn, "rotate_if_due",
            lambda cfg=None, busy=None: calls.append(1),
        )
        monkeypatch.setattr(processing.models, "get_queue", lambda: [])
        with processing.inline_download():
            processing._dispatch_next_from_queue()
        assert calls == []
        processing._dispatch_next_from_queue()
        assert calls == [1]

    def test_the_manual_rotation_is_refused(self, client, gluetun, processing):
        _configure(gluetun)
        with processing.inline_download():
            resp = client.post("/api/vpn/rotate")
        assert resp.status_code == 409
        assert not vpn.rotating()
        assert not [r for r in gluetun.requests if r[0] == "PUT"]


class TestManualRetryIsAnActiveDownload:
    URL = "https://www.youtube.com/watch?v=abcdefghijk"

    @pytest.fixture()
    def retry(self, client, monkeypatch, tmp_path):
        """POST a retry of a failed playlist track; returns what the download saw."""
        import manual_download
        import models
        import processing

        models.add_track_download(
            album_id=-1, album_title="P", artist_name="Various",
            track_title="Song", track_number=1, success=False,
            error_message="x", youtube_url="", youtube_title="",
            match_score=0.0, duration_seconds=0,
            album_path=str(tmp_path / "P"), lidarr_album_path="", cover_url="",
        )
        monkeypatch.setattr(manual_download, "_validate_target_path", lambda *a, **k: True)
        monkeypatch.setattr(manual_download, "makedirs_safe", lambda *a, **k: None)
        seen = {"downloads": 0, "active": []}

        def download(*args, **kwargs):
            seen["downloads"] += 1
            seen["active"].append(processing.any_download_active())
            return {"success": False, "error_message": "stop here"}

        monkeypatch.setattr(manual_download, "download_youtube_candidate", download)

        def post():
            return client.post("/api/download/manual", json={
                "youtube_url": self.URL, "track_title": "Song",
                "track_num": 1, "album_id": -1,
            })

        return post, seen, processing

    def test_it_counts_as_active_while_it_downloads(self, retry):
        post, seen, processing = retry
        assert processing.any_download_active() is False
        assert post().status_code == 500  # the fake download failed
        assert seen == {"downloads": 1, "active": [True]}
        assert processing.any_download_active() is False

    def test_a_rotation_that_started_meanwhile_is_honoured(self, retry, monkeypatch):
        """The check at the top of the route can be stale by the time it downloads."""
        post, seen, processing = retry
        answers = iter([False, True])
        monkeypatch.setattr(vpn, "rotating", lambda: next(answers, True))
        resp = post()
        assert resp.status_code == 409
        assert "changing its IP" in resp.get_json()["message"]
        assert seen["downloads"] == 0
        assert processing.any_download_active() is False


class TestRequeueAfterBlock:
    """A run that stopped on a block goes back to the head of the queue, but
    only when a rotation is due to give it a new IP."""

    @pytest.fixture(autouse=True)
    def _clean(self):
        import processing

        processing._blocked_albums.clear()
        processing._block_requeues.clear()
        yield
        processing._blocked_albums.clear()
        processing._block_requeues.clear()

    @pytest.fixture()
    def processing(self, monkeypatch):
        import processing

        monkeypatch.setattr(processing.vpn, "due_reason", lambda cfg=None: "x")
        return processing

    @staticmethod
    def _run(processing, monkeypatch, *, blocked=True, forced=False, album_id=5):
        """Dispatch ``album_id`` and run it; returns the queue afterwards."""
        import models

        models.enqueue_album(album_id, force=forced)
        models.set_queue_status(album_id, models.QUEUE_STATUS_DOWNLOADING)

        def fake_process(album_id_, **kwargs):
            if blocked:
                processing._blocked_albums.add(album_id_)
            return {"error": "All tracks failed"}

        monkeypatch.setattr(processing, "process_album_download", fake_process)
        processing._run_queued_album(album_id, ignore_backoff=forced)
        return [(r["album_id"], r["status"], r["force"]) for r in models.get_queue()]

    def test_goes_back_to_the_head_of_the_queue(self, processing, monkeypatch):
        import models

        models.enqueue_album(9)
        assert self._run(processing, monkeypatch) == [
            (5, "queued", 0), (9, "queued", 0),
        ]

    def test_a_manual_request_stays_manual(self, processing, monkeypatch):
        """Still bypasses the retry backoff when it comes back."""
        assert self._run(processing, monkeypatch, forced=True) == [(5, "queued", 1)]

    def test_nothing_comes_back_without_a_block(self, processing, monkeypatch):
        assert self._run(processing, monkeypatch, blocked=False) == []

    def test_nothing_comes_back_when_no_rotation_is_due(self, processing, monkeypatch):
        """Inside the cooldown the IP would not change: the scheduler decides."""
        monkeypatch.setattr(processing.vpn, "due_reason", lambda cfg=None: None)
        assert self._run(processing, monkeypatch) == []

    def test_a_crashing_rotation_check_leaves_it_to_the_scheduler(self, processing, monkeypatch):
        def crash(cfg=None):
            raise RuntimeError("boom")

        monkeypatch.setattr(processing.vpn, "due_reason", crash)
        assert self._run(processing, monkeypatch) == []

    def test_gives_up_after_a_few_rounds_and_then_starts_over(self, processing, monkeypatch):
        for _ in range(processing.MAX_BLOCK_REQUEUES):
            assert self._run(processing, monkeypatch) == [(5, "queued", 0)]
            import models

            models.dequeue_album(5)  # the dispatcher would start it again
        assert self._run(processing, monkeypatch) == []  # the scheduler takes over
        assert self._run(processing, monkeypatch) == [(5, "queued", 0)]  # fresh count

    def test_a_clean_run_resets_the_count(self, processing, monkeypatch):
        import models

        for _ in range(processing.MAX_BLOCK_REQUEUES - 1):
            self._run(processing, monkeypatch)
            models.dequeue_album(5)
        assert self._run(processing, monkeypatch, blocked=False) == []
        assert processing._block_requeues == {}

    def test_follows_the_real_rotation_trigger_and_its_cooldown(self, monkeypatch):
        import time

        import models
        import processing

        _set(gluetun_control_url="http://gluetun:8000")
        vpn.note_block("HTTP Error 429: Too Many Requests")
        assert self._run(processing, monkeypatch) == [(5, "queued", 0)]
        models.dequeue_album(5)
        vpn._state["last_attempt"] = time.time()  # a rotation was just tried
        assert self._run(processing, monkeypatch) == []


class TestAlbumCounting:
    """The counter advances where tracks were really attempted, once."""

    @pytest.fixture()
    def album(self, tmp_path, monkeypatch):
        import processing
        from tests.test_processing import TestDeferredAlbumSkipsNetworkWork as Wire

        Wire()._wire(monkeypatch, tmp_path, [], [])
        monkeypatch.setattr(
            processing, "load_config",
            lambda: {"track_retry_backoff": False, "max_track_retries": 0,
                     "scheduler_retry_after_hours": 24, "audio_format": "mp3"},
        )
        monkeypatch.setattr(
            processing, "find_album_on_ytmusic", lambda *a, **k: None,
        )
        monkeypatch.setattr(
            processing, "_download_tracks",
            lambda *a, **k: ([], [{"title": "Song"}], 0, {}),
        )
        monkeypatch.setattr(
            processing, "_handle_post_download", lambda *a, **k: {"ok": True},
        )
        return processing

    def test_a_queue_album_counts_once(self, album):
        album.process_album_download(77)
        assert vpn._state["albums_since_rotation"] == 1

    def test_the_queue_wrapper_does_not_count_again(self, album):
        album._run_queued_album(77)
        assert vpn._state["albums_since_rotation"] == 1

    def test_a_download_client_job_counts_once(self, album):
        import download_client

        download_client.run_album_job(77, state=album._make_download_state())
        assert vpn._state["albums_since_rotation"] == 1

    def test_a_busy_slot_is_not_counted(self, album, monkeypatch):
        monkeypatch.setitem(album.download_process, "active", True)
        assert album.process_album_download(77) == {"error": "Busy"}
        assert vpn._state["albums_since_rotation"] == 0

    def test_a_user_stop_is_not_counted(self, album, monkeypatch):
        def stop(tracks, path, alb, ctx, state):
            state["stop"] = True
            return [], [], 0, {}

        monkeypatch.setattr(album, "_download_tracks", stop)
        assert album.process_album_download(77) == {"stopped": True}
        assert vpn._state["albums_since_rotation"] == 0

    def test_an_album_skipped_by_the_backoff_costs_nothing(self, album, monkeypatch):
        import time as _t
        from tests.test_processing import _add_attempt

        monkeypatch.setattr(
            album, "load_config",
            lambda: {"track_retry_backoff": True, "max_track_retries": 0,
                     "scheduler_retry_after_hours": 24, "audio_format": "mp3"},
        )
        for _ in range(4):
            _add_attempt(77, "Song", False, ts=_t.time() - 3600)
        assert album.process_album_download(77)["message"] == "Skipped"
        assert vpn._state["albums_since_rotation"] == 0

    def test_an_album_that_fails_before_any_track_is_not_counted(self, album, monkeypatch):
        def lidarr_down(*a, **k):
            raise RuntimeError("Lidarr is down")

        monkeypatch.setattr(album, "lidarr_request", lidarr_down)
        assert "error" in album.process_album_download(77)
        assert vpn._state["albums_since_rotation"] == 0


# -- HTTP API -------------------------------------------------------------------------------

class TestStatusRoute:
    def test_not_configured(self, client):
        data = client.get("/api/vpn/status").get_json()
        assert data["proxy_configured"] is False
        assert data["control_configured"] is False
        assert data["public_ip"] is None and data["error"] == ""

    def test_configured(self, client, gluetun):
        _configure(gluetun, vpn_proxy_url="http://gluetun:8888")
        data = client.get("/api/vpn/status").get_json()
        assert data["proxy_configured"] and data["control_configured"]
        assert data["public_ip"] == "10.0.0.1"
        assert data["vpn_status"] == "running"

    def test_reports_a_pending_block(self, client, gluetun):
        _configure(gluetun)
        assert client.get("/api/vpn/status").get_json()["block_seen"] is False
        vpn.note_block("Sign in to confirm you're not a bot")
        assert client.get("/api/vpn/status").get_json()["block_seen"] is True

    def test_unreachable_server_is_an_error_message_not_a_crash(self, client):
        _set(gluetun_control_url="http://127.0.0.1:9")
        data = client.get("/api/vpn/status").get_json()
        assert data["public_ip"] is None
        assert "Could not reach" in data["error"]


class TestTestRoute:
    def test_nothing_to_test(self, client):
        assert client.post("/api/vpn/test", json={}).status_code == 400

    def test_invalid_url(self, client):
        resp = client.post("/api/vpn/test", json={"control_url": "gluetun:8000"})
        assert resp.status_code == 400

    def test_control_server_with_unsaved_values(self, client, gluetun):
        resp = client.post("/api/vpn/test", json={
            "control_url": gluetun.url, "api_key": "secret",
        })
        data = resp.get_json()
        assert data["success"] is True
        assert data["control"]["public_ip"] == "10.0.0.1"
        assert data["proxy"] is None

    def test_bad_key_is_reported(self, client, gluetun):
        data = client.post("/api/vpn/test", json={
            "control_url": gluetun.url, "api_key": "nope",
        }).get_json()
        assert data["success"] is False
        assert "API key" in data["control"]["message"]

    def test_masked_key_uses_the_saved_one_for_the_saved_server(self, client, gluetun):
        _configure(gluetun)
        data = client.post("/api/vpn/test", json={
            "control_url": gluetun.url, "api_key": config.SECRET_MASK,
        }).get_json()
        assert data["success"] is True

    def test_the_saved_key_never_goes_to_another_server(self, client, gluetun):
        _configure(gluetun)
        other = FakeGluetun(api_key="")
        try:
            data = client.post("/api/vpn/test", json={
                "control_url": other.url, "api_key": config.SECRET_MASK,
            }).get_json()
            assert data["success"] is True
            assert other.requests and all(r[2] is None for r in other.requests)
        finally:
            other.close()

    def test_proxy(self, client):
        response = MagicMock(status_code=204)
        with patch("requests.get", return_value=response) as get:
            data = client.post("/api/vpn/test", json={
                "proxy_url": "http://gluetun:8888",
            }).get_json()
        assert data["success"] is True and "reachable" in data["proxy"]["message"]
        assert get.call_args.args[0] == "https://www.youtube.com/generate_204"
        assert get.call_args.kwargs["proxies"]["https"] == "http://gluetun:8888"

    def test_proxy_that_asks_for_a_login(self, client):
        with patch("requests.get", return_value=MagicMock(status_code=407)):
            data = client.post("/api/vpn/test", json={
                "proxy_url": "http://gluetun:8888",
            }).get_json()
        assert data["success"] is False
        assert "username and password" in data["proxy"]["message"]

    def test_masked_proxy_uses_the_saved_one(self, client):
        _set(vpn_proxy_url="http://u:pw@gluetun:8888")
        with patch("requests.get", return_value=MagicMock(status_code=204)) as get:
            client.post("/api/vpn/test", json={"proxy_url": config.SECRET_MASK})
        assert get.call_args.kwargs["proxies"]["https"] == "http://u:pw@gluetun:8888"

    def test_a_dead_proxy_does_not_leak_the_exception(self, client):
        import requests as rq

        with patch("requests.get", side_effect=rq.ConnectionError("http://u:pw@h secret")):
            body = client.post("/api/vpn/test", json={
                "proxy_url": "http://gluetun:8888",
            }).get_data(as_text=True)
        assert "pw@h" not in body and "secret" not in body


class TestRotateRoute:
    @staticmethod
    def _wait_until_done():
        import time as _t

        deadline = _t.time() + 15
        while vpn.rotating() and _t.time() < deadline:
            _t.sleep(0.02)
        assert not vpn.rotating()

    def test_needs_a_control_server(self, client):
        assert client.post("/api/vpn/rotate").status_code == 400

    def test_refused_during_a_download_and_the_lock_is_released(self, client, gluetun):
        _configure(gluetun)
        with patch("processing.any_download_active", return_value=True):
            resp = client.post("/api/vpn/rotate")
        assert resp.status_code == 409
        assert not vpn.rotating()
        assert not [r for r in gluetun.requests if r[0] == "PUT"]

    def test_answers_at_once_and_rotates_in_the_background(self, client, gluetun):
        _configure(gluetun)
        with patch("processing.any_download_active", return_value=False):
            resp = client.post("/api/vpn/rotate")
        assert resp.status_code == 202
        assert resp.get_json() == {"success": True, "started": True}
        self._wait_until_done()
        last = client.get("/api/vpn/status").get_json()["last_rotation"]
        assert last["success"] and last["changed"] and last["new_ip"] == "10.0.0.2"
        assert last["reason"] == "manual"

    def test_is_already_rotating_from_the_moment_it_answers(self, client, gluetun):
        """No window in which the dispatcher or a manual job could start."""
        _configure(gluetun)
        release = threading.Event()
        original = vpn._restart

        def slow(*args, **kwargs):
            release.wait(5)
            return original(*args, **kwargs)

        with patch.object(vpn, "_restart", slow), \
                patch("processing.any_download_active", return_value=False):
            assert client.post("/api/vpn/rotate").status_code == 202
            assert vpn.rotating() is True
            assert client.get("/api/vpn/status").get_json()["rotating"] is True
            second = client.post("/api/vpn/rotate")
            assert second.status_code == 409
            release.set()
            self._wait_until_done()

    def test_a_failure_is_reported_by_the_status(self, client):
        _set(gluetun_control_url="http://127.0.0.1:9")
        with patch("processing.any_download_active", return_value=False):
            assert client.post("/api/vpn/rotate").status_code == 202
        self._wait_until_done()
        last = client.get("/api/vpn/status").get_json()["last_rotation"]
        assert last["success"] is False and "Could not reach" in last["message"]

    def test_cross_site_requests_are_refused(self, client, gluetun):
        _configure(gluetun)
        resp = client.post("/api/vpn/rotate", headers={"Origin": "http://evil.example"})
        assert resp.status_code == 403
        assert not [r for r in gluetun.requests if r[0] == "PUT"]


class TestWhileRotating:
    """Nothing may claim the download slot while the tunnel is down."""

    @pytest.fixture()
    def rotating(self):
        assert vpn._rotate_lock.acquire(blocking=False)
        yield
        vpn._rotate_lock.release()

    def test_a_playlist_import_is_refused(self, client, rotating):
        resp = client.post("/api/youtube/playlist/download", json={
            "artist_name": "A", "album_title": "B",
            "entries": [{"url": "https://www.youtube.com/watch?v=jNQXAC9IVRw", "title": "t"}],
        })
        assert resp.status_code == 409
        assert "changing its IP" in resp.get_json()["message"]

    @pytest.mark.parametrize("path,body", [
        ("/api/download/manual", {"youtube_url": "https://www.youtube.com/watch?v=jNQXAC9IVRw", "track_title": "t"}),
        ("/api/album/1/track/manual-download", {"youtube_url": "https://www.youtube.com/watch?v=jNQXAC9IVRw", "track_title": "t"}),
    ])
    def test_a_manual_download_is_refused(self, client, rotating, path, body):
        resp = client.post(path, json=body)
        assert resp.status_code == 409

    def test_the_status_says_so(self, client, rotating):
        assert client.get("/api/vpn/status").get_json()["rotating"] is True

    def test_the_status_route_is_rate_limited(self, client):
        codes = {client.get("/api/vpn/status").status_code for _ in range(35)}
        assert 429 in codes


class TestPlaylistImportCounts:
    ENTRIES = [{"url": "https://www.youtube.com/watch?v=jNQXAC9IVRw", "title": "t"}]

    @pytest.fixture(autouse=True)
    def offline(self, tmp_path, monkeypatch):
        import youtube_import

        monkeypatch.setattr("utils.DOWNLOAD_DIR", str(tmp_path))
        monkeypatch.setitem(youtube_import.download_process, "active", False)
        monkeypatch.setitem(youtube_import.download_process, "stop", False)
        # No network: the one track "fails" at once, which still ends the run.
        monkeypatch.setattr(
            youtube_import, "download_youtube_candidate",
            lambda *a, **k: {"success": False, "error_message": "offline"},
        )
        monkeypatch.setattr(youtube_import, "_cleanup_temp_files", lambda *a: None)

    def _run(self, tmp_path):
        import youtube_import

        return youtube_import._execute_playlist_download(
            "A", "B", self.ENTRIES, str(tmp_path / "A" / "B"), config.load_config(),
        )

    def test_a_finished_import_counts_as_an_album(self, tmp_path):
        self._run(tmp_path)
        assert vpn._state["albums_since_rotation"] == 1

    def test_a_stopped_import_is_not_counted(self, tmp_path, monkeypatch):
        import youtube_import

        def stop(*a, **k):
            youtube_import.download_process["stop"] = True
            return {"success": False, "error_message": "stopped"}

        monkeypatch.setattr(youtube_import, "download_youtube_candidate", stop)
        self._run(tmp_path)
        assert vpn._state["albums_since_rotation"] == 0

    def test_an_unusable_target_is_not_counted(self, tmp_path, monkeypatch):
        import youtube_import
        from utils import BaseNotMountedError

        def refuse(*a, **k):
            raise BaseNotMountedError("not mounted")

        monkeypatch.setattr(youtube_import, "makedirs_safe", refuse)
        self._run(tmp_path)
        assert vpn._state["albums_since_rotation"] == 0


# -- every YouTube request goes through the proxy ------------------------------
#
# One test per call site: a route or helper that builds its own yt-dlp / requests
# options and forgets the proxy would leak the host's real IP while the rest of
# the app uses the VPN, so each one is pinned individually.

PROXY = "http://gluetun:8888"
PROXIES = {"http": PROXY, "https": PROXY}


def _ydl_returning(info):
    """A stand-in for ``yt_dlp.YoutubeDL`` whose extract_info gives ``info``."""
    ydl = MagicMock()
    ydl.return_value.__enter__.return_value.extract_info.return_value = info
    return ydl


def _opts_of(ydl):
    return ydl.call_args.args[0]


class TestProxyCallSites:
    @pytest.mark.parametrize("proxy, expected", [(PROXY, PROXY), ("", None)])
    def test_search_route(self, client, proxy, expected):
        _set(vpn_proxy_url=proxy)
        ydl = _ydl_returning({"entries": []})
        with patch("yt_dlp.YoutubeDL", ydl):
            resp = client.post("/api/youtube/search", json={"query": "daft punk"})
        assert resp.status_code == 200
        assert _opts_of(ydl).get("proxy") == expected

    @pytest.mark.parametrize("proxy, expected", [(PROXY, PROXY), ("", None)])
    def test_preview_stream_route(self, client, proxy, expected):
        import app as app_module

        _set(vpn_proxy_url=proxy)
        app_module._audio_stream_cache.clear()
        ydl = _ydl_returning({"url": "https://rr1.googlevideo.com/audio"})
        with patch("yt_dlp.YoutubeDL", ydl), patch.object(
            app_module, "_proxy_audio_stream", return_value=("ok", 200),
        ):
            resp = client.get("/api/youtube/stream?url=jNQXAC9IVRw")
        assert resp.status_code == 200
        assert _opts_of(ydl).get("proxy") == expected
        app_module._audio_stream_cache.clear()

    @pytest.mark.parametrize("proxy, expected", [(PROXY, PROXY), ("", None)])
    def test_cookies_test_route(self, client, tmp_path, proxy, expected):
        cookies = tmp_path / "cookies.txt"
        cookies.write_text(
            "# Netscape HTTP Cookie File\n"
            ".youtube.com\tTRUE\t/\tTRUE\t0\tLOGIN_INFO\tabc\n"
            ".youtube.com\tTRUE\t/\tTRUE\t0\tSAPISID\txyz\n"
        )
        _set(vpn_proxy_url=proxy, yt_cookies_file=str(cookies))
        ydl = _ydl_returning({"formats": [{}] * 14})
        with patch("yt_dlp.YoutubeDL", ydl):
            data = client.post("/api/cookies/test").get_json()
        assert data["success"] is True
        assert _opts_of(ydl).get("proxy") == expected

    @pytest.mark.parametrize("proxy, expected", [(PROXY, PROXY), ("", None)])
    def test_playlist_info(self, proxy, expected):
        import youtube_import

        _set(vpn_proxy_url=proxy)
        ydl = _ydl_returning({"entries": []})
        with patch("yt_dlp.YoutubeDL", ydl):
            youtube_import._flat_youtube_info(
                "https://www.youtube.com/playlist?list=PL1", config.load_config(),
            )
        assert _opts_of(ydl).get("proxy") == expected

    @pytest.mark.parametrize("proxy, expected", [(PROXY, PROXIES), ("", None)])
    def test_youtube_music_page_scrape(self, proxy, expected):
        import youtube_import

        _set(vpn_proxy_url=proxy)
        page = MagicMock(status_code=404, text="")
        with patch("requests.get", return_value=page) as get:
            youtube_import._scrape_ytmusic_art(
                "https://music.youtube.com/playlist?list=OLAK5uy_x",
            )
        assert get.call_args.kwargs["proxies"] == expected

    def test_album_lookups_in_a_download_use_the_proxy(self):
        import downloader

        _set(vpn_proxy_url=PROXY)
        with patch("ytmusicapi.YTMusic") as ytmusic:
            downloader._ytmusicapi_client(via_vpn=True)
        assert ytmusic.call_args.kwargs["proxies"] == PROXIES

    def test_explore_keeps_browsing_directly(self):
        """Explore browses on the host's own connection; only downloads proxy."""
        import downloader

        _set(vpn_proxy_url=PROXY)
        with patch("ytmusicapi.YTMusic") as ytmusic:
            downloader._ytmusicapi_client(language="en", location="IT")
        assert "proxies" not in ytmusic.call_args.kwargs

    def test_album_lookups_go_direct_without_a_proxy(self):
        import downloader

        with patch("ytmusicapi.YTMusic") as ytmusic:
            downloader._ytmusicapi_client(via_vpn=True)
        assert "proxies" not in ytmusic.call_args.kwargs

    def test_both_download_lookups_ask_for_the_proxy(self):
        """The two callers on the download path must pass via_vpn=True."""
        import downloader

        calls = []
        with patch.object(
            downloader, "_ytmusicapi_client",
            side_effect=lambda **kw: calls.append(kw) or None,
        ), patch.object(downloader, "_ytm_album_result", return_value=None):
            assert downloader._find_album_via_ytmusicapi("Artist", "Album") is None
            downloader.album_from_ytmusic_hint(
                "OLAK5uy_abcdefghij", browse_id="MPREb_abcdefg",
            )
        assert len(calls) == 2
        assert all(kw.get("via_vpn") is True for kw in calls)


# -- a block seen at any download call site is remembered -----------------------

class TestBlockSites:
    BLOCK = "ERROR: HTTP Error 429: Too Many Requests"

    @pytest.fixture(autouse=True)
    def _control_server(self):
        _set(gluetun_control_url="http://gluetun:8000")
        assert vpn.due_reason() is None

    def test_a_failed_download_is_noted(self):
        import downloader

        ydl = MagicMock()
        ydl.return_value.__enter__.return_value.download.side_effect = Exception(self.BLOCK)
        with patch("downloader.yt_dlp.YoutubeDL", ydl):
            result = downloader.download_youtube_candidate(
                {"url": "https://www.youtube.com/watch?v=jNQXAC9IVRw",
                 "title": "T", "duration": 200, "score": 0.9},
                "/tmp/never-written",
            )
        assert result["success"] is False
        assert vpn.due_reason() == "YouTube blocked a request"

    def test_a_failed_format_listing_is_noted(self):
        import downloader

        ydl = MagicMock()
        ydl.return_value.__enter__.return_value.extract_info.side_effect = Exception(self.BLOCK)
        with patch("downloader.yt_dlp.YoutubeDL", ydl), pytest.raises(Exception):
            downloader.list_video_formats("https://www.youtube.com/watch?v=jNQXAC9IVRw")
        assert vpn.due_reason() == "YouTube blocked a request"

    def test_a_failed_search_is_noted(self):
        import downloader

        ydl = MagicMock()
        ydl.return_value.__enter__.return_value.extract_info.side_effect = Exception(self.BLOCK)
        with patch("downloader.yt_dlp.YoutubeDL", ydl):
            assert downloader.search_youtube_candidates("Artist Track", "Track") == []
        assert vpn.due_reason() == "YouTube blocked a request"

    @pytest.mark.parametrize("call", ["download", "formats", "search"])
    def test_an_ordinary_error_is_not_a_block(self, call):
        import downloader

        ydl = MagicMock()
        boom = Exception("ERROR: Video unavailable")
        inner = ydl.return_value.__enter__.return_value
        inner.download.side_effect = boom
        inner.extract_info.side_effect = boom
        with patch("downloader.yt_dlp.YoutubeDL", ydl):
            if call == "download":
                downloader.download_youtube_candidate(
                    {"url": "https://www.youtube.com/watch?v=jNQXAC9IVRw",
                     "title": "T", "duration": 200, "score": 0.9},
                    "/tmp/never-written",
                )
            elif call == "formats":
                with pytest.raises(Exception):
                    downloader.list_video_formats(
                        "https://www.youtube.com/watch?v=jNQXAC9IVRw",
                    )
            else:
                downloader.search_youtube_candidates("Artist Track", "Track")
        assert vpn.due_reason() is None


# -- the VPN routes sit behind the login ---------------------------------------

class TestLoginGate:
    @pytest.mark.parametrize("method, path", [
        ("get", "/api/vpn/status"),
        ("post", "/api/vpn/test"),
        ("post", "/api/vpn/rotate"),
    ])
    def test_vpn_routes_need_a_login(self, client, method, path):
        resp = client.post(
            "/api/auth/setup", json={"username": "lucio", "password": "secret-pass"},
        )
        assert resp.status_code == 200, resp.get_json()
        from app import app

        anonymous = app.test_client()
        answer = getattr(anonymous, method)(path)
        assert answer.status_code == 401
        assert answer.headers["X-Login-Required"] == "1"
