# Repost Studio

Repost Studio is a standalone local application for a small TikTok watchlist, a persistent video queue, and human-assisted posting to X. It has its own FastAPI backend, SQLite database, and managed download folder. It does not connect to Clipz-Stream, MedCode, Supabase, or an AWS account.

TikTok profile scans and downloads use yt-dlp on publicly accessible material. Availability can change, and a blocked profile is reported and backed off. The app does not use TikTok or X APIs and does not solve challenges or evade access controls. You must own or have permission to reuse every video.

## Run locally on Windows

Use Python 3.12 or newer.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Put the generated value in `.env` as `ADMIN_TOKEN`. Do not share or commit that file. The app refuses API access when the token is missing, a placeholder, or shorter than 32 characters.

The development requirements include a portable FFmpeg build for local media validation. A production installation should use the host's FFmpeg package. If neither is available, the app checks file size and the MP4/WebM container signature, and labels that lower validation level in the queue.

Start the local server with one worker:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000` and enter the token from `.env`. The token stays in the current browser tab's session storage. The server binds to loopback; do not change that binding for a remote deployment.

## Run locally on Linux or macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
cp .env.example .env
python -c 'import secrets; print(secrets.token_urlsafe(48))'
```

Add the generated value to `.env`, then start the single-worker server:

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## Local workflow

1. Add a profile you are authorized to monitor.
2. Select **Scan** to check it, or use **Scan active profiles**.
3. Review queue status and any retrieval error. New visible videos are downloaded automatically after a successful scan.
4. Edit the caption, confirm reuse permission, and select **Prepare for X**.
5. Repost Studio downloads the file, copies the caption where browser permissions allow, and opens X. Upload and publish manually.
6. Select **Confirm posted** only after the post is live. The app records your confirmation; it does not infer a successful post.

You can also add a canonical TikTok video link directly. Short links and arbitrary URLs are rejected.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

## Data and known limits

- SQLite state lives in `data/repost.db`.
- Managed media lives in `downloads/` and is ignored by Git.
- Poll interval defaults to 15 minutes. Failed scans back off; settings have bounded ranges.
- Duplicate source IDs are ignored. Watchlist removal preserves queued videos and history.
- Download validation stores file size, SHA-256, and the validation level. FFmpeg validation is stronger than a container-signature check.
- History is local to this installation. The prototype does not yet include tested backup/restore or AWS deployment configuration.
- No selected TikTok handle was included with the supplied request. The app can be tested locally with fixtures now; account-specific live retrieval needs the authorized profile URL or username.
- X browser automation is not included in this local build. Publishing is deliberately human-assisted.

AWS deployment is a later phase, after a real authorized profile has been validated locally and the deployment guide, backup/restore, HTTPS, and budget controls are ready.
