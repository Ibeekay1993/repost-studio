import os
import subprocess
import sys
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ["ADMIN_TOKEN"] = "local-test-token-that-is-long-enough-0123456789"
os.environ["ENABLE_BACKGROUND_WORKER"] = "false"

from app import main


TEST_TOKEN = "local-test-token-that-is-long-enough-0123456789"
HEADERS = {"X-Admin-Token": TEST_TOKEN}
FIXTURE_MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2"


@pytest.fixture
def client(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    downloads_dir = tmp_path / "downloads"
    data_dir.mkdir()
    downloads_dir.mkdir()
    monkeypatch.setattr(main, "DATA_DIR", data_dir)
    monkeypatch.setattr(main, "DOWNLOADS_DIR", downloads_dir)
    monkeypatch.setattr(main, "DB_PATH", data_dir / "repost.db")
    monkeypatch.setattr(main, "WORKER_ENABLED", False)
    monkeypatch.setattr(main, "last_heartbeat", None)
    main.init_db()
    with TestClient(main.app) as test_client:
        yield test_client


def test_health_is_public_but_private_api_requires_token(client):
    assert client.get("/api/healthz").status_code == 200
    assert client.get("/api/state").status_code == 401
    assert client.get("/api/state", headers=HEADERS).status_code == 200


def test_missing_admin_token_locks_private_api(client, monkeypatch):
    monkeypatch.setattr(main, "ADMIN_TOKEN", "")
    response = client.get("/api/state")
    assert response.status_code == 503
    assert "Set a random ADMIN_TOKEN" in response.json()["detail"]
    monkeypatch.setattr(main, "ADMIN_TOKEN", "replace-with-a-random-secret-at-least-32-characters")
    assert client.get("/api/state").status_code == 503


def test_watchlist_crud_normalizes_and_keeps_video_history(client):
    created = client.post("/api/accounts", headers=HEADERS, json={"username": "https://www.tiktok.com/@Example.Creator"})
    assert created.status_code == 201
    account = created.json()
    assert account["username"] == "example.creator"
    assert client.post("/api/accounts", headers=HEADERS, json={"username": "@EXAMPLE.CREATOR"}).status_code == 409

    video = client.post(
        "/api/videos",
        headers=HEADERS,
        json={"url": "https://www.tiktok.com/@example.creator/video/1234567890123456789?is_from_webapp=1", "caption": "Original caption"},
    )
    assert video.status_code == 201
    paused = client.patch(f"/api/accounts/{account['id']}", headers=HEADERS, json={"enabled": False})
    assert paused.json()["enabled"] == 0
    resumed = client.patch(f"/api/accounts/{account['id']}", headers=HEADERS, json={"enabled": True})
    assert resumed.json()["enabled"] == 1

    assert client.delete(f"/api/accounts/{account['id']}", headers=HEADERS).status_code == 200
    state = client.get("/api/state", headers=HEADERS).json()
    assert state["accounts"] == []
    assert state["videos"][0]["account_id"] is None
    assert any(item["action"] == "watchlist_remove" for item in state["history"])


def test_direct_video_rejects_non_tiktok_and_duplicate_ids(client):
    payload = {"url": "https://www.tiktok.com/@creator/video/9876543210123456789", "caption": "Test"}
    first = client.post("/api/videos", headers=HEADERS, json=payload)
    assert first.status_code == 201
    assert first.json()["source_url"] == payload["url"]
    assert client.post("/api/videos", headers=HEADERS, json=payload).status_code == 409
    assert client.post("/api/videos", headers=HEADERS, json={"url": "https://example.com/@creator/video/12345"}).status_code == 400
    assert client.post("/api/videos", headers=HEADERS, json={"url": "https://www.tiktok.com.evil.example/@creator/video/12345"}).status_code == 400


def install_fake_downloader(monkeypatch, payload=FIXTURE_MP4, source_id="1234567890123456789"):
    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, _url, download=False):
            return {
                "entries": [
                    {
                        "id": source_id,
                        "webpage_url": f"https://www.tiktok.com/@sample/video/{source_id}",
                        "description": "Fixture caption",
                    }
                ]
            }

        def download(self, _urls):
            output = self.options["outtmpl"].replace("%(ext)s", "mp4")
            Path(output).write_bytes(payload)

    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=FakeYoutubeDL))
    monkeypatch.setattr(main, "find_ffmpeg_executable", lambda: None)


def test_scan_deduplicates_downloads_validates_and_hashes_fixture(client, monkeypatch):
    install_fake_downloader(monkeypatch)
    profile = client.post("/api/accounts", headers=HEADERS, json={"username": "sample"}).json()

    main.scan_account(profile["id"])
    main.scan_account(profile["id"])

    state = client.get("/api/state", headers=HEADERS).json()
    assert len(state["videos"]) == 1
    video = state["videos"][0]
    assert video["status"] == "downloaded"
    assert video["filepath"] == f"video_{video['id']}.mp4"
    assert video["media_size_bytes"] == len(FIXTURE_MP4)
    assert len(video["sha256"]) == 64
    assert video["validation_level"] == "container_signature"
    assert sum(item["action"] == "detected" for item in state["history"]) == 1


def test_download_rejects_invalid_media_and_keeps_safe_failure(client, monkeypatch):
    install_fake_downloader(monkeypatch, payload=b"not a video")
    video = client.post(
        "/api/videos",
        headers=HEADERS,
        json={"url": "https://www.tiktok.com/@sample/video/5555555555555555555"},
    ).json()

    main.download_video(video["id"])

    failed = client.get("/api/state", headers=HEADERS).json()["videos"][0]
    assert failed["status"] == "download_failed"
    assert "signature" in failed["error"].lower()
    assert failed["filepath"] == ""
    assert not list(main.DOWNLOADS_DIR.glob("_tmp_video_*"))


def test_ffmpeg_decodes_a_generated_video_fixture(tmp_path):
    import imageio_ffmpeg

    video_path = tmp_path / "decode-check.mp4"
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x64:r=4",
            "-t",
            "1",
            "-an",
            "-c:v",
            "mpeg4",
            "-pix_fmt",
            "yuv420p",
            str(video_path),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    validation, size = main.validate_media(video_path, 10 * 1024 * 1024)
    assert validation == "ffmpeg_decode"
    assert size > 0


def test_manual_publish_requires_permission_and_explicit_confirmation(client, monkeypatch):
    install_fake_downloader(monkeypatch)
    video = client.post(
        "/api/videos",
        headers=HEADERS,
        json={"url": "https://www.tiktok.com/@sample/video/7777777777777777777", "caption": "Caption for X"},
    ).json()
    main.download_video(video["id"])

    denied = client.post(f"/api/videos/{video['id']}/prepare-publish", headers=HEADERS, json={"permission_confirmed": False})
    assert denied.status_code == 400
    ready = client.post(f"/api/videos/{video['id']}/prepare-publish", headers=HEADERS, json={"permission_confirmed": True})
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready_to_post"
    assert ready.json()["caption"] == "Caption for X"

    invalid_url = client.post(
        f"/api/videos/{video['id']}/confirm-published",
        headers=HEADERS,
        json={"x_url": "https://example.com/post"},
    )
    assert invalid_url.status_code == 400
    confirmed = client.post(
        f"/api/videos/{video['id']}/confirm-published",
        headers=HEADERS,
        json={"x_url": "https://x.com/operator/status/123456789"},
    )
    assert confirmed.status_code == 200
    saved = client.get("/api/state", headers=HEADERS).json()["videos"][0]
    assert saved["status"] == "posted_confirmed"
    assert saved["x_url"] == "https://x.com/operator/status/123456789"


def test_media_route_rejects_unmanaged_path(client):
    item = client.post(
        "/api/videos",
        headers=HEADERS,
        json={"url": "https://www.tiktok.com/@sample/video/8888888888888888888"},
    ).json()
    with main.connect() as db:
        db.execute("UPDATE videos SET filepath=? WHERE id=?", ("../../secret.mp4", item["id"]))
    response = client.get(f"/api/videos/{item['id']}/file", headers=HEADERS)
    assert response.status_code == 404


def test_settings_are_bounded(client):
    good = client.put(
        "/api/settings",
        headers=HEADERS,
        json={"poll_interval_minutes": 20, "max_video_mb": 500, "retention_days": 10},
    )
    assert good.status_code == 200
    assert good.json()["poll_interval_minutes"] == 20
    bad = client.put(
        "/api/settings",
        headers=HEADERS,
        json={"poll_interval_minutes": 1, "max_video_mb": 500, "retention_days": 10},
    )
    assert bad.status_code == 422
