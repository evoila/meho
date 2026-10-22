{{/*
Helpers of the built-in OpenBao subchart. The parent maps its `coreVault:`
values here through a dependency `alias`, and an alias also renames
`.Chart.Name`; the resource name is therefore pinned to `vault`, so the
Service is always `<release>-vault` (MEHO's names stay `vault`, ADR 0008).
*/}}

{{- define "coreVault.name" -}}
openbao
{{- end }}

{{/*
Resource name. Cut to 52 characters, not 63: Kubernetes adds a revision
hash to a StatefulSet's name in the `controller-revision-hash` pod label,
and a label value may have at most 63 characters.
*/}}
{{- define "coreVault.fullname" -}}
{{- printf "%s-vault" .Release.Name | trunc 52 | trimSuffix "-" }}
{{- end }}

{{/*
The headless Service. It gives the pod its own DNS name
(`<release>-vault-0.<release>-vault-internal`).
*/}}
{{- define "coreVault.internalName" -}}
{{- printf "%s-internal" (include "coreVault.fullname" .) }}
{{- end }}

{{- define "coreVault.labels" -}}
helm.sh/chart: {{ printf "openbao-%s" .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "coreVault.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/component: secrets
{{- end }}

{{- define "coreVault.selectorLabels" -}}
app.kubernetes.io/name: {{ include "coreVault.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
The image's home folder (`/openbao` or `/vault`): the start script reads the
config from `<home>/config`.
*/}}
{{- define "coreVault.home" -}}
{{- ternary "/openbao" "/vault" (eq .Values.binary "bao") -}}
{{- end }}

{{/*
Prefix of the server's environment variables (`BAO_` or `VAULT_`).
*/}}
{{- define "coreVault.envPrefix" -}}
{{- ternary "BAO" "VAULT" (eq .Values.binary "bao") -}}
{{- end }}

{{/*
The image reference and the OpenShift-aware security context come from the
parent chart's shared core helpers (templates/_core-postgres-helpers.tpl):
`meho.core.image` and `meho.core.podSecurityContext`.
*/}}
