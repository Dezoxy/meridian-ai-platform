"""The images the platform charts run are pinned by digest (S063, T-91).

``infra/kind/up.sh`` hands every chart the tag and the digest of each image it
runs, read from ``infra/kind/pins.env`` as ``X_IMAGE_TAG`` and ``X_IMAGE_DIGEST``
(a full reference, ``POSTGRES_IMAGE``, where one line holds all three). No
cluster, registry or network is needed: the tests read the script, the pins, the
values files and the README. They cannot say that the list is complete (that
needs ``helm template`` of every chart; infra/kind/README.md gives the command),
nor that a digest is the multi-architecture index of its tag.

What a chart's key takes is not uniform, and a file cannot show it, so the
differences are written down here: ``digest`` takes ``sha256:<hex>``; ``sha`` in
kube-prometheus-stack and Grafana takes the hex alone, because the template
writes ``@sha256:`` itself; ``sha`` in kube-state-metrics takes the whole digest;
Tempo and CloudNativePG have no digest key and take ``tag@digest`` as the tag.
"""

import re
import shlex
from itertools import pairwise
from pathlib import Path

import pytest
import yaml
from certpolicysupport import KIND_DIR

PINS_FILE = KIND_DIR / "pins.env"
UP_SH = KIND_DIR / "up.sh"
README = KIND_DIR / "README.md"
VALUES_DIR = KIND_DIR / "values"

DIGEST = r"sha256:[0-9a-f]{64}"
BARE_DIGEST = r"[0-9a-f]{64}"
# The one `sha` key whose template prints the value as it is.
WHOLE_DIGEST_SHA_KEYS = {"kube-state-metrics.image.sha"}
FULL_REFERENCE_KEYS = ("image", "imageName")
DIGEST_KEYS = ("digest", "sha")

# Per release, the pins up.sh passes to the chart (the prefix of X_IMAGE_TAG).
IMAGES_BY_RELEASE = {
    "envoy-gateway": {"ENVOY_GATEWAY_IMAGE"},
    "cert-manager": {
        "CERT_MANAGER_CONTROLLER_IMAGE",
        "CERT_MANAGER_WEBHOOK_IMAGE",
        "CERT_MANAGER_CAINJECTOR_IMAGE",
        "CERT_MANAGER_STARTUPAPICHECK_IMAGE",
    },
    "approver-policy": {"APPROVER_POLICY_IMAGE"},
    "cnpg": {"CNPG_OPERATOR_IMAGE"},
    "platform-db": {"POSTGRES_IMAGE"},
    "kube-prometheus-stack": {
        "PROMETHEUS_OPERATOR_IMAGE",
        "PROMETHEUS_CONFIG_RELOADER_IMAGE",
        "KUBE_WEBHOOK_CERTGEN_IMAGE",
        "PROMETHEUS_IMAGE",
        "KUBE_STATE_METRICS_IMAGE",
        "GRAFANA_IMAGE",
        "GRAFANA_SIDECAR_IMAGE",
    },
    "tempo": {"TEMPO_IMAGE"},
    "loki": {"LOKI_IMAGE"},
    "otel-collector": {"OTEL_COLLECTOR_IMAGE"},
    # The contrib build of the same collector, as a DaemonSet (S064).
    "log-agent": {"LOG_AGENT_IMAGE"},
}

# Images a chart names that nothing starts here, left by tag on purpose.
LEFT_BY_TAG = (
    "quay.io/jetstack/cert-manager-acmesolver",
    "quay.io/thanos/thanos",
    "docker.io/envoyproxy/ratelimit",
    "alpine:3.17",
)


def read_pins(text: str) -> dict[str, str]:
    found = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator and re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            found[key] = value
    return found


def install_statements(text: str) -> dict[str, list[str]]:
    """Each ``install_release`` line of up.sh, by release name, as its words."""
    joined = re.sub(r"\\\n\s*", "", text)
    found = {}
    for line in joined.splitlines():
        if line.startswith("install_release "):
            words = shlex.split(line)
            found[words[1]] = words
    return found


def set_arguments(words: list[str]) -> dict[str, str]:
    found = {}
    for flag, argument in pairwise(words):
        if flag == "--set":
            key, _, value = argument.partition("=")
            found[key] = value
    return found


REFERENCE = re.compile(r"\$\{([A-Z0-9_]+)(#sha256:)?\}")


def expand(value: str, pins: dict[str, str]) -> str | None:
    """The value with each ``${NAME}`` and ``${NAME#sha256:}`` read from the
    pins; None when a name is not a pin."""
    failed = []

    def read(match: re.Match[str]) -> str:
        name, strip = match.groups()
        if name not in pins:
            failed.append(name)
            return ""
        return pins[name].removeprefix("sha256:") if strip else pins[name]

    expanded = REFERENCE.sub(read, value)
    return None if failed else expanded


def pins_passed(arguments: dict[str, str]) -> set[str]:
    """The prefixes (``X_IMAGE``) of the pins the arguments name."""
    names = re.findall(
        r"\$\{([A-Z0-9_]+?_IMAGE)(?:_(?:REPOSITORY|TAG|DIGEST))?(?:#sha256:)?\}",
        " ".join(arguments.values()),
    )
    return set(names)


def image_problems(arguments: dict[str, str], pins: dict[str, str]) -> list[str]:
    """What is wrong with the image keys of one release's ``--set`` arguments:
    a reference, tag or digest that is not by digest, or a pin that is missing."""
    problems = []
    tags: dict[str, str] = {}
    pinned: set[str] = set()
    for key, value in arguments.items():
        prefix, _, leaf = key.rpartition(".")
        if leaf not in ("tag", "repository", *FULL_REFERENCE_KEYS, *DIGEST_KEYS):
            continue
        expanded = expand(value, pins)
        if expanded is None:
            problems.append(f"{key} names something that is not in pins.env")
            continue
        if leaf in FULL_REFERENCE_KEYS and not re.fullmatch(
            rf"[^@\s]+@{DIGEST}", expanded
        ):
            problems.append(f"{key} is not a reference by digest")
        elif leaf == "digest" and not re.fullmatch(DIGEST, expanded):
            problems.append(f"{key} does not take sha256: and 64 hex")
        elif leaf == "sha":
            wanted = DIGEST if key in WHOLE_DIGEST_SHA_KEYS else BARE_DIGEST
            if not re.fullmatch(wanted, expanded):
                problems.append(f"{key} does not take {wanted}")
        if leaf in DIGEST_KEYS:
            pinned.add(prefix)
        elif leaf == "tag":
            tags[prefix] = expanded
        elif leaf == "repository":
            tags.setdefault(prefix, "")
    for prefix, tag in tags.items():
        if prefix not in pinned and not re.search(rf"@{DIGEST}$", tag):
            problems.append(f"{prefix} is pinned by tag, not by digest")
    return problems


def pin_file_problems(text: str) -> list[str]:
    """What is wrong with the image pins of a pins.env: a tag with no Renovate
    comment above it, no digest right below it, a digest that is not sha256 and
    64 hex, a repository the comment does not name, a one-line image without one."""
    lines = text.splitlines()
    problems = []
    for at, line in enumerate(lines):
        key, _, value = line.partition("=")
        if key.endswith("_IMAGE_TAG"):
            stem = key.removesuffix("_TAG")
            comment = re.fullmatch(
                r"# renovate: datasource=docker depName=(\S+)", lines[at - 1]
            )
            if not comment:
                problems.append(f"{key} has no '# renovate: datasource=docker' above")
            below = lines[at + 1] if at + 1 < len(lines) else ""
            if not re.fullmatch(rf"{stem}_DIGEST={DIGEST}", below):
                problems.append(f"{key} has no {stem}_DIGEST=sha256:<64 hex> below")
            if "@" in value:
                problems.append(f"{key} holds a digest; it belongs in {stem}_DIGEST")
            if comment and lines[at - 2].startswith(f"{stem}_REPOSITORY="):
                repository = lines[at - 2].partition("=")[2]
                if repository != comment.group(1):
                    problems.append(f"{stem}_REPOSITORY and its comment disagree")
        elif key.endswith("_IMAGE_DIGEST"):
            if not lines[at - 1].startswith(key.removesuffix("_DIGEST") + "_TAG="):
                problems.append(f"{key} is not right below its tag")
        elif key.endswith("_IMAGE") and not re.fullmatch(
            rf"[a-z0-9][a-z0-9./-]*:[\w.-]+@{DIGEST}", value
        ):
            problems.append(f"{key} is not name:tag@sha256:<64 hex>")
    return problems


def values_problems(node: object, path: str = "") -> list[str]:
    """An image a values file names that is not by digest."""
    problems = []
    if isinstance(node, dict):
        if ("repository" in node or "tag" in node) and not any(
            key in node for key in DIGEST_KEYS
        ):
            tag = str(node.get("tag", ""))
            if not re.search(rf"@{DIGEST}$", tag):
                problems.append(f"{path} names an image without a digest")
        for key, child in node.items():
            if key in FULL_REFERENCE_KEYS and isinstance(child, str):
                if not re.fullmatch(rf"[^@\s]+@{DIGEST}", child):
                    problems.append(f"{path}.{key} is not a reference by digest")
            else:
                problems.extend(values_problems(child, f"{path}.{key}"))
    elif isinstance(node, list):
        for number, child in enumerate(node):
            problems.extend(values_problems(child, f"{path}[{number}]"))
    return problems


UP = install_statements(UP_SH.read_text(encoding="utf-8"))
PINS_TEXT = PINS_FILE.read_text(encoding="utf-8")
PINS = read_pins(PINS_TEXT)


# ── what up.sh passes ────────────────────────────────────────────────────────


def test_each_release_is_given_the_pins_of_the_images_it_runs() -> None:
    passed = {name: pins_passed(set_arguments(words)) for name, words in UP.items()}

    assert {name: found for name, found in passed.items() if found} == (
        IMAGES_BY_RELEASE
    )


def test_every_image_up_passes_to_a_chart_is_pinned_by_digest() -> None:
    problems = [
        f"{name}: {problem}"
        for name, words in UP.items()
        for problem in image_problems(set_arguments(words), PINS)
    ]

    assert problems == []


def test_kube_state_metrics_takes_the_whole_digest_and_the_other_sha_keys_the_hex() -> (
    None
):
    arguments = set_arguments(UP["kube-prometheus-stack"])
    sha_keys = {key: value for key, value in arguments.items() if key.endswith(".sha")}

    assert len(sha_keys) == 7
    assert [key for key in sha_keys if key in WHOLE_DIGEST_SHA_KEYS] == [
        "kube-state-metrics.image.sha"
    ]
    for key, value in sha_keys.items():
        whole = key in WHOLE_DIGEST_SHA_KEYS
        assert value.endswith("_DIGEST}") == whole, key
        assert value.endswith("_DIGEST#sha256:}") == (not whole), key


def test_the_pins_a_release_takes_are_the_ones_pins_env_holds() -> None:
    for name, expected in IMAGES_BY_RELEASE.items():
        for stem in expected:
            split = f"{stem}_TAG" in PINS and f"{stem}_DIGEST" in PINS
            assert split or stem in PINS, f"{name}: {stem} is not in pins.env"


def test_every_split_image_pin_is_passed_to_a_chart() -> None:
    held = {key.removesuffix("_TAG") for key in PINS if key.endswith("_IMAGE_TAG")}
    passed = set().union(*(pins_passed(set_arguments(w)) for w in UP.values()))

    assert held - passed == set()


# ── what pins.env holds ──────────────────────────────────────────────────────


def test_every_image_pin_has_its_comment_its_digest_and_a_repository_that_agrees() -> (
    None
):
    assert pin_file_problems(PINS_TEXT) == []


def test_no_two_image_pins_share_a_digest() -> None:
    digests = [value for key, value in PINS.items() if key.endswith("_IMAGE_DIGEST")]

    assert len(digests) >= 15
    assert len(set(digests)) == len(digests)


# ── what the values files name ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "path", sorted(VALUES_DIR.glob("*.yaml")), ids=lambda p: p.name
)
def test_no_values_file_names_an_image_that_is_not_by_digest(path: Path) -> None:
    values = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    assert values_problems(values) == []


# ── approver-policy ──────────────────────────────────────────────────────────


def test_approver_policy_keeps_its_limit_and_says_it_was_measured_on_kind() -> None:
    path = VALUES_DIR / "approver-policy.yaml"
    text = path.read_text(encoding="utf-8")
    values = yaml.safe_load(text)

    assert values["resources"]["limits"] == {"memory": "96Mi"}
    assert "measured on kind alone" in text
    # Not in the values file, because the chart's schema refuses the key (S073):
    # the probe is the manifest's, which up.sh applies after the install. The
    # manifest is read here so this line cannot go on passing while the probe is
    # nowhere; its port, numbers and ownership are test_kind_approver_liveness's.
    assert "livenessProbe" not in values
    manifest = (KIND_DIR / "manifests" / "approver-policy-liveness.yaml").read_text(
        encoding="utf-8"
    )
    assert len(re.findall(r"^\s+livenessProbe:", manifest, re.MULTILINE)) == 1
    assert "path: /readyz" in manifest
    assert "image" not in values


# ── the README says what is by tag, and why ──────────────────────────────────


def test_the_readme_lists_every_pinned_image_and_each_image_left_by_tag() -> None:
    readme = README.read_text(encoding="utf-8")
    lines = PINS_TEXT.splitlines()
    listed = [
        f"{above.rpartition('=')[2]}:{line.partition('=')[2]}"
        for above, line in pairwise(lines)
        if above.startswith("# renovate: datasource=docker")
        and line.partition("=")[0].endswith("_IMAGE_TAG")
    ]

    assert len(listed) >= 15
    assert [image for image in listed if image not in readme] == []
    assert [image for image in LEFT_BY_TAG if image not in readme] == []


# ── the checks themselves can fail ───────────────────────────────────────────

SYNTHETIC_DIGEST = "sha256:" + "a" * 64
SYNTHETIC_PINS = {
    "X_IMAGE_TAG": "1.0.0",
    "X_IMAGE_DIGEST": SYNTHETIC_DIGEST,
    "Y_IMAGE": f"example.org/y:1@{SYNTHETIC_DIGEST}",
}


def test_a_tag_without_a_digest_key_is_reported() -> None:
    arguments = {"image.tag": "${X_IMAGE_TAG}"}

    assert image_problems(arguments, SYNTHETIC_PINS) == [
        "image is pinned by tag, not by digest"
    ]


def test_a_tag_with_its_digest_key_is_not_reported() -> None:
    arguments = {"image.tag": "${X_IMAGE_TAG}", "image.digest": "${X_IMAGE_DIGEST}"}

    assert image_problems(arguments, SYNTHETIC_PINS) == []


def test_a_tag_that_carries_its_digest_is_not_reported() -> None:
    arguments = {"tempo.tag": "${X_IMAGE_TAG}@${X_IMAGE_DIGEST}"}

    assert image_problems(arguments, SYNTHETIC_PINS) == []


def test_a_sha_key_that_gets_the_prefix_is_reported_but_not_the_whole_digest_one() -> (
    None
):
    pinned = {"a.image.sha": "${X_IMAGE_DIGEST}", "a.image.tag": "${X_IMAGE_TAG}"}
    whole = {
        "kube-state-metrics.image.sha": "${X_IMAGE_DIGEST}",
        "kube-state-metrics.image.tag": "${X_IMAGE_TAG}",
    }

    assert image_problems(pinned, SYNTHETIC_PINS) == [
        f"a.image.sha does not take {BARE_DIGEST}"
    ]
    assert image_problems(whole, SYNTHETIC_PINS) == []


def test_a_bare_hex_for_a_sha_key_is_accepted() -> None:
    arguments = {"a.image.sha": "${X_IMAGE_DIGEST#sha256:}", "a.image.tag": "1"}

    assert image_problems(arguments, SYNTHETIC_PINS) == []


def test_a_full_reference_without_a_digest_is_reported() -> None:
    arguments = {"cluster.imageName": "example.org/y:1"}

    assert image_problems(arguments, SYNTHETIC_PINS) == [
        "cluster.imageName is not a reference by digest"
    ]


def test_a_name_that_is_not_a_pin_is_reported() -> None:
    arguments = {"image.digest": "${NOT_A_PIN}"}

    assert image_problems(arguments, SYNTHETIC_PINS) == [
        "image.digest names something that is not in pins.env"
    ]


def test_a_malformed_digest_is_reported() -> None:
    pins = {**SYNTHETIC_PINS, "X_IMAGE_DIGEST": "sha256:" + "a" * 63}
    arguments = {"image.tag": "1", "image.digest": "${X_IMAGE_DIGEST}"}

    assert image_problems(arguments, pins) == [
        "image.digest does not take sha256: and 64 hex"
    ]


def test_a_pin_file_with_a_tag_and_no_digest_is_reported() -> None:
    text = (
        "# renovate: datasource=docker depName=example.org/x\n"
        "X_IMAGE_TAG=1.0.0\n"
        "OTHER=1\n"
    )

    assert pin_file_problems(text) == [
        "X_IMAGE_TAG has no X_IMAGE_DIGEST=sha256:<64 hex> below"
    ]


def test_a_pin_file_with_a_tag_and_no_comment_is_reported() -> None:
    text = f"X_IMAGE_TAG=1.0.0\nX_IMAGE_DIGEST={SYNTHETIC_DIGEST}\n"

    assert pin_file_problems(text) == [
        "X_IMAGE_TAG has no '# renovate: datasource=docker' above"
    ]


def test_a_pin_file_whose_repository_and_comment_disagree_is_reported() -> None:
    text = (
        "X_IMAGE_REPOSITORY=example.org/x\n"
        "# renovate: datasource=docker depName=example.org/other\n"
        "X_IMAGE_TAG=1.0.0\n"
        f"X_IMAGE_DIGEST={SYNTHETIC_DIGEST}\n"
    )

    assert pin_file_problems(text) == ["X_IMAGE_REPOSITORY and its comment disagree"]


def test_a_one_line_image_without_a_digest_is_reported() -> None:
    assert pin_file_problems("Y_IMAGE=example.org/y:1\n") == [
        "Y_IMAGE is not name:tag@sha256:<64 hex>"
    ]


def test_a_pin_file_with_a_complete_pin_is_not_reported() -> None:
    text = (
        "X_IMAGE_REPOSITORY=example.org/x\n"
        "# renovate: datasource=docker depName=example.org/x\n"
        "X_IMAGE_TAG=1.0.0\n"
        f"X_IMAGE_DIGEST={SYNTHETIC_DIGEST}\n"
        f"Y_IMAGE=example.org/y:1@{SYNTHETIC_DIGEST}\n"
    )

    assert pin_file_problems(text) == []


def test_a_values_file_that_names_an_image_by_tag_is_reported() -> None:
    values = {"image": {"repository": "example.org/x", "tag": "1"}}

    assert values_problems(values) == [".image names an image without a digest"]


def test_a_values_file_that_names_an_image_with_its_digest_is_not_reported() -> None:
    values = {
        "image": {"repository": "example.org/x", "tag": "1", "digest": "sha256:ab"},
        "other": {"imageName": f"example.org/y:1@{SYNTHETIC_DIGEST}"},
    }

    assert values_problems(values) == []


def test_a_values_file_that_names_a_full_reference_by_tag_is_reported() -> None:
    values = {"cluster": {"imageName": "example.org/y:1"}}

    assert values_problems(values) == [
        ".cluster.imageName is not a reference by digest"
    ]
