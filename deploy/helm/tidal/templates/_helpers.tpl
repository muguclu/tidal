{{- define "tidal.labels" -}}
app.kubernetes.io/name: tidal
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end }}

{{- define "tidal.workerLabels" -}}
{{ include "tidal.labels" . }}
app.kubernetes.io/component: backtest-worker
{{- end }}

{{- define "tidal.image" -}}
{{- $r := required "image.repository is required" .Values.image.repository -}}
{{- if .Values.image.digest -}}
{{- printf "%s@%s" $r .Values.image.digest -}}
{{- else -}}
{{- $t := required "image.tag or image.digest is required" .Values.image.tag -}}
{{- if eq $t "latest" -}}{{- fail "image.tag must not be latest" -}}{{- end -}}
{{- printf "%s:%s" $r $t -}}
{{- end -}}
{{- end }}
