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
moved. The repository carries neither a tag nor a digest of its own: those are
image.tag and image.digest, and a second one would make the reference say two
things.
*/ -}}
{{- define "meridian.image" -}}
{{- $repository := required "image.repository is required: pass --set-string image.repository=<repository>" .Values.image.repository -}}
{{- if or (contains "@" $repository) (contains ":" (splitList "/" $repository | last)) -}}
{{- fail (printf "image.repository must not carry a tag or a digest of its own (no @, and no : after the last /; a registry port before it is fine); got %q: pass the tag as image.tag or the digest as image.digest" $repository) -}}
{{- end -}}
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

{{- /*
tag: the image tag as a string (twelve digits are a number to --set). It must
match the OCI tag grammar and must not be `latest`, a tag that names a different
image each time it is built. A Job's name ends in the tag, so this is also the
one place the suffix is checked.
*/ -}}
{{- define "meridian.tag" -}}
{{- $tag := toString (required "image.tag is required (or image.digest): pass --set-string image.tag=<tag> or --set-string image.digest=sha256:<64 hex digits>" .Values.image.tag) -}}
{{- if or (not (regexMatch "^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$" $tag)) (eq $tag "latest") -}}
{{- fail (printf "image.tag must match ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ and must not be latest; got %q" $tag) -}}
{{- end -}}
{{- $tag -}}
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

{{- /*
url: <scheme>://<host>; takes root, name. The scheme is https for a service whose
values say `tls: true` and http for any other, the Claims API's included: the
address is read from the target's own entry, so it cannot disagree with what the
target serves.
*/ -}}
{{- define "meridian.url" -}}
{{- $target := get .root.Values.services .name | default dict -}}
{{ if $target.tls }}https{{ else }}http{{ end }}://{{ include "meridian.host" . }}
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
documentsDeadlineDays: sweep.documentsDeadlineDays as the text both workloads
get, or the render fails with the value in the message. The Claims API and the
sweep read it with deadline_days_of
(src/meridian/workloads/claims_triage/lifecycle.py), which refuses anything but
one to four ASCII digits that make a whole number from 1 to 365 when the
process starts, so a bad value would crash-loop the Claims API. The two bounds
are literals here: tests/meridian/test_helm_documents_deadline.py reads the
code's and fails when they differ. A number from a values file arrives as a
float and one from --set as an integer: toString makes both the digits that
were written.
*/ -}}
{{- define "meridian.documentsDeadlineDays" -}}
{{- $text := toString .Values.sweep.documentsDeadlineDays -}}
{{- if not (and (regexMatch "^[0-9]{1,4}$" $text) (ge (atoi $text) 1) (le (atoi $text) 365)) -}}
{{- fail (printf "sweep.documentsDeadlineDays is %q, which is not a whole number of days from 1 to 365 (the Claims API and the sweep refuse anything else when they start, so the Claims API would crash-loop: deadline_days_of in src/meridian/workloads/claims_triage/lifecycle.py)" $text) -}}
{{- end -}}
{{- $text -}}
{{- end -}}

{{- /*
envItem: one variable of a values `env` list; takes root, item. The item has
one of value, serviceUrl, serviceHost, serviceMap or documentsDeadlineDays
(values.yaml says which). documentsDeadlineDays takes the sweep's value of the
same name, so the Claims API and the sweep's CronJob read one value.
*/ -}}
{{- define "meridian.envItem" -}}
- name: {{ .item.name }}
{{- if hasKey .item "value" }}
  value: {{ .item.value | quote }}
{{- else if hasKey .item "documentsDeadlineDays" }}
  value: {{ include "meridian.documentsDeadlineDays" .root | quote }}
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
{{- fail (printf "env %s needs one of value, serviceUrl, serviceHost, serviceMap, documentsDeadlineDays" .item.name) }}
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
runAsId: the user and the group every container runs as, as an int64; takes the
root. Zero is root, and the chart refuses it.
*/ -}}
{{- define "meridian.runAsId" -}}
{{- $id := int64 .Values.runAsId -}}
{{- if eq $id 0 -}}
{{- fail "runAsId must not be 0: the containers never run as root" -}}
{{- end -}}
{{- $id -}}
{{- end -}}

{{- /*
podSecurityContext: the pod's, the same for every pod of the chart; takes the
root. A literal, not a value: a `--set` or a values file cannot loosen it (see
values.yaml). The user, the group and fsGroup are runAsId. fsGroup gives a
mounted Secret volume to that group (see meridian.tlsVolume); the helper is
shared, so every pod of the chart gets it, the Jobs' and the sweep's too, and
it also gives the group to the pod's /tmp.
*/ -}}
{{- define "meridian.podSecurityContext" -}}
{{- toYaml (dict "runAsNonRoot" true "runAsUser" (include "meridian.runAsId" . | int64) "runAsGroup" (include "meridian.runAsId" . | int64) "fsGroup" (include "meridian.runAsId" . | int64) "seccompProfile" (dict "type" "RuntimeDefault")) -}}
{{- end -}}

{{- /*
containerSecurityContext: the container's, the same for every container of the
chart; takes the root. A literal, not a value. The user is runAsId, the pod's
too, so the two levels cannot disagree. It repeats the pod's runAsNonRoot and
seccompProfile on purpose: a container added to a pod later keeps them. The
image's filesystem cannot be written; the pod's /tmp can.
*/ -}}
{{- define "meridian.containerSecurityContext" -}}
{{- toYaml (dict "runAsNonRoot" true "runAsUser" (include "meridian.runAsId" . | int64) "allowPrivilegeEscalation" false "readOnlyRootFilesystem" true "capabilities" (dict "drop" (list "ALL")) "seccompProfile" (dict "type" "RuntimeDefault")) -}}
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
identity: the chart's identity values, checked, as JSON (trustDomain, issuerName,
issuerKind); takes the root. Each is required and there is no value that turns
identity off, so a chart without them does not render: a service would otherwise
start without a certificate and refuse every call, or serve with none.
*/ -}}
{{- define "meridian.identity" -}}
{{- $identity := .Values.identity | default dict -}}
{{- $issuer := $identity.issuer | default dict -}}
{{- $trustDomain := required "identity.trustDomain is required: the SPIFFE trust domain of the services' certificates (kind's is in infra/kind/values/meridian.yaml)" $identity.trustDomain -}}
{{- $name := required "identity.issuer.name is required: the cert-manager issuer that signs the services' certificates (kind's is in infra/kind/values/meridian.yaml)" $issuer.name -}}
{{- $kind := required "identity.issuer.kind is required: ClusterIssuer or Issuer (kind's is in infra/kind/values/meridian.yaml)" $issuer.kind -}}
{{- if not (regexMatch "^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$" (toString $trustDomain)) -}}
{{- fail (printf "identity.trustDomain must be a lower-case DNS name (no scheme, no path); got %q" $trustDomain) -}}
{{- end -}}
{{- if not (has $kind (list "ClusterIssuer" "Issuer")) -}}
{{- fail (printf "identity.issuer.kind must be ClusterIssuer or Issuer; got %q" $kind) -}}
{{- end -}}
{{- dict "trustDomain" $trustDomain "issuerName" $name "issuerKind" $kind | toJson -}}
{{- end -}}

{{- /*
identityPrefix: the start of every service's URI, up to and including /sa/; what
a service that serves TLS is given to read its callers' identity with. Takes the
root.
*/ -}}
{{- define "meridian.identityPrefix" -}}
{{- $identity := include "meridian.identity" . | fromJson -}}
spiffe://{{ $identity.trustDomain }}/ns/{{ .Release.Namespace }}/sa/
{{- end -}}

{{- /* tlsDirectory: where a pod reads its certificate, key and the CA. A literal. */ -}}
{{- define "meridian.tlsDirectory" -}}
/etc/meridian/tls
{{- end -}}

{{- /* tlsMount: the pod's own certificate Secret, read-only. */ -}}
{{- define "meridian.tlsMount" -}}
- name: tls
  mountPath: {{ include "meridian.tlsDirectory" . }}
  readOnly: true
{{- end -}}

{{- /*
tlsVolume: the Secret cert-manager makes for the workload's Certificate
(<name>-tls: tls.crt, tls.key and ca.crt). Takes the workload's name: a pod
mounts its own and no other. defaultMode is 0440, a literal that Go's template
reads as octal and prints as the integer 288 (YAML 1.2 would read 0440 as 440):
with the pod's fsGroup (meridian.podSecurityContext) the files are root's and
the pod's group's, so the one user the container runs as reads them through its
group and no other user in the container does. 0400 would stop that user
reading its own key.
*/ -}}
{{- define "meridian.tlsVolume" -}}
- name: tls
  secret:
    secretName: {{ . }}-tls
    defaultMode: {{ 0440 }}
{{- end -}}

{{- /*
tlsFlags: what uvicorn is given to serve TLS and to ask for a client
certificate; the same for every service that sets `tls`, so it is written once.
--ssl-cert-reqs 1 is CERT_OPTIONAL: the kubelet's probe presents no certificate,
and a certificate from another CA still fails the handshake. --http selects the
protocol that puts the verified certificate's URIs in the request's scope. Takes
the root.
*/ -}}
{{- define "meridian.tlsFlags" -}}
{{- $directory := include "meridian.tlsDirectory" . -}}
- --ssl-certfile
- {{ $directory }}/tls.crt
- --ssl-keyfile
- {{ $directory }}/tls.key
- --ssl-ca-certs
- {{ $directory }}/ca.crt
- --ssl-cert-reqs
- "1"
- --http
- "meridian.platform.common.peercert:PeerCertProtocol"
- --ws
- none
{{- end -}}

{{- /*
tlsEnv: the variables a workload's identity needs; takes root, mounts (it mounts
its certificate: the caller sets it, as every service does and a Job does when
it has an identity) and serves (it serves TLS). A container that mounts the
certificate gets the three files' paths, to load its client context and for
/healthz to watch the certificate's end; one that serves TLS gets the prefix its
callers' URIs start with. No container without the mount gets any of them.
*/ -}}
{{- define "meridian.tlsEnv" -}}
{{- $directory := include "meridian.tlsDirectory" .root -}}
{{- if .mounts }}
- name: MERIDIAN_TLS_CERT_FILE
  value: {{ $directory }}/tls.crt
- name: MERIDIAN_TLS_KEY_FILE
  value: {{ $directory }}/tls.key
- name: MERIDIAN_TLS_CA_FILE
  value: {{ $directory }}/ca.crt
{{- end }}
{{- if .serves }}
- name: MERIDIAN_IDENTITY_PREFIX
  value: {{ include "meridian.identityPrefix" .root | quote }}
{{- end }}
{{- end -}}

{{- /* telemetryCaDirectory: where a service reads the collector's authority. A literal. */ -}}
{{- define "meridian.telemetryCaDirectory" -}}
/etc/meridian/telemetry-ca
{{- end -}}

{{- /*
telemetryCa: the name of the ConfigMap that holds the collector's authority, or
empty when the services push to no https address; takes the root. Refused, with
the value's name in the message:

  an https endpoint with no   the exporters would fall back to the system's CAs,
  ConfigMap                   which do not know the collector's authority, and
                              every push would fail after the pod had started
  a ConfigMap with an http    nothing would read it, and the name would say the
  endpoint                    telemetry is encrypted when it is not
  a name that is not a DNS    the apiserver would refuse the volume
  subdomain

The scheme is read without regard to case, as the SDK reads it. An empty endpoint
sends nothing, so no name is needed or mounted.
*/ -}}
{{- define "meridian.telemetryCa" -}}
{{- $telemetry := .Values.telemetry | default dict -}}
{{- $endpoint := lower (toString ($telemetry.otlpEndpoint | default "")) -}}
{{- $name := toString ($telemetry.caConfigMap | default "") -}}
{{- if and (hasPrefix "https://" $endpoint) (not $name) -}}
{{- fail "telemetry.otlpEndpoint is an https address, but telemetry.caConfigMap is empty: name the ConfigMap that holds the collector's CA certificate in its key ca.crt (kind's is telemetry-ca, which make up creates), or the services could not verify the collector and every push would fail" -}}
{{- end -}}
{{- if and $name (hasPrefix "http://" $endpoint) -}}
{{- fail (printf "telemetry.caConfigMap is %q, but telemetry.otlpEndpoint is an http address: nothing would read the CA certificate and the telemetry would not be encrypted; use an https address or leave telemetry.caConfigMap empty" $name) -}}
{{- end -}}
{{- if and $name (or (gt (len $name) 253) (not (regexMatch "^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$" $name))) -}}
{{- fail (printf "telemetry.caConfigMap is %q, which is not a ConfigMap's name (lower-case letters, digits, - and ., at most 253 characters)" $name) -}}
{{- end -}}
{{- if hasPrefix "https://" $endpoint -}}
{{- $name -}}
{{- end -}}
{{- end -}}

{{- /*
minutes: a duration of hours and minutes (2160h, 1h30m, 45m) in minutes; takes
name (the value's, for the message) and value. Go's duration syntax has more
units (cert-manager reads them); the chart reads these two so that it can
compare one duration with another, and fails on any other text, an empty one
included. The pattern is anchored, so a value with a line break cannot reach a
Certificate. A number has at most nine digits: the policy's cap in minutes
(129600) has six, and a longer one would overflow the parse and wrap into range
instead of being refused.
*/ -}}
{{- define "meridian.minutes" -}}
{{- $text := toString .value -}}
{{- $pattern := "^(?:([0-9]{1,9})h)?(?:([0-9]{1,9})m)?$" -}}
{{- if or (not $text) (not (regexMatch $pattern $text)) -}}
{{- fail (printf "%s is %q, which is not a duration of hours and minutes, like 2160h or 1h30m (cert-manager reads more units; the chart reads these two so that it can compare the lifetime with the policy's cap and with renewBefore)" .name $text) -}}
{{- end -}}
{{- add (mul (atoi (regexReplaceAll $pattern $text "${1}")) 60) (atoi (regexReplaceAll $pattern $text "${2}")) -}}
{{- end -}}

{{- /*
certificateLifetime: the duration and renewBefore of every Certificate, as JSON;
takes the root. certificate.duration is 2160h and certificate.renewBefore is
empty (left out of the Certificate) in values.yaml, which says what they mean.
Refused, with the value's name in the message:

  a duration above 2160h   the issuer's policy (maxDuration of meridian-services
                           in infra/kind/manifests/certificate-policy.yaml)
                           denies a longer request, and the Certificate would
                           never be issued. The cap is a literal here, not a
                           value: tests/meridian/test_helm_certificate_lifetime.py
                           reads the policy and fails when the two differ
  a duration below 1h      cert-manager's shortest lifetime
  a renewBefore below 5m   cert-manager's webhook refuses it ("renewBefore must
                           be greater than 5m0s"; 5m itself was accepted when
                           the cluster was asked by a server-side dry run)
  a renewBefore that is    cert-manager would renew a certificate as soon as it
  not shorter than the     is issued, for ever
  duration
*/ -}}
{{- define "meridian.certificateLifetime" -}}
{{- $certificate := .Values.certificate | default dict -}}
{{- $duration := $certificate.duration | default "" -}}
{{- $renewBefore := $certificate.renewBefore | default "" -}}
{{- $capMinutes := 129600 -}}
{{- $minutes := include "meridian.minutes" (dict "name" "certificate.duration" "value" $duration) | int -}}
{{- if gt $minutes $capMinutes -}}
{{- fail (printf "certificate.duration is %s, above %dh, the most the issuer's policy signs (maxDuration in infra/kind/manifests/certificate-policy.yaml): a longer request is denied and the Certificate is never issued" $duration (div $capMinutes 60)) -}}
{{- end -}}
{{- if lt $minutes 60 -}}
{{- fail (printf "certificate.duration is %s, below 1h, the shortest lifetime cert-manager accepts" $duration) -}}
{{- end -}}
{{- if $renewBefore -}}
{{- $before := include "meridian.minutes" (dict "name" "certificate.renewBefore" "value" $renewBefore) | int -}}
{{- if lt $before 5 -}}
{{- fail (printf "certificate.renewBefore is %s, below 5m, the shortest renewBefore cert-manager's webhook accepts (it refused 1m and 4m and accepted 5m, 2026-10-06)" $renewBefore) -}}
{{- end -}}
{{- if ge $before $minutes -}}
{{- fail (printf "certificate.renewBefore is %s, which is not shorter than certificate.duration (%s): cert-manager would renew a certificate as soon as it is issued, for ever" $renewBefore $duration) -}}
{{- end -}}
{{- end -}}
{{- dict "duration" (toString $duration) "renewBefore" (toString $renewBefore) | toJson -}}
{{- end -}}

{{- /*
certificate: one workload's Certificate, after a `---`; takes root, identity (the
output of meridian.identity, as a dict), name (the workload's, also its
ServiceAccount's) and server (it serves TLS: it also gets a DNS name and the
server usage). templates/certificates.yaml says what each field is for. The
duration comes from certificate.duration (90 days, 2160h, by default) and is
always said explicitly: the policy that lets the issuer sign
(infra/kind/manifests/certificate-policy.yaml) caps it at 2160h and
approver-policy (v0.28.0) cannot evaluate a request that names none while a cap
is set: it panics, the request is tried again for ever and is neither approved
nor denied, so a Certificate without one would never be issued.
meridian.certificateLifetime checks both values. renewBefore is left out unless
certificate.renewBefore is set: cert-manager's default, a third of the lifetime
(30 days), stays. The key is new at every
renewal by an explicit rotationPolicy: Always, not by cert-manager's default,
which was Never before v1.18.0 (the CA's key, kept by Never, is in
infra/kind/manifests/service-ca.yaml).
*/ -}}
{{- define "meridian.certificate" -}}
{{- $lifetime := include "meridian.certificateLifetime" .root | fromJson -}}
---
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: {{ .name }}
  namespace: {{ .root.Release.Namespace }}
  labels:
    {{- include "meridian.labels" .name | nindent 4 }}
spec:
  secretName: {{ .name }}-tls
  duration: {{ $lifetime.duration }}
  {{- with $lifetime.renewBefore }}
  renewBefore: {{ . }}
  {{- end }}
  privateKey:
    algorithm: ECDSA
    size: 256
    rotationPolicy: Always
  usages:
    - digital signature
    - client auth
    {{- if .server }}
    - server auth
    {{- end }}
  uris:
    - spiffe://{{ .identity.trustDomain }}/ns/{{ .root.Release.Namespace }}/sa/{{ .name }}
  {{- if .server }}
  dnsNames:
    - {{ .name }}.{{ .root.Release.Namespace }}.svc
  {{- end }}
  issuerRef:
    name: {{ .identity.issuerName }}
    kind: {{ .identity.issuerKind }}
    group: cert-manager.io
{{- end -}}

{{- /*
probe: a probe of the values, with scheme HTTPS when the service serves TLS; takes
probe (the values') and tls.
*/ -}}
{{- define "meridian.probe" -}}
{{- $probe := deepCopy .probe -}}
{{- if .tls -}}
{{- $_ := set $probe.httpGet "scheme" "HTTPS" -}}
{{- end -}}
{{- toYaml $probe -}}
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
list: the services it calls), collector (true: it sets the collector's address,
so it may reach the collector) and rateStore (true: it is given the rate store's
address, so it may reach the store; a caller that does not pass it gets none).
Every workload gets DNS and the database; nothing else unless its environment
names it.
*/ -}}
{{- define "meridian.egress" -}}
{{- $callees := include "meridian.callees" .env | fromJsonArray -}}
{{- include "meridian.peerRule" (dict "root" .root "key" "dns") }}
{{ include "meridian.peerRule" (dict "root" .root "key" "database") }}
{{- if .collector }}
{{ include "meridian.peerRule" (dict "root" .root "key" "collector") }}
{{- end }}
{{- if .rateStore }}
{{ include "meridian.rateStoreRule" (dict "direction" "to" "name" "rate-store") }}
{{- end }}
{{- if $callees }}
{{ include "meridian.serviceRule" (dict "root" .root "direction" "to" "names" $callees) }}
{{- end }}
{{- end -}}

{{- /*
job: a Job, its ServiceAccount and its NetworkPolicy, each Job its own account
and policy; takes root, name (migrate, seed or ingest), job (its values), databaseUrl
(the variable its command reads) and secret (the Secret of the database role that
variable holds: the owner's for the migration alone, the seed's and the
ingestion's own roles for the other two, S063). A Job that calls a service mounts the Secret of its Certificate (certificates.yaml,
rendered in the release, which holds no Job: the Secret exists before the Job). The
Job's name ends in the tag, or in the digest's first twelve digits; the policy's
does not, so a later deploy replaces it instead of adding one. It lives here,
not in the release, because deploy.sh applies it with the Job: the policy is
never one deploy behind its Job.
*/ -}}
{{- define "meridian.job" -}}
{{- $root := .root -}}
{{- $job := .job -}}
{{- $app := printf "meridian-%s" .name -}}
{{- $hasIdentity := include "meridian.callees" $job.env | fromJsonArray -}}
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
            {{- include "meridian.secretEnv" (dict "name" .databaseUrl "secret" .secret) | nindent 12 }}
            {{- range $job.env }}
            {{- include "meridian.envItem" (dict "root" $root "item" .) | nindent 12 }}
            {{- end }}
            {{- with include "meridian.tlsEnv" (dict "root" $root "mounts" $hasIdentity "serves" false) | trim }}
            {{- . | nindent 12 }}
            {{- end }}
          resources:
            {{- toYaml $job.resources | nindent 12 }}
          securityContext:
            {{- include "meridian.containerSecurityContext" $root | nindent 12 }}
          volumeMounts:
            {{- include "meridian.caMount" . | nindent 12 }}
            {{- include "meridian.tmpMount" . | nindent 12 }}
            {{- if $hasIdentity }}
            {{- include "meridian.tlsMount" $root | nindent 12 }}
            {{- end }}
      volumes:
        {{- include "meridian.caVolume" $root | nindent 8 }}
        {{- include "meridian.tmpVolume" $root | nindent 8 }}
        {{- if $hasIdentity }}
        {{- include "meridian.tlsVolume" $app | nindent 8 }}
        {{- end }}
{{- end -}}
