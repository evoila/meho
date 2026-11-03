# request-body-limit — one byte cap on every HTTP request body

## Overview

`RequestBodyLimitMiddleware` (`backend/src/meho_backplane/request_body_limit.py`)
is the outermost middleware of the backplane app. It refuses any HTTP
request body above `HTTP_MAX_REQUEST_BODY_BYTES` (default 32 MiB) with
a `413` answer, before any handler or inner middleware reads the body.

Before it, only `/mcp` (1 MiB) and the event ingest (256 KiB) capped
their body. Every other route parsed the whole body first. The cap is
the one backstop for all routes; routes with a tighter cap keep it.

## Key types

| Symbol | Role |
|---|---|
| `RequestBodyLimitMiddleware` | Pure-ASGI middleware. Reads the cap from `Settings.http_max_request_body_bytes` once a request carries body bytes; a body-less request needs no settings. |
| `RequestBodyTooLargeError` | Raised from the wrapped `receive` when the running byte total crosses the cap. An `HTTPException` with status `413`. |

## Control flow

1. A `Content-Length` header above the cap gets the `413` at once. No
   body byte is read, no inner middleware runs.
2. Without a trustworthy length (chunked transfer, or a header that
   understates the size), the middleware counts the bytes of every
   `http.request` message the handler reads. When the total crosses
   the cap, the read raises `RequestBodyTooLargeError`.
3. The error is an `HTTPException` with status `413`. When a route
   reads the body, FastAPI's body parsing re-raises it as is (any other
   error from the read would become its generic `400`), and the app's
   exception handler answers `413`. When an inner middleware reads the
   body, the error escapes the app and this middleware catches it: when
   the response has not started yet it sends the `413`; otherwise it
   re-raises, because a second response cannot be sent.

The `413` body is `{"detail": "request body exceeds <N> bytes"}`. Each
refusal logs one `request_body_too_large` warning with the path and the
sizes.

## Dependencies

- `Settings.http_max_request_body_bytes` (`HTTP_MAX_REQUEST_BODY_BYTES`,
  at least 1024). The default of 32 MiB sits above the largest body
  MEHO accepts on purpose: the 20 MiB inline spec of
  `POST /api/v1/connectors/ingest`.
- Registered last in `main.py`, so it is the outermost layer.

## Known issues

- The cap counts request bytes, not decoded form fields. The console
  routes keep their own field caps for that.
- The Helm chart cannot set the matching cap on the ingress controller,
  because the annotation name depends on the controller. The comment on
  `ingress.annotations` in `deploy/charts/meho/values.yaml` names the
  ingress-nginx one.

## References

- `backend/tests/test_request_body_limit.py`
- `docs/codebase/backend.md` (middleware order)
