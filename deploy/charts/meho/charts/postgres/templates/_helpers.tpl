{{/*
Built-in PostgreSQL subchart helpers. The parent chart maps its
`corePostgres:` values here through a dependency `alias`, and an alias also
renames `.Chart.Name`. So the names are pinned to `postgres`: the
backplane's DATABASE_URL and Keycloak's database host both use
`<release>-postgres`.
*/}}

{{/*
Service name: the host every client uses.
*/}}
{{- define "corePostgres.fullname" -}}
{{- printf "%s-postgres" .Release.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
StatefulSet name. The same as the Service name for any release name up to
43 characters. A StatefulSet name longer than 52 characters breaks its pods:
the `controller-revision-hash` label adds 11 characters and must stay
within 63.
*/}}
{{- define "corePostgres.statefulSetName" -}}
{{- include "corePostgres.fullname" . | trunc 52 | trimSuffix "-" }}
{{- end }}

{{/*
Chart label (chart name + version).
*/}}
{{- define "corePostgres.chart" -}}
{{- printf "%s-%s" "postgres" .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels.
*/}}
{{- define "corePostgres.labels" -}}
helm.sh/chart: {{ include "corePostgres.chart" . }}
{{ include "corePostgres.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/component: database
{{- end }}

{{/*
Selector labels.
*/}}
{{- define "corePostgres.selectorLabels" -}}
app.kubernetes.io/name: postgres
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Names of the three Secrets. The install's secrets Job makes them with
these names; `existingSecrets.*` names an operator-made Secret instead.
*/}}
{{- define "corePostgres.superuserSecretName" -}}
{{- .Values.existingSecrets.superuser | default (printf "%s-core-postgres-superuser" .Release.Name) }}
{{- end }}

{{- define "corePostgres.mehoSecretName" -}}
{{- printf "%s-core-postgres-meho" .Release.Name }}
{{- end }}

{{- define "corePostgres.keycloakSecretName" -}}
{{- .Values.existingSecrets.keycloak | default (printf "%s-core-postgres-keycloak" .Release.Name) }}
{{- end }}
