{{/*
meho.redis.url — resolves the backend's `REDIS_URL` value based on
whether embedded Redis is enabled.

Embedded mode: emits a `redis://` URL that points at the Bitnami
subchart's master Service. The Bitnami `redis` subchart with alias
`embeddedredis` exposes its master Service at
`<release>-embeddedredis-master:6379` *even in standalone architecture*
(the `-master` suffix is the chart's invariant Service name) and emits
a Secret named `<release>-embeddedredis` with the password under key
`redis-password`. URL embeds `$(REDIS_PASSWORD)` — same env-var
expansion mechanism as the Postgres helper. Bitnami's Redis uses the
implicit `default` user when AUTH is on; including it in the URL
explicitly avoids client-library mode confusion.

External mode: requires `redis.external.url` and surfaces the
`required` error fail-loud at template time.

Database index `0` is hardcoded — MEHO uses a single Redis logical
database; operators wanting a different index pass it via the
external URL.
*/}}
{{- define "meho.redis.url" -}}
{{- if .Values.embedded.enabled -}}
redis://default:$(REDIS_PASSWORD)@{{ .Release.Name }}-embeddedredis-master:6379/0
{{- else -}}
{{ required "redis.external.url must be set when embedded.enabled is false" .Values.redis.external.url }}
{{- end -}}
{{- end -}}
