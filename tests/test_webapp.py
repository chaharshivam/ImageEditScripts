"""Flask test-client checks for the local Framewipe UI."""
from __future__ import annotations

from pathlib import Path

import pytest

import webapp
import split_grids as sg


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "WORK", tmp_path / "work")
    webapp.WORK.mkdir()
    webapp.JOBS.clear()
    webapp.RUNS.clear()
    return webapp.app.test_client()


def test_index_is_framewipe(client):
    r = client.get("/")
    assert r.status_code == 200
    html = r.data.decode("utf-8", "replace")
    assert "Framewipe" in html
    assert "Cleanroom" not in html
    assert "prep frames locally" in html.lower() or "Nothing uploaded" in html


def test_capabilities(client):
    r = client.get("/api/capabilities")
    assert r.status_code == 200
    data = r.get_json()
    assert data["app"] == "Framewipe"
    assert "ffmpeg" in data
    assert "realesrgan" in data


def test_unique_dest(tmp_path):
    folder = tmp_path
    (folder / "shot.jpg").write_bytes(b"x")
    used = set()
    a = webapp.unique_dest(folder, "shot.jpg", used)
    (folder / a).write_bytes(b"y")
    b = webapp.unique_dest(folder, "shot.jpg", used)
    assert a != b
    assert a == "shot_1.jpg" or b.startswith("shot_")


def test_safe_under_blocks_escape(tmp_path):
    base = tmp_path / "out"
    base.mkdir()
    (base / "ok.png").write_bytes(b"png")
    assert webapp.safe_under(base, "ok.png").name == "ok.png"
    with pytest.raises(ValueError):
        webapp.safe_under(base, "../secret.txt")


def test_inspect_rejects_unsupported(client):
    from io import BytesIO
    r = client.post("/api/inspect", data={"files": (BytesIO(b"hello"), "notes.txt")})
    assert r.status_code == 400
    assert "supported" in r.get_json()["error"].lower()
