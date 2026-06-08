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
