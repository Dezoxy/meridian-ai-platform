#!/usr/bin/env python3
"""Fail when a step's pull request leaves the step's file unchanged (S102).

A pull request whose title starts with a step (``S102: …``, ``S072, first part: …``)
changes that step's file, ``docs/plan/steps/<folder of 20>/S0NN.md``: the step's record
is its file, and a pull request that does not touch it left the record behind. Any
other title (``docs: …``, ``chore: …``, a Renovate title) has nothing to check.

Inputs, from the environment and nothing else about the pull request:
``PR_TITLE`` (required; missing or empty is exit 2, fail closed) and ``PR_BASE`` (the
commit to diff against; ``HEAD^1`` without it: on a ``pull_request`` event the checkout
is the merge commit and its first parent is the base branch's tip).

The title is untrusted text. It is matched against one pattern, never passed to a
shell or into a command, and never printed: the messages print the step ID the
pattern matched and nothing else of it.

Exit 0: nothing to check, or the file is among the added, modified or renamed files.
Exit 1: it is not. Exit 2: no title, or git failed (never a pass).

Python 3 standard library only. Run from the repository root:
PR_TITLE='S102: …' python3 scripts/check_pr_step.py
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

# A step is S and three digits (as in the plan), then a colon, a comma or a space.
STEP_TITLE = re.compile(r"^(S[0-9]{3})[:, ]")
STEPS = "docs/plan/steps"


def step_path(step: str) -> str:
    """The step's file: ``S021`` is in ``S020-S039`` (folders of 20 steps)."""
    low = int(step[1:]) // 20 * 20
    return f"{STEPS}/S{low:03d}-S{low + 19:03d}/{step}.md"


def changed_files(base: str) -> list[str]:
    """The files the pull request adds, modifies or renames; raises OSError or
    CalledProcessError when git cannot say."""
    done = subprocess.run(
        # --end-of-options: a base that starts with a dash is a revision, not a flag.
        [
            "git",
            "diff",
            "--name-only",
            "--diff-filter=AMR",
            "--end-of-options",
            base,
            "HEAD",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.splitlines()


def main() -> int:
    title = os.environ.get("PR_TITLE", "")
    if not title.strip():
        print("pr step: PR_TITLE is missing or empty; cannot tell which step")
        return 2
    match = STEP_TITLE.match(title)
    if not match:
        print("pr step: the title names no step; nothing to check")
        return 0
    step, path = match[1], step_path(match[1])
    try:
        files = changed_files(os.environ.get("PR_BASE") or "HEAD^1")
    except subprocess.CalledProcessError as error:
        lines = (error.stderr or "").strip().splitlines()
        print(f"pr step: git failed: {lines[-1] if lines else 'no message'}")
        return 2
    except OSError as error:
        print(f"pr step: git could not be run: {error.strerror}")
        return 2
    if path in files:
        print(f"pr step: {step} changes its file, {path}")
        return 0
    print(
        f"pr step: {step} does not change {path}; the step's record is its file: "
        f"write there what this pull request decided, built and left open"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
