{{/*
Helpers for the built-in core parts (#4039). They live in the parent chart
so every core subchart and the parent's own templates (the helm test pod)
can use them. A subchart turned off by its `condition` contributes no
templates, so a helper the parent needs in bring-your-own mode cannot live
in a subchart.
*/}}

{{/*
Image reference from a `{repository, tag, digest}` map. The digest wins:
`repository@digest`, else `repository:tag`. Every pod of the chart and its
subcharts uses this helper.
With `global.imageRegistry` set (#4100), the registry replaces the
registry host of the repository; a repository with no host gets the
registry in front. A one-word name gets `library/` too, as Docker Hub
does (`busybox` -> `<registry>/library/busybox`). The digest and the tag
stay. Empty = the reference is unchanged.
Usage: {{ include "meho.core.image" (dict "image" .Values.image "global" .Values.global) }}
*/}}
{{- define "meho.core.image" -}}
{{- /* The old call shape (`include "meho.core.image" .Values.image`) would render `%!s(<nil>)` silently. */ -}}
{{- $image := required "meho.core.image takes (dict \"image\" .Values.image \"global\" .Values.global)" .image -}}
{{- $repository := $image.repository -}}
{{- with and .global .global.imageRegistry -}}
{{- $parts := splitList "/" $repository -}}
{{- $first := first $parts -}}
{{- if eq (len $parts) 1 -}}
{{- $repository = printf "%s/library/%s" . $repository -}}
{{- else if or (contains "." $first) (contains ":" $first) (eq $first "localhost") -}}
{{- $repository = printf "%s/%s" . (join "/" (rest $parts)) -}}
{{- else -}}
{{- $repository = printf "%s/%s" . $repository -}}
{{- end -}}
{{- end -}}
{{- if $image.digest -}}
{{- printf "%s@%s" $repository $image.digest -}}
{{- else -}}
{{- printf "%s:%s" $repository $image.tag -}}
{{- end -}}
{{- end }}

{{/*
The pod's image pull secrets as a YAML list: `global.imagePullSecrets`,
then the chart's own `imagePullSecrets` (only the parent chart has one).
Empty when both are empty, so the caller can leave the key out.
Usage:
  {{- with include "meho.core.imagePullSecrets" . }}
  imagePullSecrets:
    {{- . | nindent 4 }}
  {{- end }}
*/}}
{{- define "meho.core.imagePullSecrets" -}}
{{- with concat (.Values.global.imagePullSecrets | default list) (.Values.imagePullSecrets | default list) | uniq -}}
{{- toYaml . -}}
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
