"""What the log agent keeps of a file and of a line (S064, G1).

``infra/kind/values/log-agent.yaml`` reads only the files the chart's long-lived
pods write, drops a record whose file resolves outside them, takes none of the
resource's names from a line's own JSON, and gives Python's ``CRITICAL`` a
severity. The values are read as committed (the neighbour, ``test_log_agent.py``,
says why no test renders the collector's chart). What the pinned contrib image
did with each of these over fixture files is a manual check, recorded in the
report of the change: it ran the real pipeline over a tree with the node's modes
and printed what it made of each line.
"""

import re

import pytest
from test_log_agent import (
    LISTED_FILES,
    POD_LOGS,
    POD_UID,
    chart_pod_families,
    listed_files_expression,
    listed_files_regex,
    operator,
    operators,
    pod_directory,
    receiver,
    values,
)

# What the pipeline itself puts on a record, and the names a line of JSON could
# plant to look like it.
RESOURCES_NAMES = (
    "service.name",
    "k8s.container.name",
    "k8s.namespace.name",
    "k8s.pod.name",
    "k8s.pod.uid",
    "k8s.container.restart_count",
    "log.file.path",
    "log.file.path_resolved",
    "log.file.name",
    "log.iostream",
)
# What a service's own JSON line carries and the agent must keep.
LINES_OWN_WORDS = ("level", "logger", "service", "message", "method", "path", "status")


def resolved(directory: str, container: str = "claims-api") -> str:
    return f"{POD_LOGS}/{directory}/{container}/0.log"


# ── a resolved path outside the list is dropped (L2) ─────────────────────────


def test_the_receiver_records_the_resolved_path_of_each_file() -> None:
    assert receiver()["include_file_path_resolved"] is True
    assert receiver()["include_file_path"] is True


def test_the_first_operator_drops_a_record_whose_resolved_path_is_not_listed() -> None:
    first = operators()[0]

    assert first["id"] == LISTED_FILES and first["type"] == "filter"
    # `filter` drops when the expression is true: so it is a `not matches`.
    expression = listed_files_expression()
    assert expression.startswith('attributes["log.file.path_resolved"] not matches ')
    # Before the container operator, so nothing is read from a path it rejects.
    assert operators()[1]["type"] == "container"


def test_the_filters_list_is_the_include_lists_and_the_charts() -> None:
    (alternatives,) = re.findall(r"meridian_\(([^)]*)\)-", listed_files_expression())

    assert sorted(alternatives.split("|")) == chart_pod_families()
    assert sorted(
        re.fullmatch(rf"{re.escape(POD_LOGS)}/meridian_(.*)-\*/\*/\*\.log", pattern)[1]
        for pattern in receiver()["include"]
    ) == sorted(alternatives.split("|"))


@pytest.mark.parametrize("family", chart_pod_families())
def test_a_file_of_each_listed_family_passes_the_filter(family: str) -> None:
    path = resolved(pod_directory(family), family)

    assert listed_files_regex().fullmatch(path)


@pytest.mark.parametrize(
    "path",
    [
        # The review's fixture: a symlink under a listed directory resolves to the
        # file of another namespace's pod.
        resolved(f"kube-system_coredns-abc_{POD_UID}", "coredns"),
        # ... or to the database's, or a Job's, in the same namespace.
        resolved(f"meridian_platform-db-1_{POD_UID}", "postgres"),
        resolved(f"meridian_meridian-migrate-0123456789ab-qwert_{POD_UID}", "migrate"),
        # ... or to a file that is not a container's log, or leaves the directory.
        "/etc/hostname",
        f"{POD_LOGS}/meridian_claims-api-7d9f8-abcde_{POD_UID}/claims-api/0.log.gz",
        f"{POD_LOGS}/../etc/meridian_claims-api-x_{POD_UID}/c/0.log",
        # A family that only starts like a listed one.
        resolved(f"meridian_claims-apiary-7d9f8-abcde_{POD_UID}", "claims-api"),
        # No path at all: the record has no resolved path to match.
        "",
    ],
)
def test_a_resolved_path_outside_the_list_does_not_pass_the_filter(path: str) -> None:
    assert not listed_files_regex().fullmatch(path)


# ── a line cannot name itself another service (L1) ───────────────────────────


def test_the_stream_is_set_aside_on_the_resource_before_the_line_is_parsed() -> None:
    ids = [each["id"] for each in operators()]
    aside = operator("stream-aside")

    # The line's own JSON is parsed into the same attributes, so the stream the
    # container operator found is parked where no line can write, the resource,
    # and put back after the names are removed.
    assert ids.index("stream-aside") < ids.index("json")
    assert aside["type"] == "move"
    assert aside["from"] == 'attributes["log.iostream"]'
    assert aside["to"] == 'resource["meridian.stream"]'
    assert aside["if"] == 'attributes["log.iostream"] != nil'


def transform_groups() -> list[dict]:
    return values()["config"]["processors"]["transform/line"]["log_statements"]


def removal_pattern() -> re.Pattern[str]:
    (statement, *_) = transform_groups()[0]["statements"]
    (literal,) = re.findall(r'"(\^[^"]*)"', statement)
    return re.compile(literal.replace("\\\\", "\\"))


def test_the_names_the_pipeline_sets_are_removed_from_a_records_attributes() -> None:
    first, second = transform_groups()

    assert values()["config"]["processors"]["transform/line"]["error_mode"] == "ignore"
    assert first["context"] == "log"
    assert first["statements"][0].startswith("delete_matching_keys(log.attributes, ")
    assert second["context"] == "resource"


@pytest.mark.parametrize("name", RESOURCES_NAMES)
def test_a_name_the_resource_carries_is_matched_by_the_removal(name: str) -> None:
    assert removal_pattern().search(name)


@pytest.mark.parametrize(
    "name", [*LINES_OWN_WORDS, "logtag", "login", "services", "k8sfoo", "meridian.x"]
)
def test_the_lines_own_words_are_not_matched_by_the_removal(name: str) -> None:
    assert not removal_pattern().search(name)


def test_the_stream_is_put_back_from_the_resource_after_the_removal() -> None:
    first, second = transform_groups()
    removal, restore = first["statements"]

    assert removal.startswith("delete_matching_keys(")
    assert restore == (
        'set(log.attributes["log.iostream"], resource.attributes["meridian.stream"])'
        ' where resource.attributes["meridian.stream"] != nil'
    )
    # Removed from the resource in a statement of its own, once, after every
    # record has read it: inside the first group the first record would take it
    # from the records after it.
    assert second["statements"] == [
        'delete_key(resource.attributes, "meridian.stream")'
    ]


def test_the_services_name_and_the_path_are_never_taken_from_the_line() -> None:
    # The resource is made by the container operator from the file's path, and
    # `service.name` from the container's name; a JSON line parses into the
    # record's attributes only.
    json = operator("json")

    assert json["parse_from"] == "body"
    assert "parse_to" not in json
    assert not any("resource" in str(each.get("to", "")) for each in operators()[3:])


# ── CRITICAL has a severity (M2) ─────────────────────────────────────────────


def test_python_critical_is_mapped_to_fatal() -> None:
    severity = operator("json")["severity"]

    assert severity == {
        "parse_from": "attributes.level",
        "mapping": {"fatal": ["critical"]},
    }
