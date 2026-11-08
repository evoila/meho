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
- the client ids and the admin URL are empty or the value the chart uses
  (the realm has exactly one client for each, #4766).
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
{{- $kc := include "meho.keycloakSettings" . | fromYaml -}}
{{- $named := dict "corePostgres.existingSecrets.keycloak" (list .Values.corePostgres.existingSecrets.keycloak (printf "%s-core-postgres-keycloak" .Release.Name)) "keycloakAdmin.clientSecret.secretName" (list .Values.keycloakAdmin.clientSecret.secretName $kc.adminSecretName) "uiConsole.secretName" (list .Values.uiConsole.secretName $kc.uiSecretName) "checkRunner.clientSecret.secretName" (list .Values.checkRunner.clientSecret.secretName $kc.checkRunnerSecretName) "config.uiKeycloakClientId" (list .Values.config.uiKeycloakClientId $kc.uiClientId) "checkRunner.clientId" (list .Values.checkRunner.clientId $kc.checkRunnerClientId) "keycloakAdmin.url" (list .Values.keycloakAdmin.url $kc.adminUrl) "keycloakAdmin.clientId" (list .Values.keycloakAdmin.clientId $kc.adminClientId) -}}
{{- range $key, $pair := $named -}}
{{- if and (index $pair 0) (ne (index $pair 0) (index $pair 1)) -}}
{{- fail (printf "%s must be empty or %q while coreKeycloak.enabled is on: the chart sets it itself." $key (index $pair 1)) -}}
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
uiSecretName: {{ printf "%s-core-ui-auth" .Release.Name | quote }}
checkRunnerClientId: meho-check-runner
checkRunnerSecretName: {{ printf "%s-core-check-runner" .Release.Name | quote }}
{{- else -}}
issuerUrl: {{ .Values.config.keycloakIssuerUrl | quote }}
audience: {{ .Values.config.keycloakAudience | quote }}
cliClientId: {{ .Values.config.keycloakCliClientId | quote }}
cliApproveClientId: {{ .Values.config.keycloakCliApproveClientId | quote }}
uiClientId: {{ .Values.config.uiKeycloakClientId | quote }}
uiSecretName: {{ .Values.uiConsole.secretName | quote }}
checkRunnerClientId: {{ .Values.checkRunner.clientId | quote }}
checkRunnerSecretName: {{ .Values.checkRunner.clientSecret.secretName | quote }}
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
