## Note
The open runner enrollment route (`POST /api/v1/runner-principals/enroll`) now limits requests per source address: 60 per minute by default. Over the limit it answers 429 `enrollment_rate_limited` with a `Retry-After` header. When the request counter (Valkey) cannot be reached, it answers 503 `enrollment_limit_unavailable`, and the runner tries again at its next start. Behind a proxy, MEHO uses the client address only when the proxy is trusted (`FORWARDED_ALLOW_IPS`). Otherwise all callers share the proxy address and one limit. A runner now also gets at most one refusal audit row per reason and minute.

## Action
Many runners that enroll at the same time from one address (for example behind one NAT or an untrusted proxy) may need a higher limit: set `RUNNER_ENROLL_RATE_PER_MINUTE`. `0` turns the limit off and makes no Valkey call. The migration adds the table `runner_enroll_refusal`; no manual schema action.
