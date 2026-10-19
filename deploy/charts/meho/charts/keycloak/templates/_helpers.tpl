{{/*
Helpers of the built-in Keycloak subchart. The parent maps its
`coreKeycloak:` values here through a dependency `alias`, and an alias also
renames `.Chart.Name`; the name is therefore pinned to `keycloak`, so the
Service is always `<release>-keycloak` (the parent's Ingress and every
in-cluster caller use that name).
*/}}

{{- define "coreKeycloak.name" -}}
keycloak
{{- end }}

{{/*
Resource name. Cut to 52 characters, not 63: Kubernetes adds a revision
hash to a StatefulSet's name in the `controller-revision-hash` pod label,
and a label value may have at most 63 characters. The parent chart's
Ingress and NOTES call this helper too; it reads only `.Release.Name`, so
it gives the same name in both charts.
*/}}
{{- define "coreKeycloak.fullname" -}}
{{- printf "%s-keycloak" .Release.Name | trunc 52 | trimSuffix "-" }}
{{- end }}

{{- define "coreKeycloak.labels" -}}
helm.sh/chart: {{ printf "keycloak-%s" .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "coreKeycloak.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/component: login
{{- end }}

{{- define "coreKeycloak.selectorLabels" -}}
app.kubernetes.io/name: {{ include "coreKeycloak.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Image reference from a `{repository, tag, digest}` map: the digest wins.
*/}}
{{- define "coreKeycloak.image" -}}
{{- if .digest -}}
{{ .repository }}@{{ .digest }}
{{- else -}}
{{ .repository }}:{{ .tag }}
{{- end -}}
{{- end }}

{{/*
Security context without user and group ids when `global.openshift` is
true: OpenShift's restricted SCC assigns them and refuses fixed ones.
Call with (dict "ctx" <map> "root" $).
*/}}
{{- define "coreKeycloak.securityContext" -}}
{{- $ctx := deepCopy .ctx -}}
{{- if dig "openshift" false (.root.Values.global | default dict) -}}
{{- $ctx = omit $ctx "runAsUser" "runAsGroup" "fsGroup" -}}
{{- end -}}
{{- toYaml $ctx -}}
{{- end }}

{{/*
Name of a Secret: the operator's `existingSecrets.<key>`, or the name the
install's secrets Job creates. Call with (list $ "<key>" "<suffix>").
*/}}
{{- define "coreKeycloak.secretName" -}}
{{- $root := index . 0 -}}
{{- $name := index $root.Values.existingSecrets (index . 1) -}}
{{- if $name -}}
{{ $name }}
{{- else -}}
{{ printf "%s-core-%s" $root.Release.Name (index . 2) }}
{{- end -}}
{{- end }}

{{- define "coreKeycloak.databaseHost" -}}
{{- default (printf "%s-postgres" .Release.Name | trunc 63 | trimSuffix "-") .Values.database.host -}}
{{- end }}
