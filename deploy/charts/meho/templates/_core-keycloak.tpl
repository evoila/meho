{{/*
Render-time check for the built-in Keycloak (`coreKeycloak.enabled`),
called from core-keycloak-checks.yaml. The subchart sees only its own
values, so the parent checks that `coreKeycloak.hostname` equals
`ingress.host`: Keycloak's public address is `https://<ingress.host>/auth`.
*/}}
{{- define "meho.coreKeycloakChecks" -}}
{{- if and .Values.ingress.enabled (ne .Values.coreKeycloak.hostname .Values.ingress.host) -}}
{{- fail (printf "coreKeycloak.hostname (%q) must equal ingress.host (%q): Keycloak's public address is https://<ingress.host>/auth." .Values.coreKeycloak.hostname .Values.ingress.host) -}}
{{- end -}}
{{- $names := list -}}
{{- range .Values.coreKeycloak.groups -}}
{{- $names = append $names .name -}}
{{- end -}}
{{- range $who, $person := .Values.coreKeycloak.firstLogin -}}
{{- if not (has $person.group $names) -}}
{{- fail (printf "coreKeycloak.firstLogin.%s.group (%q) is not a name in coreKeycloak.groups." $who $person.group) -}}
{{- end -}}
{{- end -}}
{{- end }}

{{/*
The bootstrap Job's plan as JSON (#4043): the Keycloak Service URL, the
first tenant id, the role groups and the two first-login people. No
secret: the Job reads those from Secrets through its environment. The Job
also checks that each person's group is in the list.
*/}}
{{- define "meho.core.bootstrapPlan" -}}
{{- $kc := .Values.coreKeycloak -}}
{{- $groups := list -}}
{{- range $kc.groups -}}
{{- $groups = append $groups (dict "name" .name "tenant_role" .tenantRole "approver" (.approver | default false)) -}}
{{- end -}}
{{- $url := printf "http://%s:%v/auth" (include "coreKeycloak.fullname" .) ($kc.service.port | default 8080) -}}
{{- dict "keycloak_url" $url "tenant_id" $kc.firstTenantId "groups" $groups "first_login_secret" (printf "%s-core-first-login" .Release.Name) "admin" $kc.firstLogin.admin "approver" $kc.firstLogin.approver | toJson -}}
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
