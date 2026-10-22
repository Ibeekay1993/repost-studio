# Security notes

## Local dashboard

- The dashboard is intended to bind to `127.0.0.1` for local use.
- All `/api/*` routes except `/api/healthz` require the `X-Admin-Token` header. A missing, placeholder, or weak token locks API access.
- Keep `.env` private. The token is stored in the active browser tab's session storage and is never placed in a URL.
- The interface escapes captions, profile names, and errors before rendering them.

## Source and media handling

- Profile scans construct TikTok profile URLs from validated handles.
- Direct video imports accept only canonical HTTPS TikTok video URLs. Login details, custom ports, short links, and arbitrary hosts are rejected.
- Download output uses generated filenames and the app-managed `downloads/` directory. File responses do not accept a caller-supplied path.
- Downloads have a configured size limit, a container-signature check, SHA-256, and optional FFmpeg decode validation when the executable is available.
- Errors saved to the queue are shortened and remote URLs are removed. No browser cookies or X session are used.

## Publishing and reuse

- The local build has no automatic X publisher. Preparing a post requires a per-video permission confirmation and leaves upload/publish to the operator.
- The app records `posted_confirmed` only after the operator explicitly confirms a completed post.
- Only use content you own or are authorized to republish.

## Not production deployment guidance

The local prototype has not yet had a security audit, external penetration test, backup/restore drill, or HTTPS deployment review. Keep it on loopback. Complete those items before exposing it on a network or deploying it to AWS.
