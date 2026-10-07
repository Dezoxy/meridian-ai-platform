"""The line of ``make smoke`` for the database's own certificates (S073, K5).

On kind CloudNativePG signs the database's server and client certificates with
an authority of its own and renews them seven days before they end
(``EXPIRING_CHECK_THRESHOLD``, in whole days: no renewal can be seen inside one
cluster run, since the shortest lifetime, ``CERTIFICATE_DURATION``, is one day).
cert-manager does not issue them and Prometheus holds no series for them, so
nothing else would say that a renewal did not happen. The line reads the three
expirations in the Cluster's status, text in Go's default time format, and
fails inside half of the operator's own threshold, or when it cannot tell.

The function runs in bash against a stub ``kctl`` and the real ``jq``. The
clock is a parameter of ``check_database_certificates``: every date here is
built from ``NOW``, a fixed number, never from the clock.
"""

import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from kindsupport import (
    KIND_DIR,
    SMOKE_SH,
    function_definition,
    one_line_function,
    requires_jq,
)

pytestmark = requires_jq

ROOT = KIND_DIR.parents[1]
NOW = 1_790_000_000  # a fixed second: 2026-09-21 14:13:20 UTC
DAY = 86_400
MARGIN = 302_400  # half of the operator's seven days, in seconds
CERTIFICATES = (
    "platform-db-ca",
    "platform-db-server",
    "platform-db-replication",
)
ARGUMENT_LIMIT = 131_072


def go_time(epoch: int) -> str:
    """A date as Go's ``time.Time.String`` prints it for a UTC instant."""
    moment = datetime.fromtimestamp(epoch, tz=UTC)
    return moment.strftime("%Y-%m-%d %H:%M:%S +0000 UTC")


def status_of(expirations: object, **more: object) -> dict:
    """A Cluster object as ``kubectl get -o json`` prints it, with the
    expirations where the operator writes them."""
    return {
        "apiVersion": "postgresql.cnpg.io/v1",
        "kind": "Cluster",
        "metadata": {"name": "platform-db", "namespace": "meridian"},
        "status": {"certificates": {"expirations": expirations}, **more},
    }


def ends_in(seconds_each: dict[str, int]) -> dict[str, str]:
    return {name: go_time(NOW + seconds) for name, seconds in seconds_each.items()}


def all_three(seconds: int) -> dict[str, str]:
    return ends_in(dict.fromkeys(CERTIFICATES, seconds))


def run_certificates_check(
    tmp_path: Path, *, cluster: dict | str | list | None, now: int = NOW
) -> tuple[list[str], str]:
    """``check_database_certificates`` from smoke.sh in bash. ``cluster`` is the
    Cluster's answer (a string is sent as it is; ``None``: the read fails). The
    answer goes through a file, since one string of the environment is as
    limited as an argument. Returns the output lines and what ``kctl`` was
    asked."""
    asked = tmp_path / "kctl-calls"
    asked.touch()
    answer = tmp_path / "cluster-answer.json"
    if cluster is not None:
        answer.write_text(cluster if isinstance(cluster, str) else json.dumps(cluster))
    reply = (
        f'cat "{answer}"'
        if cluster is not None
        else 'echo "Error from server (NotFound): secret-ish text" >&2; return 1'
    )
    script = "\n".join(
        [
            "set -euo pipefail",
            "failures=0; skips=0",
            'pass() { echo "PASS  $*"; }',
            'fail() { echo "FAIL  $*"; }',
            'skip() { echo "SKIP  $*"; }',
            *re.findall(r"^readonly DATABASE_CERTIFICATE_\w+=.*$", SMOKE_SH, re.M),
            one_line_function(SMOKE_SH, "clean_lines"),
            "kctl() {",
            f'  echo "$*" >>"{asked}"',
            '  case "$*" in',
            f"    *get\\ clusters.postgresql.cnpg.io\\ platform-db*) {reply} ;;",
            '    *) echo "stub kctl: unexpected $*" >&2; return 99 ;;',
            "  esac",
            "}",
            function_definition(SMOKE_SH, "database_certificates_verdict"),
            function_definition(SMOKE_SH, "check_database_certificates"),
            f"check_database_certificates {now}",
        ]
    )
    done = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.splitlines(), asked.read_text()


def healthy_certificate_lines(tmp_path: Path) -> list[str]:
    """The lines of a database whose three certificates have 90 days left."""
    return run_certificates_check(tmp_path, cluster=status_of(all_three(90 * DAY)))[0]


def only_line(tmp_path: Path, *, cluster: dict | str | list | None) -> str:
    (line,) = run_certificates_check(tmp_path, cluster=cluster)[0]
    return line


# ── the verdicts ─────────────────────────────────────────────────────────────


def test_ninety_days_left_passes_and_says_the_earliest_and_the_days(
    tmp_path: Path,
) -> None:
    cluster = status_of(all_three(90 * DAY))

    line = only_line(tmp_path, cluster=cluster)

    assert line.startswith("PASS  certificate policy: the database's certificates")
    assert "90 days left" in line
    assert go_time(NOW + 90 * DAY).removesuffix(" +0000 UTC") in line


def test_one_day_left_fails_and_names_the_certificate_and_the_days(
    tmp_path: Path,
) -> None:
    expirations = ends_in(
        {
            "platform-db-ca": 90 * DAY,
            "platform-db-server": 1 * DAY,
            "platform-db-replication": 90 * DAY,
        }
    )

    line = only_line(tmp_path, cluster=status_of(expirations))

    assert line.startswith("FAIL  certificate policy: the database's certificates")
    assert "platform-db-server" in line
    assert "1 day" in line
    assert "platform-db-ca" not in line


def test_the_earliest_of_three_different_dates_is_the_one_judged(
    tmp_path: Path,
) -> None:
    expirations = ends_in(
        {
            "platform-db-ca": 300 * DAY,
            "platform-db-server": 60 * DAY,
            "platform-db-replication": 2 * DAY,
        }
    )

    line = only_line(tmp_path, cluster=status_of(expirations))

    assert line.startswith("FAIL  ")
    assert "platform-db-replication" in line
    assert "2 days" in line


def test_the_earliest_is_the_one_a_pass_names_when_none_is_close(
    tmp_path: Path,
) -> None:
    expirations = ends_in(
        {
            "platform-db-ca": 300 * DAY,
            "platform-db-server": 60 * DAY,
            "platform-db-replication": 80 * DAY,
        }
    )

    line = only_line(tmp_path, cluster=status_of(expirations))

    assert line.startswith("PASS  ")
    assert "platform-db-server" in line
    assert "60 days left" in line


def test_just_over_the_margin_passes_and_the_margin_itself_and_less_fail(
    tmp_path: Path,
) -> None:
    over = only_line(tmp_path, cluster=status_of(all_three(MARGIN + 1)))
    (tmp_path / "at").mkdir()
    at = only_line(tmp_path / "at", cluster=status_of(all_three(MARGIN)))
    (tmp_path / "under").mkdir()
    under = only_line(tmp_path / "under", cluster=status_of(all_three(MARGIN - 1)))

    assert over.startswith("PASS  ")
    assert at.startswith("FAIL  ")
    assert under.startswith("FAIL  ")


def test_a_certificate_that_has_ended_fails_and_says_so(tmp_path: Path) -> None:
    expirations = ends_in(
        {
            "platform-db-ca": 90 * DAY,
            "platform-db-server": -3 * DAY,
            "platform-db-replication": 90 * DAY,
        }
    )

    line = only_line(tmp_path, cluster=status_of(expirations))

    assert line.startswith("FAIL  ")
    assert "platform-db-server" in line
    assert "has ended" in line


def test_the_status_of_a_cluster_without_expirations_fails(tmp_path: Path) -> None:
    for number, cluster in enumerate(
        (
            status_of({}),
            status_of(None),
            {"status": {"certificates": {}}},
            {"status": {}},
            {"metadata": {"name": "platform-db"}},
            [],
        )
    ):
        directory = tmp_path / str(number)
        directory.mkdir()

        line = only_line(directory, cluster=cluster)

        assert line.startswith("FAIL  certificate policy: the database's certificates")
        assert "no expiration" in line, cluster


def test_a_date_in_another_zone_is_a_failure_that_says_it_cannot_tell(
    tmp_path: Path,
) -> None:
    expirations = all_three(90 * DAY) | {
        "platform-db-server": "2027-01-04 19:05:31 +0100 CET"
    }

    line = only_line(tmp_path, cluster=status_of(expirations))

    assert line.startswith("FAIL  ")
    assert "cannot tell" in line
    assert "platform-db-server" in line
    # The shape that is wrong, never the text that was in it.
    assert "+0100" not in line and "CET" not in line


def test_a_date_in_rfc_3339_is_a_failure_that_says_it_cannot_tell(
    tmp_path: Path,
) -> None:
    expirations = all_three(90 * DAY) | {"platform-db-ca": "2027-01-04T18:05:31Z"}

    line = only_line(tmp_path, cluster=status_of(expirations))

    assert line.startswith("FAIL  ")
    assert "cannot tell" in line
    assert "platform-db-ca" in line
    assert "2027-01-04T18:05:31Z" not in line


def test_a_second_with_a_fraction_or_a_value_that_is_not_text_cannot_be_told(
    tmp_path: Path,
) -> None:
    for number, bad in enumerate(
        ("2027-01-04 18:05:31.5 +0000 UTC", 1798826731, None, "", "never")
    ):
        directory = tmp_path / str(number)
        directory.mkdir()
        expirations = all_three(90 * DAY) | {"platform-db-replication": bad}

        line = only_line(directory, cluster=status_of(expirations))

        assert line.startswith("FAIL  "), bad
        assert "cannot tell" in line, bad


def test_a_date_of_the_right_form_that_is_no_date_cannot_be_told_by_name(
    tmp_path: Path,
) -> None:
    for number, bad in enumerate(
        (
            "2027-13-04 18:05:31 +0000 UTC",
            "2027-00-04 18:05:31 +0000 UTC",
            "2027-01-04 25:05:31 +0000 UTC",
        )
    ):
        directory = tmp_path / str(number)
        directory.mkdir()
        expirations = all_three(90 * DAY) | {"platform-db-server": bad}

        line = only_line(directory, cluster=status_of(expirations))

        assert line.startswith("FAIL  "), bad
        assert "cannot tell when platform-db-server ends" in line, bad
        assert "not a date" in line, bad
        # Not the line for an answer that is not JSON, and not the value.
        assert "as JSON" not in line, bad
        assert bad.split(" ")[0] not in line, bad


def test_one_date_that_cannot_be_told_fails_even_when_the_others_are_far(
    tmp_path: Path,
) -> None:
    expirations = all_three(3000 * DAY) | {"platform-db-ca": "soon"}

    line = only_line(tmp_path, cluster=status_of(expirations))

    assert line.startswith("FAIL  ")
    assert "cannot tell" in line


def test_a_cluster_that_cannot_be_read_fails_and_repeats_nothing_it_wrote(
    tmp_path: Path,
) -> None:
    line = only_line(tmp_path, cluster=None)

    assert line.startswith("FAIL  certificate policy: the database's certificates")
    assert "could not read" in line
    assert "secret-ish" not in line


def test_an_answer_that_is_not_json_fails_and_does_not_pass(tmp_path: Path) -> None:
    line = only_line(tmp_path, cluster="<html>gateway timeout</html>")

    assert line.startswith("FAIL  ")
    assert "gateway timeout" not in line


def test_a_name_with_a_control_character_is_printed_cleaned(tmp_path: Path) -> None:
    expirations = {"platform-db-\x1b[31mserver": go_time(NOW + DAY)}

    line = only_line(tmp_path, cluster=status_of(expirations))

    assert "\x1b" not in line


def test_the_cluster_is_read_once_and_a_status_over_the_argument_limit_is_read(
    tmp_path: Path,
) -> None:
    cluster = status_of(all_three(90 * DAY), padding="x" * (ARGUMENT_LIMIT + 1))

    lines, asked = run_certificates_check(tmp_path, cluster=cluster)

    assert [line.split()[0] for line in lines] == ["PASS"]
    # The padded status was read: the line names what it holds.
    assert "the earliest of 3 is platform-db-ca, with 90 days left" in lines[0]
    (call,) = asked.splitlines()
    assert call.split() == [
        "-n",
        "meridian",
        "get",
        "clusters.postgresql.cnpg.io",
        "platform-db",
        "-o",
        "json",
    ]


# ── the script and the documents ─────────────────────────────────────────────


def test_the_margin_is_half_of_the_operators_threshold_of_seven_days() -> None:
    done = subprocess.run(
        [
            "bash",
            "-c",
            "\n".join(
                [
                    *re.findall(
                        r"^readonly DATABASE_CERTIFICATE_\w+=.*$", SMOKE_SH, re.M
                    ),
                    'echo "${DATABASE_CERTIFICATE_MARGIN_SECONDS}"',
                ]
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.stdout.strip() == str(MARGIN)
    assert "EXPIRING_CHECK_THRESHOLD" in SMOKE_SH


def test_the_check_is_the_fifth_line_of_the_certificate_policy_check() -> None:
    calls = re.findall(
        r"^  (check_\w+)$",
        function_definition(SMOKE_SH, "check_certificate_policy"),
        re.M,
    )

    assert calls[-1] == "check_database_certificates"


def the_certificate_policy_passage() -> str:
    """The README's passage on the policy check: from its own first words to
    the next numbered item's bold heading (found by its shape, not by a number
    or a title that an edit may change)."""
    readme = " ".join((KIND_DIR / "README.md").read_text("utf-8").split())
    after = readme.split("**Certificate policy.** Five lines", 1)[1]
    return re.split(r" \d+\. \*\*", after, maxsplit=1)[0]


def test_the_passage_is_cut_at_the_next_item_and_not_at_a_fixed_title() -> None:
    passage = the_certificate_policy_passage()

    assert "CloudNativePG" in passage
    assert not re.search(r"\b\d+\. \*\*", passage)
    assert "read-only, run last" not in passage


def test_the_readme_labels_what_was_seen_of_the_line_and_what_was_not() -> None:
    passage = the_certificate_policy_passage()

    assert "Tested with a stand-in and the real jq" in passage
    assert re.search(r"Seen on kind on \d{4}-\d\d-\d\d", passage)
    assert "Not seen" in passage
    assert "NOT YET SEEN" not in passage


def test_the_runbook_says_who_renews_the_databases_certificates_and_the_gap() -> None:
    runbook = " ".join(
        (ROOT / "docs" / "operations" / "runbooks" / "certificate-expiry.md")
        .read_text("utf-8")
        .split()
    )

    assert "CloudNativePG renews the database's certificates" in runbook
    assert "Nothing alerts between two smoke runs" in runbook
