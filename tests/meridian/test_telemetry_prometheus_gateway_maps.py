"""The maps that decide a request at Prometheus's gateway (S072, contract M4).

The ``map`` blocks of the gateway's nginx configuration are evaluated with nginx's
rules over the raw request URI and over the form nginx normalises it to, for every
kind of client: a read path is served to all of them, the one write path only to
the collector's certificate, and everything else, remote write, the admin API, the
lifecycle endpoints, the odd forms of a path and every path nobody listed, is 403
whatever the certificate. The certificate, the manifest and the configuration are
judged in ``test_telemetry_prometheus_gateway.py``; what both read in common is in
``prometheusgatewaysupport.py``.
"""

import re

import pytest
from certpolicysupport import COLLECTOR_CLIENT_POLICY, KIND_DIR
from prometheusgatewaysupport import (
    CLOSED_WRITE_SURFACES,
    COLLECTORS,
    EVERY_CLIENT,
    NO_CERTIFICATE,
    NO_NAME,
    NOT_VERIFIED,
    ODD_FORMS,
    OTHER_CLOSED_PATHS,
    OTHER_NAME,
    READ_PATHS,
    REMOVED_STATUS_PATHS,
    ROUTES_SEEN,
    WRITE_PATH,
    WRITER,
    class_of,
    denied,
    nginx_conf,
    parse_maps,
)
from test_telemetry_ca import COLLECTOR_CLIENT, certificate, policies

# ── the maps that decide a request ───────────────────────────────────────────


def test_the_maps_and_the_location_check_are_the_ones_the_tests_model() -> None:
    maps = parse_maps()

    assert set(maps) == {
        "$prometheus_gateway_cert",
        "$prometheus_gateway_key",
        "$prometheus_class_raw",
        "$prometheus_class_uri",
        "$prometheus_class",
        "$prometheus_denied",
    }
    # The default of each is the closed class, and nothing but `r` is open to a
    # client with no certificate.
    for name in ("$prometheus_class_raw", "$prometheus_class_uri", "$prometheus_class"):
        assert dict(maps[name][1])["default"] == "x", name
    assert dict(maps["$prometheus_denied"][1])["default"] == "1"
    assert nginx_conf().count("if ($prometheus_denied) { return 403; }") == 1


def test_the_two_views_of_the_path_are_the_same_list() -> None:
    maps = parse_maps()

    # One list on each view of the request: a pattern changed in one and not in the
    # other would open a path on one view only.
    assert maps["$prometheus_class_raw"][1] == maps["$prometheus_class_uri"][1]


@pytest.mark.parametrize("path", READ_PATHS)
@pytest.mark.parametrize("client", EVERY_CLIENT)
def test_a_read_path_is_served_to_every_client(
    path: str, client: tuple[str, str]
) -> None:
    assert class_of(path) == "r", path
    assert not denied(path, *client)


@pytest.mark.parametrize("path", [p for p in READ_PATHS if p != "/"])
def test_a_read_path_with_a_query_string_is_still_a_read_path(path: str) -> None:
    query = "?query=up%7Bjob%3D%22x%22%7D&start=0&end=60"

    assert class_of(path + query) == "r"
    assert not denied(path + query, *NO_CERTIFICATE)


def test_the_one_write_path_is_served_only_to_the_collectors_certificate() -> None:
    assert class_of(WRITE_PATH) == "w"
    assert not denied(WRITE_PATH, *COLLECTORS)
    for client in (NO_CERTIFICATE, OTHER_NAME, NO_NAME, NOT_VERIFIED, ("", "")):
        assert denied(WRITE_PATH, *client), client


def test_the_write_path_with_a_query_string_is_still_the_write_path() -> None:
    path = WRITE_PATH + "?x=1"

    assert class_of(path) == "w"
    assert denied(path, *NO_CERTIFICATE)
    assert not denied(path, *COLLECTORS)


@pytest.mark.parametrize("path", [*CLOSED_WRITE_SURFACES, *OTHER_CLOSED_PATHS], ids=str)
@pytest.mark.parametrize("client", EVERY_CLIENT)
def test_remote_write_the_admin_api_the_lifecycle_and_every_other_path_are_closed(
    path: str, client: tuple[str, str]
) -> None:
    # 403 whatever the certificate, and whether or not Prometheus enables the
    # endpoint today: the collector's key does not open them.
    assert class_of(path) == "x", path
    assert denied(path, *client), (path, client)


@pytest.mark.parametrize("path", ODD_FORMS, ids=repr)
@pytest.mark.parametrize("client", EVERY_CLIENT)
def test_a_path_written_to_get_past_a_pattern_is_refused_whatever_the_certificate(
    path: str, client: tuple[str, str]
) -> None:
    # Fail closed on what the client SENT: a raw path with `%`, `//`, a dot segment,
    # a trailing slash, another case or a control character is no read and no write,
    # even where nginx would decode, merge or resolve it into one.
    assert class_of(path) == "x", path
    assert denied(path, *client), (path, client)


def test_the_raw_view_holds_a_percent_or_a_doubled_slash_in_no_pattern() -> None:
    # The patterns of the raw view match the path literally, so a path nginx would
    # decode or merge before it matches a location is not on any list. (This test
    # was longer: its other lines asserted the Python model of nginx's
    # normalisation against itself, which no nginx behaviour stands behind here;
    # the live check is line 9 of smoke's check 12.)
    _, entries = parse_maps()["$prometheus_class_raw"]

    for pattern, _ in entries:
        assert "%" not in pattern and "//" not in pattern


def read_alternation() -> set[str]:
    """The paths the manifest's read pattern names, taken out of the pattern."""
    _, entries = parse_maps()["$prometheus_class_raw"]
    found = [
        re.fullmatch(r"~\^/api/v1/\(([^)]*)\)\(\\\?\.\*\)\?\$", key)
        for key, value in entries
        if value == "r" and key.startswith("~^/api/v1/(")
    ]
    (alternation,) = found
    assert alternation is not None
    return {f"/api/v1/{name}" for name in alternation.group(1).split("|")}


def test_the_read_alternation_in_the_manifest_is_the_read_list_of_this_file() -> None:
    # EXTRACTED from the pattern, not restated: a path added to the pattern and not
    # to READ_PATHS (with its source), or the other way, fails here.
    plain = {p for p in READ_PATHS if p != "/" and "/label/" not in p}
    _, entries = parse_maps()["$prometheus_class_raw"]
    reads = [key for key, value in entries if value == "r"]

    assert read_alternation() == plain
    # Three patterns of class r and no more: `/`, the alternation and the label
    # values (one pattern for any label name).
    assert len(reads) == 3
    assert r"~^/api/v1/label/[A-Za-z_][A-Za-z0-9_]*/values(\?.*)?$" in reads
    for path in (p for p in READ_PATHS if "/label/" in p):
        assert class_of(path) == "r", path
    # The pages that were taken off in contract M4b are in neither.
    assert not set(REMOVED_STATUS_PATHS) & read_alternation()


def test_every_route_the_pinned_image_serves_is_a_read_the_write_or_closed() -> None:
    open_paths = {*READ_PATHS, WRITE_PATH}

    for path in sorted(ROUTES_SEEN):
        if path in open_paths:
            continue
        assert class_of(path) == "x", path
        for client in EVERY_CLIENT:
            assert denied(path, *client), (path, client)


def test_each_read_path_has_a_source_and_the_ones_from_smoke_are_in_smoke() -> None:
    smoke = "\n".join(p.read_text("utf-8") for p in (KIND_DIR / "smoke.d").glob("*.sh"))

    vocabulary = {"smoke", "grafana", "review", "probe", "nginx"}
    for path, source in READ_PATHS.items():
        assert source.split() and set(source.split()) <= vocabulary, path
    assert set(READ_PATHS) <= ROUTES_SEEN
    for path, source in READ_PATHS.items():
        if "smoke" in source.split():
            assert path in smoke, path
    # The list in the maps is the list here, no more and no less (the label values
    # are one pattern for any label name).
    assert not (set(READ_PATHS) - {"/"} - classified_as("r"))
    assert classified_as("r") <= set(READ_PATHS) | {"/api/v1/label/x/values"}


def classified_as(kind: str) -> set[str]:
    probes = {p for p in ROUTES_SEEN | {"/api/v1/label/x/values"}}
    return {p for p in probes if class_of(p) == kind}


def test_the_name_the_gateway_admits_is_the_name_the_collectors_policy_allows() -> None:
    allowed = policies()[COLLECTOR_CLIENT_POLICY]["spec"]["allowed"]["commonName"]
    _, entries = parse_maps()["$prometheus_denied"]
    admitted = [key for key, value in entries if value == "0" and key.startswith('"w:')]

    # One exact string admits a write, and its subject is the common name that the
    # client certificate carries and that the policy allows (and requires): the
    # same name as Loki's gateway admits.
    assert admitted == [f'"w:SUCCESS:CN={allowed["value"]}"']
    assert allowed == {"value": "otel-collector-client", "required": True}
    assert certificate(COLLECTOR_CLIENT)["spec"]["commonName"] == allowed["value"]
    assert f"CN={allowed['value']}" == WRITER
