{{/*
Built-in core: the `vault-init` Job that sets up OpenBao (#4044,
docs/codebase/core-vault-init.md). The Job, its ServiceAccount and its Role
are normal release objects, not hooks.
*/}}

{{/*
Name of the Job's ServiceAccount, Role, RoleBinding and policy ConfigMap,
and the start of the Job's name. Cut to 54 characters, so the Job name
`<name>-<8 hash characters>` stays within 63.
*/}}
{{- define "meho.core.vaultInitName" -}}
{{- printf "%s-vault-init" .Release.Name | trunc 54 | trimSuffix "-" -}}
{{- end }}

{{/*
The Job's plan as JSON in `MEHO_CORE_VAULT_PLAN`: names and addresses only.
The Python model is `Plan` in backend/src/meho_backplane/core/vault_init.py.
OpenBao and the Job reach Keycloak through its Service; tokens carry the
public issuer.
*/}}
{{- define "meho.core.vaultPlan" -}}
{{- $rel := .Release.Name -}}
{{- $kc := include "meho.keycloakSettings" . | fromYaml -}}
{{- $keycloak := printf "http://%s:%v/auth" (include "coreKeycloak.fullname" .) (.Values.coreKeycloak.service.port | default 8080) -}}
{{- dict
  "openbao_url" (printf "http://%s:%v" (include "coreVault.fullname" .) (.Values.coreVault.service.port | default 8200))
  "keycloak_url" $keycloak
  "jwks_url" (printf "%s/realms/meho/protocol/openid-connect/certs" $keycloak)
  "bound_issuer" $kc.issuerUrl
  "audience" $kc.audience
  "policy_dir" "/opt/meho/openbao-policies"
  "service_account" (include "meho.core.vaultInitName" .)
  "sa_token_file" "/var/run/secrets/openbao/token"
  "unseal_secret" (printf "%s-core-vault-unseal" $rel)
  "init_secret" (printf "%s-core-vault-init" $rel)
  "scheduler_secret" (printf "%s-core-vault-scheduler" $rel)
  "snapshot_secret" (printf "%s-core-vault-snapshot" $rel)
  | toJson -}}
{{- end }}

{{/*
The Job's pod template. The Job's name ends with a hash of this text and
the policy files, so every change here starts a new Job: a Job's pod
template cannot be changed in place.
*/}}
{{- define "meho.core.vaultInitPod" -}}
metadata:
  labels:
    # Not `meho.selectorLabels`: the backplane NetworkPolicy allows no
    # egress to the Kubernetes API, which this Job needs. The OpenBao and
    # Keycloak NetworkPolicies admit this component.
    app.kubernetes.io/instance: {{ .Release.Name }}
    app.kubernetes.io/component: vault-init
spec:
  # `Never`: a failed pod keeps its log, which says what to fix.
  restartPolicy: Never
  serviceAccountName: {{ include "meho.core.vaultInitName" . }}
  automountServiceAccountToken: true
  {{- with include "meho.core.imagePullSecrets" . }}
  imagePullSecrets:
    {{- . | nindent 4 }}
  {{- end }}
  securityContext:
    {{- toYaml .Values.podSecurityContext | nindent 4 }}
  containers:
    - name: vault-init
      # The backplane image, with the same digest pin as the Deployment.
      image: {{ include "meho.core.image" (dict "image" .Values.image "global" .Values.global) | quote }}
      imagePullPolicy: {{ .Values.image.pullPolicy }}
      command: ["python", "-m", "meho_backplane.core.vault_init"]
      terminationMessagePolicy: FallbackToLogsOnError
      env:
        - name: POD_NAMESPACE
          valueFrom:
            fieldRef:
              fieldPath: metadata.namespace
        - name: MEHO_CORE_VAULT_PLAN
          value: {{ include "meho.core.vaultPlan" . | quote }}
      resources:
        {{- toYaml .Values.migrationJob.resources | nindent 8 }}
      securityContext:
        {{- toYaml .Values.securityContext | nindent 8 }}
      volumeMounts:
        - name: tmp
          mountPath: /tmp
        - name: policies
          mountPath: /opt/meho/openbao-policies
          readOnly: true
        - name: openbao-token
          mountPath: /var/run/secrets/openbao
          readOnly: true
  volumes:
    - name: tmp
      emptyDir: {}
    - name: policies
      configMap:
        name: {{ include "meho.core.vaultInitName" . }}-policies
    # The Job's login to OpenBao on later runs: a token for the audience
    # `openbao`, which the API server does not accept and OpenBao does.
    - name: openbao-token
      projected:
        sources:
          - serviceAccountToken:
              audience: openbao
              expirationSeconds: 600
              path: token
  {{- with .Values.nodeSelector }}
  nodeSelector:
    {{- toYaml . | nindent 4 }}
  {{- end }}
  {{- with .Values.tolerations }}
  tolerations:
    {{- toYaml . | nindent 4 }}
  {{- end }}
  {{- with .Values.affinity }}
  affinity:
    {{- toYaml . | nindent 4 }}
  {{- end }}
{{- end }}
