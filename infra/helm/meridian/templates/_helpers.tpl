{{- /*
Helpers of the Meridian chart. Each one says what it takes in its first line;
the callers of a helper that needs the release pass `$` (the root) in a dict,
because inside a `range` the dot is no longer the root.

Adoption constraint: a live cluster holds these objects already and Helm adopts
them (`--take-ownership`). Selectors are immutable, so a selector is the
`app.kubernetes.io/name` label alone, and so are the labels of a pod template:
no release name, no chart version, or a version bump would roll every pod.
*/ -}}

{{- /*
image: the reference every container runs; takes the root. A digest gives
<repository>@<digest> and the tag is no part of it. Without one the reference is
<repository>:<tag>, only when the pull policy is Never: a tag names an image
loaded into the node, which is never pulled, and a tag in a registry can be
moved.
*/ -}}
{{- define "meridian.image" -}}
{{- $repository := required "image.repository is required: pass --set-string image.repository=<repository>" .Values.image.repository -}}
{{- $digest := toString (.Values.image.digest | default "") -}}
{{- if $digest -}}
{{- if not (regexMatch "^sha256:[0-9a-f]{64}$" $digest) -}}
{{- fail (printf "image.digest must match ^sha256:[0-9a-f]{64}$ (sha256: and 64 lower-case hex digits); got %q" $digest) -}}
{{- end -}}
{{- printf "%s@%s" $repository $digest -}}
{{- else if eq .Values.image.pullPolicy "Never" -}}
{{- printf "%s:%s" $repository (include "meridian.tag" .) -}}
{{- else -}}
{{- fail (printf "a tag is accepted only with image.pullPolicy Never, for an image loaded into the node, which is never pulled; an image from a registry needs image.digest: pass --set-string image.digest=sha256:<64 hex digits> (image.pullPolicy is %q)" .Values.image.pullPolicy) -}}
{{- end -}}
{{- end -}}

{{- /* tag: the image tag as a string (twelve digits are a number to --set). */ -}}
{{- define "meridian.tag" -}}
{{- toString (required "image.tag is required (or image.digest): pass --set-string image.tag=<tag> or --set-string image.digest=sha256:<64 hex digits>" .Values.image.tag) -}}
{{- end -}}

{{- /*
jobSuffix: what a Job's name ends in, the tag, or without one the first twelve
hex digits of the digest; takes the root.
*/ -}}
{{- define "meridian.jobSuffix" -}}
{{- if or .Values.image.tag (not .Values.image.digest) -}}
{{- include "meridian.tag" . -}}
{{- else -}}
{{- trimPrefix "sha256:" (toString .Values.image.digest) | trunc 12 -}}
{{- end -}}
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
podSecurityContext: the pod's; takes the root. The user and the group are
runAsId, which wins over a value of the same name in podSecurityContext.
*/ -}}
{{- define "meridian.podSecurityContext" -}}
{{- toYaml (merge (dict "runAsUser" (int64 .Values.runAsId) "runAsGroup" (int64 .Values.runAsId)) .Values.podSecurityContext) -}}
{{- end -}}

{{- /*
containerSecurityContext: the container's; takes the root. The user is runAsId,
the pod's too, so the two levels cannot disagree.
*/ -}}
{{- define "meridian.containerSecurityContext" -}}
{{- toYaml (merge (dict "runAsUser" (int64 .Values.runAsId)) .Values.securityContext) -}}
{{- end -}}

{{- /* tmpMount: the pod's one writable path (the root filesystem is read-only). */ -}}
{{- define "meridian.tmpMount" -}}
- name: tmp
  mountPath: /tmp
{{- end -}}

{{- /*
tmpVolume: an emptyDir on the default medium (a memory-backed one would count
against the container's memory limit), with a size limit. Takes the root.
*/ -}}
{{- define "meridian.tmpVolume" -}}
- name: tmp
  emptyDir:
    sizeLimit: {{ .Values.tmpSizeLimit | quote }}
{{- end -}}

{{- /*
callees: the services an `env` list calls, as a JSON list of names, sorted and
without repeats; takes the env list. A serviceUrl and each name of a serviceMap
is a call. A serviceHost is not: it is the service's own name, the Host header
its callers send. The NetworkPolicies take their egress from this, so the
addresses a pod is given and the connections it is allowed cannot disagree.
*/ -}}
{{- define "meridian.callees" -}}
{{- $names := list -}}
{{- range . -}}
{{- if hasKey . "serviceUrl" -}}
{{- $names = append $names .serviceUrl -}}
{{- else if hasKey . "serviceMap" -}}
{{- $names = concat $names .serviceMap -}}
{{- end -}}
{{- end -}}
{{- $names | uniq | sortAlpha | toJson -}}
{{- end -}}

{{- /*
callers: the pods that call a service, as a JSON list of their
app.kubernetes.io/name labels, sorted; takes root, name. A service's own name is
its label; a Job's is meridian-<job>. Every Job counts whether its flag is set or
not: the release never holds a Job, and the service's policy, which is in the
release, must already admit the Job deploy.sh applies later.
*/ -}}
{{- define "meridian.callers" -}}
{{- $target := .name -}}
{{- $callers := list -}}
{{- range $name, $service := .root.Values.services -}}
{{- if has $target (include "meridian.callees" $service.env | fromJsonArray) -}}
{{- $callers = append $callers $name -}}
{{- end -}}
{{- end -}}
{{- range $name, $job := .root.Values.jobs -}}
{{- if has $target (include "meridian.callees" $job.env | fromJsonArray) -}}
{{- $callers = append $callers (printf "meridian-%s" $name) -}}
{{- end -}}
{{- end -}}
{{- $callers | sortAlpha | toJson -}}
{{- end -}}

{{- /*
peer: one of networkPolicy.peers as JSON; takes root, key. Fails with the path
of the value when it is missing, because a policy that cannot name its peer
would be wrong, not narrower.
*/ -}}
{{- define "meridian.peer" -}}
{{- $peer := get .root.Values.networkPolicy.peers .key -}}
{{- if not $peer -}}
{{- fail (printf "networkPolicy.peers.%s is required while networkPolicy.enabled is true: its namespace, podLabels and ports (kind's are in infra/kind/values/meridian.yaml)" .key) -}}
{{- end -}}
{{- if not $peer.podLabels -}}
{{- fail (printf "networkPolicy.peers.%s.podLabels is required: a peer is named by its pod labels" .key) -}}
{{- end -}}
{{- toJson $peer -}}
{{- end -}}

{{- /*
peerSelector: one entry of a rule's `to` or `from`; takes namespace (empty: the
policy's own namespace) and labels (the pod labels). A pod selector alone
selects in the policy's own namespace; with a namespace both must match.
*/ -}}
{{- define "meridian.peerSelector" -}}
{{- with .namespace }}
namespaceSelector:
  matchLabels:
    kubernetes.io/metadata.name: {{ . | quote }}
{{- end }}
podSelector:
  matchLabels:
    {{- toYaml .labels | nindent 4 }}
{{- end -}}

{{- /* peerRule: an egress rule to one of networkPolicy.peers, on its ports; takes root, key. */ -}}
{{- define "meridian.peerRule" -}}
{{- $peer := include "meridian.peer" . | fromJson -}}
{{- $ports := required (printf "networkPolicy.peers.%s.ports is required: the port and protocol a rule allows" .key) $peer.ports -}}
- to:
    - {{- include "meridian.peerSelector" (dict "namespace" $peer.namespace "labels" $peer.podLabels) | nindent 6 }}
  ports:
    {{- toYaml $ports | nindent 4 }}
{{- end -}}

{{- /*
serviceRule: a rule to or from the pods named by app.kubernetes.io/name, on the
services' port; takes root, direction (to or from), names (a list, not empty).
*/ -}}
{{- define "meridian.serviceRule" -}}
- {{ .direction }}:
    {{- range .names }}
    - podSelector:
        matchLabels:
          app.kubernetes.io/name: {{ . }}
    {{- end }}
  ports:
    - port: {{ .root.Values.port }}
      protocol: TCP
{{- end -}}

{{- /*
egress: the rules of a workload's egress; takes root, env (the workload's env
list: the services it calls) and collector (true: it sets the collector's
address, so it may reach the collector). Every workload gets DNS and the
database; nothing else unless its environment names it.
*/ -}}
{{- define "meridian.egress" -}}
{{- $callees := include "meridian.callees" .env | fromJsonArray -}}
{{- include "meridian.peerRule" (dict "root" .root "key" "dns") }}
{{ include "meridian.peerRule" (dict "root" .root "key" "database") }}
{{- if .collector }}
{{ include "meridian.peerRule" (dict "root" .root "key" "collector") }}
{{- end }}
{{- if $callees }}
{{ include "meridian.serviceRule" (dict "root" .root "direction" "to" "names" $callees) }}
{{- end }}
{{- end -}}

{{- /*
job: a Job, its ServiceAccount and its NetworkPolicy, each Job its own account
and policy; takes root, name (migrate, seed or ingest) and job (its values). The
Job's name ends in the tag, or in the digest's first twelve digits; the policy's
does not, so a later deploy replaces it instead of adding one. It lives here,
not in the release, because deploy.sh applies it with the Job: the policy is
never one deploy behind its Job.
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
{{- if $root.Values.networkPolicy.enabled }}
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: {{ $app }}
  namespace: {{ $root.Release.Namespace }}
  labels:
    {{- include "meridian.labels" $app | nindent 4 }}
spec:
  podSelector:
    matchLabels:
      app.kubernetes.io/name: {{ $app }}
  policyTypes: [Ingress, Egress]
  egress:
    {{- include "meridian.egress" (dict "root" $root "env" $job.env "collector" false) | nindent 4 }}
{{- end }}
---
apiVersion: batch/v1
kind: Job
metadata:
  name: {{ $app }}-{{ include "meridian.jobSuffix" $root }}
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
      securityContext:
        {{- include "meridian.podSecurityContext" $root | nindent 8 }}
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
            {{- include "meridian.containerSecurityContext" $root | nindent 12 }}
          volumeMounts:
            {{- include "meridian.caMount" . | nindent 12 }}
            {{- include "meridian.tmpMount" . | nindent 12 }}
      volumes:
        {{- include "meridian.caVolume" $root | nindent 8 }}
        {{- include "meridian.tmpVolume" $root | nindent 8 }}
{{- end -}}
