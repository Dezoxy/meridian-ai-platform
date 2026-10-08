"""infra/kind/identity.sh: one generation for the two Secrets, a loopback cluster
before a password is read, and the small fixes of the infra review of Y2e (Y2f).

The same real script and stand-in ``kubectl`` as ``test_kind_identity_script.py``
(its ``run_script``; the stand-in keeps the annotations of a Secret that was
applied, answers a read of one annotation, and answers ``config view`` with
``STUB_SERVER``). The generator's duplicate-key refusal runs the real
``identity-realm.sh`` from a copy with an edited cast.

Not shown: that a cluster behaves so (server-side apply under one field manager
removes the old keys of a Secret on a rotation is reasoned, not observed here).
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from kindsupport import KIND_DIR, requires_jq
from test_kind_identity_cast import SHOW, marker_realm, parse_rows, prepared
from test_kind_identity_script import (
    CREDENTIALS_SECRET,
    REALM_SECRET,
    SCRIPT_FILES,
    Run,
    run_script,
)

pytestmark = requires_jq

GENERATION_KEY = "meridian.local/identity-generation"
NOT_ONE_GENERATION = "not of one generation"
CREATES = "create secret generic"


def generation_of(tmp_path: Path, secret: str) -> str:
    """The generation annotation the stand-in holds for a Secret ('' if none)."""
    notes = json.loads((tmp_path / "stub" / f"notes-{secret}").read_text())
    return notes.get(GENERATION_KEY, "")


def generations(tmp_path: Path) -> tuple[str, str]:
    return (
        generation_of(tmp_path, REALM_SECRET),
        generation_of(tmp_path, CREDENTIALS_SECRET),
    )


def set_generations(tmp_path: Path, realm: str | None, credentials: str | None) -> None:
    """Replace what the stand-in holds: a value, or None for no annotation."""
    for secret, value in ((REALM_SECRET, realm), (CREDENTIALS_SECRET, credentials)):
        notes = {} if value is None else {GENERATION_KEY: value}
        (tmp_path / "stub" / f"notes-{secret}").write_text(json.dumps(notes))


def creates(run: Run) -> list[str]:
    return [c for c in run.calls if CREATES in c]


# ── one generation for the two Secrets ───────────────────────────────────────


def test_a_first_run_stamps_both_secrets_with_one_non_empty_generation(
    tmp_path: Path,
) -> None:
    run = run_script(tmp_path, "up")

    assert run.process.returncode == 0, run.output
    realm, credentials = generations(tmp_path)
    assert re.fullmatch(r"[0-9a-f]{16}", realm)
    assert realm == credentials
    assert realm in run.output  # the value is no secret and the log says it


def test_the_generation_is_one_annotation_of_the_pod_templates_prefix() -> None:
    text = (KIND_DIR / "identity.sh").read_text(encoding="utf-8")
    manifest = (KIND_DIR / "manifests" / "identity.yaml").read_text(encoding="utf-8")

    assert f"readonly IDENTITY_GENERATION_KEY={GENERATION_KEY}\n" in text
    assert text.count(GENERATION_KEY) == 1  # defined once
    assert GENERATION_KEY.split("/")[0] + "/realm-sha256" in manifest


def test_a_second_plain_run_keeps_the_secrets_of_one_generation(tmp_path: Path) -> None:
    run_script(tmp_path, "up")
    before = generations(tmp_path)

    run = run_script(tmp_path, "up")

    assert run.process.returncode == 0, run.output
    assert creates(run) == []
    assert run.leftovers() == []  # no generator ran, no folder was made
    assert "kept" in run.output and NOT_ONE_GENERATION not in run.output
    assert generations(tmp_path) == before


def test_the_generation_is_read_from_the_annotations_and_never_the_data(
    tmp_path: Path,
) -> None:
    run_script(tmp_path, "up")

    run = run_script(tmp_path, "up")

    reads = [c for c in run.calls if "get secret" in c and "jsonpath" in c]
    generation_reads = [c for c in reads if "metadata.annotations" in c]
    assert len(generation_reads) == 2
    assert all(GENERATION_KEY.replace(".", "\\.") in c for c in generation_reads)
    assert not any("{.data" in c for c in generation_reads)


def test_two_secrets_of_different_generations_are_both_made_anew(
    tmp_path: Path,
) -> None:
    run_script(tmp_path, "up")
    set_generations(tmp_path, "0123456789abcdef", "fedcba9876543210")

    run = run_script(tmp_path, "up")

    assert run.process.returncode == 0, run.output
    assert len(creates(run)) == 2
    assert NOT_ONE_GENERATION in run.output
    assert "a run stopped between the two" in run.output
    realm, credentials = generations(tmp_path)
    assert realm == credentials and realm not in {
        "0123456789abcdef",
        "fedcba9876543210",
    }


@pytest.mark.parametrize(
    ("realm", "credentials"),
    [(None, "0123456789abcdef"), ("0123456789abcdef", None), (None, None), ("", "")],
    ids=["realm-has-none", "credentials-has-none", "neither", "both-empty"],
)
def test_secrets_with_no_generation_are_both_made_anew(
    tmp_path: Path, realm: str | None, credentials: str | None
) -> None:
    run_script(tmp_path, "up")
    set_generations(tmp_path, realm, credentials)

    run = run_script(tmp_path, "up")

    assert run.process.returncode == 0, run.output
    assert len(creates(run)) == 2
    assert NOT_ONE_GENERATION in run.output
    assert "before generations were recorded" in run.output
    new_realm, new_credentials = generations(tmp_path)
    assert re.fullmatch(r"[0-9a-f]{16}", new_realm) and new_realm == new_credentials


def test_secrets_made_before_generations_were_recorded_are_made_anew(
    tmp_path: Path,
) -> None:
    # The stand-in's flags: both Secrets exist and nothing was ever annotated.
    run = run_script(tmp_path, "up", realm_secret=True, credentials_secret=True)

    assert run.process.returncode == 0, run.output
    assert len(creates(run)) == 2
    assert NOT_ONE_GENERATION in run.output


def test_a_rotation_that_died_between_the_two_secrets_is_made_anew_by_a_plain_run(
    tmp_path: Path,
) -> None:
    # The review's window: the realm Secret is replaced, the run stops before the
    # credentials Secret is, and the next plain run used to keep the pair.
    run_script(tmp_path, "up")
    first = generations(tmp_path)[0]
    broken = run_script(
        tmp_path, "up", rotate="1", fail=f"{CREATES} {CREDENTIALS_SECRET}"
    )
    realm, credentials = generations(tmp_path)
    assert broken.process.returncode != 0
    assert realm != credentials and credentials == first  # out of step

    again = run_script(tmp_path, "up")

    assert again.process.returncode == 0, again.output
    assert len(creates(again)) == 2
    assert NOT_ONE_GENERATION in again.output
    healed_realm, healed_credentials = generations(tmp_path)
    assert healed_realm == healed_credentials
    assert healed_realm not in {realm, first}
    third = run_script(tmp_path, "up")  # and now a plain run keeps them
    assert creates(third) == [] and "kept" in third.output


def test_a_rotation_gives_another_generation_to_both(tmp_path: Path) -> None:
    run_script(tmp_path, "up")
    first = generations(tmp_path)

    run = run_script(tmp_path, "up", rotate="1")

    assert run.process.returncode == 0, run.output
    assert len(creates(run)) == 2
    assert NOT_ONE_GENERATION not in run.output  # asked for, so no such line
    second = generations(tmp_path)
    assert second[0] == second[1]
    assert second != first


def test_only_one_secret_is_still_said_in_its_own_words(tmp_path: Path) -> None:
    run = run_script(tmp_path, "up", realm_secret=True)

    assert "only one of the two Secrets exists" in run.output
    assert NOT_ONE_GENERATION not in run.output


# ── passwords: a loopback cluster before a password is read ──────────────────

REFUSED = [
    ("https://192.0.2.7:6443", "192.0.2.7"),
    ("https://kind.example.com:6443", "kind.example.com"),
    ("https://localhost.example.com:6443", "localhost.example.com"),
    ("https://127.0.0.1.example.com:6443", "127.0.0.1.example.com"),
    ("https://[2001:db8::1]:6443", "[2001:db8::1]"),
    ("https://admin@192.0.2.7:6443/api", "192.0.2.7"),
    ("https://128.0.0.1:6443", "128.0.0.1"),
]


@pytest.mark.parametrize(("server", "host"), REFUSED)
def test_passwords_refuses_a_cluster_that_is_not_on_this_machine(
    tmp_path: Path, server: str, host: str
) -> None:
    prepared(tmp_path, marker_realm())

    run = run_script(tmp_path, "passwords", env={**SHOW, "STUB_SERVER": server})

    assert run.process.returncode != 0
    assert run.process.stdout == ""
    assert host in run.process.stderr and "nothing was read" in run.process.stderr
    assert not any("get secret" in call for call in run.calls)
    assert not any("get nodes" in call for call in run.calls)  # before need_cluster


@pytest.mark.parametrize("server", ["", "not a url", "https://", "localhost:6443"])
def test_passwords_refuses_a_server_it_cannot_read(tmp_path: Path, server: str) -> None:
    prepared(tmp_path, marker_realm())

    run = run_script(tmp_path, "passwords", env={**SHOW, "STUB_SERVER": server})

    assert run.process.returncode != 0
    assert run.process.stdout == ""
    assert "nothing was read" in run.process.stderr
    assert not any("get secret" in call for call in run.calls)


@pytest.mark.parametrize(
    "server",
    [
        "https://127.0.0.1:6443",
        "https://localhost:6443",
        "https://[::1]:6443",
        "https://127.0.0.1",
        "https://localhost/",
    ],
)
def test_passwords_is_allowed_on_a_loopback_cluster(
    tmp_path: Path, server: str
) -> None:
    prepared(tmp_path, marker_realm())

    run = run_script(tmp_path, "passwords", env={**SHOW, "STUB_SERVER": server})

    assert run.process.returncode == 0, run.output
    assert len(parse_rows(run.process.stdout)) == 7


def test_the_server_is_read_from_the_kubeconfig_of_kctl_and_nothing_else_is_asked(
    tmp_path: Path,
) -> None:
    prepared(tmp_path, marker_realm())

    run = run_script(tmp_path, "passwords", env=SHOW)

    (view,) = [c for c in run.calls if "config view" in c]
    assert "--minify" in view and "jsonpath" in view and "server" in view
    assert "--context kind-" in view  # the context kctl uses
    assert run.calls.index(view) < min(
        i for i, c in enumerate(run.calls) if "get nodes" in c
    )


@pytest.mark.parametrize("command", ["users", "status"])
def test_users_and_status_do_not_ask_for_a_loopback_cluster(
    tmp_path: Path, command: str
) -> None:
    prepared(tmp_path, marker_realm())

    run = run_script(
        tmp_path, command, namespace=True, env={"STUB_SERVER": "https://192.0.2.7:6443"}
    )

    assert run.process.returncode == 0, run.output
    assert not any("config view" in call for call in run.calls)


# ── the small fixes ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("command", ["users", "passwords"])
def test_a_user_with_no_name_stops_the_command_with_the_fixed_sentence(
    tmp_path: Path, command: str
) -> None:
    realm = json.loads(marker_realm())
    del realm["users"][1]["username"]
    prepared(tmp_path, json.dumps(realm))

    run = run_script(tmp_path, command, env=SHOW)

    assert run.process.returncode != 0
    assert run.process.stdout == ""
    assert "a name for every user" in run.process.stderr


def test_an_escape_byte_in_a_display_name_does_not_reach_the_users_output(
    tmp_path: Path,
) -> None:
    realm = json.loads(marker_realm())
    realm["users"][0]["firstName"] = "Aino\x1b[31m"
    prepared(tmp_path, json.dumps(realm))

    run = run_script(tmp_path, "users")

    assert run.process.returncode == 0, run.output
    assert "\x1b" not in run.output
    rows = parse_rows(run.process.stdout)
    assert len(rows) == 7 and rows[0][0] == "aino.lindqvist"
    assert rows[0][1].startswith("Aino")


def test_an_escape_byte_in_a_user_name_does_not_reach_the_passwords_output(
    tmp_path: Path,
) -> None:
    realm = json.loads(marker_realm())
    realm["users"][0]["username"] = "aino\x1b[2J.lindqvist"
    prepared(tmp_path, json.dumps(realm))

    run = run_script(tmp_path, "passwords", env=SHOW)

    assert run.process.returncode == 0, run.output
    assert "\x1b" not in run.output
    assert len(parse_rows(run.process.stdout)) == 7


def test_a_tab_and_a_newline_still_separate_the_columns_and_rows(
    tmp_path: Path,
) -> None:
    prepared(tmp_path, marker_realm())

    run = run_script(tmp_path, "users")

    assert len(run.process.stdout.splitlines()) == 8  # the header and seven people


def test_passwords_hands_the_passwords_to_its_loop_on_a_pipe_not_a_here_string() -> (
    None
):
    text = (KIND_DIR / "identity.sh").read_text(encoding="utf-8")
    body = text.split("\npasswords() {\n", 1)[1].split("\n}\n", 1)[0]

    assert "printf '%s\\n' \"${cast}\" | while" in body
    assert "<<<" not in body


# ── the generator refuses two names that map to one secret ───────────────────


def copy_generator(tmp_path: Path, *edits: tuple[str, str]) -> Path:
    """A copy of the generator's folder with the script's text edited."""
    kind = tmp_path / "infra" / "kind"
    kind.mkdir(parents=True)
    for name in SCRIPT_FILES:
        shutil.copy2(KIND_DIR / name, kind / name)
    script = kind / "identity-realm.sh"
    text = script.read_text(encoding="utf-8")
    for old, new in edits:
        assert old in text
        text = text.replace(old, new, 1)
    script.write_text(text, encoding="utf-8")
    return script


def generate(script: Path, out: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            str(script),
            str(out),
            "http://claims.meridian.localhost:8088/auth/callback",
            "http://claims.meridian.localhost:8088",
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_generator_still_writes_the_same_files_for_todays_cast(
    tmp_path: Path,
) -> None:
    out = tmp_path / "out"

    done = generate(copy_generator(tmp_path / "copy"), out)

    assert done.returncode == 0, done.stderr
    keys = [
        line.split("=", 1)[0]
        for line in (out / "secrets.env").read_text(encoding="utf-8").splitlines()
    ]
    assert keys[0] == "MERIDIAN_STAFF_USER_AINO_LINDQVIST_PASSWORD"
    assert keys[-2:] == [
        "MERIDIAN_STAFF_CLIENT_MERIDIAN_CLAIMS_WEB_SECRET",
        "MERIDIAN_STAFF_CLIENT_MERIDIAN_SCRIPTS_SECRET",
    ]
    assert len(keys) == 9 and len(set(keys)) == 9


@pytest.mark.parametrize(
    ("edit", "key"),
    [
        (
            ('"username": "mikkel.vang"', '"username": "ingrid_strand"'),
            "MERIDIAN_STAFF_USER_INGRID_STRAND_PASSWORD",
        ),
        (
            ('{clientId: "meridian-scripts"', '{clientId: "meridian_claims_web"'),
            "MERIDIAN_STAFF_CLIENT_MERIDIAN_CLAIMS_WEB_SECRET",
        ),
    ],
    ids=["two-users", "two-clients"],
)
def test_the_generator_stops_before_writing_when_two_names_map_to_one_key(
    tmp_path: Path, edit: tuple[str, str], key: str
) -> None:
    out = tmp_path / "out"

    done = generate(copy_generator(tmp_path / "copy", edit), out)

    assert done.returncode != 0
    assert key in done.stderr
    assert "same secret" in done.stderr
    assert not out.exists()  # nothing was written, not even the folder
    assert done.stdout == ""
