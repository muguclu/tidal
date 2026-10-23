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

{{/* Fails the render on a missing required key or a secret-looking key. Emits nothing. */}}
{{- define "tidal.validateConfig" -}}
{{- range $k, $v := .Values.worker.config -}}
{{- if mustRegexMatch "(CONNECTION_STRING|PASSWORD|SECRET|KEY$)" $k -}}
{{- fail (printf "worker.config.%s looks like a secret; secrets never go in values" $k) -}}
{{- end -}}
{{- end -}}
{{- range $k := list "TIDAL_STORAGE__ACCOUNT_URL" "TIDAL_SQL__SERVER" "TIDAL_SERVICEBUS__FQDN" "TIDAL_QUERY__BASE_URL" "TIDAL_QUERY__AUDIENCE" -}}
{{- if not (index $.Values.worker.config $k) -}}
{{- fail (printf "worker.config.%s is required" $k) -}}
{{- end -}}
{{- end -}}
{{- if ne (index .Values.worker.config "TIDAL_SQL__AUTH") "entra_mi" -}}
{{- fail "worker.config.TIDAL_SQL__AUTH must be entra_mi" -}}
{{- end -}}
{{- end }}
