{{/*
Helpers for the built-in core parts (#4039). They live in the parent chart
so every core subchart and the parent's own templates (the helm test pod)
can use them. A subchart turned off by its `condition` contributes no
templates, so a helper the parent needs in bring-your-own mode cannot live
in a subchart.
*/}}

{{/*
Image reference from a `{repository, tag, digest}` map. The digest wins:
`repository@digest`, else `repository:tag`. Same rule as the backplane
image in deployment.yaml.
Usage: {{ include "meho.core.image" .Values.image }}
*/}}
{{- define "meho.core.image" -}}
{{- if .digest -}}
{{- printf "%s@%s" .repository .digest -}}
{{- else -}}
{{- printf "%s:%s" .repository .tag -}}
{{- end -}}
{{- end }}

{{/*
Pod security context for a core pod. With `global.openshift: true` it
leaves out `runAsUser`, `runAsGroup` and `fsGroup`, because OpenShift picks
them from the project's range and refuses fixed ones. Everything else
(`runAsNonRoot`, `seccompProfile`, ...) stays.
Usage: {{ include "meho.core.podSecurityContext" (dict "podSecurityContext" .Values.podSecurityContext "global" .Values.global) }}
*/}}
{{- define "meho.core.podSecurityContext" -}}
{{- $ctx := .podSecurityContext | default dict -}}
{{- if and .global .global.openshift -}}
{{- $ctx = omit $ctx "runAsUser" "runAsGroup" "fsGroup" -}}
{{- end -}}
{{- toYaml $ctx -}}
{{- end }}
