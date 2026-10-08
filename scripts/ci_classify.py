#!/usr/bin/env python3
"""Decide whether a pull request changes nothing but documents (S074).

CI's ``classify`` job runs this. When it says ``docs_only=true`` the four shards
of the suite are skipped and one job runs the tests that read documents
(``tests/documents-group.txt``); in every other case all the tests run. The one
failure to design against is a "true" for a change that can break code, so:

- the rule is a CLOSED allowlist: ``docs/`` (any depth) and, at the root,
  ``README.md``, ``CLAUDE.md``, ``AGENTS.md``, ``NOTICE`` and ``LICENSE``. A
  README beside code, such as ``infra/kind/README.md``, is not on it: tests read
  those, and the group would have to hold them all, so they run everything;
- every doubt is "false": an event that is not a pull request (a push to main
  always runs everything), no base, a diff that fails, a diff that lists
  nothing, and a path that is not a plain repository path;
- the diff is ``git diff --name-only --no-renames -z BASE HEAD``: without
  ``--no-renames`` a file moved out of ``src/`` into ``docs/`` would be listed
  by its new name only, and the code it left would go unseen.

The job prints the changed files and the verdict, and writes ``docs_only`` and
``shard_list`` (the shards' numbers as JSON, from the one count the workflow
holds) to ``$GITHUB_OUTPUT`` for the jobs that need them.

Run: python3 scripts/ci_classify.py --event EVENT --base SHA --shards N
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

DOCUMENTS_FOLDER = "docs/"
ROOT_DOCUMENTS = frozenset({"README.md", "CLAUDE.md", "AGENTS.md", "NOTICE", "LICENSE"})


def parse_shards(text: str) -> int:
    """The number of shards, from a string of ASCII digits and nothing else."""
    if not (text.isascii() and text.isdigit()) or int(text) < 1:
        raise ValueError(f"the shard count {text!r} is not a positive whole number")
    return int(text)


def shard_list(count: int) -> list[int]:
    return list(range(1, count + 1))


def _plain(path: str) -> bool:
    """A repository path as git writes it: no empty, dot or dot-dot part."""
    if not path or path.startswith("/") or "\\" in path:
        return False
    if any(ord(char) < 32 or ord(char) == 127 for char in path):
        return False
    return all(part not in ("", ".", "..") for part in path.split("/"))


def on_the_allowlist(path: str) -> bool:
    if not _plain(path):
        return False
    return path in ROOT_DOCUMENTS or path.startswith(DOCUMENTS_FOLDER)


def documents_only(event: str, changed: list[str] | None) -> bool:
    """True only for a pull request whose every changed file is on the list."""
    if event != "pull_request" or not changed:
        return False
    return all(on_the_allowlist(path) for path in changed)


def changed_files(base: str) -> list[str] | None:
    """The files changed from BASE to HEAD, or None when git cannot say."""
    if not base:
        return None
    done = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames", "-z", base, "HEAD"],
        capture_output=True,
        check=False,
    )
    if done.returncode != 0:
        sys.stderr.write(done.stderr.decode("utf-8", "replace"))
        return None
    names = [name for name in done.stdout.split(b"\0") if name]
    return [name.decode("utf-8", "replace") for name in names]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--event", required=True, help="GitHub's event name")
    parser.add_argument("--base", default="", help="the pull request's base commit")
    parser.add_argument("--shards", required=True, help="the number of shards")
    arguments = parser.parse_args(argv)
    try:
        shards = shard_list(parse_shards(arguments.shards))
    except ValueError as error:
        print(f"ci_classify: {error}", file=sys.stderr)
        return 2

    changed = None
    if arguments.event == "pull_request":
        changed = changed_files(arguments.base)
    verdict = documents_only(arguments.event, changed)

    print(f"event: {arguments.event}")
    if changed is None:
        print("changed files: not read (not a pull request, or git failed)")
    else:
        print(f"changed files: {len(changed)}")
        for path in changed:
            # ascii() so that a file name cannot start a line with ::, which the
            # runner reads as a command, or hold a control character.
            print(f"  - {ascii(path)}")
    print(f"docs_only={str(verdict).lower()}")

    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"docs_only={str(verdict).lower()}\n")
            handle.write(f"shard_list={json.dumps(shards, separators=(',', ':'))}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
