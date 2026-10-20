{{/*
Built-in core: the Secrets the `secrets` hook Job makes (#4041,
docs/codebase/core-secrets.md). Every made Secret is named
`<release>-core-<kind>`; the Job's Python module fixes each kind's keys and
format (backend/src/meho_backplane/core/secrets.py).
*/}}

{{/*
"true" when any built-in part is on, else empty.
*/}}
{{- define "meho.core.enabled" -}}
{{- if or .Values.corePostgres.enabled .Values.coreKeycloak.enabled .Values.coreVault.enabled -}}
true
{{- end -}}
{{- end }}

{{/*
Name of the hook ServiceAccount, Role and RoleBinding. The `bootstrap` Job
(#4043) and the `vault-init` Job (#4044) run under the same account.
*/}}
{{- define "meho.core.hookName" -}}
{{- printf "%s-core-bootstrap" .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end }}

{{/*
Every `<release>-core-*` Secret name the hook Role may read and patch, one
per line: the Secrets this Job makes, then the ones the realm stage (#4043)
and the vault-init Job (#4044) make.
*/}}
{{- define "meho.core.secretNames" -}}
{{- range list "postgres-superuser" "postgres-meho" "postgres-keycloak" "keycloak-bootstrap" "keycloak-admin" "ui-auth" "check-runner" "approval-handoff" "broadcast-auth" "tls" "first-login" "vault-unseal" "vault-init" "vault-snapshot" "vault-scheduler" }}
{{ printf "%s-core-%s" $.Release.Name . }}
{{- end }}
{{- end }}

{{/*
The Job's plan as JSON: `make` lists the core Secrets to make when missing,
`check` lists Secrets the operator named in values instead, with the keys
MEHO reads from them. A values key that is empty, or that holds the
`<release>-core-<kind>` name itself, means "make it". A TLS Secret named in
`ingress.tls.secretName` is never checked: cert-manager may create it after
the install.
*/}}
{{- define "meho.core.secretsPlan" -}}
{{- $rel := .Release.Name -}}
{{- $want := list -}}
{{- if .Values.corePostgres.enabled -}}
{{- $want = append $want (dict "kind" "postgres-superuser" "named" .Values.corePostgres.existingSecrets.superuser "valuesKey" "corePostgres.existingSecrets.superuser" "keys" (list "password")) -}}
{{- /* The database Service is `<release>-postgres` (core design §1, #4039). */ -}}
{{- $want = append $want (dict "kind" "postgres-meho" "named" .Values.postgres.credentialsSecret "valuesKey" "postgres.credentialsSecret" "keys" (list "password" "url") "host" (printf "%s-postgres" $rel)) -}}
{{- $want = append $want (dict "kind" "postgres-keycloak" "named" .Values.corePostgres.existingSecrets.keycloak "valuesKey" "corePostgres.existingSecrets.keycloak" "keys" (list "password")) -}}
{{- end -}}
{{- if .Values.coreKeycloak.enabled -}}
{{- $want = append $want (dict "kind" "keycloak-bootstrap" "named" .Values.coreKeycloak.existingSecrets.bootstrap "valuesKey" "coreKeycloak.existingSecrets.bootstrap" "keys" (list "client_id" "client_secret")) -}}
{{- $want = append $want (dict "kind" "keycloak-admin" "named" .Values.keycloakAdmin.clientSecret.secretName "valuesKey" "keycloakAdmin.clientSecret.secretName" "keys" (list .Values.keycloakAdmin.clientSecret.secretKey)) -}}
{{- $want = append $want (dict "kind" "ui-auth" "named" .Values.uiConsole.secretName "valuesKey" "uiConsole.secretName" "keys" (list .Values.uiConsole.clientSecretKey .Values.uiConsole.sessionEncryptionKey)) -}}
{{- $want = append $want (dict "kind" "check-runner" "named" .Values.checkRunner.clientSecret.secretName "valuesKey" "checkRunner.clientSecret.secretName" "keys" (list .Values.checkRunner.clientSecret.secretKey)) -}}
{{- end -}}
{{- $want = append $want (dict "kind" "approval-handoff" "named" .Values.approvalHandoff.secretName "valuesKey" "approvalHandoff.secretName" "keys" (list .Values.approvalHandoff.secretKey)) -}}
{{- if and .Values.broadcast.enabled .Values.broadcast.auth.enabled -}}
{{- $want = append $want (dict "kind" "broadcast-auth" "named" .Values.broadcast.auth.existingSecret "valuesKey" "broadcast.auth.existingSecret" "keys" (list .Values.broadcast.auth.existingSecretPasswordKey)) -}}
{{- end -}}
{{- $tlsName := printf "%s-core-tls" $rel -}}
{{- if and .Values.ingress.enabled .Values.ingress.tls.enabled (or (not .Values.ingress.tls.secretName) (eq .Values.ingress.tls.secretName $tlsName)) -}}
{{- $want = append $want (dict "kind" "tls" "host" .Values.ingress.host) -}}
{{- end -}}
{{- $make := list -}}
{{- $check := list -}}
{{- range $want -}}
{{- $coreName := printf "%s-core-%s" $rel .kind -}}
{{- if and .named (ne .named $coreName) -}}
{{- $check = append $check (dict "name" .named "values_key" .valuesKey "keys" .keys) -}}
{{- else -}}
{{- $entry := dict "name" $coreName "kind" .kind -}}
{{- with .host -}}
{{- $_ := set $entry "host" . -}}
{{- end -}}
{{- $make = append $make $entry -}}
{{- end -}}
{{- end -}}
{{- dict "make" $make "check" $check | toJson -}}
{{- end }}
