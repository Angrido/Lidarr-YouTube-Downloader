import json

import pytest

from db import close_db, init_db


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", str(tmp_path / "test.db"))
    init_db()
    yield
    close_db()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("config.CONFIG_FILE", str(tmp_path / "config.json"))
    monkeypatch.setenv("DOWNLOAD_PATH", str(tmp_path / "downloads"))
    monkeypatch.setenv("LIDARR_URL", "http://localhost:8686")
    monkeypatch.setenv("LIDARR_API_KEY", "test-key")
    monkeypatch.delenv("AUTH_USERNAME", raising=False)
    monkeypatch.delenv("AUTH_PASSWORD", raising=False)
    from app import app
    from utils import rate_limit_store

    rate_limit_store.clear()
    app.config["TESTING"] = True  # nosemgrep
    with app.test_client() as c:
        yield c
    rate_limit_store.clear()


def _enable(client, username="lucio", password="secret-pass"):
    resp = client.post(
        "/api/auth/setup", json={"username": username, "password": password},
    )
    assert resp.status_code == 200, resp.get_json()
    return resp


def _fresh(client):
    from app import app

    return app.test_client()


class TestLoginOff:
    def test_pages_open_without_login(self, client):
        assert client.get("/api/stats").status_code == 200
        assert client.get("/settings").status_code == 200

    def test_login_page_redirects_home(self, client):
        resp = client.get("/login")
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/")

    def test_status_reports_off(self, client):
        data = client.get("/api/auth/status").get_json()
        assert data["enabled"] is False
        assert data["from_env"] is False


class TestEnableLogin:
    def test_short_password_rejected(self, client):
        resp = client.post("/api/auth/setup", json={"username": "a", "password": "short"})
        assert resp.status_code == 400

    def test_setup_keeps_session_and_stores_hash(self, client, tmp_path):
        _enable(client)
        assert client.get("/api/stats").status_code == 200
        saved = json.loads((tmp_path / "config.json").read_text())
        assert saved["auth_enabled"] is True
        assert saved["auth_username"] == "lucio"
        assert saved["auth_password_hash"] and "secret-pass" not in saved["auth_password_hash"]
        assert len(saved["auth_api_key"]) == 32

    def test_config_api_hides_auth_keys(self, client):
        _enable(client)
        conf = client.get("/api/config").get_json()
        for key in ("auth_enabled", "auth_username", "auth_password_hash", "auth_api_key"):
            assert key not in conf

    def test_config_api_cannot_disable_login(self, client):
        _enable(client)
        client.post("/api/config", json={"auth_enabled": False})
        other = _fresh(client)
        assert other.get("/api/stats").status_code == 401


class TestProtection:
    def test_api_requires_login(self, client):
        _enable(client)
        other = _fresh(client)
        resp = other.get("/api/stats")
        assert resp.status_code == 401
        assert resp.headers["X-Login-Required"] == "1"

    def test_page_redirects_to_login_with_next(self, client):
        _enable(client)
        resp = _fresh(client).get("/downloads?x=1")
        assert resp.status_code == 302
        assert "/login?next=%2Fdownloads%3Fx%3D1" in resp.headers["Location"]

    def test_public_paths_stay_open(self, client):
        _enable(client)
        other = _fresh(client)
        assert other.get("/api/health").status_code == 200
        assert other.get("/login").status_code == 200
        assert other.get("/static/app.js").status_code == 200
        assert other.get("/api/sabnzbd/api?mode=version").status_code != 401

    def test_api_key_header_and_param(self, client):
        _enable(client)
        key = client.get("/api/auth/status").get_json()["api_key"]
        other = _fresh(client)
        assert other.get("/api/stats", headers={"X-Api-Key": key}).status_code == 200
        assert other.get(f"/api/stats?apikey={key}").status_code == 200
        assert other.get("/api/stats", headers={"X-Api-Key": "wrong"}).status_code == 401

    def test_regenerated_key_replaces_old(self, client):
        _enable(client)
        old = client.get("/api/auth/status").get_json()["api_key"]
        new = client.post("/api/auth/api-key").get_json()["api_key"]
        assert new != old
        other = _fresh(client)
        assert other.get("/api/stats", headers={"X-Api-Key": old}).status_code == 401
        assert other.get("/api/stats", headers={"X-Api-Key": new}).status_code == 200


class TestLoginFlow:
    def test_wrong_password(self, client):
        _enable(client)
        other = _fresh(client)
        resp = other.post("/api/auth/login", json={"username": "lucio", "password": "nope"})
        assert resp.status_code == 401
        assert other.get("/api/stats").status_code == 401

    def test_right_password_and_safe_next(self, client):
        _enable(client)
        other = _fresh(client)
        resp = other.post(
            "/api/auth/login",
            json={"username": "lucio", "password": "secret-pass", "next": "//evil.example/x"},
        )
        assert resp.status_code == 200
        assert resp.get_json()["next"] == "/"
        assert other.get("/api/stats").status_code == 200

    def test_next_kept_when_local(self, client):
        _enable(client)
        resp = _fresh(client).post(
            "/api/auth/login",
            json={"username": "lucio", "password": "secret-pass", "next": "/explore?q=a"},
        )
        assert resp.get_json()["next"] == "/explore?q=a"

    def test_logout(self, client):
        _enable(client)
        assert client.post("/api/auth/logout").status_code == 200
        assert client.get("/api/stats").status_code == 401

    def test_update_username_without_password_keeps_hash(self, client, tmp_path):
        _enable(client)
        before = json.loads((tmp_path / "config.json").read_text())["auth_password_hash"]
        resp = client.post("/api/auth/setup", json={"username": "other", "password": ""})
        assert resp.status_code == 200
        saved = json.loads((tmp_path / "config.json").read_text())
        assert saved["auth_username"] == "other"
        assert saved["auth_password_hash"] == before
        assert client.get("/api/stats").status_code == 200

    def test_disable(self, client):
        _enable(client)
        assert client.post("/api/auth/disable").status_code == 200
        assert _fresh(client).get("/api/stats").status_code == 200

    def test_login_rate_limited(self, client):
        _enable(client)
        other = _fresh(client)
        codes = [
            other.post("/api/auth/login", json={"username": "x", "password": "y"}).status_code
            for _ in range(10)
        ]
        assert 429 in codes


class TestEnvCredentials:
    def test_env_credentials_enforced(self, client, monkeypatch):
        monkeypatch.setenv("AUTH_USERNAME", "admin")
        monkeypatch.setenv("AUTH_PASSWORD", "from-env-pass")
        other = _fresh(client)
        assert other.get("/api/stats").status_code == 401
        resp = other.post("/api/auth/login", json={"username": "admin", "password": "from-env-pass"})
        assert resp.status_code == 200
        assert other.get("/api/stats").status_code == 200
        assert other.post("/api/auth/disable").status_code == 409
        assert other.get("/api/auth/status").get_json()["from_env"] is True
