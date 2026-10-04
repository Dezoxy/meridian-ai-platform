{{- /*
Helpers of the Meridian chart. Each one says what it takes in its first line;
the callers of a helper that needs the release pass `$` (the root) in a dict,
because inside a `range` the dot is no longer the root.

Adoption constraint: a live cluster holds these objects already and Helm adopts
them (`--take-ownership`). Selectors are immutable, so a selector is the
`app.kubernetes.io/name` label alone, and so are the labels of a pod template:
no release name, no chart version, or a version bump would roll every pod.
*/ -}}

{{- /* image: <repository>:<tag>; both are required. */ -}}
{{- define "meridian.image" -}}
{{- $repository := required "image.repository is required: pass --set-string image.repository=<repository>" .Values.image.repository -}}
{{- printf "%s:%s" $repository (include "meridian.tag" .) -}}
{{- end -}}

{{- /* tag: the image tag as a string (twelve digits are a number to --set). */ -}}
{{- define "meridian.tag" -}}
{{- toString (required "image.tag is required: pass --set-string image.tag=<tag>" .Values.image.tag) -}}
{{- end -}}

{{- /* labels NAME: the labels of an object and of its pod template. */ -}}
{{- define "meridian.labels" -}}
app.kubernetes.io/name: {{ . }}
app.kubernetes.io/part-of: meridian
{{- end -}}

{{- /* host: <name>.<release namespace>.svc:<port>; takes root, name. */ -}}
{{- define "meridian.host" -}}
{{ .name }}.{{ .root.Release.Namespace }}.svc:{{ .root.Values.port }}
{{- end -}}

{{- /* url: http://<host>; takes root, name. */ -}}
{{- define "meridian.url" -}}
http://{{ include "meridian.host" . }}
{{- end -}}

{{- /* secretEnv: a variable read from the key `uri` of a Secret; takes name, secret. */ -}}
{{- define "meridian.secretEnv" -}}
- name: {{ .name }}
  valueFrom:
    secretKeyRef:
      name: {{ .secret }}
      key: uri
{{- end -}}

{{- /*
envItem: one variable of a values `env` list; takes root, item. The item has
one of value, serviceUrl, serviceHost or serviceMap (values.yaml says which).
*/ -}}
{{- define "meridian.envItem" -}}
- name: {{ .item.name }}
{{- if hasKey .item "value" }}
  value: {{ .item.value | quote }}
{{- else if hasKey .item "serviceUrl" }}
  value: {{ include "meridian.url" (dict "root" .root "name" .item.serviceUrl) | quote }}
{{- else if hasKey .item "serviceHost" }}
  value: {{ include "meridian.host" (dict "root" .root "name" .item.serviceHost) | quote }}
{{- else if hasKey .item "serviceMap" }}
  value: |-
    {
    {{- $last := sub (len .item.serviceMap) 1 }}
    {{- range $index, $name := .item.serviceMap }}
      {{ $name | quote }}: {{ include "meridian.url" (dict "root" $.root "name" $name) | quote }}{{ if lt $index $last }},{{ end }}
    {{- end }}
    }
{{- else }}
{{- fail (printf "env %s needs one of value, serviceUrl, serviceHost, serviceMap" .item.name) }}
{{- end }}
{{- end -}}

{{- /* caMount: where the pods read the database's CA certificate. */ -}}
{{- define "meridian.caMount" -}}
- name: db-ca
  mountPath: /etc/meridian/db-ca
  readOnly: true
{{- end -}}

{{- /*
caVolume: only the public certificate: the Secret also holds the CA's private
key. Takes the root.
*/ -}}
{{- define "meridian.caVolume" -}}
- name: db-ca
  secret:
    secretName: {{ .Values.database.caSecret }}
    items:
      - key: ca.crt
        path: ca.crt
{{- end -}}

{{- /*
job: a Job and its ServiceAccount, each Job its own account; takes root, name
(migrate, seed or ingest) and job (its values). The Job's name ends in the tag.
*/ -}}
{{- define "meridian.job" -}}
{{- $root := .root -}}
{{- $job := .job -}}
{{- $app := printf "meridian-%s" .name -}}
apiVersion: v1
kind: ServiceAccount
metadata:
  name: {{ $app }}
  namespace: {{ $root.Release.Namespace }}
  labels:
    {{- include "meridian.labels" $app | nindent 4 }}
automountServiceAccountToken: false
---
apiVersion: batch/v1
kind: Job
metadata:
  name: {{ $app }}-{{ include "meridian.tag" $root }}
  namespace: {{ $root.Release.Namespace }}
  labels:
    {{- include "meridian.labels" $app | nindent 4 }}
spec:
  backoffLimit: {{ $job.backoffLimit }}
  activeDeadlineSeconds: {{ $job.activeDeadlineSeconds }}
  {{- if hasKey $job "ttlSecondsAfterFinished" }}
  ttlSecondsAfterFinished: {{ $job.ttlSecondsAfterFinished }}
  {{- end }}
  template:
    metadata:
      labels:
        {{- include "meridian.labels" $app | nindent 8 }}
    spec:
      restartPolicy: Never
      serviceAccountName: {{ $app }}
      automountServiceAccountToken: false
      containers:
        - name: {{ .name }}
          image: {{ include "meridian.image" $root | quote }}
          imagePullPolicy: {{ $root.Values.image.pullPolicy }}
          command:
            {{- toYaml $job.command | nindent 12 }}
          env:
            {{- include "meridian.secretEnv" (dict "name" "MERIDIAN_MIGRATIONS_DATABASE_URL" "secret" $root.Values.database.ownerSecret) | nindent 12 }}
            {{- range $job.env }}
            {{- include "meridian.envItem" (dict "root" $root "item" .) | nindent 12 }}
            {{- end }}
          resources:
            {{- toYaml $job.resources | nindent 12 }}
          securityContext:
            {{- toYaml $root.Values.securityContext | nindent 12 }}
          volumeMounts:
            {{- include "meridian.caMount" . | nindent 12 }}
      volumes:
        {{- include "meridian.caVolume" $root | nindent 8 }}
{{- end -}}
