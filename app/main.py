from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
DATA_DIR = ROOT / "data"
DOWNLOADS_DIR = ROOT / "downloads"
DB_PATH = DATA_DIR / "repost.db"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()
WORKER_ENABLED = os.getenv("ENABLE_BACKGROUND_WORKER", "true").lower() in {"1", "true", "yes"}
LOG = logging.getLogger("repost_studio")
stop_event = threading.Event()
worker_thread: threading.Thread | None = None
worker_lock = threading.Lock()
last_heartbeat: str | None = None
last_scan_started: str | None = None
video_locks: dict[int, threading.Lock] = {}
video_locks_guard = threading.Lock()
profile_locks: dict[int, threading.Lock] = {}
profile_locks_guard = threading.Lock()

DEFAULT_SETTINGS = {
    "poll_interval_minutes": "15",
    "max_video_mb": "300",
    "retention_days": "7",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_text(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat(timespec="seconds")


def admin_token_is_configured() -> bool:
    lowered = ADMIN_TOKEN.casefold()
    placeholders = ("replace-with-", "change-me-", "your-admin-token")
    return len(ADMIN_TOKEN) >= 32 and not lowered.startswith(placeholders)


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    return db


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                last_checked_at TEXT,
                last_success_at TEXT,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                next_check_at TEXT
            );
            CREATE TABLE IF NOT EXISTS videos (
                id INTEGER PRIMARY KEY,
                account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
                source_id TEXT NOT NULL UNIQUE,
                source_url TEXT NOT NULL,
                source_caption TEXT NOT NULL DEFAULT '',
                caption TEXT NOT NULL DEFAULT '',
                filepath TEXT NOT NULL DEFAULT '',
                media_size_bytes INTEGER,
                sha256 TEXT,
                validation_level TEXT NOT NULL DEFAULT '',
                download_attempts INTEGER NOT NULL DEFAULT 0,
                permission_confirmed INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'discovered',
                error TEXT NOT NULL DEFAULT '',
                x_url TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                posted_at TEXT
            );
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY,
                video_id INTEGER REFERENCES videos(id) ON DELETE SET NULL,
                account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
                action TEXT NOT NULL,
                outcome TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_videos_status_created
                ON videos(status, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_history_created
                ON history(created_at DESC);
            """
        )
        # Keep this small migration path so early prototype databases remain usable.
        account_columns = {row["name"] for row in db.execute("PRAGMA table_info(accounts)")}
        account_additions = {
            "last_checked_at": "TEXT",
            "last_success_at": "TEXT",
            "consecutive_failures": "INTEGER NOT NULL DEFAULT 0",
            "next_check_at": "TEXT",
        }
        for name, declaration in account_additions.items():
            if name not in account_columns:
                db.execute(f"ALTER TABLE accounts ADD COLUMN {name} {declaration}")
        columns = {row["name"] for row in db.execute("PRAGMA table_info(videos)")}
        additions = {
            "source_caption": "TEXT NOT NULL DEFAULT ''",
            "media_size_bytes": "INTEGER",
            "sha256": "TEXT",
            "validation_level": "TEXT NOT NULL DEFAULT ''",
            "download_attempts": "INTEGER NOT NULL DEFAULT 0",
            "permission_confirmed": "INTEGER NOT NULL DEFAULT 0",
            "posted_at": "TEXT",
        }
        for name, declaration in additions.items():
            if name not in columns:
                db.execute(f"ALTER TABLE videos ADD COLUMN {name} {declaration}")
        for key, value in DEFAULT_SETTINGS.items():
            db.execute(
                "INSERT OR IGNORE INTO settings(key,value,updated_at) VALUES(?,?,?)",
                (key, value, utc_text()),
            )


def rows(sql: str, args: tuple = ()) -> list[dict]:
    with connect() as db:
        return [dict(row) for row in db.execute(sql, args).fetchall()]


def row(sql: str, args: tuple = ()) -> dict | None:
    found = rows(sql, args)
    return found[0] if found else None


def record_history(
    action: str,
    outcome: str,
    detail: str = "",
    video_id: int | None = None,
    account_id: int | None = None,
) -> None:
    safe_detail = re.sub(r"(?i)(authorization|cookie|token|password)=\S+", r"\1=[redacted]", detail)
    with connect() as db:
        db.execute(
            "INSERT INTO history(video_id,account_id,action,outcome,detail,created_at) VALUES(?,?,?,?,?,?)",
            (video_id, account_id, action[:80], outcome[:80], safe_detail[:500], utc_text()),
        )


def get_settings() -> dict[str, int]:
    values = {item["key"]: item["value"] for item in rows("SELECT key,value FROM settings")}
    return {
        "poll_interval_minutes": int(values.get("poll_interval_minutes", "15")),
        "max_video_mb": int(values.get("max_video_mb", "300")),
        "retention_days": int(values.get("retention_days", "7")),
    }


def update_settings(values: dict[str, int]) -> dict[str, int]:
    bounds = {
        "poll_interval_minutes": (5, 180),
        "max_video_mb": (10, 2048),
        "retention_days": (1, 90),
    }
    for key, value in values.items():
        low, high = bounds[key]
        if not low <= value <= high:
            raise HTTPException(400, f"{key.replace('_', ' ').capitalize()} must be between {low} and {high}.")
    with connect() as db:
        for key, value in values.items():
            db.execute(
                "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (key, str(value), utc_text()),
            )
    return get_settings()


def safe_error(exc: Exception) -> str:
    message = str(exc).replace("\r", " ").replace("\n", " ")
    message = re.sub(r"https?://\S+", "[source URL]", message)
    message = re.sub(r"(?i)(authorization|cookie|token|password)=\S+", r"\1=[redacted]", message)
    return (message or exc.__class__.__name__)[:300]


def normalize_username(value: str) -> str:
    raw = value.strip()
    if raw.startswith("https://") or raw.startswith("http://"):
        parsed = urlsplit(raw)
        if parsed.hostname not in {"tiktok.com", "www.tiktok.com"} or parsed.username or parsed.password:
            raise HTTPException(400, "Use a TikTok profile URL or username.")
        match = re.fullmatch(r"/@([A-Za-z0-9_.]{2,24})/?", parsed.path)
        if not match:
            raise HTTPException(400, "Enter a profile URL such as https://www.tiktok.com/@creator.")
        raw = match.group(1)
    else:
        raw = raw.lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_.]{2,24}", raw):
        raise HTTPException(400, "Username must be 2–24 letters, numbers, dots, or underscores.")
    return raw.lower()


def canonical_video_url(value: str) -> tuple[str, str]:
    parsed = urlsplit(value.strip())
    if parsed.scheme != "https" or parsed.hostname not in {"tiktok.com", "www.tiktok.com"}:
        raise HTTPException(400, "Use a public TikTok video URL on tiktok.com.")
    try:
        has_custom_port = parsed.port is not None
    except ValueError as exc:
        raise HTTPException(400, "The video URL has an invalid port.") from exc
    if parsed.username or parsed.password or has_custom_port:
        raise HTTPException(400, "The video URL must not contain login details or a custom port.")
    match = re.fullmatch(r"/@([A-Za-z0-9_.]{2,24})/video/(\d{1,30})/?", parsed.path)
    if not match:
        raise HTTPException(400, "Use a TikTok video URL in the format /@creator/video/123456.")
    username, source_id = match.groups()
    return source_id, f"https://www.tiktok.com/@{username}/video/{source_id}"


def get_video(video_id: int) -> dict:
    item = row("SELECT * FROM videos WHERE id=?", (video_id,))
    if not item:
        raise HTTPException(404, "Queue item not found.")
    return item


def managed_media_path(video: dict) -> Path | None:
    filename = video.get("filepath") or ""
    if not filename:
        return None
    if Path(filename).name != filename or not re.fullmatch(r"video_\d+\.(mp4|m4v|mov|webm|mkv)", filename, re.I):
        return None
    path = DOWNLOADS_DIR / filename
    try:
        if path.is_symlink() or path.resolve().parent != DOWNLOADS_DIR.resolve():
            return None
    except OSError:
        return None
    return path


def ytdlp_json(url: str) -> dict:
    import yt_dlp

    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "playlistend": 12,
        "socket_timeout": 20,
        "noplaylist": False,
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        return ydl.extract_info(url, download=False)


def ingest_account(account: dict) -> int:
    import yt_dlp  # fail clearly if the downloader is not installed

    profile_url = f"https://www.tiktok.com/@{account['username']}"
    info = ytdlp_json(profile_url)
    entries = (info or {}).get("entries") or [info]
    added = 0
    for entry in entries:
        if not entry:
            continue
        source_id = str(entry.get("id") or "")
        if not source_id.isdigit() or len(source_id) > 30:
            continue
        link = entry.get("webpage_url") or ""
        try:
            normalized_id, source_url = canonical_video_url(str(link))
            if normalized_id != source_id:
                source_url = f"https://www.tiktok.com/@{account['username']}/video/{source_id}"
        except HTTPException:
            source_url = f"https://www.tiktok.com/@{account['username']}/video/{source_id}"
        source_caption = str(entry.get("description") or entry.get("title") or "")[:280]
        now = utc_text()
        video_id = None
        with connect() as db:
            result = db.execute(
                "INSERT OR IGNORE INTO videos(account_id,source_id,source_url,source_caption,caption,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'discovered',?,?)",
                (account["id"], source_id, source_url, source_caption, source_caption, now, now),
            )
            if result.rowcount:
                added += 1
                video_id = int(result.lastrowid)
        if video_id is not None:
            record_history("detected", "success", "New video added to the queue.", video_id=video_id, account_id=account["id"])
    return added


def validate_media(path: Path, max_bytes: int) -> tuple[str, int]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Downloaded file is missing or unsafe.")
    size = path.stat().st_size
    if size <= 0:
        raise ValueError("Downloaded file is empty.")
    if size > max_bytes:
        raise ValueError("Video exceeds the configured file-size limit.")
    with path.open("rb") as media:
        header = media.read(16)
    if len(header) < 8:
        raise ValueError("Downloaded file is too short to be a video.")
    if b"ftyp" in header[4:12]:
        level = "container_signature"
    elif header.startswith(b"\x1aE\xdf\xa3"):
        level = "container_signature"
    else:
        raise ValueError("Downloaded file does not have a supported MP4, MOV, WebM, or MKV signature.")

    ffmpeg = find_ffmpeg_executable()
    if ffmpeg:
        try:
            result = subprocess.run(
                [ffmpeg, "-v", "error", "-i", str(path), "-f", "null", "-"],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ValueError("FFmpeg validation timed out.") from exc
        if result.returncode:
            raise ValueError("FFmpeg could not decode the downloaded video.")
        level = "ffmpeg_decode"
    return level, size


def find_ffmpeg_executable() -> str | None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        return ffmpeg
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError):
        return None


def _lock_for_video(video_id: int) -> threading.Lock:
    with video_locks_guard:
        return video_locks.setdefault(video_id, threading.Lock())


def download_video(video_id: int) -> None:
    lock = _lock_for_video(video_id)
    if not lock.acquire(blocking=False):
        return
    temp_prefix = f"_tmp_video_{video_id}"
    try:
        video = get_video(video_id)
        if video["status"] in {"downloading", "downloaded", "ready_to_post", "posted_confirmed"}:
            return
        settings = get_settings()
        max_bytes = settings["max_video_mb"] * 1024 * 1024
        with connect() as db:
            db.execute(
                "UPDATE videos SET status='downloading',error='',download_attempts=download_attempts+1,updated_at=? WHERE id=?",
                (utc_text(), video_id),
            )
        record_history("download", "started", "Video download started.", video_id=video_id, account_id=video.get("account_id"))
        import yt_dlp

        prefix = DOWNLOADS_DIR / temp_prefix
        options = {
            "outtmpl": str(prefix) + ".%(ext)s",
            "format": "best[ext=mp4]/best",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "max_filesize": max_bytes,
            "socket_timeout": 30,
            "overwrites": True,
        }
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([video["source_url"]])
        candidates = [p for p in DOWNLOADS_DIR.glob(temp_prefix + ".*") if not p.name.endswith((".part", ".ytdl"))]
        if not candidates:
            raise RuntimeError("Downloader completed without creating a video file.")
        candidate = max(candidates, key=lambda path: path.stat().st_mtime)
        if candidate.resolve().parent != DOWNLOADS_DIR.resolve():
            raise RuntimeError("Downloader wrote outside managed media storage.")
        validation, size = validate_media(candidate, max_bytes)
        extension = candidate.suffix.lower()
        if extension not in {".mp4", ".m4v", ".mov", ".webm", ".mkv"}:
            raise RuntimeError("Downloader returned an unsupported video format.")
        final_path = DOWNLOADS_DIR / f"video_{video_id}{extension}"
        os.replace(candidate, final_path)
        digest = hashlib.sha256()
        with final_path.open("rb") as media:
            for chunk in iter(lambda: media.read(1024 * 1024), b""):
                digest.update(chunk)
        with connect() as db:
            db.execute(
                "UPDATE videos SET filepath=?,media_size_bytes=?,sha256=?,validation_level=?,status='downloaded',error='',updated_at=? WHERE id=?",
                (final_path.name, size, digest.hexdigest(), validation, utc_text(), video_id),
            )
        record_history(
            "download",
            "success",
            f"Downloaded {size} bytes; validation: {validation}.",
            video_id=video_id,
            account_id=video.get("account_id"),
        )
    except Exception as exc:
        error = safe_error(exc)
        with connect() as db:
            db.execute(
                "UPDATE videos SET status='download_failed',error=?,updated_at=? WHERE id=?",
                (error, utc_text(), video_id),
            )
        video = row("SELECT account_id FROM videos WHERE id=?", (video_id,)) or {}
        record_history("download", "failed", error, video_id=video_id, account_id=video.get("account_id"))
        LOG.warning("Download failed for queue item %s: %s", video_id, error)
    finally:
        for path in DOWNLOADS_DIR.glob(temp_prefix + ".*"):
            try:
                if path.is_file() and not path.is_symlink():
                    path.unlink()
            except OSError:
                pass
        lock.release()


def _lock_for_profile(account_id: int) -> threading.Lock:
    with profile_locks_guard:
        return profile_locks.setdefault(account_id, threading.Lock())


def scan_account(account_id: int, respect_schedule: bool = False) -> None:
    lock = _lock_for_profile(account_id)
    if not lock.acquire(blocking=False):
        return
    global last_scan_started
    last_scan_started = utc_text()
    try:
        account = row("SELECT * FROM accounts WHERE id=?", (account_id,))
        if not account or not account["enabled"]:
            return
        if respect_schedule and account.get("next_check_at") and account["next_check_at"] > utc_text():
            return
        try:
            count = ingest_account(account)
            now = utc_now()
            interval = get_settings()["poll_interval_minutes"]
            with connect() as db:
                db.execute(
                    "UPDATE accounts SET last_checked_at=?,last_success_at=?,consecutive_failures=0,next_check_at=? WHERE id=?",
                    (utc_text(now), utc_text(now), utc_text(now + timedelta(minutes=interval)), account_id),
                )
            record_history("scan", "success", f"Scan complete; {count} new item(s).", account_id=account_id)
        except Exception as exc:
            failures = int(account.get("consecutive_failures") or 0) + 1
            interval = min(get_settings()["poll_interval_minutes"] * (2 ** min(failures, 5)), 360)
            now = utc_now()
            error = safe_error(exc)
            with connect() as db:
                db.execute(
                    "UPDATE accounts SET last_checked_at=?,consecutive_failures=?,next_check_at=? WHERE id=?",
                    (utc_text(now), failures, utc_text(now + timedelta(minutes=interval)), account_id),
                )
            record_history("scan", "failed", error, account_id=account_id)
            LOG.info("Scan failed for profile %s (%s failures): %s", account["username"], failures, error)
        pending = rows("SELECT id FROM videos WHERE account_id=? AND status='discovered' ORDER BY id LIMIT 3", (account_id,))
        for video in pending:
            download_video(video["id"])
    finally:
        lock.release()


def cleanup_old_media() -> int:
    cutoff = utc_text(utc_now() - timedelta(days=get_settings()["retention_days"]))
    handled = rows(
        "SELECT * FROM videos WHERE status IN ('posted_confirmed','skipped') AND filepath<>'' AND updated_at<=?",
        (cutoff,),
    )
    removed = 0
    for video in handled:
        path = managed_media_path(video)
        if path and path.exists():
            try:
                path.unlink()
                removed += 1
            except OSError as exc:
                LOG.warning("Could not remove retained media for item %s: %s", video["id"], safe_error(exc))
                continue
        with connect() as db:
            db.execute("UPDATE videos SET filepath='',media_size_bytes=NULL,sha256=NULL,updated_at=? WHERE id=?", (utc_text(), video["id"]))
        record_history("cleanup", "success", "Handled video file removed; history retained.", video_id=video["id"])
    return removed


def monitor_once() -> None:
    global last_heartbeat
    with worker_lock:
        last_heartbeat = utc_text()
        for account in rows("SELECT id FROM accounts WHERE enabled=1 ORDER BY id"):
            scan_account(account["id"], respect_schedule=True)
        pending = rows("SELECT id FROM videos WHERE status='discovered' ORDER BY id LIMIT 3")
        for video in pending:
            download_video(video["id"])
        cleanup_old_media()
        last_heartbeat = utc_text()


def worker_loop() -> None:
    while not stop_event.is_set():
        try:
            monitor_once()
        except Exception:
            LOG.exception("Background monitor cycle failed.")
        interval = get_settings()["poll_interval_minutes"]
        stop_event.wait(interval * 60)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global worker_thread
    init_db()
    if WORKER_ENABLED and (worker_thread is None or not worker_thread.is_alive()):
        stop_event.clear()
        worker_thread = threading.Thread(target=worker_loop, name="repost-monitor", daemon=True)
        worker_thread.start()
    yield
    stop_event.set()
    if worker_thread and worker_thread.is_alive():
        worker_thread.join(timeout=2)


app = FastAPI(title="Repost Studio", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.middleware("http")
async def protect_private_api(request: Request, call_next):
    path = request.url.path
    if path == "/api/healthz":
        response = await call_next(request)
    elif path == "/api" or path.startswith("/api/"):
        if not admin_token_is_configured():
            response = JSONResponse(
                {"detail": "Dashboard is locked. Set a random ADMIN_TOKEN of at least 32 characters in .env and restart."},
                status_code=503,
            )
        else:
            supplied = request.headers.get("x-admin-token", "")
            if not hmac.compare_digest(supplied, ADMIN_TOKEN):
                response = JSONResponse({"detail": "Dashboard token is missing or incorrect."}, status_code=401)
            else:
                response = await call_next(request)
    else:
        response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "DENY")
    if path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.get("/", response_class=HTMLResponse)
def home():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/healthz")
def healthz():
    return {"ok": True, "service": "repost-studio"}


@app.get("/api/state")
def state():
    accounts = rows("SELECT * FROM accounts ORDER BY id DESC")
    videos = rows(
        "SELECT v.*,a.username FROM videos v LEFT JOIN accounts a ON a.id=v.account_id ORDER BY v.id DESC LIMIT 100"
    )
    history = rows(
        "SELECT h.*,v.source_url,a.username FROM history h LEFT JOIN videos v ON v.id=h.video_id "
        "LEFT JOIN accounts a ON a.id=h.account_id ORDER BY h.id DESC LIMIT 50"
    )
    media_bytes = sum(path.stat().st_size for path in DOWNLOADS_DIR.iterdir() if path.is_file() and not path.is_symlink())
    return {
        "accounts": accounts,
        "videos": videos,
        "history": history,
        "settings": get_settings(),
        "worker": {
            "enabled": WORKER_ENABLED,
            "heartbeat_at": last_heartbeat,
            "last_scan_started_at": last_scan_started,
            "media_bytes": media_bytes,
        },
    }


class AccountIn(BaseModel):
    username: str = Field(min_length=2, max_length=200)


class AccountUpdate(BaseModel):
    enabled: bool


@app.post("/api/accounts", status_code=201)
def add_account(data: AccountIn):
    username = normalize_username(data.username)
    with connect() as db:
        try:
            db.execute("INSERT INTO accounts(username,enabled,created_at) VALUES(?,1,?)", (username, utc_text()))
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "That profile is already on the watchlist.") from exc
        account_id = db.execute("SELECT id FROM accounts WHERE username=?", (username,)).fetchone()["id"]
    record_history("watchlist_add", "success", f"Added @{username} to the watchlist.", account_id=account_id)
    return row("SELECT * FROM accounts WHERE id=?", (account_id,))


@app.patch("/api/accounts/{account_id}")
def update_account(account_id: int, data: AccountUpdate):
    account = row("SELECT * FROM accounts WHERE id=?", (account_id,))
    if not account:
        raise HTTPException(404, "Profile not found.")
    with connect() as db:
        db.execute("UPDATE accounts SET enabled=? WHERE id=?", (int(data.enabled), account_id))
    action = "profile_resume" if data.enabled else "profile_pause"
    record_history(action, "success", f"{'Resumed' if data.enabled else 'Paused'} @{account['username']}.", account_id=account_id)
    return row("SELECT * FROM accounts WHERE id=?", (account_id,))


@app.delete("/api/accounts/{account_id}")
def delete_account(account_id: int):
    account = row("SELECT * FROM accounts WHERE id=?", (account_id,))
    if not account:
        raise HTTPException(404, "Profile not found.")
    with connect() as db:
        db.execute("UPDATE videos SET account_id=NULL WHERE account_id=?", (account_id,))
        db.execute("DELETE FROM accounts WHERE id=?", (account_id,))
    record_history("watchlist_remove", "success", f"Removed @{account['username']}; queued videos were retained.")
    return {"ok": True}


def start_scan(account_id: int) -> None:
    threading.Thread(target=scan_account, args=(account_id, False), name=f"scan-profile-{account_id}", daemon=True).start()


@app.post("/api/scan")
def scan_all():
    active = rows("SELECT id FROM accounts WHERE enabled=1")
    for account in active:
        start_scan(account["id"])
    return {"ok": True, "started": len(active)}


@app.post("/api/accounts/{account_id}/scan")
def scan_one(account_id: int):
    account = row("SELECT id,enabled FROM accounts WHERE id=?", (account_id,))
    if not account:
        raise HTTPException(404, "Profile not found.")
    if not account["enabled"]:
        raise HTTPException(409, "Resume this profile before scanning it.")
    start_scan(account_id)
    return {"ok": True, "started": True}


class VideoIn(BaseModel):
    url: str = Field(min_length=1, max_length=1000)
    caption: str = Field(default="", max_length=280)


@app.post("/api/videos", status_code=201)
def add_video(data: VideoIn):
    source_id, source_url = canonical_video_url(data.url)
    now = utc_text()
    with connect() as db:
        try:
            result = db.execute(
                "INSERT INTO videos(source_id,source_url,source_caption,caption,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (source_id, source_url, data.caption, data.caption, "discovered", now, now),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "That video is already in the queue.") from exc
        video_id = int(result.lastrowid)
    record_history("video_added", "success", "Video added from a direct link.", video_id=video_id)
    return get_video(video_id)


class CaptionIn(BaseModel):
    caption: str = Field(max_length=280)


@app.patch("/api/videos/{video_id}")
def update_caption(video_id: int, data: CaptionIn):
    video = get_video(video_id)
    with connect() as db:
        db.execute("UPDATE videos SET caption=?,updated_at=? WHERE id=?", (data.caption, utc_text(), video_id))
    record_history("caption_edit", "success", "X caption saved.", video_id=video_id, account_id=video.get("account_id"))
    return {"ok": True}


@app.post("/api/videos/{video_id}/download")
def queue_download(video_id: int):
    video = get_video(video_id)
    if video["status"] == "downloading":
        raise HTTPException(409, "This video is already downloading.")
    if video["status"] == "posted_confirmed":
        raise HTTPException(409, "This video is already marked as posted.")
    threading.Thread(target=download_video, args=(video_id,), name=f"download-{video_id}", daemon=True).start()
    return {"ok": True, "started": True}


@app.get("/api/videos/{video_id}/file")
def download_file(video_id: int):
    video = get_video(video_id)
    path = managed_media_path(video)
    if not path or not path.is_file():
        raise HTTPException(404, "Validated video file is not available.")
    return FileResponse(path, filename=f"repost-{video_id}{path.suffix}", media_type="application/octet-stream")


class PreparePublishIn(BaseModel):
    permission_confirmed: bool = False


@app.post("/api/videos/{video_id}/prepare-publish")
def prepare_publish(video_id: int, data: PreparePublishIn):
    video = get_video(video_id)
    path = managed_media_path(video)
    if not path or not path.is_file() or video["status"] not in {"downloaded", "ready_to_post"}:
        raise HTTPException(409, "Download and validate the video before preparing it for X.")
    if not data.permission_confirmed:
        raise HTTPException(400, "Confirm that you own or have permission to reuse this video.")
    with connect() as db:
        db.execute(
            "UPDATE videos SET permission_confirmed=1,status='ready_to_post',updated_at=? WHERE id=?",
            (utc_text(), video_id),
        )
    record_history(
        "prepare_for_x",
        "ready_for_human_publish",
        "Permission confirmed. Caption and video are ready for manual upload in X.",
        video_id=video_id,
        account_id=video.get("account_id"),
    )
    return {"ok": True, "status": "ready_to_post", "caption": video["caption"], "file": path.name}


class ConfirmPostedIn(BaseModel):
    x_url: str = Field(default="", max_length=500)


@app.post("/api/videos/{video_id}/confirm-published")
def confirm_published(video_id: int, data: ConfirmPostedIn):
    video = get_video(video_id)
    if video["status"] not in {"ready_to_post", "downloaded"} or not video["permission_confirmed"]:
        raise HTTPException(409, "Prepare the item and confirm reuse permission before recording publication.")
    x_url = data.x_url.strip()
    if x_url:
        parsed = urlsplit(x_url)
        if parsed.scheme != "https" or parsed.hostname not in {"x.com", "www.x.com"} or not re.fullmatch(r"/@?[A-Za-z0-9_]{1,20}/status/\d+/?", parsed.path):
            raise HTTPException(400, "Enter a public X post URL or leave it blank.")
    now = utc_text()
    with connect() as db:
        db.execute(
            "UPDATE videos SET status='posted_confirmed',x_url=?,posted_at=?,updated_at=? WHERE id=?",
            (x_url, now, now, video_id),
        )
    record_history(
        "publish_confirmation",
        "operator_confirmed",
        "Operator confirmed the post was published manually.",
        video_id=video_id,
        account_id=video.get("account_id"),
    )
    return {"ok": True, "status": "posted_confirmed"}


@app.get("/api/history")
def history():
    return rows(
        "SELECT h.*,v.source_url,a.username FROM history h LEFT JOIN videos v ON v.id=h.video_id "
        "LEFT JOIN accounts a ON a.id=h.account_id ORDER BY h.id DESC LIMIT 100"
    )


class SettingsIn(BaseModel):
    poll_interval_minutes: int = Field(ge=5, le=180)
    max_video_mb: int = Field(ge=10, le=2048)
    retention_days: int = Field(ge=1, le=90)


@app.get("/api/settings")
def settings():
    return get_settings()


@app.put("/api/settings")
def save_settings(data: SettingsIn):
    return update_settings(data.model_dump())


