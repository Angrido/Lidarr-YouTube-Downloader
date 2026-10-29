import hashlib
import io
import sys
import zipfile

import pytest

import ytdlp_update


def _wheel(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name, body in files.items():
            archive.writestr(name, body)
    return buf.getvalue()


class _Resp:
    def __init__(self, json_data=None, content=b""):
        self._json = json_data
        self.content = content

    def raise_for_status(self):
        pass

    def json(self):
        return self._json


def _release(name, version, wheel, requires=None, sha=None):
    return {
        "info": {"name": name, "version": version, "requires_dist": requires or []},
        "urls": [
            {"packagetype": "sdist", "filename": f"{name}-{version}.tar.gz", "url": "x", "digests": {"sha256": ""}},
            {
                "packagetype": "bdist_wheel",
                "filename": f"{name.replace('-', '_')}-{version}-py3-none-any.whl",
                "url": f"https://files/{name}-{version}.whl",
                "digests": {"sha256": sha or hashlib.sha256(wheel).hexdigest()},
            },
        ],
    }


@pytest.fixture
def pypi(monkeypatch):
    ytdlp = _wheel({
        "yt_dlp/__init__.py": "",
        "yt_dlp-2099.1.2.dist-info/METADATA": "Name: yt-dlp\nVersion: 2099.1.2\n",
    })
    ejs = _wheel({"yt_dlp_ejs/__init__.py": "", "yt_dlp_ejs-0.9.0.dist-info/METADATA": ""})
    responses = {
        "https://pypi.org/pypi/yt-dlp/2099.1.2/json": _Resp(_release(
            "yt-dlp", "2099.1.2", ytdlp,
            requires=["brotli; extra == 'default'", "yt-dlp-ejs==0.9.0; extra == 'default'"],
        )),
        "https://pypi.org/pypi/yt-dlp-ejs/0.9.0/json": _Resp(_release("yt-dlp-ejs", "0.9.0", ejs)),
        "https://files/yt-dlp-2099.1.2.whl": _Resp(content=ytdlp),
        "https://files/yt-dlp-ejs-0.9.0.whl": _Resp(content=ejs),
    }
    monkeypatch.setattr(ytdlp_update.requests, "get", lambda url, timeout: responses[url])
    return responses


def test_install_unpacks_ytdlp_and_its_pinned_ejs(tmp_path, pypi):
    target = tmp_path / "ytdlp"
    ytdlp_update.install("2099.1.2", str(target))
    assert (target / "yt_dlp" / "__init__.py").exists()
    assert (target / "yt_dlp_ejs" / "__init__.py").exists()
    assert ytdlp_update.saved_version(str(target)) == "2099.1.2"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["ytdlp"]


def test_install_rejects_a_bad_checksum_and_keeps_the_old_copy(tmp_path, pypi):
    target = tmp_path / "ytdlp"
    (target / "yt_dlp-2000.1.1.dist-info").mkdir(parents=True)
    release = pypi["https://pypi.org/pypi/yt-dlp/2099.1.2/json"]._json
    release["urls"][1]["digests"]["sha256"] = "0" * 64
    with pytest.raises(RuntimeError, match="Checksum"):
        ytdlp_update.install("2099.1.2", str(target))
    assert ytdlp_update.saved_version(str(target)) == "2000.1.1"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["ytdlp"]


def test_install_rejects_paths_outside_the_folder(tmp_path, pypi):
    evil = _wheel({"../escape.py": ""})
    pypi["https://pypi.org/pypi/yt-dlp/2099.1.2/json"]._json["urls"][1]["digests"]["sha256"] = hashlib.sha256(evil).hexdigest()
    pypi["https://files/yt-dlp-2099.1.2.whl"].content = evil
    with pytest.raises(RuntimeError, match="Unsafe"):
        ytdlp_update.install("2099.1.2", str(tmp_path / "ytdlp"))
    assert not (tmp_path / "escape.py").exists()
    assert list(tmp_path.iterdir()) == []


def test_activate_uses_a_newer_saved_copy(tmp_path, monkeypatch):
    (tmp_path / "yt_dlp-2099.1.2.dist-info").mkdir()
    monkeypatch.setattr(sys, "path", list(sys.path))
    assert ytdlp_update.activate(str(tmp_path)) == "2099.1.2"
    assert sys.path[0] == str(tmp_path)


def test_activate_drops_a_copy_the_image_has_overtaken(tmp_path, monkeypatch):
    saved = tmp_path / "ytdlp"
    (saved / "yt_dlp-2000.1.1.dist-info").mkdir(parents=True)
    monkeypatch.setattr(sys, "path", list(sys.path))
    assert ytdlp_update.activate(str(saved)) is None
    assert str(saved) not in sys.path
    assert not saved.exists()


def test_activate_without_a_saved_copy(tmp_path):
    assert ytdlp_update.activate(str(tmp_path / "missing")) is None


def test_version_key_orders_dates():
    assert ytdlp_update.version_key("2026.10.2") > ytdlp_update.version_key("2026.9.30")


def test_app_update_installs_only_a_new_version(monkeypatch):
    import app as app_module
    installed = []
    monkeypatch.setattr(app_module, "get_ytdlp_version", lambda: "2026.1.1")
    monkeypatch.setattr(ytdlp_update, "install", lambda v: installed.append(v))
    assert app_module._update_ytdlp("2026.1.1") == ("2026.1.1", "2026.1.1", None)
    assert installed == []
    assert app_module._update_ytdlp("2026.2.2") == ("2026.1.1", "2026.2.2", None)
    assert installed == ["2026.2.2"]


def test_app_update_reports_pypi_and_install_failures(monkeypatch):
    import app as app_module
    monkeypatch.setattr(app_module, "get_ytdlp_version", lambda: "2026.1.1")
    monkeypatch.setattr(app_module, "_get_ytdlp_pypi_version", lambda: None)
    assert app_module._update_ytdlp() == (None, None, "Could not reach PyPI")

    def boom(version):
        raise RuntimeError("Checksum mismatch for x.whl")
    monkeypatch.setattr(ytdlp_update, "install", boom)
    assert app_module._update_ytdlp("2026.2.2") == (None, None, "Checksum mismatch for x.whl")
