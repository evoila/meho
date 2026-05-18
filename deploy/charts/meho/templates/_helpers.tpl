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
Effective backplane base URL (no trailing slash).

Resolution order (mirrors backend mcp.auth.mcp_resource_uri's intent at
the chart layer so the documented default actually materialises — #633):

  1. `config.backplaneUrl` when the operator set it explicitly.
  2. Derived from the Ingress when `ingress.enabled` and `ingress.host`
     are set: `https://<host>` if `ingress.tls.enabled`, else
     `http://<host>`. This makes the documented `${BACKPLANE_URL}/mcp`
     default real for the common ingress-fronted deploy without the
     operator setting anything MCP-specific.
  3. Empty string otherwise (no ingress, nothing set) — the backend's
     startup guard then fails loudly with the remediation rather than
     serving a dark /mcp surface.
*/}}
{{- define "meho.backplaneUrl" -}}
{{- if .Values.config.backplaneUrl -}}
{{- .Values.config.backplaneUrl | trimSuffix "/" -}}
{{- else if and .Values.ingress.enabled .Values.ingress.host -}}
{{- $scheme := ternary "https" "http" .Values.ingress.tls.enabled -}}
{{- printf "%s://%s" $scheme .Values.ingress.host -}}
{{- end -}}
{{- end }}

{{/*
Effective MCP resource URI (canonical, no trailing slash).

  1. `config.mcpResourceUri` when set explicitly (non-default mount).
  2. `<backplaneUrl>/mcp` when the effective backplane URL is non-empty.
  3. Empty string otherwise — backend startup guard handles the fail.
*/}}
{{- define "meho.mcpResourceUri" -}}
{{- if .Values.config.mcpResourceUri -}}
{{- .Values.config.mcpResourceUri | trimSuffix "/" -}}
{{- else -}}
{{- $base := include "meho.backplaneUrl" . -}}
{{- if $base -}}
{{- printf "%s/mcp" $base -}}
{{- end -}}
{{- end -}}
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
