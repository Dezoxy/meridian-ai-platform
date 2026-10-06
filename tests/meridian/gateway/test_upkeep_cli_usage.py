"""``meridian gateway`` without a database (S066): the variable it reads, the
usage errors it refuses before it connects, the amounts it converts, the table
of the database's refusals and the commands the budget runbook names.

The commands against PostgreSQL, as the role ``gateway_upkeep``, are in
``test_upkeep_cli.py``.
"""

import re
import shlex
from pathlib import Path
from typing import Any

import psycopg
import pytest
import typer
from typer.main import get_command
from typer.testing import CliRunner

from meridian.platform.cli import app
from meridian.platform.cli import gateway as gateway_cli
from meridian.platform.cli.db import MIGRATIONS_DATABASE_URL_ENV
from meridian.platform.common.db import DATABASE_URL_ENV

REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATION = (
    REPO_ROOT
    / "src"
    / "meridian"
    / "platform"
    / "migrations"
    / "0020_gateway_upkeep.sql"
)
RUNBOOK = REPO_ROOT / "docs" / "operations" / "runbooks" / "budget-exhaustion.md"
SECRET = "s3cret-value"  # noqa: S105 (a test password, not a credential)
ATTEMPT = "3f2b8c1e-5d4a-4e6f-9a1b-0c2d3e4f5a6b"
# Typer styles a usage error when it thinks a terminal is there, as it does in
# GitHub Actions, and the escape codes and the panel's frame then split a message.
ANSI_STYLE = re.compile(r"\x1b\[[0-9;]*m")
FRAME = re.compile(r"[│╭╮╰╯─]")
runner = CliRunner()

# One valid line of each command: a test changes one argument of it.
RESERVATIONS = ["gateway", "reservations", "--older-than", "30"]
CLOSE = ["gateway", "close", ATTEMPT, "--reason", "dead-process"]
CREDIT = ["gateway", "credit", "tenant-a", "--tokens", "10", "--reason", "goodwill"]
EXPIRE = ["gateway", "expire", "--before", "2026-01", "--reason", "retention-test"]
COMMANDS = {
    "reservations": RESERVATIONS,
    "close": CLOSE,
    "credit": CREDIT,
    "expire": EXPIRE,
}


def plain(output: str) -> str:
    """What the operator reads: no style codes, no frame, one space between words."""
    return " ".join(FRAME.sub(" ", ANSI_STYLE.sub("", output)).split())


@pytest.fixture
def connections(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The addresses the command tried to connect to; each attempt fails."""
    asked: list[str] = []

    def refuse(dsn: str, application_name: str) -> psycopg.Connection:
        asked.append(dsn)
        raise psycopg.OperationalError("no database in this test")

    monkeypatch.setattr(gateway_cli, "connect", refuse)
    return asked


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        gateway_cli.UPKEEP_DATABASE_URL_ENV, "postgresql://x@db.invalid/m"
    )


# ── the one variable ─────────────────────────────────────────────────────────
def test_the_variable_is_the_upkeeps_own() -> None:
    assert gateway_cli.UPKEEP_DATABASE_URL_ENV == "MERIDIAN_GATEWAY_UPKEEP_DATABASE_URL"
    assert gateway_cli.APPLICATION_NAME == "meridian-gateway-upkeep"


@pytest.mark.parametrize("argv", COMMANDS.values(), ids=COMMANDS.keys())
def test_a_missing_variable_exits_1_and_names_it(
    monkeypatch: pytest.MonkeyPatch, connections: list[str], argv: list[str]
) -> None:
    monkeypatch.delenv(gateway_cli.UPKEEP_DATABASE_URL_ENV, raising=False)

    result = runner.invoke(app, argv)

    assert result.exit_code == 1
    assert result.output.startswith("ERROR ")
    assert gateway_cli.UPKEEP_DATABASE_URL_ENV in result.output
    assert connections == []


@pytest.mark.parametrize("argv", COMMANDS.values(), ids=COMMANDS.keys())
def test_the_services_and_the_owners_variables_are_not_a_fallback(
    monkeypatch: pytest.MonkeyPatch, connections: list[str], argv: list[str]
) -> None:
    monkeypatch.delenv(gateway_cli.UPKEEP_DATABASE_URL_ENV, raising=False)
    monkeypatch.setenv(DATABASE_URL_ENV, f"postgresql://svc:{SECRET}@db.invalid/m")
    monkeypatch.setenv(
        MIGRATIONS_DATABASE_URL_ENV, f"postgresql://owner:{SECRET}@db.invalid/m"
    )

    result = runner.invoke(app, argv)

    assert result.exit_code == 1
    assert gateway_cli.UPKEEP_DATABASE_URL_ENV in result.output
    assert connections == []
    assert SECRET not in result.output


@pytest.mark.parametrize("argv", COMMANDS.values(), ids=COMMANDS.keys())
def test_an_unreachable_database_exits_1_without_printing_the_dsn(
    monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    dsn = f"postgresql://nobody:{SECRET}@127.0.0.1:1/none?connect_timeout=1"
    monkeypatch.setenv(gateway_cli.UPKEEP_DATABASE_URL_ENV, dsn)

    result = runner.invoke(app, argv)

    assert result.exit_code == 1
    assert result.output.startswith("ERROR ")
    assert SECRET not in result.output
    assert "127.0.0.1" not in result.output


def test_meridian_help_lists_gateway() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert re.search(r"\bgateway\b", plain(result.output))


# ── usage errors are refused before the database is touched ─────────────────
def replace(argv: list[str], old: str, new: str) -> list[str]:
    return [new if word == old else word for word in argv]


USAGE_ERRORS = [
    pytest.param(
        replace(CLOSE, "dead-process", "Dead Process"),
        "slug",
        id="a-reason-with-a-space",
    ),
    pytest.param(
        replace(CLOSE, "dead-process", "Dead"), "slug", id="an-upper-case-reason"
    ),
    pytest.param(replace(CLOSE, "dead-process", ""), "slug", id="an-empty-reason"),
    pytest.param(replace(CLOSE, "dead-process", "a" * 65), "slug", id="a-long-reason"),
    pytest.param(replace(CLOSE, "dead-process", "a_b"), "slug", id="an-underscore"),
    pytest.param(
        replace(CLOSE, "dead-process", "ok\n"), "slug", id="a-reason-with-a-newline"
    ),
    pytest.param(replace(CREDIT, "goodwill", "No Way"), "slug", id="a-credit-reason"),
    pytest.param(
        replace(EXPIRE, "retention-test", "Not A Slug"), "slug", id="an-expiry-reason"
    ),
    pytest.param(
        replace(CLOSE, ATTEMPT, "not-a-uuid"), "not-a-uuid", id="a-bad-attempt"
    ),
    pytest.param(
        ["gateway", "credit", "tenant-a", "--reason", "goodwill"],
        "exactly one of --tokens and --eur",
        id="no-amount",
    ),
    pytest.param(
        [*CREDIT, "--eur", "1"], "exactly one of --tokens and --eur", id="both-amounts"
    ),
    pytest.param(replace(CREDIT, "10", "0"), "above zero", id="zero-tokens"),
    pytest.param(replace(CREDIT, "10", "-5"), "above zero", id="negative-tokens"),
    pytest.param(replace(CREDIT, "10", str(2**63)), "above zero", id="too-many-tokens"),
    pytest.param(replace(CREDIT, "10", "1.5"), "1.5", id="fractional-tokens"),
    pytest.param(
        replace(EXPIRE, "2026-01", "2026-1"), "YYYY-MM", id="a-month-without-zero"
    ),
    pytest.param(replace(EXPIRE, "2026-01", "2026-13"), "YYYY-MM", id="month-thirteen"),
    pytest.param(replace(EXPIRE, "2026-01", "2026-00"), "YYYY-MM", id="month-zero"),
    pytest.param(replace(EXPIRE, "2026-01", "2026-01-01"), "YYYY-MM", id="a-day"),
    pytest.param(replace(EXPIRE, "2026-01", "0000-01"), "YYYY-MM", id="year-zero"),
    pytest.param(replace(EXPIRE, "2026-01", "next"), "YYYY-MM", id="a-word"),
    pytest.param(
        ["gateway", "expire", "--reason", "retention-test"],
        "--before",
        id="no-month",
    ),
    pytest.param(replace(RESERVATIONS, "30", "9"), "10 minutes", id="below-the-floor"),
    pytest.param(replace(RESERVATIONS, "30", "0"), "10 minutes", id="zero-minutes"),
    pytest.param(
        replace(RESERVATIONS, "30", "-5"), "10 minutes", id="negative-minutes"
    ),
    pytest.param(replace(RESERVATIONS, "30", "ten"), "ten", id="minutes-in-words"),
    pytest.param(
        ["gateway", "reservations"], "--older-than", id="no-age-for-reservations"
    ),
]


@pytest.mark.parametrize(("argv", "fragment"), USAGE_ERRORS)
def test_a_usage_error_exits_2_with_the_reason_and_never_connects(
    configured: None, connections: list[str], argv: list[str], fragment: str
) -> None:
    result = runner.invoke(app, argv)

    assert result.exit_code == 2, result.output
    assert fragment in plain(result.output)
    assert connections == []


def test_a_usage_error_is_refused_before_the_missing_variable_is_read(
    monkeypatch: pytest.MonkeyPatch, connections: list[str]
) -> None:
    monkeypatch.delenv(gateway_cli.UPKEEP_DATABASE_URL_ENV, raising=False)

    result = runner.invoke(app, replace(CLOSE, "dead-process", "Dead Process"))

    assert result.exit_code == 2
    assert gateway_cli.UPKEEP_DATABASE_URL_ENV not in result.output


@pytest.mark.parametrize("minutes", ["10", "11", "1440"])
def test_the_floor_and_what_is_above_it_are_accepted(
    configured: None, connections: list[str], minutes: str
) -> None:
    result = runner.invoke(app, replace(RESERVATIONS, "30", minutes))

    # It got as far as the connection, which this test refuses.
    assert result.exit_code == 1
    assert len(connections) == 1


# ── the amounts ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("text", "micro_eur"),
    [
        ("1", 1_000_000),
        ("0.000001", 1),
        ("1.5", 1_500_000),
        ("0.1", 100_000),
        ("12.345678", 12_345_678),
        ("1.000000", 1_000_000),
        ("0.00001", 10),
        ("9223372036854.775807", 2**63 - 1),
    ],
)
def test_an_amount_in_euro_is_converted_to_micro_euro_exactly(
    text: str, micro_eur: int
) -> None:
    assert gateway_cli.micro_eur_from(text) == micro_eur


@pytest.mark.parametrize(
    "text",
    [
        "0.0000001",
        "1.0000000",
        "-1",
        "-0.5",
        "+1",
        "0",
        "0.000000",
        "1e3",
        "1E-6",
        "NaN",
        "Infinity",
        "",
        " 1",
        "1 ",
        "1,5",
        ".5",
        "1.",
        "one",
        "9223372036854.775808",
        "10000000000000",
        chr(0x661),  # an Arabic-Indic digit one: `\d` matches it, a ledger must not
    ],
)
def test_an_amount_that_is_not_a_positive_euro_with_up_to_six_decimals_is_refused(
    text: str,
) -> None:
    with pytest.raises(typer.BadParameter):
        gateway_cli.micro_eur_from(text)


def test_the_conversion_is_exact_where_a_float_is_not() -> None:
    # As a float 0.000249 euro is 248 micro-euro after int(); exactly it is 249.
    assert int(float("0.000249") * 1_000_000) == 248
    assert gateway_cli.micro_eur_from("0.000249") == 249


# ── the database's refusals ─────────────────────────────────────────────────
def migration_codes() -> set[str]:
    """The GU codes the header of 0020 lists, one per line starting `--   GU`."""
    text = MIGRATION.read_text(encoding="utf-8")
    return set(re.findall(r"^--\s+(GU\d{3})\s", text, re.MULTILINE))


def test_the_table_of_refusals_holds_every_code_the_migration_names() -> None:
    assert len(migration_codes()) == 13
    assert set(gateway_cli.REFUSALS) == migration_codes()


@pytest.mark.parametrize("code", sorted(migration_codes()))
def test_each_refusal_has_one_line_of_help_that_says_what_to_do(code: str) -> None:
    line = gateway_cli.REFUSALS[code]

    assert "\n" not in line
    assert 20 < len(line) <= 200
    assert not line.endswith(" ")


# ── the module imports no provider SDK ──────────────────────────────────────
def test_the_module_imports_nothing_of_the_gateway() -> None:
    import ast

    source = Path(gateway_cli.__file__).read_text(encoding="utf-8")
    imported = [
        alias.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Import)
        for alias in node.names
    ] + [
        node.module or ""
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom)
    ]

    assert not [
        name for name in imported if name.startswith("meridian.platform.gateway")
    ]
    assert not [name for name in imported if name.split(".")[0] in {"openai", "azure"}]


# ── the budget runbook names commands that exist ────────────────────────────
SHELL_FENCE = re.compile(r"^```(?:sh|bash|shell)\s*$(?P<body>.*?)^```\s*$", re.M | re.S)


def runbook_lines() -> list[list[str]]:
    """Every `meridian gateway ...` line of the runbook's shell fences, as words.
    A line that ends in a backslash continues on the next."""
    found: list[list[str]] = []
    text = RUNBOOK.read_text(encoding="utf-8")
    for fence in SHELL_FENCE.finditer(text):
        body = fence["body"].replace("\\\n", " ")
        for line in body.splitlines():
            words = shlex.split(line, comments=True)
            if words[:2] == ["meridian", "gateway"]:
                found.append(words)
    return found


def gateway_commands() -> dict[str, Any]:
    """The commands Typer registered under ``meridian gateway``. (Typer's own
    classes, not ``click``'s, so they are read by attribute.)"""
    return get_command(app).commands["gateway"].commands  # type: ignore[attr-defined]


def registered_options(command: str) -> set[str]:
    return {
        opt
        for param in gateway_commands()[command].params
        if param.param_type_name == "option"
        for opt in param.opts
    }


def registered_required_options(command: str) -> set[str]:
    return {
        opt
        for param in gateway_commands()[command].params
        if param.param_type_name == "option" and param.required
        for opt in param.opts
    }


def test_the_runbook_names_each_of_the_four_commands() -> None:
    named = {words[2] for words in runbook_lines()}

    assert named == {"reservations", "close", "credit", "expire"}


@pytest.mark.parametrize("words", runbook_lines(), ids=" ".join)
def test_a_command_line_of_the_runbook_names_a_command_and_options_that_exist(
    words: list[str],
) -> None:
    assert words[2] in gateway_commands(), words
    options = [word for word in words[3:] if word.startswith("--")]

    assert options
    assert set(options) <= registered_options(words[2]) | {"--help"}, words
    assert registered_required_options(words[2]) <= set(options), words


@pytest.mark.parametrize("name", ["reservations", "close", "credit", "expire"])
def test_each_command_answers_help(name: str) -> None:
    result = runner.invoke(app, ["gateway", name, "--help"])

    assert result.exit_code == 0, result.output
    for option in registered_options(name):
        assert option in plain(result.output)


def test_the_help_of_expire_says_the_month_is_the_operators_decision() -> None:
    result = runner.invoke(app, ["gateway", "expire", "--help"])

    text = plain(result.output)
    assert "decision" in text
    assert "no default" in text
    assert "--confirm" in text


def test_the_help_of_close_says_what_kept_and_released_mean() -> None:
    result = runner.invoke(app, ["gateway", "close", "--help"])

    text = plain(result.output)
    assert "kept" in text
    assert "released" in text
    assert "not billed" in text


def test_the_help_of_reservations_says_it_writes_no_audit_row() -> None:
    result = runner.invoke(app, ["gateway", "reservations", "--help"])

    assert "no audit row" in plain(result.output)
