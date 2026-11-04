{{/*
Wiring of the backplane to the built-in PostgreSQL and OpenBao (#4047).
The Keycloak half lives in _core-keycloak.tpl (#4591). With a built-in part
off, each helper returns the bring-your-own value unchanged, so that render
stays byte for byte the same.
*/}}

{{/*
Render-time checks, called from core-checks.yaml. A subchart reads only
the `<release>-core-…` Secret, so a values key that names another Secret
would leave the core Secret unmade and the pod waiting forever.
*/}}
{{- define "meho.coreWiringChecks" -}}
{{- if .Values.corePostgres.enabled -}}
{{- $want := printf "%s-core-postgres-meho" .Release.Name -}}
{{- if and .Values.postgres.credentialsSecret (ne .Values.postgres.credentialsSecret $want) -}}
{{- fail (printf "postgres.credentialsSecret must be empty or %q while corePostgres.enabled is on: the built-in PostgreSQL sets the meho password from that Secret." $want) -}}
{{- end -}}
{{- if .Values.eso.enabled -}}
{{- fail "eso.enabled must be false while corePostgres.enabled is on: the install makes the database Secret itself." -}}
{{- end -}}
{{- end -}}
{{- if .Values.coreVault.enabled -}}
{{- range $key, $value := dict "config.vaultAddr" .Values.config.vaultAddr "vault.address" .Values.vault.address -}}
{{- if $value -}}
{{- fail (printf "%s must be empty while coreVault.enabled is on: the chart sets the address to http://%s:%v." $key (include "meho.coreVaultName" $) $.Values.coreVault.service.port) -}}
{{- end -}}
{{- end -}}
{{- if ne .Values.config.credentialBackend "vault" -}}
{{- fail "config.credentialBackend must be vault while coreVault.enabled is on." -}}
{{- end -}}
{{- range .Values.extraEnv -}}
{{- if and (eq .name "VAULT_KV_TENANT_SCOPE_PREFIX") (not .value) -}}
{{- fail "extraEnv must not set VAULT_KV_TENANT_SCOPE_PREFIX empty while coreVault.enabled is on: the built-in OpenBao keeps each tenant's secrets apart, and MEHO checks the same." -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end }}

{{/*
Name of the built-in OpenBao Service. Same rule as `coreVault.fullname` in
charts/openbao/: the parent cannot call a helper of a subchart that is off.
*/}}
{{- define "meho.coreVaultName" -}}
{{- printf "%s-vault" .Release.Name | trunc 52 | trimSuffix "-" }}
{{- end }}

{{/*
Name of the Secret that holds DATABASE_URL at key `url`: the one the
install makes when the built-in PostgreSQL is on, else
`postgres.credentialsSecret` (rendered unchanged).
*/}}
{{- define "meho.databaseSecretName" -}}
{{- if .Values.corePostgres.enabled -}}
{{- printf "%s-core-postgres-meho" .Release.Name -}}
{{- else -}}
{{- .Values.postgres.credentialsSecret -}}
{{- end -}}
{{- end }}

{{/*
The backplane's Vault settings as YAML: the built-in OpenBao when
`coreVault.enabled` is on, else the `config.vault*` values (rendered
unchanged). The login role and mount are the ones the setup Job makes
(backend/src/meho_backplane/core/vault_init.py): one role `meho-mcp` on
`jwt` for every tenant, which copies the token's tenant_id into the login,
so `VAULT_OIDC_ROLE_BY_TENANT` stays empty. `schedulerSecret` is the
Secret with the scheduler's token, empty for bring-your-own.
Usage: {{- $vault := include "meho.vaultSettings" . | fromYaml }}
*/}}
{{- define "meho.vaultSettings" -}}
{{- if .Values.coreVault.enabled -}}
addr: {{ printf "http://%s:%v" (include "meho.coreVaultName" .) .Values.coreVault.service.port | quote }}
role: meho-mcp
mountPath: jwt
schedulerSecret: {{ printf "%s-core-vault-scheduler" .Release.Name | quote }}
{{- else -}}
addr: {{ .Values.config.vaultAddr | quote }}
role: {{ .Values.config.vaultOidcRole | quote }}
mountPath: {{ .Values.config.vaultOidcMountPath | quote }}
schedulerSecret: ""
{{- end -}}
{{- end }}

{{/*
The approval hand-off key as YAML. `approvalHandoff.enabled` left unset
or null means on with a built-in part (the secrets Job makes
`<release>-core-approval-handoff`) and off without one. An empty
`secretName` means that made Secret. The check is on the value's type,
not `hasKey`: Helm 3 keeps a null default as a key, Helm 4 drops it.
Usage: {{- $handoff := include "meho.approvalHandoff" . | fromYaml }}
*/}}
{{- define "meho.approvalHandoff" -}}
{{- $core := include "meho.core.enabled" . -}}
{{- $set := .Values.approvalHandoff.enabled -}}
{{- $enabled := ternary $set (eq $core "true") (kindIs "bool" $set) -}}
{{- $name := .Values.approvalHandoff.secretName -}}
{{- if and $enabled (not $name) -}}
{{- if not $core -}}
{{- fail "approvalHandoff.secretName is empty: name the Secret with the Fernet key, or turn on a built-in core part so the install makes it." -}}
{{- end -}}
{{- $name = printf "%s-core-approval-handoff" .Release.Name -}}
{{- end -}}
enabled: {{ $enabled }}
secretName: {{ $name | quote }}
secretKey: {{ .Values.approvalHandoff.secretKey | quote }}
{{- end }}
