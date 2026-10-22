# REPOST STUDIO

**Project Design Document, Product Requirements, Technical Plan & Master Build Prompt**  
Version 1.0 | 22 October 2026 | Standalone AWS-hosted prototype

> This Markdown version is a project-ready companion to the DOCX. It includes the product requirements and master coding-agent prompt. The DOCX contains the full formatted specification.

## Purpose
Repost Studio is a standalone application that monitors selected TikTok profiles, detects new posts, downloads eligible videos into a review queue, and helps an operator initiate publication to X with a deliberate click. It must not depend on Clipz-Stream, MedCode, Supabase, or any other existing project.

**Important boundary:** No official TikTok API or X API is assumed. The default publishing flow is human-assisted. Browser-based posting is optional, experimental, and may be restricted or unreliable. Never bypass CAPTCHAs, rate limits, account restrictions, or access controls.

## Product decisions
- Dedicated new AWS account.
- Python 3.12+, FastAPI, HTML/CSS/vanilla JS, SQLite, yt-dlp where supported and permitted, FFmpeg, pytest.
- One small EC2 instance initially, after checking account-specific Free Tier/credit eligibility and creating a budget alert.
- Human-assisted X publishing as default; experimental Playwright posting disabled by default.
- No dependency on existing Clipz-Stream or MedCode infrastructure.
- One operator; small watchlist; no multi-tenant SaaS in MVP.

## Core user journey
1. Add supported TikTok profile.
2. Worker polls conservatively (default 15 minutes) with jitter/backoff.
3. Normalise and deduplicate post IDs.
4. Queue eligible post and download where permitted.
5. Validate media, hash it, and store in app-managed directory.
6. Operator reviews video, edits caption, and confirms reuse permission.
7. Click “Post to X”; default flow prepares caption and file, opens X, and leaves final upload/publish to the human.
8. Record confirmed publication only after explicit confirmation or reliable evidence.
9. Retain history and clean old media safely.

## Main screens
- Overview: active profiles, new items, awaiting review, failures, worker heartbeat, last scan, disk usage.
- Watchlist: add/pause/resume/scan/remove profiles, last checked and failure count.
- Queue: status filters, creator, caption, source URL, download status, retries.
- Item detail: preview/download, editable caption, permission confirmation, prepare/post action, safe error detail.
- History: detection/download/publish attempt/confirmation timestamps.
- Settings: polling interval, retention, max file size, timezone; no secrets.

## Architecture
| Layer | MVP |
|---|---|
| UI | FastAPI-served HTML/CSS/JS |
| Internal API | FastAPI |
| Monitor | Python worker with single-worker protection |
| Downloader | yt-dlp adapter where supported and permitted |
| Media validation | FFmpeg + file signature/MIME/size/duration checks |
| Database | SQLite with unique constraints and foreign keys |
| Storage | EBS-backed app-managed directory |
| Publisher | Human-assisted default; optional experimental browser adapter |
| Process supervision | systemd |
| HTTPS | Caddy or Nginx |
| Tests | pytest |

## Suggested repository layout
```text
repost-studio/
  app/
    main.py
    config.py
    auth.py
    db.py
    models.py
    repositories/
    services/
    workers/
    templates/index.html
    static/app.css
    static/app.js
  tests/unit/
  tests/integration/
  data/                 # runtime only, gitignored
  downloads/            # runtime only, gitignored
  scripts/backup.sh
  scripts/healthcheck.sh
  deploy/repost-studio.service
  deploy/Caddyfile.example
  .env.example
  .gitignore
  requirements.txt
  README.md
  PROJECT_DESIGN.md
  SECURITY.md
  docs/IMPLEMENTATION_STATUS.md
```

## Data model
- `profiles`: id, platform, handle, profile_url, active, poll_interval_seconds, last_checked_at, last_success_at, consecutive_failures, created_at, updated_at.
- `source_posts`: id, profile_id, platform_post_id, source_url, source_caption, posted_at, first_seen_at, last_seen_at, status; unique platform + post ID.
- `jobs`: id, source_post_id, type, status, attempts, next_attempt_at, started_at, finished_at, last_error_code, last_error_safe, local_media_path, media_size_bytes, sha256, caption_for_x, created_at, updated_at.
- `publish_events`: id, job_id, action, outcome, attempted_at, confirmed_at, confirmation_method, safe_message.
- `settings`: key, value, updated_at. Store non-secret settings only.

Job states: `DISCOVERED`, `DOWNLOAD_QUEUED`, `DOWNLOADING`, `DOWNLOADED`, `READY_TO_POST`, `POST_ATTEMPTED`, `POSTED_CONFIRMED`, `DOWNLOAD_FAILED`, `POST_FAILED`, `POSTING_NEEDS_CONFIRMATION`, `HELD_FOR_PERMISSION_REVIEW`, `SKIPPED`.

## Internal API endpoints
These are endpoints for the application, not TikTok/X APIs:
- `GET /`, `GET /healthz`
- `GET/POST /api/profiles`
- `PATCH/DELETE /api/profiles/{id}`
- `POST /api/profiles/{id}/scan`
- `GET /api/queue`, `GET /api/posts/{id}`
- `PATCH /api/posts/{id}/caption`
- `POST /api/posts/{id}/download`
- `GET /api/posts/{id}/media`
- `POST /api/posts/{id}/prepare-publish`
- `POST /api/posts/{id}/publish-attempt` (optional experimental only)
- `POST /api/posts/{id}/confirm-published`
- `GET /api/history`
- `GET/PUT /api/settings`

All private routes require authentication. Use CSRF protection, strict validation, pagination, rate limits, safe file paths, and SSRF protections.

## Security and operations
- New AWS account, root MFA, least-privilege IAM, no root access keys.
- Dashboard behind HTTPS and authentication; restrict SSH.
- Never commit `.env`, session cookies, browser profiles, databases, logs with secrets, or videos.
- Treat browser session data as credentials and keep it outside the repository.
- Validate media and generate local filenames; do not trust remote filenames.
- Retention default can be 7 days after handling; preserve history.
- Warn at 75% disk use; pause downloads at 90%.
- Create budget alerts before launching resources.
- Avoid NAT Gateway, RDS, S3, Lambda, SQS, and other managed services for MVP unless justified and approved.
- Include SQLite backup/restore and test a restore.

## Test and release gates
- Unit tests for URL normalisation, duplicate detection, state transitions, retries, captions, auth/CSRF, path traversal, media validation, cleanup, worker restart, and publish ambiguity.
- Integration test a permitted test profile; blocked profiles should back off gracefully.
- Test manual X publishing flow end-to-end.
- If browser automation is tested, any challenge or ambiguous result must halt and require human intervention.
- Do not claim tests, deployment, or successful publishing unless supported by evidence.

## Master prompt for Codex / coding agent

```text
You are the product manager, senior full-stack engineer, UX/UI designer, QA lead, security reviewer, and DevOps engineer for Repost Studio.

Build a fresh, independent MVP that monitors selected publicly accessible TikTok profiles, detects new posts, downloads eligible videos where access and reuse are permitted, queues them, and lets the operator click a clear “Post to X” action. Do not depend on Clipz-Stream, MedCode, Supabase, or any previous project.

No official TikTok or X API is assumed. The reliable default publishing flow must be human-assisted: prepare caption, make video downloadable, open X, let the human complete upload/publish, then allow explicit confirmation. Any browser-based X posting must be optional, experimental, disabled by default, and must not bypass CAPTCHA, rate limits, account restrictions, or access controls. Stop and require a human if challenges or ambiguous outcomes occur.

Use Python 3.12+, FastAPI, HTML/CSS/vanilla JS, SQLite, yt-dlp where supported and permitted, FFmpeg, pytest, systemd, and Caddy/Nginx. Keep source extraction and publishing behind interfaces. Do not add Supabase, RDS, S3, Lambda, SQS, Redis, or other managed services unless justified and approved.

Build screens: Overview, Watchlist, Queue, Item Detail, History, Settings. Implement the data model and job states in PROJECT_DESIGN.md. Use persistent job state, unique source IDs, idempotent retries, safe file validation, retention cleanup, and an auditable history.

Security is mandatory: environment-based secrets, no committed .env/session data/media/database, strong admin authentication, CSRF protection, HTTPS in deployment, least-privilege AWS, safe paths, SSRF prevention, log redaction, disk thresholds, backups, and tested restore.

First inspect the repository and runtime. Then implement end-to-end in phases: foundation/auth/schema; watchlist; source adapter/monitoring/deduplication; downloads/media validation; queue UI; human-assisted X workflow; tests; AWS deployment docs. Do not stop after writing a plan. After each phase run relevant tests and fix failures. Maintain docs/IMPLEMENTATION_STATUS.md with exact test commands/results and known limitations.

Acceptance criteria:
- Clean install works from README.
- Authentication protects private routes.
- Watchlist CRUD and pause/resume work.
- Repeated scans do not duplicate posts.
- A supported test video downloads, validates, and enters the queue.
- Caption edits persist.
- Human-assisted X publishing works without X API and never falsely reports success.
- Restart preserves queue/history.
- Failed jobs can be retried safely.
- Cleanup cannot delete outside managed storage.
- No secrets/session/media files are committed.
- Tests and deployment steps are accurately reported.
- AWS deployment includes budget alerts and hardening.

Deliver complete source, PROJECT_DESIGN.md, README.md, SECURITY.md, .env.example, requirements.txt, tests, systemd/reverse-proxy examples, backup/restore scripts, and IMPLEMENTATION_STATUS.md. Never claim a test, deployment, or live success unless it actually ran and output supports the claim. Keep browser publishing disabled by default.
```

## AWS rollout checklist
1. Create the new AWS account and enable root MFA.
2. Verify current account-specific Free Tier/credit eligibility and region.
3. Create budget alerts before launching EC2.
4. Create a fresh private Git repository.
5. Choose one authorised test profile and one X account for pilot.
6. Test locally, then deploy behind HTTPS with authentication.
7. Test SQLite backup/restore.
8. Run a 24–72 hour pilot and document missed detections/failures.
9. Keep human-assisted X publishing as default unless browser automation is reviewed and explicitly accepted.

## Definition of done
The MVP is done when an operator can securely manage a watchlist, detect and deduplicate accessible posts, download and validate an eligible video, edit a caption, initiate the human-assisted X workflow, record the confirmed result, recover from errors, and restart without losing the queue.
