# shellcheck shell=bash
# The functions and names `make up` uses for Prometheus's gateway (S072, contract
# M4 and M4b), cut out of up.sh so that it stays under the size ceiling. up.sh
# sources this file right after common.sh and calls apply_prometheus_gateway where
# it always did; nothing here runs when the file is sourced. It is not run on its
# own. It uses `die`, `log`, `kctl` and `KIND_DIR` (common.sh), the
# NGINX_GATEWAY_IMAGE_* pins (pins.env) and `object_fingerprint` (up.sh), which are
# all defined by the time a function below is called. The last function, at the
# end of the file, is the edge Gateway's manifest (S021, Y2b); identity.sh sources
# this file too, for fill_placeholder.

# The policies of Prometheus's port and its gateway's, and the gateway itself (S072,
# contract M4): no placeholder for an address, applied right after the stack's
# release and not at the start of the run (the collector's and Grafana's egress rules
# towards the gateway are in the file applied at the start, which is what opens the
# window of a warm run: see release_failure_note). The gateway's file holds four
# placeholders (its image, its own digest, the authority's certificate's digest, the
# Prometheus Service's cluster address) that prometheus_gateway_manifest fills in.
readonly PROMETHEUS_POLICY_FILE="${KIND_DIR}/manifests/observability-prometheus-networkpolicy.yaml"
readonly PROMETHEUS_GATEWAY_FILE="${KIND_DIR}/manifests/observability-prometheus-gateway.yaml"
readonly PROMETHEUS_GATEWAY_IMAGE_PLACEHOLDER=IMAGE-PLACEHOLDER
readonly PROMETHEUS_GATEWAY_MANIFEST_PLACEHOLDER=MANIFEST-SHA256-PLACEHOLDER
readonly PROMETHEUS_GATEWAY_CA_PLACEHOLDER=CA-SHA256-PLACEHOLDER
readonly PROMETHEUS_GATEWAY_ADDRESS_PLACEHOLDER=SERVICE-ADDRESS-PLACEHOLDER

# fill_placeholder TEXT PLACEHOLDER VALUE: TEXT with VALUE in place of PLACEHOLDER,
# on stdout. Stops when TEXT does not hold PLACEHOLDER exactly once: a manifest that
# lost one would be applied with the placeholder as it stands, and one that holds it
# twice would be half filled. Cut and joined with bash's own expansions, not sed or
# a pattern, so nothing in VALUE can be read as an expression.
fill_placeholder() {
  local text=$1 placeholder=$2 value=$3 before after
  [[ "${text}" == *"${placeholder}"* ]] ||
    die "the gateway's manifest does not hold the placeholder ${placeholder}"
  before="${text%%"${placeholder}"*}"
  after="${text#*"${placeholder}"}"
  [[ "${after}" != *"${placeholder}"* ]] ||
    die "the gateway's manifest holds the placeholder ${placeholder} more than once"
  printf '%s%s%s' "${before}" "${value}" "${after}"
}

# prometheus_service_address: the cluster address of the stack's Prometheus Service,
# on stdout (S072, contract M4b). nginx resolves the Service's name once, when it
# starts, so a Service that was made again (a stack uninstalled and installed in
# place) has an address the running gateway does not know. The address is a pod
# annotation: a `make up` after that changes the pod template and the pod is
# rolled. Stops on an empty answer (a headless Service has none: "None" is not an
# address either).
prometheus_service_address() {
  local address
  address="$(kctl -n observability get service kube-prometheus-stack-prometheus \
    -o 'jsonpath={.spec.clusterIP}')" ||
    die "could not read the cluster address of the Service kube-prometheus-stack-prometheus in observability (kubectl -n observability get service)"
  [[ "${address}" =~ ^[0-9a-fA-F.:]+$ ]] ||
    die "the Service kube-prometheus-stack-prometheus in observability has no cluster address (read: '${address}'); the gateway's upstream is that Service"
  printf '%s' "${address}"
}

# prometheus_gateway_manifest FILE IMAGE CA_SHA ADDRESS: the gateway's manifest FILE
# with its four placeholders filled in, on stdout (S072, contracts M4 and M4b):
# IMAGE is the whole reference of the image (name:tag@digest), CA_SHA the fingerprint
# of the authority's certificate, ADDRESS the Prometheus Service's cluster address,
# and the SHA-256 of FILE as it stands, placeholders and all, goes where the file's
# own digest is wanted. The last three are pod annotations: a changed configuration
# (it is in the file), a renewed authority or a Service made again changes the pod
# template, and the pod is rolled; nginx reads neither the client CA, its
# configuration nor the upstream's address again by itself. Stops when a placeholder
# word is still in the text after the fills: a placeholder added to the file and not
# filled here would be applied as it stands.
prometheus_gateway_manifest() {
  local file=$1 image=$2 ca_sha=$3 address=$4 manifest file_sha
  file_sha="$(sha256sum "${file}" | cut -d' ' -f1)"
  manifest="$(<"${file}")"
  manifest="$(fill_placeholder "${manifest}" "${PROMETHEUS_GATEWAY_IMAGE_PLACEHOLDER}" "${image}")" || exit 1
  manifest="$(fill_placeholder "${manifest}" "${PROMETHEUS_GATEWAY_MANIFEST_PLACEHOLDER}" "${file_sha}")" || exit 1
  manifest="$(fill_placeholder "${manifest}" "${PROMETHEUS_GATEWAY_CA_PLACEHOLDER}" "${ca_sha}")" || exit 1
  manifest="$(fill_placeholder "${manifest}" "${PROMETHEUS_GATEWAY_ADDRESS_PLACEHOLDER}" "${address}")" || exit 1
  [[ "${manifest}" != *PLACEHOLDER* ]] ||
    die "the gateway's manifest still holds a placeholder word after the fills: a placeholder that prometheus_gateway_manifest does not fill"
  printf '%s\n' "${manifest}"
}

# apply_prometheus_gateway: the policies of Prometheus's port and of the gateway,
# then the gateway (S072, contract M4), and wait until ITS ROLLOUT is done. Called
# right after the stack's release, which points Grafana's Prometheus datasource at
# the gateway: Grafana reads Prometheus again when this returns. The policies come
# first, so Prometheus's port is closed to Grafana and the collector only when the
# gateway that replaces them is about to exist, and the manifest is built (and
# checked) BEFORE the policies are applied, so a manifest that lost a placeholder
# stops the run with nothing closed. The window itself opened earlier: the
# collector's and Grafana's egress rules towards the gateway are in the file applied
# at the start of the run, so on a warm cluster Grafana has been unable to read
# Prometheus (its datasource, until the stack's release, still named Prometheus's
# own port) and the old collector's metrics have timed out since that apply, not
# since the stack's release; release_failure_note, set right after that apply, says
# so. If this run stops before the collector's release the window stays open until a
# re-run of `make up` converges.
# The wait is `rollout status`, not `wait --for=condition=Available`: with one
# replica, one surge and none unavailable, the Deployment stays Available while the
# OLD pod serves, so that condition returns at once on a warm cluster even when the
# new pod (a changed configuration, a renewed authority) crash-loops. `rollout
# status` returns when the new ReplicaSet's pod is Ready and the old one is gone.
apply_prometheus_gateway() {
  local ca_sha image address manifest
  ca_sha="$(object_fingerprint secret prometheus-gateway-tls 'ca\.crt')"
  image="${NGINX_GATEWAY_IMAGE_REPOSITORY}:${NGINX_GATEWAY_IMAGE_TAG}@${NGINX_GATEWAY_IMAGE_DIGEST}"
  address="$(prometheus_service_address)" || exit 1
  manifest="$(prometheus_gateway_manifest "${PROMETHEUS_GATEWAY_FILE}" "${image}" "${ca_sha}" "${address}")" || exit 1
  log "observability: Prometheus's and its gateway's NetworkPolicies"
  kctl apply --server-side --force-conflicts -f "${PROMETHEUS_POLICY_FILE}" >/dev/null
  log "observability: Prometheus's gateway (nginx, TLS 1.3, a client certificate for the OTLP receiver's path)"
  kctl apply --server-side --force-conflicts -f - <<<"${manifest}" >/dev/null
  kctl -n observability rollout status deployment/prometheus-gateway \
    --timeout=5m >/dev/null ||
    die "Prometheus's gateway (deployment/prometheus-gateway in observability) did not finish rolling out in 5m (its new pod was not Ready): Grafana reads no Prometheus and the collector's metrics are refused and dropped until it is, and on a warm cluster the old pod may still serve the old configuration; look at its pods (kubectl -n observability get pods -l app.kubernetes.io/name=prometheus-gateway; describe the newest) and its log, then run make up again"
}

# The edge Gateway's manifest (S021, Y2b), on stdout. With the sign-in issuer
# add-on off (MERIDIAN_IDENTITY empty) it is the committed file, byte for byte: the
# listener admits routes from the namespace `meridian` only. With it on (keycloak),
# the one change is who may attach a route to the listener: the namespaces whose
# name label is `meridian` or `identity`, by a selector on that label (the API server
# sets it and nobody can edit it) and never `All`. matchLabels cannot say "or", so
# the selector becomes matchExpressions. The text is cut and joined as
# fill_placeholder does, so a file that lost the block is refused and not applied
# as it stands. A later run with the switch off applies the file again, and the
# widening should go from the Gateway (server-side apply drops a field its manager
# no longer sends; the widening was seen on kind in run KR1, the narrowing again
# was not).
# edge_gateway_manifest [FILE]: FILE is the Gateway's manifest (the committed one by
# default; a test gives it another).
readonly EDGE_GATEWAY_FILE="${KIND_DIR}/manifests/gateway.yaml"
readonly EDGE_ROUTES_FROM_MERIDIAN=$'            matchLabels:\n              kubernetes.io/metadata.name: meridian'
readonly EDGE_ROUTES_FROM_BOTH=$'            matchExpressions:\n              - key: kubernetes.io/metadata.name\n                operator: In\n                values: [meridian, identity]'
edge_gateway_manifest() {
  local file="${1:-${EDGE_GATEWAY_FILE}}" manifest
  if ! identity_on; then
    cat "${file}"
    return
  fi
  manifest="$(<"${file}")"
  manifest="$(fill_placeholder "${manifest}" "${EDGE_ROUTES_FROM_MERIDIAN}" "${EDGE_ROUTES_FROM_BOTH}")" || exit 1
  printf '%s\n' "${manifest}"
}
