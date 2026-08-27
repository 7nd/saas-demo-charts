{{- define "deployment-demo.fullname" -}}
{{ .Release.Name }}
{{- end -}}

{{- define "deployment-demo.labels" -}}
app.kubernetes.io/name: deployment-demo
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "deployment-demo.selectorLabels" -}}
app.kubernetes.io/name: deployment-demo
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
