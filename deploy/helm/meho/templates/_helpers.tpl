{{/*
Common helpers for the MEHO chart. Subsequent Deployment / Service
/ Secret templates invoke these named templates. This file emits
no manifests on its own.
*/}}

{{/*
Expand the name of the chart.
*/}}
{{- define "meho.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Create a default fully qualified app name.

The name is truncated at 63 characters to fit Kubernetes label
length limits — RFC 1123 / DNS subdomain rules.
*/}}
{{- define "meho.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "meho.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Common labels — applied to every resource the chart renders.
*/}}
{{- define "meho.labels" -}}
helm.sh/chart: {{ include "meho.chart" . }}
{{ include "meho.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/*
Selector labels — must be stable for the lifetime of a Deployment.
Do NOT add fields that change across upgrades (version, chart
hash) here; those go in `meho.labels`.
*/}}
{{- define "meho.selectorLabels" -}}
app.kubernetes.io/name: {{ include "meho.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
ServiceAccount name — fully qualified chart name when the chart
creates its own ServiceAccount, an explicit override, or "default"
when relying on the namespace default.
*/}}
{{- define "meho.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "meho.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end -}}

{{/*
Backend resource name — `<release-fullname>-backend`, truncated to
fit RFC 1123 limits. Used by the backend Deployment, Service, and
the Secret reference in `meho.backend.secretName`.
*/}}
{{- define "meho.backend.fullname" -}}
{{- printf "%s-backend" (include "meho.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Backend selector labels — stable across upgrades (no version, no
chart hash). The Deployment's `selector.matchLabels` and the
Service's `selector` invoke this; both must agree forever for a
given release.
*/}}
{{- define "meho.backend.selectorLabels" -}}
{{ include "meho.selectorLabels" . }}
app.kubernetes.io/component: backend
{{- end -}}

{{/*
Backend common labels — selector labels plus the
chart/version/managed-by trio that may rotate across upgrades.
Applied to the Deployment metadata and Service metadata.
*/}}
{{- define "meho.backend.labels" -}}
helm.sh/chart: {{ include "meho.chart" . }}
{{ include "meho.backend.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/*
Backend Secret name — defaults to `<backend-fullname>` when the
operator does not supply one. The Secret resource itself ships in
a sibling task (#528); the Deployment's `envFrom` references this
name unconditionally so the chart's secret-reference layout is
locked in even before the resource lands.
*/}}
{{- define "meho.backend.secretName" -}}
{{- default (include "meho.backend.fullname" .) .Values.backend.existingSecret -}}
{{- end -}}
