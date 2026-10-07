"""Reading the Azure platform module's text, and running its validations (S020).

``infra/terraform/azure`` is checked by ``terraform validate`` and by tests that
read its files: no account exists to plan against, and nothing here has seen an
Azure API. The helpers below are the form ``test_gcp_module.py`` wrote inline,
moved here because the module's contracts (Z1 to Z4) each add a test file that
reads the same files: ``resources_in(file_name)`` is how a later file lists what
it wrote.

The tests that run ``terraform console`` carry ``terraformsupport.needs_terraform``,
not a marker of this module's own: where Terraform is missing it skips the test
on a development machine and fails it under ``GITHUB_ACTIONS=true``.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_DIR = REPO_ROOT / "infra" / "terraform" / "azure"
FOUNDATION_DIR = REPO_ROOT / "infra" / "terraform" / "foundation"
GCP_DIR = REPO_ROOT / "infra" / "terraform" / "gcp"

REFUSED = "Invalid value for variable"


def without_comments(text: str) -> str:
    """The text with each ``#`` comment removed, line by line. A ``#`` inside a
    double-quoted string (a URL fragment, a message) is part of the string and
    stays; a backslash keeps the quote after it from ending the string."""
    return "\n".join(_without_comment(line) for line in text.splitlines())


def _without_comment(line: str) -> str:
    in_string = False
    escaped = False
    for index, char in enumerate(line):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "#":
            return line[:index].rstrip()
    return line


def tf_files(directory: Path = MODULE_DIR) -> list[Path]:
    return sorted(directory.glob("*.tf"))


def raw_text(name: str, directory: Path = MODULE_DIR) -> str:
    """A file as written, comments and all: for what a comment must say."""
    return (directory / name).read_text(encoding="utf-8")


def file_text(name: str, directory: Path = MODULE_DIR) -> str:
    """A file with its comments removed: a word in a comment is not a setting."""
    return without_comments(raw_text(name, directory))


def comments_of(name: str, directory: Path = MODULE_DIR) -> str:
    """The lines of a file that are a comment and nothing else, squeezed: what a
    comment must say, with no code line to satisfy a word the code also holds."""
    lines = raw_text(name, directory).splitlines()
    return squeezed("\n".join(line for line in lines if line.lstrip().startswith("#")))


def module_text() -> str:
    return "\n".join(file_text(path.name) for path in tf_files())


def squeezed(text: str) -> str:
    """A comment wraps where it likes: the words, joined by single spaces and
    without the comment markers."""
    return " ".join(re.sub(r"^\s*#", "", text, flags=re.MULTILINE).split())


def top_level_blocks(text: str, kind: str) -> dict[str, str]:
    """The bodies of the top-level blocks of one kind (``resource``, ``data``,
    ``variable``...), keyed by their labels joined with a dot. A block is
    ``kind "a" "b" {``, a newline, its body and a closing brace at the start of
    a line (``terraform fmt`` puts it there), or ``{}`` alone on the line for an
    empty one, whose body is the empty string."""
    found = re.findall(
        rf'^{kind} ((?:"[^"]+"\s*)+)\{{(?:\}}$|\n(.*?)^\}}$)',
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    return {".".join(re.findall(r'"([^"]+)"', labels)): body for labels, body in found}


def started_blocks(text: str, kind: str) -> list[str]:
    """The labels of every line that STARTS a block of this kind, however the
    block is written: what ``top_level_blocks`` must equal, so that a block
    written on one line, where its reader cannot see it, is still counted."""
    return [
        ".".join(re.findall(r'"([^"]+)"', labels))
        for labels in re.findall(rf"^{kind} ((?:\"[^\"]+\"\s*)+)", text, re.MULTILINE)
    ]


def resources_in(file_name: str) -> dict[str, str]:
    """The resources one file declares, keyed ``type.name``, with their bodies
    (comments removed). Later contracts' tests list what each file holds."""
    return top_level_blocks(file_text(file_name), "resource")


def data_sources_in(file_name: str) -> dict[str, str]:
    return top_level_blocks(file_text(file_name), "data")


def resources() -> dict[str, str]:
    return top_level_blocks(module_text(), "resource")


def data_sources() -> dict[str, str]:
    return top_level_blocks(module_text(), "data")


def variable_blocks() -> dict[str, str]:
    return top_level_blocks(file_text("variables.tf"), "variable")


def variable_block(name: str) -> str:
    return variable_blocks()[name]


def has_attribute(block: str, name: str) -> bool:
    return (
        re.search(rf"^\s*{re.escape(name)}\s*=", block, flags=re.MULTILINE) is not None
    )


def attribute(block: str, name: str) -> str | None:
    """The text right of the equals sign of a one-line attribute, or ``None``."""
    found = re.search(rf"^\s*{re.escape(name)}\s*=\s*(.+)$", block, flags=re.MULTILINE)
    return found.group(1).strip() if found else None


def local_string(name: str, file_name: str = "main.tf") -> str:
    """The value of a ``name = "value"`` line of the module's locals."""
    (value,) = re.findall(
        rf'^\s*{re.escape(name)}\s*=\s*"([^"]*)"$',
        file_text(file_name),
        flags=re.MULTILINE,
    )
    return value


def quoted_list_in(text: str, anchor: str) -> list[str]:
    """The quoted strings of the list that ``contains([...], <anchor>)`` tests."""
    (items,) = re.findall(
        rf"contains\(\[(.*?)\],\s*{re.escape(anchor)}\)", text, flags=re.DOTALL
    )
    return re.findall(r'"([^"]+)"', items)


def listed_in(text: str, anchor: str) -> list[str]:
    """The items, as written, of the list that ``contains([...], <anchor>)``
    tests: quoted strings stay quoted, numbers stay numbers."""
    (items,) = re.findall(
        rf"contains\(\[(.*?)\],\s*{re.escape(anchor)}\)", text, flags=re.DOTALL
    )
    return [item.strip() for item in items.split(",") if item.strip()]


# ── terraform console ────────────────────────────────────────────────────────


def _tf_var(literal: str) -> str:
    if len(literal) >= 2 and literal.startswith('"') and literal.endswith('"'):
        return literal[1:-1]
    return literal


def evaluate(tmp_path: Path, values: dict[str, str], name: str) -> str:
    """Everything ``terraform console`` prints for ``var.<name>``, with the
    variables in ``values`` set, written as HCL literals: a string in double
    quotes, a number bare, a list in brackets. ``TF_VAR_`` takes a string or a
    number variable raw, with no quotes (a quoted one is a value of its own, with
    the quotes in it, and every closed list would refuse it), so a quoted literal
    is passed as its content and a list as it is. The
    scratch copy holds ``variables.tf`` and nothing else: no provider, no
    backend, no account, no sign-in; a few kilobytes, so ``tmp_path`` is where it
    lives, and the test removes nothing."""
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    shutil.copy2(MODULE_DIR / "variables.tf", scratch / "variables.tf")
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        **{f"TF_VAR_{key}": _tf_var(value) for key, value in values.items()},
    }
    done = subprocess.run(
        ["terraform", f"-chdir={scratch}", "console", "-no-color"],
        input=f"var.{name}\n",
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    return done.stdout + done.stderr


# ── nested blocks of a resource ──────────────────────────────────────────────


def nested_blocks(body: str, name: str) -> list[str]:
    """The bodies of the blocks called ``name`` written one level inside a
    resource's body (two spaces in: ``terraform fmt`` indents them so), in the
    order written. A block with no body at all gives the empty string."""
    return re.findall(
        rf"^  {re.escape(name)} \{{(?:\}}$|\n(.*?)^  \}}$)",
        body,
        flags=re.MULTILINE | re.DOTALL,
    )


def own_text(body: str) -> str:
    """A resource's body with its nested blocks taken out, so that an attribute
    read from it is the resource's own and not a nested block's of the same
    name (``name``, ``type``, ``location``)."""
    return re.sub(
        r"^  \w+ \{(?:\}\n|\n.*?^  \}\n)",
        "",
        body + "\n",
        flags=re.MULTILINE | re.DOTALL,
    )


# ── what the database's contract (Z3) reads ──────────────────────────────────


def ephemerals_in(file_name: str) -> dict[str, str]:
    """The ``ephemeral`` blocks one file declares, keyed ``type.name``: values
    Terraform never stores, which only a write-only argument may take."""
    return top_level_blocks(file_text(file_name), "ephemeral")


def locals_text() -> str:
    """The bodies of every ``locals`` block of the module, comments removed."""
    return "\n".join(
        re.findall(
            r"^locals \{\n(.*?)^\}$", module_text(), flags=re.MULTILINE | re.DOTALL
        )
    )


def outputs() -> dict[str, str]:
    """The module's output blocks (none until a later contract writes them)."""
    return top_level_blocks(module_text(), "output")


def lines_naming(text: str, needle: str) -> list[str]:
    """The stripped lines of ``text`` that contain ``needle``."""
    return [line.strip() for line in text.splitlines() if needle in line]


def argument_name(line: str) -> str | None:
    """The name left of the equals sign of an ``name = value`` line, or ``None``."""
    found = re.match(r"\s*([A-Za-z_][\w-]*)\s*=", line)
    return found.group(1) if found else None


# ── what the identities' contract (Z4) reads ─────────────────────────────────


def role_assignments_everywhere() -> dict[str, str]:
    """Every role assignment of the whole module, keyed ``type.name``: a file a
    later contract adds is read too, so no assignment hides in it."""
    return {
        name: body
        for name, body in resources().items()
        if name.startswith("azurerm_role_assignment.")
    }


def argument_names(body: str) -> list[str]:
    """The name left of the equals sign of every ``name = value`` line of a
    block's text, nested blocks included, in the order written."""
    names = (argument_name(line) for line in body.splitlines())
    return [name for name in names if name is not None]
