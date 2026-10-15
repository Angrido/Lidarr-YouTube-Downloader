"""Shared pytest fixtures."""

import pytest

import config


@pytest.fixture(autouse=True)
def _reset_config_cache():
    """Clear the load_config cache around every test.

    The cache is a process-global keyed on config.json's path/mtime/size;
    resetting it keeps tests isolated from each other regardless of how
    they redirect CONFIG_FILE.
    """
    config.invalidate_config_cache()
    yield
    config.invalidate_config_cache()


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "real_artist_image: keep the real artist-image lookups (requests still patched by the test)",
    )


@pytest.fixture(autouse=True)
def _no_artist_image_network(request, monkeypatch):
    if request.node.get_closest_marker("real_artist_image"):
        return
    import explore
    import metadata
    monkeypatch.setattr(metadata, "get_deezer_artist_image", lambda *a, **k: None)
    monkeypatch.setattr(explore, "artist_image_for_name", lambda *a, **k: "")
