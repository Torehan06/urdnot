{{/*
Named templates ("helpers"). A file starting with _ renders no manifest; it
only defines snippets that the other templates pull in with `include`.
*/}}

{{/* Labels put on every object's metadata. Selectors use only the name label,
because a Deployment's selector is immutable after creation. */}}
{{- define "urdnot.labels" -}}
app.kubernetes.io/part-of: urdnot
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end }}

{{/* The Secret every workload reads: the chart's own, or one that already exists. */}}
{{- define "urdnot.secretName" -}}
{{- .Values.secrets.existingSecret | default "urdnot-secrets" -}}
{{- end }}

{{/* The fewest API Pods that can be running: the autoscaler's minimum when it
is on, else the fixed replica count. */}}
{{- define "urdnot.apiMinReplicas" -}}
{{- if .Values.api.autoscaling.enabled -}}
{{ .Values.api.autoscaling.minReplicas }}
{{- else -}}
{{ .Values.api.replicaCount }}
{{- end -}}
{{- end }}

{{/* repository:tag, plus @digest when one is set. */}}
{{- define "urdnot.apiImage" -}}
{{- with .Values.api.image -}}
{{ .repository }}:{{ required "api.image.tag is required" .tag }}{{ with .digest }}@{{ . }}{{ end }}
{{- end -}}
{{- end }}
