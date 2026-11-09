{{/*
Approval notifications in the browser (web push, docs/codebase/ui.md).
The push key as YAML: `enabled`, `secretName`, `secretKey`. Off unless
`webPush.enabled`. `webPush.secretName` names your own Secret; empty means
`<release>-core-web-push`, which the secrets Job makes with a built-in core
part. With every built-in part off, `webPush.enabled` needs `secretName`.
Usage: {{- $push := include "meho.webPush" . | fromYaml }}
*/}}
{{- define "meho.webPush" -}}
{{- $name := .Values.webPush.secretName -}}
{{- if .Values.webPush.enabled -}}
{{- if not $name -}}
{{- if not (include "meho.core.enabled" .) -}}
{{- fail "webPush.secretName is empty: name the Secret with the push key, or turn on a built-in core part so the install makes it." -}}
{{- end -}}
{{- $name = printf "%s-core-web-push" .Release.Name -}}
{{- end -}}
{{- end -}}
enabled: {{ .Values.webPush.enabled }}
secretName: {{ $name | quote }}
secretKey: {{ .Values.webPush.secretKey | quote }}
{{- end }}
