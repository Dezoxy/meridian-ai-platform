"""The chart refuses a documents deadline the code would refuse at start (S062).

``sweep.documentsDeadlineDays`` reaches two workloads as one env value: the
sweep's CronJob and, since the status page names the day from it, the Claims
API. Both read it through ``deadline_days_of`` in
``src/meridian/workloads/claims_triage/lifecycle.py``, which raises
``SettingsError`` for anything but a whole number of days from
``MIN_DEADLINE_DAYS`` to ``MAX_DEADLINE_DAYS``: a bad value that reached the
Claims API would crash-loop it. The chart's template fails the render first,
with a message that names the value, and its range is the code's.
"""

from pathlib import Path

import pytest
from chartsupport import CHART_DIR, helm_arguments, render, rendered_chart, run_helm

from meridian.platform.common.env import SettingsError
from meridian.workloads.claims_triage.lifecycle import (
    MAX_DEADLINE_DAYS,
    MIN_DEADLINE_DAYS,
    deadline_days_of,
)
from meridian.workloads.claims_triage.sweep import (
    DOCUMENTS_DEADLINE_ENV as DEADLINE_ENV,
)

VALUE = "sweep.documentsDeadlineDays"


def deadlines(documents: list[dict]) -> dict[str, str]:
    """The deadline each of the two workloads is given."""

    def env_of(template: dict) -> str:
        (container,) = template["spec"]["containers"]
        (item,) = [e for e in container["env"] if e["name"] == DEADLINE_ENV]
        return item["value"]

    (api,) = [
        d
        for d in documents
        if d["kind"] == "Deployment" and d["metadata"]["name"] == "claims-api"
    ]
    (sweep,) = [d for d in documents if d["kind"] == "CronJob"]
    return {
        "claims-api": env_of(api["spec"]["template"]),
        "sweep": env_of(sweep["spec"]["jobTemplate"]["spec"]["template"]),
    }


def refusal(*arguments: str) -> str:
    done = run_helm([*helm_arguments(), *arguments])
    assert done.returncode != 0, "the render should have been refused"
    return done.stderr


def accepted_by_the_code(text: str) -> bool:
    try:
        deadline_days_of(text)
    except SettingsError:
        return False
    return True


def test_the_default_render_gives_both_workloads_fourteen_days() -> None:
    assert deadlines(list(rendered_chart())) == {"claims-api": "14", "sweep": "14"}


@pytest.mark.parametrize("days", [MIN_DEADLINE_DAYS, 14, MAX_DEADLINE_DAYS])
def test_a_whole_number_in_the_codes_range_renders_in_both_workloads(days: int) -> None:
    # `--set` reads a number as an integer.
    found = deadlines(render([*helm_arguments(), "--set", f"{VALUE}={days}"]))

    assert found == {"claims-api": str(days), "sweep": str(days)}


@pytest.mark.parametrize("days", [MIN_DEADLINE_DAYS, 30, MAX_DEADLINE_DAYS])
def test_a_number_from_a_values_file_renders_the_same(
    tmp_path: Path, days: int
) -> None:
    # A values file reads a number as a float: the render must not print 30.0.
    values = tmp_path / "values.yaml"
    values.write_text(f"sweep:\n  documentsDeadlineDays: {days}\n", encoding="utf-8")

    found = deadlines(render([*helm_arguments(), "-f", str(values)]))

    assert found == {"claims-api": str(days), "sweep": str(days)}


def test_the_charts_range_is_the_codes_range() -> None:
    assert (MIN_DEADLINE_DAYS, MAX_DEADLINE_DAYS) == (1, 365)

    below = refusal("--set-string", f"{VALUE}={MIN_DEADLINE_DAYS - 1}")
    above = refusal("--set-string", f"{VALUE}={MAX_DEADLINE_DAYS + 1}")

    for message, value in ((below, MIN_DEADLINE_DAYS - 1), (above, 366)):
        assert f'{VALUE} is "{value}"' in message
        assert f"from {MIN_DEADLINE_DAYS} to {MAX_DEADLINE_DAYS}" in message
        assert "lifecycle.py" in message


@pytest.mark.parametrize(
    "text",
    ["0", "400", "abc", "14.5", "", "-1", "1e2", " 14", "14 ", "0x10", "١٤", "00000"],
)
def test_a_value_the_code_refuses_is_refused_by_the_render_naming_the_value(
    text: str,
) -> None:
    assert not accepted_by_the_code(text)

    message = refusal("--set-string", f"{VALUE}={text}")

    assert f"{VALUE} is {text!r}".replace("'", '"') in message
    assert "whole number of days" in message


@pytest.mark.parametrize("text", ["1", "014", "0365", "365", "99"])
def test_a_value_the_code_accepts_is_accepted_by_the_render(text: str) -> None:
    assert accepted_by_the_code(text)

    found = deadlines(render([*helm_arguments(), "--set-string", f"{VALUE}={text}"]))

    assert found == {"claims-api": text, "sweep": text}


@pytest.mark.parametrize(
    "assignment", [f"{VALUE}=14.5", f"{VALUE}=0", f"{VALUE}=400", f"{VALUE}=null"]
)
def test_a_number_that_is_not_a_string_is_checked_too(assignment: str) -> None:
    # `--set` types these as float, integer and nothing: not the strings above.
    message = refusal("--set", assignment)

    assert VALUE in message
    assert "whole number of days" in message


def test_each_workload_reads_the_value_through_the_guard_and_nothing_reads_it_raw() -> (
    None
):
    templates = {
        path.name: path.read_text(encoding="utf-8")
        for path in (CHART_DIR / "templates").glob("*")
        if path.is_file()
    }

    raw = [name for name, text in templates.items() if f".Values.{VALUE}" in text]
    guarded = {
        name
        for name, text in templates.items()
        if 'include "meridian.documentsDeadlineDays"' in text
    }
    # The guard reads it once; the Claims API's env item (a helper) and the
    # sweep's CronJob both take it from the guard.
    assert raw == ["_helpers.tpl"]
    assert guarded == {"_helpers.tpl", "sweep.yaml"}


def test_a_line_break_in_the_value_cannot_reach_a_workload() -> None:
    message = refusal("--set-string", f"{VALUE}=14\nkind: Pod")

    assert VALUE in message
