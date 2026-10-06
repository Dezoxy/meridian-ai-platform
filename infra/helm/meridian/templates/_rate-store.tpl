{{- /*
The rate store's helpers (S066, T-45), moved here from _helpers.tpl, which was
at 793 lines, with no change to what the chart renders. Helm's templates share
one namespace, so a define here is used from _helpers.tpl (meridian.egress),
services.yaml, networkpolicy.yaml and rate-store.yaml. The store's own objects
are in rate-store.yaml, with its configuration and its probes.
*/ -}}

{{- /* rateStorePort: the port the rate store serves TLS on, the one the gateway's address carries by default. A literal. */ -}}
{{- define "meridian.rateStorePort" -}}
6379
{{- end -}}

{{- /*
rateStoreRule: a rule to or from the pods named by app.kubernetes.io/name, on the
rate store's port; takes direction (to or from) and name.
*/ -}}
{{- define "meridian.rateStoreRule" -}}
- {{ .direction }}:
    - podSelector:
        matchLabels:
          app.kubernetes.io/name: {{ .name }}
  ports:
    - port: {{ include "meridian.rateStorePort" . }}
      protocol: TCP
{{- end -}}

{{- /*
usesRateStore: "true" for the workload that is given the rate store's address and
so may reach the store, empty for every other; takes root, name. Only the Model
Gateway, and only when rateStore.enabled. The variable and the egress rule both
ask this, so the address a pod is given and the connection it is allowed cannot
disagree.
*/ -}}
{{- define "meridian.usesRateStore" -}}
{{- if and .root.Values.rateStore.enabled (eq .name "model-gateway") -}}true{{- end -}}
{{- end -}}

{{- /*
rateStore: the rate store's values, checked, as JSON (image, secret, maxmemory);
takes the root. Called only when rateStore.enabled. Refused, with the value's
name in the message:

  no image, or one that is not    a tag can be moved: the reference must end in
  <name>:<tag>@sha256:<64 hex>    @sha256: and 64 lower-case hex digits, and its
  (or is tagged latest)           tag is not latest
  no Secret, or a name that is    the gateway's pod and the store's pod would not
  not a Secret's                  start
  a memory limit that is not in   the chart compares it with maxmemory
  Ki, Mi or Gi
  a maxmemory that is not in      Redis reads kb, mb and gb as powers of 1024
  kb, mb or gb, or is not
  below the memory limit          the limit is the store's real bound, and a
                                  maxmemory above it would be dead text
*/ -}}
{{- define "meridian.rateStore" -}}
{{- $store := .Values.rateStore | default dict -}}
{{- $image := toString (required "rateStore.image is required while rateStore.enabled is true: the official Redis image as <name>:<tag>@sha256:<64 hex digits> (kind's is in infra/kind/values/meridian.yaml)" $store.image) -}}
{{- if not (regexMatch "^[a-z0-9][A-Za-z0-9._/:-]*@sha256:[0-9a-f]{64}$" $image) -}}
{{- fail (printf "rateStore.image must be <name>:<tag>@sha256:<64 lower-case hex digits>, with the digest: a tag can be moved; got %q" $image) -}}
{{- end -}}
{{- if hasSuffix ":latest" (index (splitList "@" $image) 0) -}}
{{- fail (printf "rateStore.image must not be tagged latest, a tag that names a different image each time it is built; got %q" $image) -}}
{{- end -}}
{{- $secret := toString (required "rateStore.secret is required while rateStore.enabled is true: the name of the Secret that holds the key uri (the gateway's address) and the key users.acl (Redis's ACL file)" $store.secret) -}}
{{- if or (gt (len $secret) 253) (not (regexMatch "^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$" $secret)) -}}
{{- fail (printf "rateStore.secret is %q, which is not a Secret's name (lower-case letters, digits, - and ., at most 253 characters)" $secret) -}}
{{- end -}}
{{- $limit := toString (($store.resources | default dict).limits | default dict).memory -}}
{{- if not (regexMatch "^[0-9]{1,9}(Ki|Mi|Gi)$" $limit) -}}
{{- fail (printf "rateStore.resources.limits.memory is %q, which is not a whole number of Ki, Mi or Gi: the chart compares it with rateStore.maxmemory" $limit) -}}
{{- end -}}
{{- $maxmemory := toString $store.maxmemory -}}
{{- if not (regexMatch "^[0-9]{1,9}(kb|mb|gb)$" $maxmemory) -}}
{{- fail (printf "rateStore.maxmemory is %q, which is not a whole number of kb, mb or gb (Redis's units: 1mb is 1024*1024 bytes)" $maxmemory) -}}
{{- end -}}
{{- $units := dict "Ki" 1024 "Mi" 1048576 "Gi" 1073741824 "kb" 1024 "mb" 1048576 "gb" 1073741824 -}}
{{- $limitBytes := mul (atoi (regexReplaceAll "[A-Za-z]+$" $limit "")) (get $units (regexReplaceAll "^[0-9]+" $limit "")) -}}
{{- $maxBytes := mul (atoi (regexReplaceAll "[A-Za-z]+$" $maxmemory "")) (get $units (regexReplaceAll "^[0-9]+" $maxmemory "")) -}}
{{- if eq $maxBytes 0 -}}
{{- fail (printf "rateStore.maxmemory is %s: Redis reads 0 as no ceiling at all" $maxmemory) -}}
{{- end -}}
{{- if ge $maxBytes $limitBytes -}}
{{- fail (printf "rateStore.maxmemory is %s, which is not below the memory limit rateStore.resources.limits.memory (%s): the limit is the store's real bound (the gateway's script writes past maxmemory), so the server must stay under it" $maxmemory $limit) -}}
{{- end -}}
{{- dict "image" $image "secret" $secret "maxmemory" $maxmemory | toJson -}}
{{- end -}}
