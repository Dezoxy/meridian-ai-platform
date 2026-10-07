"""`make deploy` applies Meridian's alert rules (S073, K3, backlog row B19).

Until now only `make up` applied ``infra/kind/alerts/meridian.yaml``, so a rule
changed in the tree was not on the cluster after a deploy, and smoke (which
compared names) did not notice. ``apply_alert_rules`` of ``common.sh`` is the one
copy of the apply; ``up.sh`` and ``deploy.sh`` each call it. Nothing here touches
a cluster: ``deploy.sh`` runs whole against stub commands, and the function runs
in bash against a stub ``kubectl``.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest
from certscriptsupport import KIND_DIR, SECONDS
from test_certificate_deploy import ALERT_RULE_STATES, run_deploy, write_stub

UP_SH = (KIND_DIR / "up.sh").read_text(encoding="utf-8")
DEPLOY_SH = (KIND_DIR / "deploy.sh").read_text(encoding="utf-8")
COMMON_SH = (KIND_DIR / "common.sh").read_text(encoding="utf-8")
RULES = "alerts/meridian.yaml"


def apply_lines(calls: str) -> list[str]:
    return [line for line in calls.splitlines() if RULES in line]


# ── deploy.sh, whole ─────────────────────────────────────────────────────────
def test_deploy_applies_the_rules_file_once_with_the_wrappers_bound(
    tmp_path: Path,
) -> None:
    done, calls = run_deploy(tmp_path, "ready")

    (applied,) = apply_lines(calls)
    # The stub's build fails, which is where this run is meant to end.
    assert "docker build failed" in done.stderr
    assert "--request-timeout=15s" in applied
    assert "apply --server-side --force-conflicts -f " in applied
    assert applied.endswith(f"/{RULES}")
    assert applied.startswith("kubectl --kubeconfig ")


def test_deploy_applies_the_rules_after_every_check_and_before_the_build(
    tmp_path: Path,
) -> None:
    _, calls = run_deploy(tmp_path, "ready")

    lines = calls.splitlines()
    (applied,) = [i for i, line in enumerate(lines) if RULES in line]
    (built,) = [i for i, line in enumerate(lines) if line.startswith("docker build")]
    checked = [
        i
        for i, line in enumerate(lines)
        if "get clusterissuer" in line
        or "get certificaterequestpolicy" in line
        or "get deployment cert-manager-approver-policy" in line
        or "get secret rate-store-credentials" in line
    ]
    assert max(checked) < applied < built


@pytest.mark.parametrize(
    "issuer", ["missing", "unknown-kind", "not-ready", "no-condition"]
)
def test_a_deploy_refused_at_a_check_applies_no_rules(
    tmp_path: Path, issuer: str
) -> None:
    done, calls = run_deploy(tmp_path, issuer)

    assert done.returncode != 0
    assert apply_lines(calls) == []


def test_a_failed_apply_stops_the_deploy_with_its_sentence_before_the_build(
    tmp_path: Path,
) -> None:
    done, calls = run_deploy(tmp_path, "ready", alert_rules="failed")

    assert done.returncode != 0
    assert done.stderr.strip().splitlines()[-1].startswith("error: ")
    assert "could not apply Meridian's alert rules" in done.stderr
    # kubectl's own error is above the sentence, not lost.
    assert "not allowed" in done.stderr
    assert "docker build" not in calls
    assert "helm" not in calls
    assert "done. Next" not in done.stdout


def test_a_cluster_that_does_not_serve_the_rule_kind_says_so_in_one_sentence(
    tmp_path: Path,
) -> None:
    done, calls = run_deploy(tmp_path, "ready", alert_rules="kind-not-served")

    sentence = done.stderr.strip().splitlines()[-1]
    assert done.returncode != 0
    assert sentence.startswith("error: ")
    assert "PrometheusRule" in sentence
    assert "Prometheus operator" in sentence
    assert "make up" in sentence
    assert "could not apply Meridian's alert rules" not in sentence
    assert "docker build" not in calls


def test_the_rule_kind_sentence_names_no_file_or_address(tmp_path: Path) -> None:
    done, _ = run_deploy(tmp_path, "ready", alert_rules="kind-not-served")

    sentence = done.stderr.strip().splitlines()[-1]
    assert "/" not in sentence.replace("monitoring.coreos.com/v1", "")
    assert str(tmp_path) not in sentence


def test_the_stub_states_cover_the_three_answers_the_tests_use() -> None:
    assert set(ALERT_RULE_STATES) == {"applied", "failed", "kind-not-served"}


# ── one copy of the apply ────────────────────────────────────────────────────
def test_up_and_deploy_each_call_the_function_once_and_apply_no_file() -> None:
    for text in (UP_SH, DEPLOY_SH):
        calls = [line for line in text.splitlines() if line == "apply_alert_rules"]

        assert len(calls) == 1
        assert RULES not in text


def test_common_holds_the_one_apply_of_the_file() -> None:
    body = re.search(r"^apply_alert_rules\(\) \{\n(.*?)^\}", COMMON_SH, re.M | re.S)

    assert body, "no function apply_alert_rules in common.sh"
    code = [
        line for line in COMMON_SH.splitlines() if not line.lstrip().startswith("#")
    ]
    assert sum(RULES in line for line in code) == 1
    assert "kctl apply --server-side --force-conflicts -f " in body.group(1)
    assert f'"${{KIND_DIR}}/{RULES}"' in body.group(1)
    assert "Meridian's alert rules" in body.group(1)


def test_up_still_applies_the_rules_between_the_dashboards_and_the_monitor() -> None:
    lines = UP_SH.splitlines()

    dashboards = lines.index("apply_dashboards")
    rules = lines.index("apply_alert_rules")
    (monitor,) = [i for i, line in enumerate(lines) if "cert-manager-metrics" in line]
    assert dashboards < rules < monitor


def run_function(
    tmp_path: Path, answer: str
) -> tuple[subprocess.CompletedProcess, str]:
    """``apply_alert_rules`` from the real ``common.sh``, against a stub
    ``kubectl`` that logs its call and answers as ``answer`` says."""
    stubs = tmp_path / "bin"
    stubs.mkdir()
    calls = tmp_path / "calls"
    calls.touch()
    write_stub(stubs, "kubectl", f'echo "$*" >>"{calls}"\n{answer}')
    done = subprocess.run(
        ["bash", "-c", f'. "{KIND_DIR}/common.sh"\napply_alert_rules'],
        capture_output=True,
        text=True,
        env={"PATH": f"{stubs}:{os.environ['PATH']}", "HOME": str(tmp_path)},
        check=False,
        timeout=SECONDS,
    )
    return done, calls.read_text(encoding="utf-8")


def test_the_function_applies_the_one_file_server_side_and_says_what_it_does(
    tmp_path: Path,
) -> None:
    done, calls = run_function(tmp_path, ":")

    assert done.returncode == 0, done.stderr
    assert "Meridian's alert rules" in done.stdout
    assert calls.count("\n") == 1
    assert "apply --server-side --force-conflicts -f " in calls
    assert calls.strip().endswith(f"{KIND_DIR}/{RULES}")


def test_the_function_removes_its_scratch_file_on_both_paths(tmp_path: Path) -> None:
    temp = tmp_path / "tmp"
    temp.mkdir()
    for index, answer in enumerate((":", "echo boom >&2; exit 1")):
        scratch = tmp_path / str(index)
        scratch.mkdir()
        stubs = scratch / "bin"
        stubs.mkdir()
        write_stub(stubs, "kubectl", answer)
        subprocess.run(
            ["bash", "-c", f'. "{KIND_DIR}/common.sh"\napply_alert_rules'],
            capture_output=True,
            text=True,
            env={
                "PATH": f"{stubs}:{os.environ['PATH']}",
                "HOME": str(scratch),
                "TMPDIR": str(temp),
            },
            check=False,
            timeout=SECONDS,
        )

    assert list(temp.iterdir()) == []
