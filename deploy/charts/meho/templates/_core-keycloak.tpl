{{/*
Render-time checks for the built-in Keycloak (`coreKeycloak.enabled`),
called from core-keycloak-checks.yaml. The subchart sees only its own
values, so the parent checks the settings the two must agree on:

- `coreKeycloak.hostname` equals `ingress.host` (Keycloak's public address
  is `https://<ingress.host>/auth`);
- the values keys that name the database and client Secrets for the secrets
  Job are empty or name the `<release>-core-…` Secret, because Keycloak
  reads exactly those names. `coreKeycloak.existingSecrets.bootstrap` is
  read by both, so it may name any Secret.
*/}}
{{- define "meho.coreKeycloakChecks" -}}
{{- if and .Values.ingress.enabled (ne .Values.coreKeycloak.hostname .Values.ingress.host) -}}
{{- fail (printf "coreKeycloak.hostname (%q) must equal ingress.host (%q): Keycloak's public address is https://<ingress.host>/auth." .Values.coreKeycloak.hostname .Values.ingress.host) -}}
{{- end -}}
{{- $named := list
  (list "corePostgres.existingSecrets.keycloak" (dig "existingSecrets" "keycloak" "" (.Values.corePostgres | default dict)) "postgres-keycloak")
  (list "keycloakAdmin.clientSecret.secretName" .Values.keycloakAdmin.clientSecret.secretName "keycloak-admin")
  (list "uiConsole.secretName" .Values.uiConsole.secretName "ui-auth")
  (list "checkRunner.clientSecret.secretName" .Values.checkRunner.clientSecret.secretName "check-runner") -}}
{{- range $named -}}
{{- $core := printf "%s-core-%s" $.Release.Name (index . 2) -}}
{{- if and (index . 1) (ne (index . 1) $core) -}}
{{- fail (printf "%s names the Secret %q, but the built-in Keycloak reads %q. Leave %s empty; to use your own value, create the Secret %q before the install (the install keeps a Secret that exists)." (index . 0) (index . 1) $core (index . 0) $core) -}}
{{- end -}}
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
