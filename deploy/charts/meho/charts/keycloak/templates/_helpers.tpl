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
The image reference and the OpenShift-aware security context come from the
parent chart's shared core helpers (templates/_core-postgres-helpers.tpl):
`meho.core.image` and `meho.core.podSecurityContext`. The latter also
serves the container security contexts: it only leaves out the user and
group ids when `global.openshift` is true.
*/}}

{{/*
Name of a Secret the install's secrets Job creates: `<release>-core-<kind>`.
*/}}
{{- define "coreKeycloak.coreSecret" -}}
{{- printf "%s-core-%s" .root.Release.Name .kind -}}
{{- end }}

{{- define "coreKeycloak.databaseHost" -}}
{{- default (printf "%s-postgres" .Release.Name | trunc 63 | trimSuffix "-") .Values.database.host -}}
{{- end }}
