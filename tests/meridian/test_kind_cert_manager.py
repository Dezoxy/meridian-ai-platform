"""cert-manager's pin and the service CA, read from the files that install them.

(S055.)
"""

import re

import yaml
from kindsupport import (
    KIND_DIR,
    UP_SH,
    load_documents,
)

SERVICE_CA_FILE = KIND_DIR / "manifests" / "service-ca.yaml"
CERT_MANAGER_VALUES = KIND_DIR / "values" / "cert-manager.yaml"
SELF_SIGNED_ISSUER = "meridian-selfsigned"
SERVICES_ISSUER = "meridian-services"
SERVICES_CA = "meridian-services-ca"


def pins() -> dict[str, str]:
    """The KEY=value lines of pins.env (never printed, only compared)."""
    found = {}
    for line in (KIND_DIR / "pins.env").read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            found[key] = value
    return found


def service_ca_objects() -> dict[tuple[str, str], dict]:
    return {
        (d["kind"], d["metadata"]["name"]): d for d in load_documents(SERVICE_CA_FILE)
    }


def joined_script_lines() -> list[str]:
    """up.sh with each backslash continuation folded into one line."""
    return re.sub(r"\\\n\s*", "", UP_SH).splitlines()


def test_the_cert_manager_chart_is_pinned_with_a_helm_reader_comment() -> None:
    values = pins()
    lines = (KIND_DIR / "pins.env").read_text(encoding="utf-8").splitlines()
    (version_at,) = [
        i for i, line in enumerate(lines) if line.startswith("CERT_MANAGER_VERSION=")
    ]

    assert values["CERT_MANAGER_CHART"] == "cert-manager"
    assert values["CERT_MANAGER_REPO"].startswith("https://")
    assert re.fullmatch(r"v\d+\.\d+\.\d+", values["CERT_MANAGER_VERSION"])
    assert lines[version_at - 1] == (
        "# renovate: datasource=helm depName=cert-manager"
        f" registryUrl={values['CERT_MANAGER_REPO']}"
    )


def test_up_installs_cert_manager_from_its_pin_into_its_own_namespace() -> None:
    (installed,) = [
        line
        for line in joined_script_lines()
        if line.startswith("install_release cert-manager ")
    ]

    assert installed.startswith(
        "install_release cert-manager cert-manager"
        ' "${CERT_MANAGER_CHART}" "${CERT_MANAGER_VERSION}"'
        ' "${CERT_MANAGER_REPO}" cert-manager.yaml --set '
    )
    # The four components' images by digest, and nothing else is set (S063).
    assert re.findall(r'--set "([A-Za-z.]+)=', installed) == [
        f"{component}image.{leaf}"
        for component in ("", "webhook.", "cainjector.", "startupapicheck.")
        for leaf in ("tag", "digest")
    ]


def test_up_installs_the_issuer_before_the_database_and_waits_for_it() -> None:
    lines = joined_script_lines()
    (release,) = [
        i
        for i, line in enumerate(lines)
        if line.startswith("install_release cert-manager ")
    ]
    (applied,) = [
        i for i, line in enumerate(lines) if "manifests/service-ca.yaml" in line
    ]
    (waited,) = [
        i for i, line in enumerate(lines) if "clusterissuer/meridian-services" in line
    ]
    (operator,) = [
        i for i, line in enumerate(lines) if line.startswith("install_release cnpg ")
    ]
    (namespaces,) = [
        i for i, line in enumerate(lines) if "manifests/namespaces.yaml" in line
    ]

    assert namespaces < release < applied < waited < operator
    assert lines[applied].startswith("kctl apply --server-side --force-conflicts -f ")
    assert "--for=condition=Ready" in lines[waited]
    assert "--timeout=" in lines[waited]
    assert lines[waited].startswith("kctl wait ")


def test_the_service_ca_is_a_self_signed_issuer_a_ca_and_an_issuer_on_it() -> None:
    objects = service_ca_objects()

    assert set(objects) == {
        ("ClusterIssuer", SELF_SIGNED_ISSUER),
        ("Certificate", SERVICES_CA),
        ("ClusterIssuer", SERVICES_ISSUER),
    }
    assert len(load_documents(SERVICE_CA_FILE)) == 3
    assert objects[("ClusterIssuer", SELF_SIGNED_ISSUER)]["spec"] == {"selfSigned": {}}
    assert objects[("ClusterIssuer", SERVICES_ISSUER)]["spec"] == {
        "ca": {"secretName": SERVICES_CA}
    }
    for document in objects.values():
        assert document["apiVersion"] == "cert-manager.io/v1"


def test_the_service_ca_certificate_is_an_ecdsa_ca_for_a_year_in_cert_manager() -> None:
    spec = service_ca_objects()[("Certificate", SERVICES_CA)]["spec"]
    metadata = service_ca_objects()[("Certificate", SERVICES_CA)]["metadata"]

    assert metadata["namespace"] == "cert-manager"
    assert spec["isCA"] is True
    assert spec["secretName"] == SERVICES_CA
    assert spec["privateKey"]["algorithm"] == "ECDSA"
    assert spec["privateKey"]["size"] == 256
    assert spec["issuerRef"] == {
        "name": SELF_SIGNED_ISSUER,
        "kind": "ClusterIssuer",
        "group": "cert-manager.io",
    }
    assert spec["duration"] == "8760h"
    assert spec["commonName"] == SERVICES_CA
    assert "meridian" not in {
        d["metadata"].get("namespace") for d in load_documents(SERVICE_CA_FILE)
    }


def test_the_service_ca_keeps_its_key_at_renewal_by_an_explicit_setting() -> None:
    # cert-manager's default is Always since v1.18.0 (Never before): with it the
    # CA would get a new key at renewal, and a pod restarted after it would no
    # longer trust the certificates of the pods that had not.
    spec = service_ca_objects()[("Certificate", SERVICES_CA)]["spec"]

    assert spec["privateKey"].get("rotationPolicy") == "Never"


def test_the_service_ca_file_says_its_private_key_stays_outside_meridian() -> None:
    header = SERVICE_CA_FILE.read_text(encoding="utf-8").split("apiVersion:")[0]

    assert "private key" in header
    assert "`cert-manager` namespace" in header
    assert "`meridian`" in header


def test_the_service_ca_file_says_the_key_is_kept_by_the_setting_not_a_default() -> (
    None
):
    header = SERVICE_CA_FILE.read_text(encoding="utf-8").split("apiVersion:")[0]
    flat = " ".join(line.removeprefix("#").strip() for line in header.splitlines())

    assert "rotationPolicy: Never" in flat
    assert "v1.18.0" in flat
    assert "cert-manager's default)" not in flat
    assert "kept at renewal, cert-manager's default" not in flat


def test_the_service_ca_file_says_who_can_read_the_key_and_who_can_ask_for_a_cert() -> (
    None
):
    header = SERVICE_CA_FILE.read_text(encoding="utf-8").split("apiVersion:")[0]
    flat = " ".join(line.removeprefix("#").strip() for line in header.splitlines())

    # The readers of the key: operators with a cluster-wide read of Secrets.
    for reader in ("cainjector", "CloudNativePG"):
        assert reader in flat
    # S056: approver-policy decides, so the issuer no longer signs a request
    # from any namespace; the file says what is left.
    assert "approver-policy decides" in flat
    assert "certificate-policy.yaml" in flat
    assert "nothing restricts who may ask" not in flat
    assert "whoever can create a Certificate in `meridian`" in flat


def test_the_readme_says_who_reads_the_ca_key_and_who_can_ask_for_a_certificate() -> (
    None
):
    readme = " ".join((KIND_DIR / "README.md").read_text(encoding="utf-8").split())

    assert "no Meridian pod can read it" in readme
    for reader in ("cainjector", "CloudNativePG"):
        assert reader in readme
    # S056: approver-policy decides; the built-in approver no longer does.
    assert "approver-policy" in readme
    assert "disableAutoApproval" in readme
    assert "nothing restricts who may ask" not in readme
    assert "a policy on requests is for AKS" not in readme
    assert "rotationPolicy: Never" in readme
    # What is left is said in the README: whoever can create a `Certificate` in
    # `meridian` has any service's identity issued.
    assert "whoever can create a `Certificate` in `meridian`" in readme


def test_the_readme_describes_what_s056_added_to_deploy_and_smoke_and_the_restart() -> (
    None
):
    readme = " ".join((KIND_DIR / "README.md").read_text(encoding="utf-8").split())

    # `make smoke` checks twelve things; the tenth is the certificate policy, the
    # eleventh the alert rules (test_smoke_alert_rules.py) and, since S072 (contract
    # M3b), the twelfth the telemetry stores.
    assert "`make smoke` checks twelve things" in readme
    assert "`make smoke` checks nine things" not in readme
    assert "**Certificate policy.** Five lines" in readme
    # The ninth check's description no longer counts three statuses.
    assert "`make smoke`'s ninth check proves 200, 401 and 403" not in readme
    # `make deploy` refuses without the policies and the add-on, too.
    assert "CertificateRequestPolicy" in readme
    assert "cert-manager-approver-policy" in readme
    assert "nothing would approve the chart's Certificates" in readme
    # The deny policy selects a request for either issuer, not every request.
    assert "selects every request and allows nothing" not in readme
    assert "meets no policy and is never approved" in readme
    # What is true about a renewed certificate and the restart.
    assert "a Deployment's pods are not restarted by cert-manager" not in readme
    assert "/healthz" in readme
    assert "503" in readme
    assert "the kubelet restarts the container" in readme
    assert "21 days" in readme


def test_the_cert_manager_values_install_the_crds_and_turn_nothing_optional_on() -> (
    None
):
    values = yaml.safe_load(CERT_MANAGER_VALUES.read_text(encoding="utf-8"))

    assert values["crds"]["enabled"] is True
    for component in (values, values["webhook"], values["cainjector"]):
        assert "cpu" in component["resources"]["requests"]
        assert "memory" in component["resources"]["requests"]
        assert "memory" in component["resources"]["limits"]
    assert not values.get("prometheus", {}).get("servicemonitor", {}).get("enabled")
    assert not values.get("replicaCount", 1) > 1
