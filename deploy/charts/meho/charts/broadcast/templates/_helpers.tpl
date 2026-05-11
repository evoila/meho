{{/*
Broadcast subchart helpers. The subchart is rendered with its own scope
(`.Chart.Name == "broadcast"`); helpers mirror the umbrella chart's shape so
operators reading both see the same conventions.
*/}}

{{/*
Expand the name of the subchart.
*/}}
{{- define "broadcast.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Fully-qualified name. The parent's release name is the prefix so multiple
MEHO installations on a single namespace remain distinct.
*/}}
{{- define "broadcast.fullname" -}}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Chart label (chart-name + version).
*/}}
{{- define "broadcast.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels applied to every broadcast resource.
*/}}
{{- define "broadcast.labels" -}}
helm.sh/chart: {{ include "broadcast.chart" . }}
{{ include "broadcast.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/component: broadcast
{{- end }}

{{/*
Selector labels — applied to `spec.selector.matchLabels`, the Pod template
labels, and the parent chart's NetworkPolicy egress rule so the selectors
all line up.
*/}}
{{- define "broadcast.selectorLabels" -}}
app.kubernetes.io/name: {{ include "broadcast.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
