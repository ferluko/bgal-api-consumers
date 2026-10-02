{{/*
_helpers.tpl — api-consumers
*/}}
{{- define "apicon.name" -}}api-consumers{{- end }}
{{- define "apicon.ns" -}}{{ .Values.apiConsumers.namespace }}{{- end }}

{{- define "apicon.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version | replace "+" "_" }}
app.kubernetes.io/name: {{ include "apicon.name" . }}
app.kubernetes.io/component: api
app.kubernetes.io/part-of: apim-toolkit
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- if .Values.cluster.name }}
gitops.bancogalicia.com.ar/cluster: {{ .Values.cluster.name }}
gitops.bancogalicia.com.ar/env: {{ .Values.cluster.env | default "" | quote }}
gitops.bancogalicia.com.ar/domain: {{ .Values.cluster.domain | default "" | quote }}
gitops.bancogalicia.com.ar/tier: {{ .Values.cluster.tier | default "" | quote }}
gitops.bancogalicia.com.ar/managed-by: argocd
{{- end }}
{{- end }}

{{- define "apicon.selector" -}}
app.kubernetes.io/name: {{ include "apicon.name" . }}
app.kubernetes.io/part-of: apim-toolkit
{{- end }}

{{- define "apicon.secretName" -}}
{{- with .Values.apiConsumers.secrets -}}
{{- if .existingSecret }}{{ .existingSecret }}{{ else }}api-consumers-secrets{{ end -}}
{{- end -}}
{{- end }}

{{- define "apicon.routeHost" -}}
{{- $r := .Values.apiConsumers.route -}}
{{- if $r.host -}}{{ $r.host }}
{{- else -}}
{{- $domain := $r.ingressDomain | default (printf "apps.%s.%s" .Values.cluster.name .Values.cluster.baseDomain) -}}
api-consumers-{{ include "apicon.ns" . }}.{{ $domain }}
{{- end -}}
{{- end }}

{{/* Validaciones: fallar explícito, como metallb.pool.addresses en gitops/ */}}
{{- define "apicon.validate" -}}
{{- $a := .Values.apiConsumers -}}
{{- if lt (int $a.replicas) 1 -}}{{ fail "apiConsumers.replicas debe ser >= 1" }}{{- end -}}
{{- if not $a.vault.addr -}}{{ fail "apiConsumers.vault.addr es obligatorio" }}{{- end -}}
{{- if not (has $a.vault.auth (list "kubernetes" "approle" "token")) -}}{{ fail "apiConsumers.vault.auth debe ser kubernetes, approle o token" }}{{- end -}}
{{- if and $a.secrets.externalSecret.enabled (not $a.secrets.existingSecret) -}}
{{- if not $a.secrets.externalSecret.storeRef.name -}}{{ fail "apiConsumers.secrets.externalSecret.storeRef.name es obligatorio" }}{{- end -}}
{{- if not $a.secrets.externalSecret.path -}}{{ fail "apiConsumers.secrets.externalSecret.path es obligatorio" }}{{- end -}}
{{- end -}}
{{- if not (has $a.tier (list "nonprd" "prd")) -}}{{ fail "apiConsumers.tier debe ser nonprd o prd" }}{{- end -}}
{{- end }}
