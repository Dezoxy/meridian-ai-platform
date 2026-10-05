"""Load the seven registry files into a ``Registry``, or say everything wrong."""

from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from meridian.platform.registry.checks import run_checks
from meridian.platform.registry.models import FILE_MODELS, Registry

YAML_SUFFIXES = frozenset({".yaml", ".yml"})
MERGE_TAG = "tag:yaml.org,2002:merge"
TIMESTAMP_TAG = "tag:yaml.org,2002:timestamp"


class RegistryError(Exception):
    """Every problem found, as ``file: path: message`` strings."""

    def __init__(self, errors: Iterable[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("\n".join(self.errors))


class UniqueKeyLoader(yaml.SafeLoader):
    """A SafeLoader for reviewable files.

    It refuses a repeated mapping key instead of keeping the last, and refuses
    anchors, aliases and merge keys, which hide the effective values from a
    reviewer. A calendar-invalid date is a YAML error with its line, not a
    ``ValueError``.
    """

    def compose_node(self, parent: Any, index: Any) -> yaml.Node | None:
        event = self.peek_event()
        if getattr(event, "anchor", None) is not None:  # anchors and aliases
            raise yaml.composer.ComposerError(
                None, None, "anchors and aliases are not allowed", event.start_mark
            )
        return super().compose_node(parent, index)

    def construct_mapping(
        self, node: yaml.MappingNode, deep: bool = False
    ) -> dict[Any, Any]:
        seen: set[Any] = set()
        for key_node, _ in node.value:
            if key_node.tag == MERGE_TAG:
                raise yaml.constructor.ConstructorError(
                    None, None, "merge keys are not allowed", key_node.start_mark
                )
            key = self.construct_object(key_node, deep=True)
            try:
                is_repeat = key in seen
                seen.add(key)
            except TypeError:
                continue  # unhashable key: the base class reports it
            if is_repeat:
                raise yaml.constructor.ConstructorError(
                    None, None, f"repeated key {key!r}", key_node.start_mark
                )
        return super().construct_mapping(node, deep=deep)

    def construct_checked_timestamp(self, node: yaml.ScalarNode) -> Any:
        try:
            return self.construct_yaml_timestamp(node)
        except ValueError as exc:
            raise yaml.constructor.ConstructorError(
                None, None, f"invalid date {node.value!r}: {exc}", node.start_mark
            ) from exc


UniqueKeyLoader.add_constructor(
    TIMESTAMP_TAG, UniqueKeyLoader.construct_checked_timestamp
)


def _yaml_error(name: str, text: str, exc: yaml.YAMLError) -> str:
    if isinstance(exc, yaml.reader.ReaderError):
        line = f"line {text.count(chr(10), 0, max(exc.position, 0)) + 1}: "
        return f"{name}: {line}{exc.reason}"
    mark = getattr(exc, "problem_mark", None)
    problem = getattr(exc, "problem", None) or str(exc)
    line = f"line {mark.line + 1}: " if mark is not None else ""
    return f"{name}: {line}{problem}"


def _format_path(location: tuple[int | str, ...]) -> str:
    path = ""
    for part in location:
        path += f"[{part}]" if isinstance(part, int) else f".{part}"
    return path.lstrip(".") or "(file)"


def _validation_errors(name: str, exc: ValidationError) -> list[str]:
    return [f"{name}: {_format_path(err['loc'])}: {err['msg']}" for err in exc.errors()]


def _parse_file(path: Path) -> tuple[Any, list[str]]:
    """Return the parsed document, or the errors that stopped it."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return None, [f"{path.name}: cannot read: {exc}"]
    try:
        # UniqueKeyLoader is a SafeLoader subclass.
        return yaml.load(text, Loader=UniqueKeyLoader), []  # noqa: S506
    except yaml.YAMLError as exc:
        return None, [_yaml_error(path.name, text, exc)]
    except RecursionError:
        return None, [f"{path.name}: nested too deeply"]


def _directory_errors(directory: Path) -> list[str]:
    expected = {f"{stem}.yaml" for stem in FILE_MODELS}
    missing = [n for n in sorted(expected) if not (directory / n).is_file()]
    strays = sorted(
        p.name
        for p in directory.iterdir()
        if p.is_file() and p.suffix in YAML_SUFFIXES and p.name not in expected
    )
    return [f"{name}: file is missing" for name in missing] + [
        f"{name}: not a registry file (expected {', '.join(sorted(expected))})"
        for name in strays
    ]


def _build_registry(files: dict[str, Any]) -> Registry:
    return Registry(
        providers=files["providers"].providers,
        deployments=files["models"].deployments,
        servers=files["tools"].servers,
        tools=files["tools"].tools,
        agents=files["agents"].agents,
        data_classes=files["policies"].data_classes,
        routes=files["policies"].routes,
        replay=files["policies"].replay,
        recorded=files["policies"].recorded,
        tenants=files["tenants"].tenants,
        exchange=files["tenants"].exchange,
        services=files["services"].services,
    )


def unreadable_directory(directory: Path, exc: OSError) -> str:
    """The problem of a directory the process cannot read: the error's class,
    never its text (which holds a path)."""
    return f"{directory}: registry directory cannot be read: {type(exc).__name__}"


def _present_files(directory: Path) -> tuple[list[str], dict[str, Path]]:
    """The directory's own problems and the registry files that are there."""
    if not directory.is_dir():
        raise RegistryError([f"{directory}: registry directory not found"])
    paths = {
        stem: directory / f"{stem}.yaml"
        for stem in FILE_MODELS
        if (directory / f"{stem}.yaml").is_file()
    }
    return _directory_errors(directory), paths


def load_registry(directory: Path) -> Registry:
    """Parse, validate and cross-check ``directory``; raise ``RegistryError``."""
    try:
        errors, paths = _present_files(directory)
    except OSError as exc:
        raise RegistryError([unreadable_directory(directory, exc)]) from None
    files: dict[str, Any] = {}
    for stem, path in paths.items():
        model = FILE_MODELS[stem]
        document, parse_errors = _parse_file(path)
        if parse_errors:
            errors += parse_errors
            continue
        try:
            files[stem] = model.model_validate(document)
        except ValidationError as exc:
            errors += _validation_errors(path.name, exc)
    if errors:
        raise RegistryError(errors)
    registry = _build_registry(files)
    if problems := run_checks(registry):
        raise RegistryError(problems)
    return registry
