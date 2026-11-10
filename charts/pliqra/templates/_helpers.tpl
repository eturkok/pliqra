{{- define "pliqra.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- define "pliqra.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else if contains (include "pliqra.name" .) .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "pliqra.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- define "pliqra.selectorLabels" -}}
app.kubernetes.io/name: {{ include "pliqra.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
{{- define "pliqra.labels" -}}
{{ include "pliqra.selectorLabels" . }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | quote }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}
{{- define "pliqra.serviceAccount" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "pliqra.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}
{{- define "pliqra.image" -}}
{{- if .Values.image.digest -}}
{{ printf "%s@%s" .Values.image.repository .Values.image.digest }}
{{- else -}}
{{ printf "%s:%s" .Values.image.repository .Values.image.tag }}
{{- end -}}
{{- end -}}
{{- define "pliqra.validate" -}}
{{- if .Release.IsUpgrade -}}
  {{- $previous := lookup "apps/v1" "Deployment" .Release.Namespace (include "pliqra.fullname" .) -}}
  {{- if $previous -}}
    {{- $oldMode := index ($previous.metadata.annotations | default dict) "pliqra.io/storage-mode" | default "" -}}
    {{- if and $oldMode (ne $oldMode .Values.storage.mode) -}}{{ fail "storage.mode cannot change on an existing release; perform an offline migration into a separate installation" }}{{- end -}}
    {{- $oldClaim := index ($previous.metadata.annotations | default dict) "pliqra.io/data-claim" | default "" -}}
    {{- $newClaim := default (include "pliqra.fullname" .) .Values.persistence.existingClaim -}}
    {{- if and (eq .Values.storage.mode "sqlite") $oldClaim (ne $oldClaim $newClaim) -}}{{ fail "SQLite data claim cannot change on an existing release; restore into a separate installation" }}{{- end -}}
  {{- end -}}
{{- end -}}
{{- if eq .Values.storage.mode "sqlite" -}}
  {{- if ne (int .Values.replicaCount) 1 -}}{{ fail "SQLite requires replicaCount=1; use PostgreSQL for multiple replicas" }}{{- end -}}
  {{- if .Values.autoscaling.enabled -}}{{ fail "SQLite cannot use autoscaling; use PostgreSQL" }}{{- end -}}
  {{- if .Values.topology.multiDC -}}{{ fail "Multi-DC requires PostgreSQL" }}{{- end -}}
  {{- if or .Values.database.existingSecret .Values.database.tls.existingSecret -}}{{ fail "Database settings require storage.mode=postgresql" }}{{- end -}}
{{- else -}}
  {{- $_ := required "PostgreSQL requires database.existingSecret" .Values.database.existingSecret -}}
  {{- $_ := required "PostgreSQL requires encryption.existingSecret shared by all replicas" .Values.encryption.existingSecret -}}
  {{- if .Values.persistence.existingClaim -}}{{ fail "PostgreSQL uses pod-local scratch space; remove persistence.existingClaim" }}{{- end -}}
{{- end -}}
{{- if and .Values.autoscaling.enabled (lt (int .Values.autoscaling.maxReplicas) (int .Values.autoscaling.minReplicas)) -}}{{ fail "autoscaling.maxReplicas must be >= minReplicas" }}{{- end -}}
{{- if .Values.autoscaling.enabled -}}{{- $_ := required "CPU autoscaling requires resources.requests.cpu" (dig "requests" "cpu" "" .Values.resources) -}}{{- end -}}
{{- if .Values.ingress.enabled -}}
  {{- $_ := required "Ingress requires ingress.host" .Values.ingress.host -}}
  {{- $_ := required "Ingress requires an existing ingress.tlsSecret" .Values.ingress.tlsSecret -}}
  {{- if ne .Values.config.publicUrl (printf "https://%s" .Values.ingress.host) -}}{{ fail "Ingress requires config.publicUrl=https://<ingress.host> without a path" }}{{- end -}}
  {{- if not .Values.config.secureCookies -}}{{ fail "Ingress requires config.secureCookies=true" }}{{- end -}}
{{- end -}}
{{- if and (hasPrefix "https://" .Values.config.publicUrl) (not .Values.config.secureCookies) -}}{{ fail "HTTPS public URLs require secure cookies" }}{{- end -}}
{{- $reserved := "^(PLIQRA|MODELPORT)_(DATA_DIR|HOST|PORT|DATABASE_URL|DATABASE_SCHEMA|ENCRYPTION_KEY(_FILE)?|SITE|REGION|MULTI_DC|PUBLIC_URL|SECURE_COOKIES|RESTART_ON_FENCE|TLS_CERTFILE|TLS_KEYFILE)$" -}}
{{- $names := dict -}}
{{- range .Values.extraEnv -}}
  {{- if or (regexMatch $reserved .name) (hasKey $names .name) -}}{{ fail (printf "extraEnv cannot override managed or duplicate variable %s" .name) }}{{- end -}}
  {{- $_ := set $names .name true -}}
{{- end -}}
{{- range .Values.extraVolumes -}}{{- if has .name (list "data" "tmp" "database-ca") -}}{{ fail "extraVolumes uses a reserved volume name" }}{{- end -}}{{- end -}}
{{- range .Values.extraVolumeMounts -}}{{- if or (has .name (list "data" "tmp" "database-ca")) (has .mountPath (list "/data" "/tmp" "/run/pliqra/database")) -}}{{ fail "extraVolumeMounts uses a reserved volume or mount path" }}{{- end -}}{{- end -}}
{{- end -}}
