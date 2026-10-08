"""infra/kind/identity.sh ``users`` and ``passwords``: the cast and its logins (Y2e).

The same real script and stand-in ``kubectl`` as ``test_kind_identity_script.py``
(its ``run_script``). ``up`` runs first, so the stand-in holds the realm Secret the
real generator made; most tests then replace its content with a realm file of
MADE-UP markers, so that every value looked for is one this file wrote and none is
a real password. ``passwords`` is the only code path that prints a password: the
tests below run every other command against the same Secret and look for the
markers in all they print, and in every call made to the cluster's tool.

Not shown: that the commands work against a real cluster (the main session runs
them there).
"""

import json
import re
from pathlib import Path

import pytest
from kindsupport import KIND_DIR, requires_jq
from test_kind_identity_script import (
    CREDENTIALS_SECRET,
    Run,
    run_script,
    secret_values,
)

pytestmark = requires_jq

ROLE_OF = {
    "aino.lindqvist": "platform-admin",
    "soren.halvorsen": "agent-developer",
    "ingrid.strand": "adjuster",
    "mikkel.vang": "adjuster",
    "freja.dahl": "adjuster",
    "henrik.eide": "auditor",
    "linnea.berg": None,
}
GROUP_OF_ROLE = {
    "platform-admin": "meridian-platform-admins",
    "agent-developer": "meridian-agent-developers",
    "adjuster": "meridian-adjusters",
    "auditor": "meridian-auditors",
}
DISPLAY_OF = {
    "aino.lindqvist": "Aino Lindqvist",
    "soren.halvorsen": "Soren Halvorsen",
    "ingrid.strand": "Ingrid Strand",
    "mikkel.vang": "Mikkel Vang",
    "freja.dahl": "Freja Dahl",
    "henrik.eide": "Henrik Eide",
    "linnea.berg": "Linnea Berg",
}
SHOW = {"MERIDIAN_IDENTITY_SHOW": "1"}
# Made-up values: nothing here was ever a password of anything.
USER_MARKERS = {name: f"made-up-user-marker-{i}" for i, name in enumerate(ROLE_OF)}
CLIENT_MARKERS = ["made-up-client-marker-web", "made-up-client-marker-scripts"]
ACCOUNT_MARKER = "made-up-account-marker"
ALL_MARKERS = [*USER_MARKERS.values(), *CLIENT_MARKERS, ACCOUNT_MARKER]


def group_of(name: str) -> str:
    role = ROLE_OF.get(name)
    return GROUP_OF_ROLE[role] if role else "(none)"


def marker_realm(
    users: dict[str, str] | None = None, *, direct_roles: bool = False
) -> str:
    """A realm file of the shape the generator writes, holding markers. With
    ``direct_roles`` the shape of an older run: roles on the users, no groups."""
    people = users if users is not None else USER_MARKERS
    role_fields = {
        name: (
            {"realmRoles": [ROLE_OF[name]] if ROLE_OF.get(name) else []}
            if direct_roles
            else {"groups": [f"/{group_of(name)}"] if ROLE_OF.get(name) else []}
        )
        for name in people
    }
    realm = {
        "realm": "meridian-staff",
        "groups": []
        if direct_roles
        else [{"name": g, "realmRoles": [r]} for r, g in GROUP_OF_ROLE.items()],
        "users": [
            {
                "username": name,
                "firstName": DISPLAY_OF.get(name, "Some Person").split()[0],
                "lastName": DISPLAY_OF.get(name, "Some Person").split()[1],
                **role_fields[name],
                "credentials": [{"type": "password", "value": marker}],
            }
            for name, marker in people.items()
        ]
        + [
            {
                "username": "service-account-meridian-scripts",
                "serviceAccountClientId": "meridian-scripts",
                "realmRoles": ["adjuster", "auditor"],
                "credentials": [{"type": "password", "value": ACCOUNT_MARKER}],
            }
        ],
        "clients": [
            {"clientId": "meridian-claims-web", "secret": CLIENT_MARKERS[0]},
            {"clientId": "meridian-scripts", "secret": CLIENT_MARKERS[1]},
        ],
    }
    return json.dumps(realm)


def prepared(tmp_path: Path, realm: str | None = None) -> Path:
    """A directory where ``up`` has run (so the stand-in holds both Secrets), then,
    unless ``realm`` is None, the realm Secret replaced by that text."""
    first = run_script(tmp_path, "up")
    assert first.process.returncode == 0, first.output
    if realm is not None:
        (tmp_path / "stub" / "cluster-keycloak-realm").write_text(realm)
    return tmp_path


def parse_rows(text: str) -> list[list[str]]:
    """The lines after the first, split at two spaces or more."""
    lines = [line for line in text.splitlines() if line.strip()]
    return [re.split(r"\s{2,}", line.strip()) for line in lines[1:]]


def reads_nothing_secret_on_a_command_line(run: Run) -> bool:
    return not any(marker in call for call in run.calls for marker in ALL_MARKERS)


# ── users: the cast, read-only ───────────────────────────────────────────────


def test_users_prints_the_cast_as_user_name_display_name_group_and_role(
    tmp_path: Path,
) -> None:
    prepared(tmp_path, marker_realm())

    run = run_script(tmp_path, "users")

    assert run.process.returncode == 0, run.output
    rows = parse_rows(run.process.stdout)
    assert rows == [
        [name, DISPLAY_OF[name], group_of(name), ROLE_OF[name] or "(none)"]
        for name in ROLE_OF
    ]
    header = run.process.stdout.splitlines()[0].lower()
    assert header.index("user name") < header.index("group") < header.index("role")


def test_users_shows_the_role_a_group_gives_and_a_role_held_directly(
    tmp_path: Path,
) -> None:
    # An older run's Secret has roles on its users and no groups: shown as they
    # are, with no group.
    prepared(tmp_path, marker_realm(direct_roles=True))

    run = run_script(tmp_path, "users")

    assert run.process.returncode == 0, run.output
    assert [(r[0], r[2], r[3]) for r in parse_rows(run.process.stdout)] == [
        (name, "(none)", ROLE_OF[name] or "(none)") for name in ROLE_OF
    ]


def test_users_prints_no_secret_and_no_service_account_and_changes_nothing(
    tmp_path: Path,
) -> None:
    prepared(tmp_path, marker_realm())

    run = run_script(tmp_path, "users")

    assert run.process.returncode == 0, run.output
    for marker in ALL_MARKERS:
        assert marker not in run.output
    assert "service-account" not in run.output
    assert run.changes() == []
    assert reads_nothing_secret_on_a_command_line(run)


def test_users_shows_what_the_cluster_holds_not_the_scripts_own_list(
    tmp_path: Path,
) -> None:
    # An older run's Secret is kept by `up`: the cluster may still hold other
    # people, and the command must not print the list of the script instead.
    prepared(tmp_path, marker_realm({"linnea.berg": "made-up-user-marker-9"}))

    run = run_script(tmp_path, "users")

    assert [row[0] for row in parse_rows(run.process.stdout)] == ["linnea.berg"]


def test_users_on_the_generators_own_realm_shows_the_seven(tmp_path: Path) -> None:
    prepared(tmp_path)

    run = run_script(tmp_path, "users")

    assert run.process.returncode == 0, run.output
    assert [(r[0], r[2], r[3]) for r in parse_rows(run.process.stdout)] == [
        (name, group_of(name), ROLE_OF[name] or "(none)") for name in ROLE_OF
    ]
    for value in secret_values(run):
        assert value not in run.output


def test_users_says_plainly_when_the_add_on_is_not_on_the_cluster(
    tmp_path: Path,
) -> None:
    run = run_script(tmp_path, "users")

    assert run.process.returncode != 0
    assert "not" in run.process.stderr and "keycloak-realm" in run.process.stderr
    assert "MERIDIAN_IDENTITY=keycloak" in run.process.stderr
    assert run.process.stdout == ""
    assert run.changes() == []


def test_users_stops_at_a_cluster_that_does_not_answer(tmp_path: Path) -> None:
    run = run_script(tmp_path, "users", fail="get nodes")

    assert run.process.returncode != 0
    assert run.process.stdout == ""


def test_users_says_so_when_the_realm_holds_no_people(tmp_path: Path) -> None:
    prepared(tmp_path, marker_realm({}))

    run = run_script(tmp_path, "users")

    assert run.process.returncode != 0
    assert "no users" in run.process.stderr


# ── passwords: the one way to read them ──────────────────────────────────────


def test_passwords_prints_a_notice_then_each_user_name_and_password_and_nothing_else(
    tmp_path: Path,
) -> None:
    prepared(tmp_path, marker_realm())

    run = run_script(tmp_path, "passwords", env=SHOW)

    assert run.process.returncode == 0, run.output
    lines = run.process.stdout.splitlines()
    notice = lines[0].lower()
    assert "disposable" in notice and "test passwords" in notice
    assert "local" in notice and "mock issuer" in notice
    assert "this cluster" in notice
    assert "MERIDIAN_IDENTITY_ROTATE=1" in lines[0] and "replace" in notice
    assert parse_rows(run.process.stdout) == [
        [name, marker] for name, marker in USER_MARKERS.items()
    ]
    # Not a client's secret, not the service account's password.
    for marker in (*CLIENT_MARKERS, ACCOUNT_MARKER):
        assert marker not in run.output
    assert run.process.stderr == ""
    assert run.changes() == []
    assert reads_nothing_secret_on_a_command_line(run)


def test_passwords_of_the_generators_realm_are_its_seven_and_no_client_secret(
    tmp_path: Path,
) -> None:
    prepared(tmp_path)

    run = run_script(tmp_path, "passwords", env=SHOW)

    assert run.process.returncode == 0, run.output
    lines = {
        key: value
        for key, _, value in (
            line.partition("=")
            for line in run.seen(CREDENTIALS_SECRET).splitlines()
            if "=" in line
        )
    }
    passwords = {k: v for k, v in lines.items() if "_USER_" in k}
    client_secrets = [v for k, v in lines.items() if "_CLIENT_" in k]
    shown = dict(parse_rows(run.process.stdout))
    assert len(passwords) == 7 and len(client_secrets) == 2
    assert sorted(shown.values()) == sorted(passwords.values())
    for name, value in shown.items():
        key = f"MERIDIAN_STAFF_USER_{name.upper().replace('.', '_')}_PASSWORD"
        assert lines[key] == value
    for value in client_secrets:
        assert value not in run.output


def test_passwords_is_given_on_a_terminal_without_the_variable(tmp_path: Path) -> None:
    prepared(tmp_path, marker_realm())

    run = run_script(tmp_path, "passwords", terminal=True)

    assert run.process.returncode == 0, run.output
    assert parse_rows(run.process.stdout) == [
        [name, marker] for name, marker in USER_MARKERS.items()
    ]


def test_passwords_refuses_when_its_output_is_not_a_terminal_and_reads_nothing(
    tmp_path: Path,
) -> None:
    prepared(tmp_path, marker_realm())

    run = run_script(tmp_path, "passwords")

    assert run.process.returncode != 0
    assert run.process.stdout == ""
    assert "MERIDIAN_IDENTITY_SHOW=1" in run.process.stderr
    assert "terminal" in run.process.stderr
    for marker in ALL_MARKERS:
        assert marker not in run.output
    # It refused before it asked the cluster for anything.
    assert run.calls == []


@pytest.mark.parametrize("shown", ["yes", "0", "true"])
def test_passwords_accepts_only_empty_or_one_as_the_variable(
    tmp_path: Path, shown: str
) -> None:
    prepared(tmp_path, marker_realm())

    run = run_script(
        tmp_path, "passwords", env={"MERIDIAN_IDENTITY_SHOW": shown}, terminal=True
    )

    assert run.process.returncode != 0
    assert "MERIDIAN_IDENTITY_SHOW" in run.process.stderr
    assert run.process.stdout == ""
    assert run.calls == []


@pytest.mark.parametrize("terminal", [True, False])
def test_passwords_refuses_a_cluster_it_cannot_tell_is_the_local_one(
    tmp_path: Path, terminal: bool
) -> None:
    prepared(tmp_path, marker_realm())

    run = run_script(
        tmp_path,
        "passwords",
        env=SHOW,
        terminal=terminal,
        docker_host="tcp://192.0.2.1:2375",
    )

    assert run.process.returncode != 0
    assert run.process.stdout == ""
    assert "not local" in run.process.stderr
    for marker in ALL_MARKERS:
        assert marker not in run.output
    assert not any("get secret" in call for call in run.calls)


def test_passwords_stops_at_a_cluster_that_does_not_answer(tmp_path: Path) -> None:
    prepared(tmp_path, marker_realm())

    run = run_script(tmp_path, "passwords", env=SHOW, fail="get nodes")

    assert run.process.returncode != 0
    assert run.process.stdout == ""
    assert not any("get secret" in call for call in run.calls)


def test_passwords_says_plainly_when_the_add_on_is_not_on_the_cluster(
    tmp_path: Path,
) -> None:
    run = run_script(tmp_path, "passwords", env=SHOW)

    assert run.process.returncode != 0
    assert run.process.stdout == ""
    assert "keycloak-realm" in run.process.stderr


def test_passwords_stops_rather_than_print_a_user_with_no_password(
    tmp_path: Path,
) -> None:
    realm = json.loads(marker_realm())
    del realm["users"][2]["credentials"]
    prepared(tmp_path, json.dumps(realm))

    run = run_script(tmp_path, "passwords", env=SHOW)

    assert run.process.returncode != 0
    assert run.process.stdout == ""


# ── no other way to a password ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "arguments",
    [("up",), ("status",), ("note",), ("users",), ("bogus",), ()],
    ids=["up", "status", "note", "users", "bogus", "none"],
)
def test_no_other_command_prints_a_password_a_client_secret_or_a_realm(
    tmp_path: Path, arguments: tuple[str, ...]
) -> None:
    prepared(tmp_path, marker_realm())

    run = run_script(
        tmp_path,
        *arguments,
        namespace=True,
        deployment=True,
        env=SHOW,
        terminal=True,  # even where `passwords` would be given
    )

    for marker in ALL_MARKERS:
        assert marker not in run.output, arguments
    assert reads_nothing_secret_on_a_command_line(run)
    assert '"credentials"' not in run.output


def test_a_run_of_every_command_on_the_generators_own_realm_prints_none_of_its_values(
    tmp_path: Path,
) -> None:
    first = run_script(tmp_path, "up")
    values = secret_values(first)
    assert len(values) == 9

    outputs = [first.output]
    for arguments in (("up",), ("status",), ("note",), ("users",), ("bogus",)):
        run = run_script(tmp_path, *arguments, namespace=True, deployment=True)
        outputs.append(run.output)
        assert all(value not in call for call in run.calls for value in values)
    for value in values:
        assert all(value not in output for output in outputs)

    shown = run_script(tmp_path, "passwords", env=SHOW)
    assert sum(value in shown.output for value in values) == 7


def test_the_two_new_commands_trace_nothing_under_bash_x() -> None:
    text = (KIND_DIR / "identity.sh").read_text(encoding="utf-8")

    for name in ("passwords", "users", "read_cast"):
        body = text.split(f"\n{name}() {{\n", 1)[1]
        assert body.startswith("  { set +x; } 2>/dev/null"), name


def test_the_usage_line_lists_the_two_new_commands(tmp_path: Path) -> None:
    run = run_script(tmp_path, "bogus")

    assert run.process.returncode != 0
    assert "users" in run.process.stderr and "passwords" in run.process.stderr


# ── the make target ──────────────────────────────────────────────────────────


def test_make_has_one_target_for_the_passwords_next_to_the_grafana_one() -> None:
    text = (KIND_DIR.parent.parent / "Makefile").read_text(encoding="utf-8")
    lines = text.splitlines()

    index = lines.index("identity-passwords:")
    assert lines[index + 1] == "\t@infra/kind/identity.sh passwords"
    assert lines.index("grafana-password:") < index
    help_line = lines[index - 1]
    assert help_line.startswith("## identity-passwords ")
    assert "prints secrets" in help_line
    assert "terminal" in help_line and "MERIDIAN_IDENTITY_SHOW=1" in help_line
    phony = next(line for line in lines if line.startswith(".PHONY:"))
    assert "identity-passwords" in phony.split()
    assert text.count("identity.sh passwords") == 1
