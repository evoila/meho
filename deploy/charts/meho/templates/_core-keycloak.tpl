{{/*
Render-time checks for the built-in Keycloak (`coreKeycloak.enabled`),
called from core-keycloak-checks.yaml. The subchart sees only its own
values, so the parent checks:
- `coreKeycloak.hostname` equals `ingress.host`: Keycloak's public address
  is `https://<ingress.host>/auth`;
- the issuer is not set by hand: the chart derives it (meho.keycloakSettings);
- the four Secret-name keys whose Secrets Keycloak reads are empty or name
  the `<release>-core-…` Secret. Keycloak reads only the core names, so any
  other name would leave the core Secret unmade and the pod waiting forever;
- the database settings fit the PostgreSQL in use: with the built-in
  PostgreSQL the `database` keys keep their defaults; with your own
  PostgreSQL (`corePostgres.enabled: false`) the host is required, and so is
  a password Secret or an admin Secret for the Job that makes the database.
*/}}
{{- define "meho.coreKeycloakChecks" -}}
{{- if and .Values.ingress.enabled (ne .Values.coreKeycloak.hostname .Values.ingress.host) -}}
{{- fail (printf "coreKeycloak.hostname (%q) must equal ingress.host (%q): Keycloak's public address is https://<ingress.host>/auth." .Values.coreKeycloak.hostname .Values.ingress.host) -}}
{{- end -}}
{{- range $key, $value := dict "config.keycloakIssuerUrl" .Values.config.keycloakIssuerUrl "keycloak.issuer" .Values.keycloak.issuer -}}
{{- if $value -}}
{{- fail (printf "%s must be empty while coreKeycloak.enabled is on: the chart sets the issuer to https://<coreKeycloak.hostname>/auth/realms/meho." $key) -}}
{{- end -}}
{{- end -}}
{{- $db := .Values.coreKeycloak.database -}}
{{- if .Values.corePostgres.enabled -}}
{{- if or (ne $db.name "keycloak") (ne $db.user "keycloak") $db.passwordSecret.name $db.create.enabled $db.sslMode -}}
{{- fail "coreKeycloak.database.name, user, passwordSecret.name, sslMode and create.enabled are only for your own PostgreSQL: leave them unset while corePostgres.enabled is on (the built-in PostgreSQL makes the database and the role itself)." -}}
{{- end -}}
{{- else -}}
{{- if not $db.host -}}
{{- fail "coreKeycloak.database.host is empty: with corePostgres.enabled off, the built-in Keycloak needs the host of your PostgreSQL." -}}
{{- end -}}
{{- if $db.create.enabled -}}
{{- if not $db.create.adminSecret.name -}}
{{- fail "coreKeycloak.database.create.adminSecret.name is empty: name a Secret (keys username and password) with a login that may create a role and a database, or set create.enabled to false." -}}
{{- end -}}
{{- if eq $db.create.adminSecret.name (printf "%s-core-keycloak-db-admin" .Release.Name) -}}
{{- fail (printf "coreKeycloak.database.create.adminSecret.name must not be %q: that name is kept for the install's own Secrets. Pick another name." $db.create.adminSecret.name) -}}
{{- end -}}
{{- else if not $db.passwordSecret.name -}}
{{- fail "coreKeycloak.database.passwordSecret.name is empty: with your own PostgreSQL, name the Secret with the password of Keycloak's database role, or set create.enabled to true so the chart makes the role and the database." -}}
{{- end -}}
{{- end -}}
{{- $named := dict "keycloakAdmin.clientSecret.secretName" (list .Values.keycloakAdmin.clientSecret.secretName "keycloak-admin") "uiConsole.secretName" (list .Values.uiConsole.secretName "ui-auth") "checkRunner.clientSecret.secretName" (list .Values.checkRunner.clientSecret.secretName "check-runner") -}}
{{- if .Values.corePostgres.enabled -}}
{{- $_ := set $named "corePostgres.existingSecrets.keycloak" (list .Values.corePostgres.existingSecrets.keycloak "postgres-keycloak") -}}
{{- end -}}
{{- range $key, $pair := $named -}}
{{- $want := printf "%s-core-%s" $.Release.Name (index $pair 1) -}}
{{- if and (index $pair 0) (ne (index $pair 0) $want) -}}
{{- fail (printf "%s must be empty or %q while coreKeycloak.enabled is on: the built-in Keycloak reads only that Secret." $key $want) -}}
{{- end -}}
{{- end -}}
{{- end }}

{{/*
The backplane's Keycloak settings as YAML: derived from the built-in
Keycloak when `coreKeycloak.enabled` is on, else the bring-your-own values
(rendered unchanged). Pods reach the built-in Keycloak through its Service;
only the issuer, which tokens carry, is the public address. The audience
and client ids are the clients of the realm file
(charts/keycloak/files/meho-realm.json).
Usage: {{- $kc := include "meho.keycloakSettings" . | fromYaml }}
*/}}
{{- define "meho.keycloakSettings" -}}
{{- if .Values.coreKeycloak.enabled -}}
{{- $service := printf "http://%s:%v/auth" (include "coreKeycloak.fullname" .) (.Values.coreKeycloak.service.port | default 8080) -}}
issuerUrl: {{ printf "https://%s/auth/realms/meho" .Values.coreKeycloak.hostname | quote }}
discoveryUrl: {{ printf "%s/realms/meho" $service | quote }}
audience: meho-backplane
cliClientId: meho-cli
cliApproveClientId: meho-cli-approve
uiClientId: meho-web
adminUrl: {{ printf "%s/admin/realms/meho" $service | quote }}
adminClientId: meho-admin
adminSecretName: {{ printf "%s-core-keycloak-admin" .Release.Name | quote }}
adminSecretKey: client_secret
{{- else -}}
issuerUrl: {{ .Values.config.keycloakIssuerUrl | quote }}
audience: {{ .Values.config.keycloakAudience | quote }}
cliClientId: {{ .Values.config.keycloakCliClientId | quote }}
cliApproveClientId: {{ .Values.config.keycloakCliApproveClientId | quote }}
uiClientId: {{ .Values.config.uiKeycloakClientId | quote }}
{{- if .Values.keycloakAdmin.enabled }}
adminUrl: {{ .Values.keycloakAdmin.url | quote }}
adminClientId: {{ .Values.keycloakAdmin.clientId | quote }}
adminSecretName: {{ include "meho.keycloakAdminSecretName" . | quote }}
adminSecretKey: {{ .Values.keycloakAdmin.clientSecret.secretKey | quote }}
{{- end }}
{{- end -}}
{{- end }}

{{/*
Ingress paths for the built-in Keycloak, added to the backplane's Ingress
so one Ingress serves the MEHO host. Keycloak is at `https://<host>/auth`.
Only the realm and the static files are routed; the admin console and the
master realm fall to `/` (the backplane's 404) unless
`coreKeycloak.ingress.exposeAdmin` is true. `Prefix` only: OpenShift drops
`Exact`. The Service name comes from the subchart's own helper.
*/}}
{{- define "meho.coreKeycloakIngressPaths" -}}
{{- $paths := list "/auth/realms/meho" "/auth/resources" -}}
{{- if .Values.coreKeycloak.ingress.exposeAdmin -}}
{{- $paths = concat $paths (list "/auth/admin" "/auth/realms/master") -}}
{{- end -}}
{{- range $paths }}
- path: {{ . }}
  pathType: Prefix
  backend:
    service:
      name: {{ include "coreKeycloak.fullname" $ }}
      port:
        number: {{ $.Values.coreKeycloak.service.port | default 8080 }}
{{- end }}
{{- end }}
