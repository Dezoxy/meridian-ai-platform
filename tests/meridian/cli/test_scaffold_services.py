"""The workload scaffold's edit of ``services.yaml`` (S061).

The new agent joins the ``agents`` of the Agent Runtime's entry, so that the
runtime's identity may name it; the scaffold writes that one line and nothing
else, and never touches ``tenants.yaml``. Every test builds a small tree in
``tmp_path`` (the ``root`` fixture): a copy of this repository's
``pyproject.toml`` and registry and an empty workloads directory.
"""

import errno
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from meridian.platform.cli import app, scaffold
from meridian.platform.cli.scaffold import (
    PATH_OUTSIDE,
    REGISTRY_INVALID,
    ROLLBACK_FAILED,
    STALE_PLAN,
    WRITE_FAILED,
    ScaffoldError,
    ScaffoldWriteError,
    plan_workload,
    write_plan,
)
from meridian.platform.cli.scaffold_services import (
    SERVICES_AGENT_LISTED,
    SERVICES_AGENTS_MISSING,
    SERVICES_AGENTS_UNUSABLE,
    SERVICES_EDIT_UNVERIFIED,
    SERVICES_NOT_YAML,
    SERVICES_RUNTIME_MISSING,
    SERVICES_RUNTIME_TWICE,
    ServicesEditError,
    services_edit,
)
from meridian.platform.registry.loader import RegistryError, load_registry

runner = CliRunner()
NAME = "fraud-review"
SERVICES = "config/registry/services.yaml"
AGENTS = "config/registry/agents.yaml"
PYPROJECT = "pyproject.toml"
REGISTRY = "config/registry"
RUNTIME_ENTRY = "  - id: agent-runtime"
AGENTS_KEY = "    agents:"
RUNTIME_LINE = "    agents: [claims-triage]"
# The runtime's line in the committed file: a tenant lists claim-brief too, so a
# test that rewrites the line in a copy of the committed file keeps it listed.
COMMITTED_RUNTIME_LINE = "    agents: [claims-triage, claim-brief]"
RUNTIME_COMMENT = "  # the agent of the run it was asked for"
# The edit logic is tested on this text, not on the committed file, whose list
# changes whenever the runtime may name another agent. It holds what the edit
# reads: the runtime's entry with one agent and a comment on the edited line, one
# other service, and the file's own header comment.
SMALL_SERVICES = "\n".join(
    [
        "# the platform's services",
        "services:",
        "  - id: claims-api",
        "    description: Takes a claim.",
        "    calls: [agent-runtime]",
        "    tenants: [claims-triage]",
        "    agents: [claims-triage]",
        RUNTIME_ENTRY,
        "    description: Runs the agent graphs.",
        "    calls: [claims-mcp]",
        "    tenants: [claims-triage]",
        RUNTIME_LINE + RUNTIME_COMMENT,
        "  - id: claims-mcp",
        "    description: Claim notes.",
        "    calls: []",
        "    tenants: []",
        "    agents: []",
        "",
    ]
)
REPO = Path(__file__).resolve().parents[3]
LEAKED = "a-path-or-a-name-the-error-must-not-repeat"
EXIT_REFUSED = 2
EXIT_FAILED = 1


def snapshot(root: Path) -> dict[str, object]:
    """Every path under ``root``: a file's bytes, a symlink's target, a
    directory as ``None``, so that a stray directory or temp file shows."""
    found: dict[str, object] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            found[relative] = ("link", os.readlink(path))
        elif path.is_dir():
            found[relative] = None
        else:
            found[relative] = path.read_bytes()
    return found


def edit(path: Path, change: Callable[[str], str]) -> None:
    text = path.read_bytes().decode("utf-8")
    path.write_bytes(change(text).encode("utf-8"))


def read(root: Path, relative: str = SERVICES) -> str:
    return (root / relative).read_bytes().decode("utf-8")


def runtime_agents(root: Path) -> tuple[str, ...]:
    """The agents the registry in ``root`` lets the runtime's identity name."""
    runtime = load_registry(root / REGISTRY).service("agent-runtime")
    assert runtime is not None
    return runtime.agents


def runtime_line_index(text: str) -> int:
    """The 0-based index of the line of the runtime's ``agents``."""
    lines = text.split("\n")
    start = lines.index(RUNTIME_ENTRY)
    return next(i for i in range(start, len(lines)) if lines[i].startswith(AGENTS_KEY))


def with_runtime_agents(text: str, replacement: str | None) -> str:
    """``text`` with the runtime's ``agents`` line replaced, or removed."""
    lines = text.split("\n")
    index = runtime_line_index(text)
    lines[index : index + 1] = [] if replacement is None else [replacement]
    return "\n".join(lines)


def new_workload(root: Path, name: str = NAME) -> Any:
    return runner.invoke(app, ["workload", "new", name, "--root", str(root)])


def validate(root: Path) -> Any:
    directory = str(root / REGISTRY)
    return runner.invoke(app, ["registry", "validate", "--registry-dir", directory])


def list_in_a_tenant(root: Path) -> None:
    old = "agents: [claims-triage, knowledge-ingestion, claim-brief]"
    edit(
        root / "config/registry/tenants.yaml",
        lambda text: text.replace(old, old.replace("]", f", {NAME}]")),
    )


def test_the_runtime_names_the_new_agent_and_one_line_of_services_yaml_changed(
    root: Path,
) -> None:
    # Arrange
    old = read(root)
    before = runtime_agents(root)

    # Act
    result = new_workload(root)

    # Assert
    assert result.exit_code == 0, result.stderr
    assert "changed config/registry/services.yaml" in result.stdout.splitlines()
    old_lines, new_lines = old.split("\n"), read(root).split("\n")
    assert len(new_lines) == len(old_lines)
    pairs = enumerate(zip(old_lines, new_lines, strict=True))
    differing = [i for i, (a, b) in pairs if a != b]
    assert differing == [runtime_line_index(old)]
    assert new_lines[differing[0]] == f"    agents: [{', '.join((*before, NAME))}]"
    assert runtime_agents(root) == (*before, NAME)
    assert validate(root).exit_code == 0


def test_listing_the_agent_in_a_tenant_by_hand_validates_after_the_scaffold(
    root: Path,
) -> None:
    new_workload(root)

    list_in_a_tenant(root)

    validated = validate(root)
    assert validated.exit_code == 0, validated.stderr
    tenant = load_registry(root / REGISTRY).tenant("claims-triage")
    assert tenant is not None
    assert NAME in tenant.agents


def test_the_same_hand_edit_fails_with_the_rules_message_without_the_write(
    root: Path,
) -> None:
    old = read(root)
    new_workload(root)
    (root / SERVICES).write_bytes(old.encode("utf-8"))
    list_in_a_tenant(root)

    with pytest.raises(RegistryError) as refused:
        load_registry(root / REGISTRY)

    assert (
        "services.yaml: services[1].agents: graph agent "
        f"{NAME!r} is listed by a tenant, so add it to the agents of "
        "'agent-runtime': without it every call for the agent is refused"
    ) in refused.value.errors
    assert validate(root).exit_code != 0


def test_a_second_workload_is_appended_after_the_first(root: Path) -> None:
    before = runtime_agents(root)
    new_workload(root)

    result = new_workload(root, "claim-audit")

    assert result.exit_code == 0, result.stderr
    assert runtime_agents(root) == (*before, NAME, "claim-audit")


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (RUNTIME_LINE, f"    agents: [claims-triage, {NAME}]"),
        ("    agents: []", f"    agents: [{NAME}]"),
        ("    agents: [ claims-triage ]", f"    agents: [ claims-triage, {NAME} ]"),
        ("    agents: [claims-triage, ]", f"    agents: [claims-triage, {NAME}, ]"),
        ('    agents: ["claims-triage"]', f'    agents: ["claims-triage", {NAME}]'),
        (
            "    agents: [claims-triage]  # see [the note]",
            f"    agents: [claims-triage, {NAME}]  # see [the note]",
        ),
    ],
)
def test_the_name_goes_inside_the_brackets_and_a_trailing_comment_survives(
    line: str, expected: str
) -> None:
    old = with_runtime_agents(SMALL_SERVICES, line)

    new = services_edit(old, NAME)

    assert new == with_runtime_agents(old, expected)


def test_a_comment_on_the_runtime_line_survives_the_whole_command(root: Path) -> None:
    edit(
        root / SERVICES,
        lambda text: with_runtime_agents(text, COMMITTED_RUNTIME_LINE + "  # kept"),
    )

    result = new_workload(root)

    assert result.exit_code == 0, result.stderr
    kept = f"    agents: [claims-triage, claim-brief, {NAME}]  # kept"
    assert kept in read(root).split("\n")


def test_a_file_with_crlf_line_endings_keeps_every_other_line_as_it_was() -> None:
    old = SMALL_SERVICES.replace("\n", "\r\n")

    new = services_edit(old, NAME)

    old_lines, new_lines = old.split("\r\n"), new.split("\r\n")
    assert len(new_lines) == len(old_lines)
    changed = [a for a, b in zip(old_lines, new_lines, strict=True) if a != b]
    assert changed == [RUNTIME_LINE + RUNTIME_COMMENT]
    assert f"    agents: [claims-triage, {NAME}]{RUNTIME_COMMENT}" in new_lines


def test_the_edit_of_the_committed_file_is_the_old_parse_plus_the_name() -> None:
    old = (REPO / SERVICES).read_text(encoding="utf-8")

    new = services_edit(old, NAME)

    expected = yaml.safe_load(old)
    runtime = next(s for s in expected["services"] if s["id"] == "agent-runtime")
    runtime["agents"].append(NAME)
    assert yaml.safe_load(new) == expected


def twice(text: str) -> str:
    copy = [RUNTIME_ENTRY, "    description: a copy", "    calls: []"]
    copy += ["    tenants: []", "    agents: []", ""]
    return text + "\n".join(copy)


def refusal_cases(text: str) -> list[tuple[str, str, str]]:
    """(id, services text, the refusal's text) for each text the edit refuses."""
    lines = text.split("\n")
    entry = lines.index(RUNTIME_ENTRY) + 1
    agents = runtime_line_index(text) + 1
    return [
        (
            "no entry",
            text.replace(RUNTIME_ENTRY, RUNTIME_ENTRY + "-copy"),
            SERVICES_RUNTIME_MISSING,
        ),
        (
            "two entries",
            twice(text),
            SERVICES_RUNTIME_TWICE.format(f"{entry} and {len(lines)}"),
        ),
        (
            "no agents key",
            with_runtime_agents(text, None),
            SERVICES_AGENTS_MISSING.format(entry),
        ),
        (
            "block style",
            with_runtime_agents(text, "    agents:\n      - claims-triage"),
            SERVICES_AGENTS_UNUSABLE.format(agents),
        ),
        (
            "a list over two lines",
            with_runtime_agents(text, "    agents: [claims-triage,\n      other]"),
            SERVICES_AGENTS_UNUSABLE.format(agents),
        ),
        (
            "a closing bracket on the next line",
            with_runtime_agents(text, "    agents: [claims-triage\n    ]"),
            SERVICES_AGENTS_UNUSABLE.format(agents),
        ),
        (
            "a text, not a list",
            with_runtime_agents(text, "    agents: claims-triage"),
            SERVICES_AGENTS_UNUSABLE.format(agents),
        ),
        (
            "an empty value",
            with_runtime_agents(text, "    agents:"),
            SERVICES_AGENTS_UNUSABLE.format(agents),
        ),
        (
            "already listed",
            with_runtime_agents(text, f"    agents: [claims-triage, {NAME}]"),
            SERVICES_AGENT_LISTED.format(agents),
        ),
        (
            "already listed in quotes",
            with_runtime_agents(text, f'    agents: ["{NAME}"]'),
            SERVICES_AGENT_LISTED.format(agents),
        ),
        (
            "two agents keys",
            with_runtime_agents(text, RUNTIME_LINE + "\n" + RUNTIME_LINE),
            SERVICES_EDIT_UNVERIFIED.format(agents),
        ),
        ("not yaml", "services: [", SERVICES_NOT_YAML),
        ("not a mapping", "- a\n- b\n", SERVICES_RUNTIME_MISSING),
        ("no services key", "other: []\n", SERVICES_RUNTIME_MISSING),
    ]


@pytest.mark.parametrize(
    ("text", "message"),
    [pytest.param(t, m, id=i) for i, t, m in refusal_cases(SMALL_SERVICES)],
)
def test_the_edit_refuses_what_it_cannot_extend_with_the_line_and_never_the_name(
    text: str, message: str
) -> None:
    with pytest.raises(ServicesEditError) as refused:
        services_edit(text, NAME)

    assert str(refused.value) == message
    assert NAME not in message
    assert NAME.replace("-", "_") not in message


def block_style(root: Path) -> None:
    edit(
        root / SERVICES,
        lambda text: with_runtime_agents(
            text, "    agents:\n      - claims-triage\n      - claim-brief"
        ),
    )


def over_two_lines(root: Path) -> None:
    edit(
        root / SERVICES,
        lambda text: with_runtime_agents(
            text, "    agents: [claims-triage, claim-brief\n    ]"
        ),
    )


@pytest.mark.parametrize("arrange", [block_style, over_two_lines])
def test_a_runtime_list_the_edit_cannot_extend_is_refused_and_nothing_is_written(
    root: Path, arrange: Callable[[Path], None]
) -> None:
    arrange(root)
    before = snapshot(root)
    line = runtime_line_index(read(root)) + 1

    result = new_workload(root)

    assert result.exit_code == EXIT_REFUSED
    assert result.stdout == ""
    assert result.stderr == "ERROR " + SERVICES_AGENTS_UNUSABLE.format(line) + "\n"
    assert NAME not in result.stderr
    assert snapshot(root) == before


def entry_agents_index(text: str, entry: str) -> int:
    """The 0-based index of the line of the ``agents`` of the entry ``entry``."""
    lines = text.split("\n")
    start = lines.index(entry)
    return next(i for i in range(start, len(lines)) if lines[i].startswith(AGENTS_KEY))


def share_the_runtimes_agents_with_another_service(text: str) -> str:
    """``text`` where the runtime's ``agents`` is an anchor and the model gateway's
    ``agents`` an alias of it: appending to the one list changes both."""
    lines = text.split("\n")
    runtime = runtime_line_index(text)
    lines[runtime] = lines[runtime].replace("agents:", "agents: &shared", 1)
    lines[entry_agents_index(text, "  - id: model-gateway")] = "    agents: *shared"
    return "\n".join(lines)


def merge_an_entry_into_the_runtime(text: str) -> str:
    """``text`` where the Claims API's entry (it comes first: an anchor is defined
    before its alias) is an anchor and the runtime's entry merges it with
    ``<<: *rt`` after its own ``agents``."""
    lines = text.split("\n")
    lines[lines.index("  - id: claims-api")] = "  - &rt\n    id: claims-api"
    runtime = runtime_line_index(text)
    lines[runtime] += "\n    <<: *rt"
    return "\n".join(lines)


@pytest.mark.parametrize(
    "arrange",
    [share_the_runtimes_agents_with_another_service, merge_an_entry_into_the_runtime],
    ids=["a shared list", "a merged entry"],
)
def test_an_anchor_or_alias_in_services_yaml_is_refused_and_nothing_is_written(
    root: Path, arrange: Callable[[str], str]
) -> None:
    edit(root / SERVICES, arrange)
    before = snapshot(root)

    result = new_workload(root)

    assert result.exit_code == EXIT_REFUSED
    assert result.stdout == ""
    first, *details = result.stderr.splitlines()
    assert first == "ERROR " + REGISTRY_INVALID
    assert any("anchors and aliases are not allowed" in line for line in details)
    assert NAME not in result.stderr
    assert snapshot(root) == before


def test_the_registry_copy_that_is_validated_holds_both_changed_files(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staged: list[dict[str, str]] = []
    real = scaffold._registry_with

    def spy(directory: Path, texts: dict[str, str]) -> None:
        staged.append(dict(texts))
        real(directory, texts)

    monkeypatch.setattr(scaffold, "_registry_with", spy)

    plan = plan_workload(root, NAME)

    assert len(staged) == 1
    assert staged[0] == {
        "agents.yaml": plan.changed[AGENTS],
        "services.yaml": plan.changed[SERVICES],
    }


def test_a_registry_that_does_not_validate_with_both_edits_is_refused_unwritten(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def invalid(directory: Path, texts: dict[str, str]) -> None:
        # What `registry validate` would say of an agents file whose agent the
        # runtime lists but which does not exist.
        if "services.yaml" in texts and NAME in texts["services.yaml"]:
            raise RegistryError(["services.yaml: services[1].agents[1]: unknown agent"])

    monkeypatch.setattr(scaffold, "_registry_with", invalid)
    before = snapshot(root)

    result = new_workload(root)

    assert result.exit_code == EXIT_REFUSED
    assert result.stderr == (
        "ERROR the registry with the new agent does not validate\n"
        "ERROR the registry: services.yaml: services[1].agents[1]: unknown agent\n"
    )
    assert snapshot(root) == before


def test_the_plan_records_the_hash_of_services_yaml_and_the_write_refuses_a_change(
    root: Path,
) -> None:
    plan = plan_workload(root, NAME)
    # The runtime's line changed after the plan: the plan is not what it was made from.
    edit(root / SERVICES, lambda text: with_runtime_agents(text, "    agents: []"))
    before = snapshot(root)

    with pytest.raises(ScaffoldError) as refused:
        write_plan(root, plan)

    assert SERVICES in plan.base
    assert str(refused.value) == STALE_PLAN
    assert "services.yaml" in STALE_PLAN
    assert not isinstance(refused.value, ScaffoldWriteError)
    assert snapshot(root) == before


def test_a_services_yaml_that_is_a_symbolic_link_is_refused(root: Path) -> None:
    (root / "elsewhere.yaml").write_bytes((root / SERVICES).read_bytes())
    (root / SERVICES).unlink()
    (root / SERVICES).symlink_to(root / "elsewhere.yaml")
    before = snapshot(root)

    with pytest.raises(ScaffoldError) as refused:
        plan_workload(root, NAME)

    assert str(refused.value) == PATH_OUTSIDE
    assert snapshot(root) == before


def replace_spy(
    monkeypatch: pytest.MonkeyPatch, target: str | None, done: list[str]
) -> list[str]:
    """Make ``os.replace`` of ``target`` fail; ``done`` collects the names of the
    files replaced before it fails. Returns what had been replaced at the failure."""
    real = os.replace
    at_failure: list[str] = []

    def replace(source: Any, destination: Any, *args: Any, **kwargs: Any) -> None:
        name = Path(destination).name
        if name == target and not at_failure:
            at_failure.extend(done)
            raise PermissionError(errno.EACCES, LEAKED, str(destination))
        real(source, destination, *args, **kwargs)
        done.append(name)

    monkeypatch.setattr(os, "replace", replace)
    return at_failure


@pytest.mark.parametrize(
    ("target", "replaced_first"),
    [
        ("agents.yaml", []),
        ("services.yaml", ["agents.yaml"]),
        ("pyproject.toml", ["agents.yaml", "services.yaml"]),
    ],
)
def test_a_write_that_fails_at_each_file_leaves_all_three_as_they_were(
    root: Path, monkeypatch: pytest.MonkeyPatch, target: str, replaced_first: list[str]
) -> None:
    plan = plan_workload(root, NAME)
    before = snapshot(root)
    at_failure = replace_spy(monkeypatch, target, [])

    with pytest.raises(ScaffoldWriteError) as refused:
        write_plan(root, plan)

    # The failure came at the position under test: that many files were replaced.
    assert at_failure == replaced_first
    assert str(refused.value) == WRITE_FAILED.format(
        f"PermissionError: {os.strerror(errno.EACCES)}"
    )
    assert refused.value.details == ()
    assert snapshot(root) == before


def test_no_half_written_state_lets_the_runtime_name_an_agent_that_does_not_exist(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = plan_workload(root, NAME)
    real = os.replace
    runtime_lists_it: list[bool] = []

    def replace(source: Any, destination: Any, *args: Any, **kwargs: Any) -> None:
        real(source, destination, *args, **kwargs)
        if Path(destination).name in {"agents.yaml", "services.yaml"}:
            # Raises RegistryError when the registry does not validate.
            registry = load_registry(root / REGISTRY)
            runtime = registry.service("agent-runtime")
            assert runtime is not None
            runtime_lists_it.append(NAME in runtime.agents)

    monkeypatch.setattr(os, "replace", replace)

    write_plan(root, plan)

    # After the agents file the runtime does not list the agent yet; after the
    # services file it does, and the agent exists.
    assert runtime_lists_it == [False, True]


def test_a_rollback_that_cannot_restore_services_yaml_leaves_the_agent_in_place(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = plan_workload(root, NAME)
    real = os.replace
    services_replacements: list[Any] = []

    def replace(source: Any, destination: Any, *args: Any, **kwargs: Any) -> None:
        # pyproject.toml fails, and so does putting services.yaml back.
        if Path(destination).name == "services.yaml":
            services_replacements.append(source)
        if Path(destination).name == "pyproject.toml" or len(services_replacements) > 1:
            raise PermissionError(errno.EACCES, LEAKED)
        real(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)

    with pytest.raises(ScaffoldWriteError) as refused:
        write_plan(root, plan)

    assert str(refused.value) == ROLLBACK_FAILED.format(
        f"PermissionError: {os.strerror(errno.EACCES)}"
    )
    assert refused.value.details == (
        "left behind: config/registry/services.yaml",
        "left behind: config/registry/agents.yaml",
    )
    # The registry still validates: the runtime lists an agent that exists.
    registry = load_registry(root / REGISTRY)
    runtime = registry.service("agent-runtime")
    assert runtime is not None
    assert registry.agent(NAME) is not None
    assert NAME in runtime.agents
    assert not (root / "src/meridian/workloads/fraud_review").exists()
