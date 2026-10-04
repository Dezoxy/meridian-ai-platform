"""Prepare Meridian's alert rules for promtool (S024).

``infra/kind/alerts/*.yaml`` are PrometheusRule manifests, which promtool
cannot read: it wants the rule file's own shape, the manifest's ``spec``.
``extract <out-dir>`` writes, for each manifest, ``<stem>.rules.yaml`` holding
its ``spec`` and copies each ``*.test.yaml`` unit-test file beside it, so
``promtool check rules`` and ``promtool test rules`` run on the output.

Run it as ``uv run python scripts/alert_rules.py extract .alerts`` (``make
alerts`` does). It prints file names only, never a file's content.
"""

import argparse
import shutil
import sys
from pathlib import Path

import yaml

ALERTS_DIR = Path(__file__).resolve().parent.parent / "infra" / "kind" / "alerts"
TEST_SUFFIX = ".test.yaml"
RULES_SUFFIX = ".rules.yaml"
KIND = "PrometheusRule"


class AlertRulesError(Exception):
    """A file that cannot be turned into a rule file; the message names it."""


def manifests(alerts_dir: Path) -> list[Path]:
    """The PrometheusRule manifests: every ``*.yaml`` that is not a test file."""
    return sorted(
        path
        for path in alerts_dir.glob("*.yaml")
        if not path.name.endswith(TEST_SUFFIX)
    )


def unit_tests(alerts_dir: Path) -> list[Path]:
    return sorted(alerts_dir.glob(f"*{TEST_SUFFIX}"))


def spec_of(path: Path) -> dict:
    """The manifest's ``spec``, or ``AlertRulesError`` naming the file when it
    is not YAML, not a PrometheusRule or holds no groups."""
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        problem = getattr(error, "problem", None) or "cannot be parsed"
        raise AlertRulesError(f"{path.name}: not valid YAML ({problem})") from error
    if not isinstance(document, dict) or document.get("kind") != KIND:
        raise AlertRulesError(f"{path.name}: not a {KIND}")
    spec = document.get("spec")
    if not isinstance(spec, dict) or not spec.get("groups"):
        raise AlertRulesError(f"{path.name}: no groups under spec")
    return spec


def remove_stale(out_dir: Path, wanted: list[Path]) -> None:
    """Remove the ``*.yaml`` files an earlier run left, the wanted ones apart."""
    for stale in out_dir.glob("*.yaml"):
        if stale not in wanted:
            stale.unlink()


def extract(out_dir: Path, alerts_dir: Path = ALERTS_DIR) -> list[Path]:
    """Write the rule files and copy the test files; the paths written. Every
    manifest is read before anything in the folder changes, so a broken one
    leaves the previous output as it was. A wanted file is rewritten in place,
    never deleted first: a container that mounts the folder (promtool, on
    Docker Desktop) can otherwise still see it as gone."""
    specs = {path.stem: spec_of(path) for path in manifests(alerts_dir)}
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for stem, spec in specs.items():
        target = out_dir / f"{stem}{RULES_SUFFIX}"
        target.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
        written.append(target)
    for source in unit_tests(alerts_dir):
        written.append(Path(shutil.copy(source, out_dir / source.name)))
    remove_stale(out_dir, written)
    return written


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("extract", help="write promtool's input files")
    command.add_argument("out_dir", type=Path)
    arguments = parser.parse_args(argv)
    try:
        written = extract(arguments.out_dir)
    except AlertRulesError as error:
        print(error, file=sys.stderr)
        return 1
    print(f"{len(written)} file(s) written to {arguments.out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
