## Note
A live announcement stream ends when the login that opened it ends. `GET /api/v1/feed` ends at the token's expiry (plus the JWT leeway), and after `FEED_STREAM_MAX_SECONDS` (default 3600) at the latest. The console stream `GET /ui/broadcast/stream` ends when its session is signed out, revoked or expired, or loses the operator role.

## Action
Update the CLI: `meho status --watch` and `meho broadcast watch` now renew the token and reconnect with `Last-Event-Id`, so they miss no announcement. A script that reads `GET /api/v1/feed` must reconnect when the stream ends, with a fresh token and the `Last-Event-Id` header set to the `id` of the last event. The web console reconnects by itself.
