"""The switch of the sign-in issuer add-on and what it changes at the edge (S021, Y2b).

``MERIDIAN_IDENTITY`` is empty (off, the default) or ``keycloak``. Off, ``make up``
does what it did before the add-on existed: here that is the Gateway's manifest,
which ``up.sh`` applies from ``gateway_manifest``'s output, and which must be the
committed file byte for byte. On, the one thing that changes at the edge is who may
attach a route to the listener: the namespace ``identity`` as well as ``meridian``,
by a selector on the namespace's name label and never ``All``.

The functions run in bash against the real ``common.sh`` and ``gateways.sh``; no
cluster, no container.
"""

import subprocess
from pathlib import Path

import pytest
import yaml
from chartsupport import NAMESPACE_LABEL
from kindsupport import KIND_DIR, UP_SH, load_documents, requires_jq

GATEWAY_FILE = KIND_DIR / "manifests" / "gateway.yaml"
USAGE = "MERIDIAN_IDENTITY must be empty"


def bash(script: str, identity: str | None) -> subprocess.CompletedProcess[str]:
    env = {"PATH": "/usr/bin:/bin"}
    if identity is not None:
        env["MERIDIAN_IDENTITY"] = identity
    return subprocess.run(
        ["bash", "-c", f"set -euo pipefail\n{script}"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        cwd=KIND_DIR,
    )


def edge_manifest(
    identity: str | None, file: Path = GATEWAY_FILE
) -> tuple[int, str, str]:
    done = bash(
        f'. "{KIND_DIR}/common.sh"\n. "{KIND_DIR}/gateways.sh"\n'
        f'edge_gateway_manifest "{file}"',
        identity,
    )
    return done.returncode, done.stdout, done.stderr


def listener(text: str) -> dict:
    (gateway,) = [d for d in yaml.safe_load_all(text) if d and d["kind"] == "Gateway"]
    (found,) = gateway["spec"]["listeners"]
    return found


# ── the switch ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", [None, ""])
def test_unset_and_empty_are_off_and_change_nothing(value: str | None) -> None:
    done = bash(
        f'. "{KIND_DIR}/common.sh"\nif identity_on; then echo on; else echo off; fi',
        value,
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "off"


def test_keycloak_is_on() -> None:
    done = bash(
        f'. "{KIND_DIR}/common.sh"\nif identity_on; then echo on; else echo off; fi',
        "keycloak",
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "on"


@pytest.mark.parametrize(
    "value",
    [
        "Keycloak",
        "KEYCLOAK",
        "dex",
        "1",
        "true",
        "on",
        "keycloak ",
        " keycloak",
        "mock",
    ],
)
def test_any_other_value_stops_with_a_usage_line_before_anything_runs(
    value: str,
) -> None:
    done = bash(f'. "{KIND_DIR}/common.sh"\necho reached', value)

    assert done.returncode != 0
    assert done.stdout == ""  # nothing after the source ran
    assert USAGE in done.stderr
    assert (
        "keycloak" in done.stderr
        and "MERIDIAN_IDENTITY=keycloak make up" in done.stderr
    )


def test_every_script_that_sources_common_sh_refuses_a_bad_value_for_free() -> None:
    # up.sh, deploy.sh and smoke.sh each source common.sh before any other line
    # that does anything, so the one check there covers all three.
    for name in ("up.sh", "deploy.sh", "smoke.sh"):
        text = (KIND_DIR / name).read_text(encoding="utf-8")
        assert '. "$(dirname "${BASH_SOURCE[0]}")/common.sh"' in text, name


# ── the edge: off is the committed file, on adds one namespace ───────────────


@requires_jq
@pytest.mark.parametrize("value", [None, ""])
def test_off_the_edge_manifest_is_the_committed_file_byte_for_byte(
    value: str | None,
) -> None:
    status, out, err = edge_manifest(value)

    assert (status, err) == (0, "")
    assert out == GATEWAY_FILE.read_text(encoding="utf-8")
    assert out.encode() == GATEWAY_FILE.read_bytes()


def test_off_the_listener_admits_routes_from_the_namespace_meridian_only() -> None:
    # The pin of the off path, and the twin of the one below.
    found = listener(edge_manifest(None)[1])

    assert found["allowedRoutes"]["namespaces"] == {
        "from": "Selector",
        "selector": {"matchLabels": {NAMESPACE_LABEL: "meridian"}},
    }


def test_on_the_listener_admits_meridian_and_identity_by_the_name_label_not_all() -> (
    None
):
    status, out, err = edge_manifest("keycloak")
    namespaces = listener(out)["allowedRoutes"]["namespaces"]

    assert (status, err) == (0, "")
    assert namespaces == {
        "from": "Selector",
        "selector": {
            "matchExpressions": [
                {
                    "key": NAMESPACE_LABEL,
                    "operator": "In",
                    "values": ["meridian", "identity"],
                }
            ]
        },
    }
    assert namespaces["from"] != "All"


def test_on_nothing_else_of_the_edge_changes() -> None:
    off = load_documents(GATEWAY_FILE)
    on = [d for d in yaml.safe_load_all(edge_manifest("keycloak")[1]) if d]

    assert len(on) == len(off) == 3
    for before, after in zip(off, on, strict=True):
        if before["kind"] != "Gateway":
            assert after == before
            continue
        before["spec"]["listeners"][0]["allowedRoutes"] = after["spec"]["listeners"][0][
            "allowedRoutes"
        ]
        assert after == before


def test_a_gateway_file_that_lost_the_selector_is_refused_not_applied_as_it_stands(
    tmp_path: Path,
) -> None:
    text = GATEWAY_FILE.read_text(encoding="utf-8")
    changed = tmp_path / "gateway.yaml"
    changed.write_text(text.replace("matchLabels", "matchLabelz"), encoding="utf-8")

    status, out, err = edge_manifest("keycloak", changed)

    assert status != 0
    assert out == ""
    assert "does not hold" in err


def test_up_applies_the_gateway_from_the_function_and_stops_when_it_fails() -> None:
    lines = UP_SH.splitlines()
    (build,) = [i for i, line in enumerate(lines) if "edge_gateway_manifest" in line]
    (apply,) = [i for i, line in enumerate(lines) if '<<<"${edge_manifest}"' in line]

    assert lines[build] == 'edge_manifest="$(edge_gateway_manifest)" || exit 1'
    assert build < apply
    assert (
        lines[apply].lstrip().startswith("kctl apply --server-side --force-conflicts")
    )
    assert "manifests/gateway.yaml" not in UP_SH  # the file is gateways.sh's to name
    assert 'readonly EDGE_GATEWAY_FILE="${KIND_DIR}/manifests/gateway.yaml"' in (
        KIND_DIR / "gateways.sh"
    ).read_text(encoding="utf-8")


def test_the_add_on_is_one_block_in_up_and_runs_after_every_wait_and_before_ok() -> (
    None
):
    lines = UP_SH.splitlines()
    (ok,) = [i for i, line in enumerate(lines) if line == "record_cluster_holder ok"]
    (available,) = [
        i
        for i, line in enumerate(lines)
        if line.startswith(
            "kctl -n envoy-gateway-system wait --for=condition=Available"
        )
    ]
    (start,) = [i for i, line in enumerate(lines) if line == "if identity_on; then"]
    block = lines[start : lines.index("fi", start) + 1]

    assert available < start < ok
    assert block == [
        "if identity_on; then",
        '  "${KIND_DIR}/identity.sh" up',
        "else",
        '  "${KIND_DIR}/identity.sh" note',
        "fi",
    ]
    # Nothing else of up.sh runs the add-on: the script is named in these two lines
    # only, and the switch is read in this block and in the Gateway's function.
    code = [line for line in lines if not line.startswith("#")]
    assert sum("identity.sh" in line for line in code) == 2
    assert sum("identity_on" in line for line in code) == 1
    assert sum("MERIDIAN_IDENTITY" in line for line in code) == 0
    assert len(lines) < 800  # the ceiling of the file size check
