{{/*
Expand the name of the chart.
*/}}
{{- define "ump.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "ump.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "ump.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "ump.labels" -}}
helm.sh/chart: {{ include "ump.chart" . }}
{{ include "ump.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "ump.selectorLabels" -}}
app.kubernetes.io/name: urban-model-platform-api
app.kubernetes.io/component: api
app.kubernetes.io/part-of: urban-model-platform
{{- end }}

{{/*
Create the name of the service account to use
*/}}
{{- define "ump.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "ump.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Create a variable that holds the current issuer (prod or staging)
*/}}
{{- define "ump.issuer" -}}
{{- if .Values.tls.clusterIssuerRef.name }}
{{- .Values.tls.clusterIssuerRef.name }}
{{- else if .Values.tls.issuer.prodEnabled }}
{{- printf "%s-le-prod" (include "ump.fullname" .) }}
{{- else }}
{{- printf "%s-le-staging" (include "ump.fullname" .) }}
{{- end }}
{{- end }}

{{/*
Validate if hostname has a value when tls is enabled
*/}}
{{- define "ump.validateValues" -}}
{{- if and .Values.tls.enabled (not .Values.tls.gateway.hostName) -}}
{{- fail "tls.gateway.hostName is required when TLS is enabled" -}}
{{- end -}}
{{- end -}}

{{/*
Create the name of the result store PVC to use
*/}}
{{- define "ump.resultStorePvcName" -}}
{{- if .Values.resultStore.persistence.existingClaim }}
{{- .Values.resultStore.persistence.existingClaim }}
{{- else }}
{{- printf "%s-results-store" (include "ump.fullname" .) }}
{{- end }}
{{- end }}