"""infra/kind/identity.sh: what it records, what it reads first, what it sweeps (Y2c).

The same real script and stand-in ``kubectl`` as ``test_kind_identity_script.py``
(its ``run_script`` and ``Run``), for the fixes of the infra review of Y2b:

- the record of who holds the cluster is ``changing`` from the last check to the
  end of ``up`` and ``ok`` after it, so a refusal that changed nothing leaves
  what ``up.sh`` recorded and a half-made add-on leaves ``changing``;
- every check that only reads runs before the first change;
- a realm folder that a killed run left is swept, and only what the script says;
- a bad ``MERIDIAN_IDENTITY`` stops ``up.sh``, ``smoke.sh`` and ``identity.sh`` and
  blocks neither teardown nor the holder command.

No cluster, no container.
"""

import os
import re
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest
from kindsupport import KIND_DIR, requires_jq
from test_kind_identity_script import TOO_LITTLE, Run, run_script

pytestmark = requires_jq


# ── who holds the cluster: changing after the last check, ok at the end ──────


def test_a_good_run_records_changing_before_its_first_change_and_ok_at_the_end(
    tmp_path: Path,
) -> None:
    run = run_script(tmp_path, "up")
    changing = run.calls.index("HOLDER changing")
    first_apply = next(i for i, c in enumerate(run.calls) if c.startswith("APPLIED"))
    last_apply = max(i for i, c in enumerate(run.calls) if c.startswith("APPLIED"))
    rollout = next(i for i, c in enumerate(run.calls) if "rollout status" in c)

    assert run.process.returncode == 0, run.output
    assert run.holders() == ["changing", "ok"]
    assert changing < first_apply < last_apply < rollout < run.calls.index("HOLDER ok")
    assert run.calls[-1] == "HOLDER ok"


@pytest.mark.parametrize(
    "refusal",
    [
        {"kilobytes": TOO_LITTLE},
        {"gateway": "narrow"},
        {"docker_host": "tcp://192.0.2.1:2375"},
        {"rotate": "yes"},
        {"identity": ""},
        {"fail": "get nodes"},
    ],
    ids=["memory", "edge", "docker", "rotate", "switch", "cluster"],
)
def test_a_refusal_before_the_first_change_leaves_the_record_alone(
    tmp_path: Path, refusal: dict
) -> None:
    run = run_script(tmp_path, "up", **refusal)

    assert run.process.returncode != 0
    assert run.holders() == []  # up.sh recorded ok before this ran, by hand: as it was
    assert "partly made" not in run.output
    assert run.changes() == []


def test_a_run_that_stops_after_its_first_change_leaves_changing_and_says_so(
    tmp_path: Path,
) -> None:
    run = run_script(tmp_path, "up", fail="create secret generic keycloak-credentials")

    assert run.process.returncode != 0
    assert run.holders() == ["changing"]
    assert "partly made" in run.process.stderr
    assert "identity.sh status" in run.process.stderr
    assert run.leftovers() == []


def test_the_trap_line_is_not_printed_for_a_run_that_ends_well(tmp_path: Path) -> None:
    run = run_script(tmp_path, "up")

    assert "partly made" not in run.output


# ── the checks that only read run before the first change ────────────────────


def one_change_would_be_seen(run: Run) -> None:
    assert run.process.returncode != 0
    assert run.changes() == []
    assert run.applied() == []
    assert run.holders() == []
    assert [c for c in run.calls if "create" in c] == []
    assert "partly made" not in run.output


def test_a_malformed_pin_leaves_no_apply_and_no_secret_behind(tmp_path: Path) -> None:
    pin = "KEYCLOAK_IMAGE=quay.io/keycloak/keycloak:26.8.0\n"
    text = (KIND_DIR / "pins.env").read_text(encoding="utf-8")
    (line,) = re.findall(r"^KEYCLOAK_IMAGE=.*\n", text, re.M)

    run = run_script(tmp_path, "up", edits=(("pins.env", line, pin),))

    one_change_would_be_seen(run)
    assert "KEYCLOAK_IMAGE" in run.output and "sha256" in run.output


@pytest.mark.parametrize(
    ("old", "new", "said"),
    [
        (
            "          image: IMAGE-PLACEHOLDER\n          args:",
            "          image: quay.io/x/y:1\n          args:",
            "exactly twice",
        ),
        (
            'meridian.local/realm-sha256: "REALM-SHA256-PLACEHOLDER"',
            'meridian.local/realm-sha256: "none"',
            "exactly once",
        ),
        (
            "        - name: copy-quarkus\n",
            "        - name: copy-quarkus\n          image: quay.io/x/y:1\n",
            "image: lines",
        ),
    ],
    ids=["one placeholder", "no fingerprint", "a third image line"],
)
def test_a_manifest_that_is_not_as_expected_is_refused_before_any_change(
    tmp_path: Path, old: str, new: str, said: str
) -> None:
    run = run_script(tmp_path, "up", edits=(("manifests/identity.yaml", old, new),))

    one_change_would_be_seen(run)
    assert said in run.output


# ── a realm folder that a killed run left ────────────────────────────────────


def make_folder(path: Path, files: tuple[str, ...], age_hours: float) -> Path:
    path.mkdir(parents=True)
    for name in files:
        (path / name).write_text("left over")
    stamp = time.time() - age_hours * 3600
    os.utime(path, (stamp, stamp))
    return path


def test_the_sweep_removes_old_realm_folders_and_only_what_it_says(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "cache" / "meridian-identity"
    folder.mkdir(parents=True)
    old = make_folder(folder / "realm.AbC123", ("secrets.env", "realm.json"), 2)
    fresh = make_folder(folder / "realm.Fresh1", ("secrets.env",), 0)
    nested = make_folder(folder / "realm.Nest12", ("secrets.env",), 3)
    (nested / "inner").mkdir()
    os.utime(nested, (time.time() - 10800, time.time() - 10800))
    wrong_shape = make_folder(folder / "realm.Odd", ("secrets.env",), 3)
    other = make_folder(folder / "elsewhere", ("keep.txt",), 3)
    outside = make_folder(tmp_path / "outside", ("precious.txt",), 3)
    (folder / "realm.Link12").symlink_to(outside)

    run = run_script(tmp_path, "up", realm_secret=True, credentials_secret=True)

    assert run.process.returncode == 0, run.output
    assert not old.exists()
    assert (
        "removed the stale realm folder" in run.output and "realm.AbC123" in run.output
    )
    assert fresh.exists()  # touched within the hour: a run that is under way
    assert (nested / "secrets.env").exists() and (nested / "inner").is_dir()
    assert "holds something that is not a regular file" in run.output
    assert wrong_shape.exists() and other.exists()  # not the shape mktemp gave
    assert (folder / "realm.Link12").is_symlink()  # a link is never followed
    assert (outside / "precious.txt").exists()
    assert "realm.Link12" in run.output and "left alone" in run.output


def test_the_sweep_runs_before_the_secrets_are_looked_at_and_makes_nothing(
    tmp_path: Path,
) -> None:
    run = run_script(tmp_path, "up")  # lays the copy out and makes the cache
    left = make_folder(run.cache / "meridian-identity" / "realm.Stale9", ("a",), 5)

    second = run_script(tmp_path, "up", realm_secret=True, credentials_secret=True)

    assert second.process.returncode == 0, second.output
    assert not left.exists()
    assert second.leftovers() == []


def test_the_scripts_text_says_what_the_sweep_removes() -> None:
    text = (KIND_DIR / "identity.sh").read_text(encoding="utf-8")

    assert "REGULAR FILES" in text and "rmdir" in text and "follows no link" in text
    assert "kill -9" in text


# ── the switch is checked by three scripts and blocks no other ───────────────

TOOL_STUBS = {
    "kind": '#!/usr/bin/env bash\nif [[ "$1 $2" == "get clusters" ]]; then\n'
    "  echo 'No kind clusters found.'\nfi\n",
    "docker": "#!/usr/bin/env bash\nexit 0\n",
}


def run_other_script(
    tmp_path: Path, name: str, identity: str
) -> subprocess.CompletedProcess[str]:
    first = run_script(tmp_path, "status")  # lays the copy and the stand-ins out
    kind = tmp_path / "infra" / "kind"
    for script in ("down.sh", "holder.sh", "up.sh", "smoke.sh"):
        shutil.copy2(KIND_DIR / script, kind / script)
    shutil.copytree(KIND_DIR / "smoke.d", kind / "smoke.d", dirs_exist_ok=True)
    for tool, text in TOOL_STUBS.items():
        binary = first.stub / tool
        binary.write_text(text)
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    (first.stub / "calls").unlink(missing_ok=True)  # what the layout run asked
    env = {
        "PATH": f"{first.stub}:{os.environ['PATH']}",
        "HOME": str(tmp_path / "home"),
        "DOCKER_HOST": "unix:///var/run/docker.sock",
        "CLUSTER_HOLDER": "tests",
        "STUB_DIR": str(first.stub),
        "MERIDIAN_IDENTITY": identity,
    }
    return subprocess.run(
        ["bash", str(kind / name)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


@pytest.mark.parametrize("name", ["up.sh", "smoke.sh", "identity.sh"])
def test_a_bad_switch_stops_up_smoke_and_identity_before_anything_is_asked(
    tmp_path: Path, name: str
) -> None:
    if name == "identity.sh":
        run = run_script(tmp_path, "status", identity="dex")
        output, calls = run.output, run.calls
    else:
        done = run_other_script(tmp_path, name, "dex")
        output, calls = done.stdout + done.stderr, (tmp_path / "stub" / "calls")
        calls = calls.read_text().splitlines() if calls.exists() else []

    assert "MERIDIAN_IDENTITY must be empty" in output
    assert calls == []


@pytest.mark.parametrize("name", ["down.sh", "holder.sh"])
def test_a_bad_switch_does_not_block_teardown_or_the_holder_command(
    tmp_path: Path, name: str
) -> None:
    done = run_other_script(tmp_path, name, "dex")

    assert "MERIDIAN_IDENTITY" not in done.stdout + done.stderr
    assert done.returncode == 0, done.stderr


def test_the_three_scripts_call_the_check_and_no_other_script_does() -> None:
    callers = sorted(
        path.name
        for path in KIND_DIR.glob("*.sh")
        if "identity_switch_check" in path.read_text(encoding="utf-8")
        and path.name != "common.sh"
    )

    assert callers == ["identity.sh", "smoke.sh", "up.sh"]
