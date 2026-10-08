"""infra/kind/identity.sh: the Secret the Claims API reads for the staff sign-in
(S021, Y4b).

With ``MERIDIAN_SIGNIN=staff``, after the two Secrets of the issuer are sure,
``identity.sh up`` makes ``claims-api-signin`` in ``meridian``: the pages
client's secret, copied from ``keycloak-credentials`` without being read into a
variable, and a cookie key of 32 random bytes as base64. It is kept when it carries
the generation of ``keycloak-credentials`` and made anew when not. Without the
switch the script does what ``test_kind_identity_script.py`` pins, call for call.

The same real script and the same stand-in ``kubectl`` as that file's, with three
more answers (the credentials Secret as JSON, the new Secret by name, the
annotations it was applied with). No cluster, no container. What none of this shows:
that the apply is accepted, that the pod reads the keys, that a rotation rolls the
pod. Every real value in the test comes from the generator of the run; nothing is
written down.
"""

import base64
import json
import re
import stat
from pathlib import Path

import pytest
from kindsupport import KIND_DIR, requires_jq
from test_kind_identity_script import (
    CREDENTIALS_SECRET,
    STUB,
    Run,
    run_script,
    secret_values,
)

from meridian.platform.common.signinsession import SessionKeys

pytestmark = requires_jq

SIGNIN_SECRET = "claims-api-signin"  # noqa: S105 (a Secret's name)
GENERATION_NOTE = "meridian.local/identity-generation"
# The key of the pages client's secret in keycloak-credentials: the generator's
# name for realm meridian-staff and client meridian-claims-web.
PAGES_KEY = "MERIDIAN_STAFF_CLIENT_MERIDIAN_CLAIMS_WEB_SECRET"

# Two more answers, ahead of the stand-in's own: the credentials Secret as the
# cluster serves it (its values base64 encoded, from the file the stand-in kept; a
# read of one annotation says `-o jsonpath`, which is another call) and the new
# Secret by name (it exists once it was applied). Nothing else changes.
EXTRA_ARMS = r"""
  *"get secret keycloak-credentials -o json "*|*"keycloak-credentials -o json")
    if [[ -n "${STUB_CREDENTIALS_JSON_FAIL-}" ]]; then
      echo "stub: failing on purpose" >&2
      exit 1
    fi
    kept="${STUB_DIR}/cluster-keycloak-credentials"
    jq -Rn '[inputs | select(contains("=")) | capture("^(?<k>[^=]+)=(?<v>.*)$")]
      | map({(.k): (.v | @base64)}) | add
      | {apiVersion: "v1", kind: "Secret",
         metadata: {name: "keycloak-credentials"}, data: .}' "${kept}"
    ;;
  *"get secret claims-api-signin -o name"*)
    if [[ -f "${STUB_DIR}/notes-claims-api-signin" ]]; then
      echo secret/claims-api-signin
    fi
    ;;
"""
STAFF_STUB = STUB.replace('case "${all}" in\n', 'case "${all}" in\n' + EXTRA_ARMS, 1)


def staff_run(tmp_path: Path, *args: str, **kwargs) -> Run:
    """``identity.sh`` with the switch on and the stand-in that knows the Secret."""
    if not (tmp_path / "stub").exists():
        run_script(tmp_path, "status")  # lays the copy of infra/kind out
        binary = tmp_path / "stub" / "kubectl"
        binary.write_text(STAFF_STUB)
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    env = {"MERIDIAN_SIGNIN": "staff", **kwargs.pop("env", {})}
    return run_script(tmp_path, *args, env=env, **kwargs)


def signin_documents(run: Run) -> list[dict]:
    """The Secrets named claims-api-signin that the run applied, parsed."""
    documents = [json.loads(text) for _, text in run.applied() if text.startswith("{")]
    return [d for d in documents if d.get("metadata", {}).get("name") == SIGNIN_SECRET]


def decoded(secret: dict, key: str) -> str:
    return base64.b64decode(secret["data"][key]).decode()


def every_value(run: Run, secret: dict | None) -> list[str]:
    """What must be in no call and no output: the credentials the generator made
    and, when a Secret was applied, the two values it carries."""
    values = secret_values(run)
    if secret is not None:
        values += [decoded(secret, "client-credential"), decoded(secret, "session-key")]
    return values


# ── a first run ──────────────────────────────────────────────────────────────


def test_a_first_run_makes_the_secret_after_the_two_and_before_the_peers_policies(
    tmp_path: Path,
) -> None:
    run = staff_run(tmp_path, "up")

    assert run.process.returncode == 0, run.output
    names = [name for name, _ in run.applied()]
    assert names == [
        "identity-networkpolicy.yaml",
        "-",  # keycloak-realm
        "-",  # keycloak-credentials
        "-",  # claims-api-signin
        "identity-peers-networkpolicy.yaml",
        "-",  # the workload
    ]
    assert len(signin_documents(run)) == 1
    order = [i for i, c in enumerate(run.calls) if c.startswith("APPLIED")]
    assert order == sorted(order)


def test_the_secret_is_in_meridian_with_the_two_keys_and_the_credentials_generation(
    tmp_path: Path,
) -> None:
    run = staff_run(tmp_path, "up")
    (secret,) = signin_documents(run)

    assert secret["kind"] == "Secret"
    assert secret["metadata"]["namespace"] == "meridian"
    assert set(secret["data"]) == {"client-credential", "session-key"}
    generation = secret["metadata"]["annotations"][GENERATION_NOTE]
    assert re.fullmatch(r"[0-9a-f]{16}", generation)
    notes = json.loads((run.stub / f"notes-{CREDENTIALS_SECRET}").read_text())
    assert notes[GENERATION_NOTE] == generation
    calls = [c for c in run.calls if "-n meridian" in c and " apply " in c]
    assert len(calls) == 1 and calls[0].endswith("-f -")


def test_the_credential_is_the_pages_clients_secret_and_the_key_is_a_valid_cookie_key(
    tmp_path: Path,
) -> None:
    run = staff_run(tmp_path, "up")
    (secret,) = signin_documents(run)

    pages = dict(
        line.split("=", 1) for line in run.seen(CREDENTIALS_SECRET).splitlines() if "="
    )[PAGES_KEY]
    assert decoded(secret, "client-credential") == pages
    # The text is what the app reads from MERIDIAN_SESSION_KEY, and its own
    # parser accepts it: base64 of at least 32 bytes with distinct values.
    key = decoded(secret, "session-key")
    keys = SessionKeys.from_env({"MERIDIAN_SESSION_KEY": key})
    assert len(keys.current) == 32
    assert "\n" not in key


def test_the_realm_the_run_loads_returns_from_sign_out_to_the_queue(
    tmp_path: Path,
) -> None:
    staff_run(tmp_path, "up")

    realm = json.loads((tmp_path / "stub" / "cluster-keycloak-realm").read_text())
    (pages,) = [c for c in realm["clients"] if c["clientId"] == "meridian-claims-web"]
    assert pages["attributes"]["post.logout.redirect.uris"] == (
        "http://claims.meridian.localhost:8088/adjuster/claims"
    )


def test_the_pages_key_is_one_the_generator_makes_for_the_pages_client() -> None:
    text = (KIND_DIR / "identity.sh").read_text(encoding="utf-8")
    (key,) = re.findall(r"^readonly SIGNIN_CREDENTIAL_SOURCE=(\S+)$", text, re.M)

    assert key == PAGES_KEY


def test_no_value_is_on_a_command_line_or_in_the_output(tmp_path: Path) -> None:
    run = staff_run(tmp_path, "up")
    (secret,) = signin_documents(run)

    assert run.process.returncode == 0, run.output
    for value in every_value(run, secret):
        assert value not in run.output
        assert all(value not in call for call in run.calls)


@pytest.mark.parametrize("function", ["publish_signin_secret", "ensure_signin_secret"])
def test_the_two_functions_that_handle_the_values_turn_tracing_off_first(
    function: str,
) -> None:
    text = (KIND_DIR / "identity.sh").read_text(encoding="utf-8")
    (body,) = re.findall(rf"^{function}\(\) \{{\n(.*?)^\}}", text, re.M | re.S)

    assert body.startswith("  { set +x; } 2>/dev/null\n")


def test_the_cookie_key_reaches_jq_on_a_descriptor_and_never_gets_a_name() -> None:
    text = (KIND_DIR / "identity.sh").read_text(encoding="utf-8")

    body = text.split("publish_signin_secret() {")[1].split("\n}\n")[0]

    assert "--rawfile session <(openssl rand -base64 32)" in body
    assert "$(openssl" not in body


def test_the_log_names_the_secret_and_its_generation_and_prints_no_value(
    tmp_path: Path,
) -> None:
    run = staff_run(tmp_path, "up")
    (secret,) = signin_documents(run)
    generation = secret["metadata"]["annotations"][GENERATION_NOTE]

    assert SIGNIN_SECRET in run.output
    assert generation in run.output


# ── a second run, a rotation, an interrupted pair ───────────────────────────


def test_a_second_run_keeps_the_secret_of_the_same_generation(tmp_path: Path) -> None:
    first = staff_run(tmp_path, "up")
    (made,) = signin_documents(first)

    run = staff_run(tmp_path, "up")

    assert run.process.returncode == 0, run.output
    assert signin_documents(run) == []
    assert "kept" in run.output and SIGNIN_SECRET in run.output
    # What the cluster holds is what the first run made: its generation is read
    # from the annotations alone, never from the data.
    reads = [c for c in run.calls if SIGNIN_SECRET in c and " get " in c]
    assert reads and all("jsonpath" in c or "-o name" in c for c in reads)
    assert not any("{.data" in c and "credentials" in c for c in run.calls)
    assert not any("{.data" in c and SIGNIN_SECRET in c for c in run.calls)
    notes = json.loads((run.stub / f"notes-{SIGNIN_SECRET}").read_text())
    assert notes == made["metadata"]["annotations"]


def test_a_secret_of_another_generation_is_made_anew_with_a_new_key(
    tmp_path: Path,
) -> None:
    first = staff_run(tmp_path, "up")
    (before,) = signin_documents(first)
    (tmp_path / "stub" / f"notes-{SIGNIN_SECRET}").write_text(
        json.dumps({GENERATION_NOTE: "0" * 16})
    )

    run = staff_run(tmp_path, "up")

    assert run.process.returncode == 0, run.output
    (after,) = signin_documents(run)
    assert after["metadata"]["annotations"] == before["metadata"]["annotations"]
    assert decoded(after, "session-key") != decoded(before, "session-key")
    assert "anew" in run.output


def test_a_rotation_makes_the_secret_anew_from_the_new_credentials(
    tmp_path: Path,
) -> None:
    first = staff_run(tmp_path, "up")
    (before,) = signin_documents(first)

    run = staff_run(tmp_path, "up", rotate="1")

    assert run.process.returncode == 0, run.output
    (after,) = signin_documents(run)
    assert (
        after["metadata"]["annotations"][GENERATION_NOTE]
        != before["metadata"]["annotations"][GENERATION_NOTE]
    )
    pages = dict(
        line.split("=", 1) for line in run.seen(CREDENTIALS_SECRET).splitlines() if "="
    )[PAGES_KEY]
    assert decoded(after, "client-credential") == pages
    assert decoded(after, "client-credential") != decoded(before, "client-credential")
    for value in every_value(run, after):
        assert value not in run.output


def test_a_secret_with_no_generation_is_made_anew(tmp_path: Path) -> None:
    staff_run(tmp_path, "up")
    (tmp_path / "stub" / f"notes-{SIGNIN_SECRET}").write_text("{}")

    run = staff_run(tmp_path, "up")

    assert run.process.returncode == 0, run.output
    assert len(signin_documents(run)) == 1


# ── what it refuses, and what it leaves ──────────────────────────────────────


def test_without_the_switch_nothing_of_it_is_read_or_made(tmp_path: Path) -> None:
    staff_run(tmp_path, "status")  # lays the stand-in out
    run = run_script(tmp_path, "up", env={"MERIDIAN_SIGNIN": ""})

    assert run.process.returncode == 0, run.output
    assert signin_documents(run) == []
    assert not [c for c in run.calls if SIGNIN_SECRET in c]


def test_the_switch_off_is_the_switch_empty(tmp_path: Path) -> None:
    staff_run(tmp_path, "status")
    run = run_script(tmp_path, "up", env={"MERIDIAN_SIGNIN": "off"})

    assert run.process.returncode == 0, run.output
    assert not [c for c in run.calls if SIGNIN_SECRET in c]


@pytest.mark.parametrize("value", ["on", "Staff", "keycloak", "1", "claimant"])
def test_another_word_is_a_usage_line_and_asks_the_cluster_nothing(
    tmp_path: Path, value: str
) -> None:
    run = staff_run(tmp_path, "up", env={"MERIDIAN_SIGNIN": value})

    assert run.process.returncode != 0
    assert "MERIDIAN_SIGNIN must be" in run.output
    assert run.calls == []


def test_the_secret_is_made_only_after_the_last_check_has_passed(
    tmp_path: Path,
) -> None:
    run = staff_run(tmp_path, "up", kilobytes=2_000_000)

    assert run.process.returncode != 0
    assert run.changes() == [] and run.applied() == []
    assert not [c for c in run.calls if SIGNIN_SECRET in c]


def test_a_credentials_secret_that_cannot_be_read_stops_the_run_and_makes_nothing(
    tmp_path: Path,
) -> None:
    run = staff_run(tmp_path, "up", env={"STUB_CREDENTIALS_JSON_FAIL": "1"})

    assert run.process.returncode != 0
    assert signin_documents(run) == []
    assert f"could not make the Secret {SIGNIN_SECRET}" in run.output
    assert "partly made" in run.output  # the trap says so: the first change was made


def test_an_apply_that_fails_stops_the_run_with_a_sentence_and_no_value(
    tmp_path: Path,
) -> None:
    run = staff_run(tmp_path, "up", fail="-n meridian apply")

    assert run.process.returncode != 0
    assert f"could not make the Secret {SIGNIN_SECRET}" in run.output
    assert "partly made" in run.output
    for value in secret_values(run):
        assert value not in run.output


def test_a_credentials_secret_without_the_pages_key_makes_no_secret(
    tmp_path: Path,
) -> None:
    staff_run(tmp_path, "up")
    cluster = tmp_path / "stub" / "cluster-keycloak-credentials"
    cluster.write_text(
        "\n".join(
            line
            for line in cluster.read_text().splitlines()
            if not line.startswith(PAGES_KEY)
        )
        + "\n"
    )
    (tmp_path / "stub" / f"notes-{SIGNIN_SECRET}").write_text("{}")

    run = staff_run(tmp_path, "up")

    assert run.process.returncode != 0
    assert signin_documents(run) == []
    assert "pages client" in run.output


# ── status ───────────────────────────────────────────────────────────────────


def test_status_lists_the_secret_by_name_and_reads_nothing_of_it(
    tmp_path: Path,
) -> None:
    staff_run(tmp_path, "up")

    run = staff_run(tmp_path, "status", namespace=True, realm_secret=True)

    assert run.process.returncode == 0, run.output
    assert f"Secret {SIGNIN_SECRET}" in run.output
    assert "present" in run.output.split(f"Secret {SIGNIN_SECRET}")[1].splitlines()[0]
    reads = [c for c in run.calls if SIGNIN_SECRET in c]
    assert reads and all(c.endswith("-o name --ignore-not-found") for c in reads)
    assert run.changes() == []


def test_make_up_with_staff_and_no_add_on_says_the_secret_was_not_made(
    tmp_path: Path,
) -> None:
    run = staff_run(tmp_path, "note", identity="")

    assert run.process.returncode == 0, run.output
    assert "MERIDIAN_SIGNIN is staff but MERIDIAN_IDENTITY is off" in run.output
    assert f"the Secret {SIGNIN_SECRET} was not made" in run.output
    assert run.changes() == []


def test_the_note_without_the_switch_is_what_it_was(tmp_path: Path) -> None:
    staff_run(tmp_path, "status")
    run = run_script(tmp_path, "note", identity="", env={"MERIDIAN_SIGNIN": ""})

    assert "MERIDIAN_SIGNIN" not in run.output
    assert len(run.calls) == 1 and "get namespace identity" in run.calls[0]


def test_status_says_absent_when_the_secret_is_not_there(tmp_path: Path) -> None:
    run = staff_run(tmp_path, "status", namespace=True, realm_secret=True)

    assert run.process.returncode == 0, run.output
    assert "absent" in run.output.split(f"Secret {SIGNIN_SECRET}")[1].splitlines()[0]
