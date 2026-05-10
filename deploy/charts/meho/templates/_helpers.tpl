{{/*
Standard Helm chart template helpers. The shape matches `helm create`'s
output (chart-name-prefixed name + fullname + selector labels) so existing
Helm tooling and operators reading the templates see the conventions they
expect.
*/}}

{{/*
Expand the name of the chart.
*/}}
{{- define "meho.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
Truncated at 63 chars because some Kubernetes name fields are limited to that
(RFC 1123 DNS label).
*/}}
{{- define "meho.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "meho.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels — applied to every resource the chart ships.
*/}}
{{- define "meho.labels" -}}
helm.sh/chart: {{ include "meho.chart" . }}
{{ include "meho.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels — applied to `spec.selector.matchLabels` and the Pod template
labels so the Service/Deployment/NetworkPolicy selectors all line up.
*/}}
{{- define "meho.selectorLabels" -}}
app.kubernetes.io/name: {{ include "meho.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Create the name of the service account to use.
*/}}
{{- define "meho.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "meho.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}
