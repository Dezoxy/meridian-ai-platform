"""What the tests of Loki's gateway share (S072, contracts M3 and M3b): Loki's
values, the gateway's host and ports, and nginx's ``map`` rules as the tests model
them (an exact string beats a regular expression, the regular expressions go in
order, then the default). Prometheus's gateway is judged with the same rules, so
its tests read them here too.
"""

import re
from urllib.parse import unquote

import yaml
from certpolicysupport import KIND_DIR

LOKI_VALUES = KIND_DIR / "values" / "loki.yaml"
GATEWAY_PORT = 8443
GATEWAY_HOST = "loki-gateway.observability.svc.cluster.local"
LOKI_PORT = 3100

MAP = re.compile(r"map\s+(\"[^\"]+\"|\S+)\s+(\$\w+)\s*\{(.*?)\}", re.S)


def loki_values() -> dict:
    return yaml.safe_load(LOKI_VALUES.read_text(encoding="utf-8"))


def gateway_values() -> dict:
    return loki_values()["gateway"]


def evaluate(entries: list[tuple[str, str]], key: str) -> str:
    """nginx's map: an exact string wins over any regular expression, the
    regular expressions are tried in order, and `default` is the last resort."""
    exact = {k.strip('"'): v for k, v in entries if not k.startswith("~")}
    if key in exact and key != "default":
        return exact[key]
    for pattern, value in entries:
        if pattern.startswith("~") and re.search(pattern[1:], key):
            return value
    return exact["default"]


def normalised(raw: str) -> str:
    """What nginx's `$uri` is for a raw request URI: the path part, percent-
    decoded, with merged slashes (`merge_slashes` is on by default) and the dot
    segments resolved. The query string is not part of it."""
    path = unquote(raw.split("?", 1)[0])
    path = re.sub(r"/{2,}", "/", path)
    out: list[str] = []
    for segment in path.split("/"):
        if segment == ".":
            continue
        if segment == "..":
            if len(out) > 1:
                out.pop()
            continue
        out.append(segment)
    return "/".join(out) or "/"
