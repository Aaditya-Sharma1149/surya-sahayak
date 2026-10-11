"""Every test runs in a fake, hermetic world: no real network, a throw-away cache folder."""
import pathlib
import sys
import time

import pytest
import requests

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import fakes  # noqa: E402


@pytest.fixture(autouse=True)
def hermetic(tmp_path, monkeypatch):
    monkeypatch.setenv("SURYA_CACHE_DIR", str(tmp_path / "cache"))
    from surya import engine, geo
    engine._pvgis_cached.cache_clear()
    geo._rev_cache.clear()
    geo._fwd_cache.clear()
    monkeypatch.setattr(geo, "NOMINATIM_MIN_INTERVAL", 0)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(requests, "get", fakes.make_fake_get())     # healthy world by default
    yield
