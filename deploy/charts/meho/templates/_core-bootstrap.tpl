{{/*
Built-in core: the plan of the `bootstrap` Job's realm stage (#4043,
docs/codebase/core-bootstrap.md), rendered as JSON into
`MEHO_CORE_KEYCLOAK_PLAN`. Names, addresses and user names only; the Python
model is `Plan` in backend/src/meho_backplane/core/keycloak.py. Keycloak
is called through its Service; the realm file is the subchart's ConfigMap,
mounted into the Job.
*/}}
{{- define "meho.core.keycloakPlan" -}}
{{- $rel := .Release.Name -}}
{{- $ci := .Values.coreKeycloak.ci -}}
{{- dict
  "keycloak_url" (printf "http://%s:%v/auth" (include "coreKeycloak.fullname" .) (.Values.coreKeycloak.service.port | default 8080))
  "realm_file" "/opt/meho/realm/meho-realm.json"
  "first_login_secret" (printf "%s-core-first-login" $rel)
  "admin_username" .Values.coreKeycloak.firstLogin.adminUsername
  "approver_username" .Values.coreKeycloak.firstLogin.approverUsername
  "ci" (dict "enabled" $ci.enabled "tenant_id" $ci.tenantId "secret_name" (printf "%s-core-ci-login" $rel))
  | toJson -}}
{{- end }}
