"""What the four policy headers say, after the infrastructure review (S072, F2).

The review of the cluster batch found sentences the files did not support: a
mechanism named wrongly, a precedent stretched, a quotation recalled and not
read, evidence that was not the evidence. Each is pinned here in its corrected
form, with the words that would bring the old claim back asserted absent, and
the cold run's list is held in the file where each check belongs. Nothing here
needs a cluster; what the headers say is unseen is still unseen.
"""

from kindsupport import KIND_DIR
from test_kind_namespace_policies import header_of

MANIFESTS = KIND_DIR / "manifests"
CERT_MANAGER = header_of(MANIFESTS / "cert-manager-networkpolicy.yaml")
OBSERVABILITY = header_of(MANIFESTS / "observability-networkpolicy.yaml")
OPERATOR = header_of(MANIFESTS / "cnpg-operator-networkpolicy.yaml")
EDGE = header_of(MANIFESTS / "envoy-gateway-networkpolicy.yaml")
README = " ".join((KIND_DIR / "README.md").read_text("utf-8").split())

# The Kubernetes page, as the main session read it on 2026-10-07.
KUBERNETES_QUOTE = (
    '"(exception: traffic to and from the node where a Pod is running is '
    'always allowed, regardless of the IP address of the Pod or the node)"'
)
BOTH_ADDRESSES = "the node's published address"


def test_loki_s_compactor_connection_is_named_for_what_it_is() -> None:
    assert "a Service's cluster address" in OBSERVABILITY
    assert "leaves the pod's network namespace and comes back to it" in OBSERVABILITY
    assert "whether the plugin filters it is not known" in OBSERVABILITY
    assert "No ingress rule admits 9095, and none has since S063" in OBSERVABILITY
    assert "while Loki's queries worked" in OBSERVABILITY
    assert "Loki's log for a compactor or a delete-request error" in OBSERVABILITY
    # The mechanism that was wrong: the connection is not a pod's own.
    assert "Both are a pod's connection to itself" not in OBSERVABILITY
    assert "its own Service (port 9095)" not in OBSERVABILITY
    # Tempo's modules do dial the pod's own address.
    assert "Tempo's modules call each other at the pod's own address" in OBSERVABILITY


def test_the_two_webhooks_that_fail_closed_are_in_two_pods() -> None:
    assert (
        "the two webhooks that fail closed, cert-manager's and "
        "approver-policy's (two pods)"
    ) in OBSERVABILITY
    assert "the two webhooks of cert-manager, which fail closed" not in OBSERVABILITY


def test_the_extra_connection_to_the_kubelet_is_known_and_not_known_in_its_parts() -> (
    None
):
    assert "Three workloads besides Prometheus are selected" in OBSERVABILITY
    assert "five pods" in OBSERVABILITY
    assert "refused without a rule" in OBSERVABILITY
    assert "smoke's network-policy check prints that line on every run: seen" in (
        OBSERVABILITY
    )
    assert "it has NOT been tried" in OBSERVABILITY
    assert "from Tempo's pod" in OBSERVABILITY
    assert "not an access" not in OBSERVABILITY


def test_the_kubernetes_page_is_quoted_with_place_and_date_beside_the_run() -> None:
    assert KUBERNETES_QUOTE in CERT_MANAGER
    assert "read 2026-10-07" in CERT_MANAGER
    assert "kubernetes.io/docs/concepts/services-networking/network-policies/" in (
        CERT_MANAGER
    )
    assert "ingress from the node passed a default-deny policy" in CERT_MANAGER
    assert "egress to the API server's Service address is refused without a rule" in (
        CERT_MANAGER
    )
    assert 'the page\'s "to" half does not hold for that path' in CERT_MANAGER
    assert "the egress rules below are not decorative" in CERT_MANAGER
    assert "always allowed). The kind cluster" not in CERT_MANAGER


def test_the_listener_rule_does_not_argue_in_a_circle() -> None:
    for text in (EDGE, README):
        assert "can reach through the proxy's Service anyway" not in text
        assert "admits every source, the pods of the cluster included" in text
    assert "a narrower rule would need an address range" in EDGE
    assert "forbids" in EDGE
    assert "a narrower one would need an address range" in README
    assert "test forbids" in README


def test_the_evidence_that_the_operator_needs_no_sql_port_is_s019() -> None:
    assert "has never admitted it on 5432" in OPERATOR
    assert "S019" in OPERATOR
    assert "pods and `pods/exec` are in its Role" not in OPERATOR
    assert "the operator's EGRESS policy is new in this change" in OPERATOR
    assert "that nothing else is needed is unproved until a cold run" in OPERATOR


def test_the_two_unbound_cluster_roles_of_the_chart_are_named() -> None:
    for text in (OPERATOR, README):
        assert "`cnpg-cloudnative-pg-view`" in text
        assert "`cnpg-cloudnative-pg-edit`" in text
        assert "nothing binds or aggregates" in text


def test_every_fall_back_for_a_webhook_names_both_addresses() -> None:
    for name, text in (
        ("cert-manager", CERT_MANAGER),
        ("observability", OBSERVABILITY),
        ("operator", OPERATOR),
        ("edge", EDGE),
        ("README", README),
    ):
        assert BOTH_ADDRESSES in text, name
        assert "the node's pod-network address" in text, name
        assert "could be either" in text or "could carry either" in text, name


def test_the_cold_runs_list_has_the_reviews_four_checks_where_they_belong() -> None:
    # The order in which a wrong webhook premise would show.
    for text in (CERT_MANAGER, OBSERVABILITY, README):
        assert "approver-policy's apply" in text
    assert "cert-manager's release" in CERT_MANAGER
    assert "the database's install" in CERT_MANAGER
    assert "silently, the Prometheus operator's target" in CERT_MANAGER
    assert "(cert-manager's header gives the order)" in OPERATOR
    assert "(cert-manager's header gives the order)" in EDGE
    # The probe from Tempo's pod to the kubelet's port, and Loki's log.
    assert "one connection from Tempo's pod to the node's kubelet port 10250" in (
        OBSERVABILITY
    )
    assert "which must hold no compactor or delete-request error" in OBSERVABILITY
    # The proxy Service's traffic policy.
    assert "the proxy Service's `externalTrafficPolicy`" in OBSERVABILITY
    assert "read the proxy Service's `externalTrafficPolicy`" in EDGE
    assert "(expected `Local`)" in EDGE


def test_up_refuses_the_old_layout_says_the_readme() -> None:
    assert "`make up` refuses such a cluster, before it changes anything" in README
