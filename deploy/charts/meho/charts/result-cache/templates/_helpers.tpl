{{/*
Result-cache subchart helpers. The subchart is rendered with its own scope
(`.Chart.Name` comes from the parent alias); helpers mirror the broadcast subchart so
operators reading both see the same conventions. The parent chart maps its
`resultCache:` values here through a dependency `alias`, and an alias also
renames `.Chart.Name`; the name is therefore pinned to `result-cache` so
resource names stay DNS-safe and match the parent's egress rule and URL.
*/}}

{{/*
Expand the name of the subchart.
*/}}
{{- define "resultCache.name" -}}
{{- default "result-cache" .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Fully-qualified name. The parent's release name is the prefix so multiple
MEHO installations on a single namespace remain distinct.
*/}}
{{- define "resultCache.fullname" -}}
{{- $name := default "result-cache" .Values.nameOverride }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Chart label (chart-name + version).
*/}}
{{- define "resultCache.chart" -}}
{{- printf "%s-%s" "result-cache" .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels applied to every result-cache resource.
*/}}
{{- define "resultCache.labels" -}}
helm.sh/chart: {{ include "resultCache.chart" . }}
{{ include "resultCache.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/component: result-cache
{{- end }}

{{/*
Selector labels — applied to `spec.selector.matchLabels`, the Pod template
labels, and the parent chart's NetworkPolicy egress rule so the selectors
all line up.
*/}}
{{- define "resultCache.selectorLabels" -}}
app.kubernetes.io/name: {{ include "resultCache.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
