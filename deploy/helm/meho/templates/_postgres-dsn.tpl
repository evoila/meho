{{/*
meho.postgres.dsn — resolves the backend's `DATABASE_URL` value
based on whether embedded Postgres is enabled.

Embedded mode: emits an asyncpg DSN that points at the Bitnami
subchart's primary Service. The Bitnami `postgresql` subchart with
alias `embeddedpostgres` exposes its primary Service at
`<release>-embeddedpostgres:5432` and emits a Secret named
`<release>-embeddedpostgres` with the user-level password under key
`password`. The DSN embeds `$(POSTGRES_PASSWORD)` — Kubernetes pod-spec
env-var expansion fills this in at runtime from a sibling env entry
declared `valueFrom: secretKeyRef` on the same Secret.

External mode: requires `postgres.external.dsn` and surfaces the
`required` error fail-loud at template time when neither the embedded
path nor an external DSN is configured.

Trailing newlines are trimmed by the surrounding `-}}` so the helper
output composes cleanly when interpolated into a YAML scalar.
*/}}
{{- define "meho.postgres.dsn" -}}
{{- if .Values.embedded.enabled -}}
postgresql+asyncpg://{{ .Values.embeddedpostgres.auth.username }}:$(POSTGRES_PASSWORD)@{{ .Release.Name }}-embeddedpostgres:5432/{{ .Values.embeddedpostgres.auth.database }}
{{- else -}}
{{ required "postgres.external.dsn must be set when embedded.enabled is false" .Values.postgres.external.dsn }}
{{- end -}}
{{- end -}}
