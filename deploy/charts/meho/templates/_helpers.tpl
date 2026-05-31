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

{{/*
Resolved Kubernetes Secret name holding the agent-runtime Anthropic API key
(G11.1 #803). Two paths converge here so operators who flip
`eso.agent.enabled: true` without also setting `agent.secretName` get a
working wiring instead of an unresolvable reference:

  1. `agent.secretName` when the operator set it explicitly (BYO Secret,
     or already pointed at the ESO target).
  2. `<release>-agent` when `eso.agent.enabled: true` — the chart's
     ExternalSecret materialises that exact name (templates/
     externalsecrets.yaml).
  3. `fail` (with a remediation message) when called under
     `agent.enabled: true` and neither path resolved. The schema can't
     express the cross-property dependency on `eso.agent.enabled`
     cleanly, so this runtime check is the authoritative gate. Caller is
     `templates/deployment.yaml` which guards the call with
     `if .Values.agent.enabled`.
*/}}
{{- define "meho.agentSecretName" -}}
{{- if .Values.agent.secretName -}}
{{- .Values.agent.secretName -}}
{{- else if and .Values.eso.agent .Values.eso.agent.enabled -}}
{{- printf "%s-agent" (include "meho.fullname" .) -}}
{{- else -}}
{{- fail "agent.enabled=true requires either agent.secretName=<your-secret> (BYO) or eso.agent.enabled=true (chart-rendered ExternalSecret)." -}}
{{- end -}}
{{- end }}

{{/*
Resolved Kubernetes Secret name holding the Keycloak Admin client secret
(G11.2 #803). Same resolution pattern as `meho.agentSecretName`:

  1. `keycloakAdmin.clientSecret.secretName` when operator-set.
  2. `<release>-keycloak-admin` when `eso.keycloakAdmin.enabled: true`.
  3. `fail` (with a remediation message) when called under
     `keycloakAdmin.enabled: true` and neither path resolved.
*/}}
{{- define "meho.keycloakAdminSecretName" -}}
{{- if .Values.keycloakAdmin.clientSecret.secretName -}}
{{- .Values.keycloakAdmin.clientSecret.secretName -}}
{{- else if and .Values.eso.keycloakAdmin .Values.eso.keycloakAdmin.enabled -}}
{{- printf "%s-keycloak-admin" (include "meho.fullname" .) -}}
{{- else -}}
{{- fail "keycloakAdmin.enabled=true requires either keycloakAdmin.clientSecret.secretName=<your-secret> (BYO) or eso.keycloakAdmin.enabled=true (chart-rendered ExternalSecret)." -}}
{{- end -}}
{{- end }}
