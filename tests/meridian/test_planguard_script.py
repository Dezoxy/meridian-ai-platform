"""The cloud-neutral protections of the Terraform wrapper (S020).

``infra/terraform/planguard.sh`` holds, moved from ``aws.sh`` (three texts were
reworded to name no cloud), the checks that name no cloud: the Terraform calls,
the git calls with less of the caller's configuration, the refusal of a variable
or override file, the default workspace, the plan record and the tree checks.
``aws.sh`` sources it the way it sources ``common.sh``, and a wrapper for another
cloud will source the same file, so that the protections are one copy.

Most tests read the three files as text; one runs the wrapper to hold a message.
What the commands do with the moved code is held by ``test_aws_script.py`` and
``test_aws_script_plan.py``, which run the wrapper against stand-in programs.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest
from awsscriptsupport import (
    MODULE_IDS,
    MODULES,
    TERRAFORM_DIR,
    Module,
    make_tree,
    with_local_file,
)

AWS_SH = TERRAFORM_DIR / "aws.sh"
COMMON_SH = TERRAFORM_DIR / "common.sh"
PLANGUARD_SH = TERRAFORM_DIR / "planguard.sh"


def source_line(name: str) -> str:
    """The line with which a wrapper sources a file that sits beside it."""
    return '. "$(dirname "${BASH_SOURCE[0]}")/' + name + '"'


# ── the closed list ──────────────────────────────────────────────────────────

# A word of a cloud, comments included. The last three alternatives are the
# shapes of a region's name: eu-central-1 (AWS), europe-west3 (Google Cloud) and
# the names Azure's regions have.
CLOUD_WORD = re.compile(
    r"\b(?:aws|azure|gcp|eks|aks)\b"
    r"|\b(?:AWS|ARM|GOOGLE)_"
    r"|\b[a-z]{2}-[a-z]+-[0-9]\b"
    r"|\b[a-z]+-[a-z]+[0-9]\b"
    r"|\b(?:westeurope|northeurope|swedencentral|germanywestcentral)\b",
    re.IGNORECASE,
)


@pytest.mark.parametrize(
    "sample",
    [
        "the aws CLI",
        "AWS_REGION",
        "the Azure subscription",
        "ARM_SUBSCRIPTION_ID",
        "a gcp project",
        "GOOGLE_PROJECT",
        "an EKS cluster",
        "an AKS cluster",
        "eu-central-1",
        "europe-west3",
        "westeurope",
    ],
)
def test_the_closed_list_finds_each_kind_of_word_it_is_made_for(sample: str) -> None:
    assert CLOUD_WORD.search(sample)


@pytest.mark.parametrize(
    "sample",
    ["the tasks of the weeks", "sha256 and git 2.32", "a leaked key", "terraform init"],
)
def test_the_closed_list_leaves_ordinary_words_alone(sample: str) -> None:
    assert CLOUD_WORD.search(sample) is None


def test_the_wrapper_itself_is_not_neutral_so_the_list_can_fail() -> None:
    assert CLOUD_WORD.search(AWS_SH.read_text(encoding="utf-8"))


def test_planguard_holds_no_word_of_a_cloud_comments_included() -> None:
    lines = PLANGUARD_SH.read_text(encoding="utf-8").splitlines()

    found = [
        f"{number}: {line}"
        for number, line in enumerate(lines, start=1)
        if CLOUD_WORD.search(line)
    ]

    assert lines != []
    assert found == []


# ── what kind of file it is ──────────────────────────────────────────────────


def test_planguard_is_sourced_and_not_executable() -> None:
    assert not os.access(PLANGUARD_SH, os.X_OK)


def test_run_directly_planguard_says_what_it_is_and_exits_non_zero() -> None:
    done = subprocess.run(
        ["bash", str(PLANGUARD_SH)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert done.returncode != 0
    assert "sourced" in done.stderr
    assert done.stdout == ""


# ── what the shell would resolve ─────────────────────────────────────────────

FUNCTION_DEFINITION = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\(\) \{", re.MULTILINE)
READONLY_DEFINITION = re.compile(r"^readonly ([A-Za-z_][A-Za-z0-9_]*)=", re.MULTILINE)

# Not functions of these files: the shell's own words, and the programs the
# wrapper runs. Anything else a command position names has to be defined.
NOT_OURS = frozenset(
    [
        # keywords and the closers of a block
        *["fi", "done", "esac", "for", "case"],
        # builtins
        *["local", "readonly", "unset", "shift", "exit", "set", "shopt", "read"],
        *["printf", "umask", "true", "return", "continue", "export"],
        # programs
        *["env", "git", "grep", "tr", "wc", "cat", "stat", "date", "mkdir", "chmod"],
        *["rm", "sha256sum", "shasum", "basename", "dirname", "compgen"],
    ]
)
KEYWORDS_KEEPING_A_COMMAND_POSITION = frozenset(
    ["if", "then", "else", "elif", "while", "until", "do", "!", "{", "time"]
)
PLAIN_WORD = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?=")
OPERATOR_CHARACTERS = ";&|()<>"


class Scanner:
    """The words a shell file puts where a command goes, found without a shell.

    It knows quotes, ``$( )``, ``${ }``, ``$(( ))``, ``(( ))``, ``[[ ]]``, arrays
    after ``=``, redirections and the patterns of a ``case``: enough for the
    scripts of this directory, and not a parser of shell in general."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.commands: list[str] = []
        self.defined: list[str] = []

    def run(self) -> "Scanner":
        self.scan(0, nested=False)
        return self

    # -- pieces ---------------------------------------------------------------

    def skip_balanced(self, i: int, opener: str, closer: str) -> int:
        """The index after the closer that matches the opener at ``i``."""
        depth = 0
        while i < len(self.text):
            char = self.text[i]
            if char == "\\":
                i += 2
                continue
            if char in "'\"":
                i = self.skip_quoted(i)
                continue
            if char == opener:
                depth += 1
            elif char == closer:
                depth -= 1
                if depth == 0:
                    return i + 1
            i += 1
        return i

    def skip_quoted(self, i: int) -> int:
        quote = self.text[i]
        i += 1
        while i < len(self.text) and self.text[i] != quote:
            if quote == '"' and self.text[i] == "\\":
                i += 2
            elif quote == '"' and self.text.startswith("$((", i):
                i = self.skip_balanced(i + 1, "(", ")")
            elif quote == '"' and self.text.startswith("$(", i):
                i = self.scan(i + 2, nested=True)
            elif quote == '"' and self.text.startswith("${", i):
                i = self.skip_balanced(i + 1, "{", "}")
            else:
                i += 1
        return i + 1

    def read_word(self, i: int, breaks_at_operators: bool = True) -> tuple[str, int]:
        """A word and the index after it; a word with a quote or an expansion in
        it comes back as ``"<complex>"``, because it names no command."""
        start, plain = i, True
        while i < len(self.text):
            char = self.text[i]
            if char in " \t\n":
                break
            if breaks_at_operators and char in OPERATOR_CHARACTERS:
                if (
                    char == "("
                    and i > start
                    and self.text[i - 1] == "="
                    and not self.text.startswith("<(", i - 1)
                ):
                    i = self.skip_balanced(i, "(", ")")
                    plain = False
                    continue
                break
            if char == "\\":
                i, plain = i + 2, False
            elif char in "'\"":
                i, plain = self.skip_quoted(i), False
            elif self.text.startswith("$((", i):
                i, plain = self.skip_balanced(i + 1, "(", ")"), False
            elif self.text.startswith("$(", i):
                i, plain = self.scan(i + 2, nested=True), False
            elif self.text.startswith("${", i):
                i, plain = self.skip_balanced(i + 1, "{", "}"), False
            else:
                i += 1
        return (self.text[start:i] if plain else "<complex>"), i

    # -- the scan ---------------------------------------------------------------

    def scan(self, i: int, *, nested: bool) -> int:
        text = self.text
        at_command, case_header, expecting_pattern, depth = True, False, False, 0
        while i < len(text):
            char = text[i]
            if char in " \t":
                i += 1
            elif char == "\n":
                i, at_command = i + 1, True
            elif char == "#":
                while i < len(text) and text[i] != "\n":
                    i += 1
            elif expecting_pattern:
                if text.startswith("esac", i) and not text[i + 4 : i + 5].isalnum():
                    expecting_pattern = False
                    continue
                while i < len(text) and text[i] != ")":
                    word, i = self.read_word(i)
                    while i < len(text) and text[i] in " \t\n|(":
                        i += 1
                i, at_command, expecting_pattern = i + 1, True, False
            elif at_command and text.startswith("((", i):
                i, at_command = self.skip_balanced(i, "(", ")"), False
            elif text.startswith(";;", i):
                i, at_command, expecting_pattern = i + 2, True, True
            elif char == ")" and depth == 0 and nested:
                return i + 1
            elif char == ")":
                i, depth = i + 1, max(depth - 1, 0)
            elif char == "(":
                i, depth, at_command = i + 1, depth + 1, True
            elif char in ";&|":
                i, at_command = i + 1, True
            elif char in "<>":
                if text.startswith(("<(", ">("), i):
                    i = self.scan(i + 2, nested=True)
                    continue
                while i < len(text) and text[i] in "<>&|":
                    i += 1
                while i < len(text) and text[i] in " \t":
                    i += 1
                if text.startswith(("<(", ">("), i):
                    i = self.scan(i + 2, nested=True)
                else:
                    _, i = self.read_word(i)
            else:
                word, i = self.read_word(i)
                if (
                    at_command
                    and word != "<complex>"
                    and re.match(r"\s*\(\)", text[i:])
                ):
                    self.defined.append(word)
                    i = text.index(")", i) + 1
                elif word == "[[" and at_command:
                    while i < len(text) and not word.startswith("]]"):
                        while i < len(text) and text[i] in " \t\n":
                            i += 1
                        word, i = self.read_word(i, breaks_at_operators=False)
                    i -= len(word) - 2 if word.startswith("]]") else 0
                    at_command = False
                elif at_command and (
                    word in KEYWORDS_KEEPING_A_COMMAND_POSITION
                    or ASSIGNMENT.match(word)
                ):
                    pass
                elif at_command:
                    if PLAIN_WORD.match(word):
                        self.commands.append(word)
                    case_header = word == "case"
                    at_command = False
                elif word == "in" and case_header:
                    case_header, expecting_pattern = False, True
        return i


def code_of(text: str) -> str:
    """The text without its comment-only lines."""
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def called_and_not_ours(text: str) -> set[str]:
    return set(Scanner(text).run().commands) - NOT_OURS


def defined_in(text: str) -> set[str]:
    return set(FUNCTION_DEFINITION.findall(text))


def test_the_scanner_finds_a_call_in_each_position_the_wrapper_puts_one() -> None:
    text = """\
frobnicate_one
x=1 frobnicate_two "arg" && frobnicate_three || frobnicate_four
[[ -n "$(frobnicate_five)" ]] || die "$(frobnicate_six "a")"
case "$1" in
  word | other) frobnicate_seven ;;
  *) not_a_call_but_a_pattern=1 ;;
esac
while read -r line; do frobnicate_eight; done < <(frobnicate_nine | tr a b)
if ((n > 0)); then frobnicate_ten; fi
name() {
  frobnicate_eleven
}
local -a list=(not_a_call and_not_this)
"""

    scanned = Scanner(text).run()

    assert set(scanned.commands) >= {
        "frobnicate_one",
        "frobnicate_two",
        "frobnicate_three",
        "frobnicate_four",
        "frobnicate_five",
        "frobnicate_six",
        "frobnicate_seven",
        "frobnicate_eight",
        "frobnicate_nine",
        "frobnicate_ten",
        "frobnicate_eleven",
    }
    assert scanned.defined == ["name"]
    for pattern_or_argument in (
        "word",
        "other",
        "n",
        "not_a_call",
        "and_not_this",
        "arg",
        "line",
    ):
        assert pattern_or_argument not in scanned.commands


def test_a_call_to_a_function_nobody_defines_is_found_by_the_check() -> None:
    text = "good_one() {\n  :\n}\ngood_one\nmissing_one\n"

    assert called_and_not_ours(text) - defined_in(text) == {"missing_one"}


def test_every_function_the_wrapper_calls_is_defined_in_a_file_it_reads() -> None:
    wrapper = AWS_SH.read_text(encoding="utf-8")
    defined = (
        defined_in(wrapper)
        | defined_in(COMMON_SH.read_text(encoding="utf-8"))
        | defined_in(PLANGUARD_SH.read_text(encoding="utf-8"))
    )

    called = called_and_not_ours(wrapper)

    # The scan reached the wrapper's own commands, the moved ones and the shared.
    assert {
        "select_module",
        "cmd_plan",
        "load_aws_env",
        "require_pinned_account",
        "current_commit",
        "require_a_plan_that_is_this_trees_and_fresh",
        "init_with_state",
        "redact",
        "die",
    } <= called
    assert sorted(called - defined) == []


# What a wrapper must provide before it sources a function of planguard.sh runs.
# The functions: run_clean alone, because the environment a program is given
# (the names it keeps, the variables it is handed) is the cloud's own. The
# globals: the selected module's row, the words of some sentences and the README
# the sentences cite. The header of planguard.sh lists the same names.
WRAPPER_FUNCTIONS = {"run_clean"}
WRAPPER_GLOBALS = {
    "MODULE_DIR",
    "MODULE_REL",
    "MODULE_NAME",
    "PLAN_FILE",
    "PLAN_RECORD_FILE",
    "MODULE_README",
    "CMD_PLAN",
    "STATE_DIR_UNDER_HOME",
    "STATE_FILE_NAME",
    "SHARED_README",
    "ENVIRONMENT_WORDS",
    "WORKSPACE_WORDS",
}
# The globals only the local state's function reads (prepare_state). A wrapper
# whose state is remote never calls prepare_state or init_with_state, so it may
# leave these three unset; the rest of WRAPPER_GLOBALS it must set.
LOCAL_STATE_GLOBALS = {"STATE_DIR_UNDER_HOME", "STATE_FILE_NAME", "ENVIRONMENT_WORDS"}
LOCAL_STATE_FUNCTIONS = {"prepare_state", "init_with_state"}

VARIABLE_REFERENCE = re.compile(r"\$\{?[!#]?([A-Z][A-Z0-9_]*)\b")
ASSIGNED_NAME = re.compile(r"^\s*(?:readonly |local )?([A-Z][A-Z0-9_]*)=", re.MULTILINE)
# Set by the shell or the caller's environment, not by a wrapper.
FROM_THE_ENVIRONMENT = frozenset({"HOME", "BASH_REMATCH", "BASH_SOURCE"})


def globals_read_from_outside(text: str) -> set[str]:
    """The upper-case variables a file reads and never assigns itself."""
    code = code_of(text)
    return (
        set(VARIABLE_REFERENCE.findall(code))
        - set(ASSIGNED_NAME.findall(code))
        - FROM_THE_ENVIRONMENT
    )


def listed_in_header(text: str, label: str) -> set[str]:
    """The names after ``#   <label>:`` in the header, and on the indented lines
    of comment that follow it."""
    lines = text.splitlines()
    start = next(n for n, line in enumerate(lines) if line.startswith(f"#   {label}:"))
    names = re.findall(r"[A-Za-z_][A-Za-z0-9_]+", lines[start].split(":", 1)[1])
    for line in lines[start + 1 :]:
        if not re.match(r"^#\s{5,}\S", line):
            break
        names += re.findall(r"[A-Za-z_][A-Za-z0-9_]+", line)
    return set(names)


def test_the_global_reader_sees_a_name_nobody_assigns_and_no_other() -> None:
    text = (
        'f() {\n  echo "${OUTSIDE_ONE}" "$OUTSIDE_TWO" "${HOME}"\n'
        '  OWN=1\n  echo "$OWN"\n}\n'
    )

    assert globals_read_from_outside(text) == {"OUTSIDE_ONE", "OUTSIDE_TWO"}


def test_planguard_calls_only_what_it_or_the_shared_file_defines_and_run_clean() -> (
    None
):
    moved = PLANGUARD_SH.read_text(encoding="utf-8")
    defined = defined_in(moved) | defined_in(COMMON_SH.read_text(encoding="utf-8"))

    called = called_and_not_ours(moved)

    # The scan reached the moved code and the shared file's functions.
    assert {"current_commit", "git_here", "tf_plain", "redact", "die", "log"} <= called
    assert called - defined == WRAPPER_FUNCTIONS


def test_planguard_reads_from_the_wrapper_exactly_the_globals_pinned_here() -> None:
    moved = PLANGUARD_SH.read_text(encoding="utf-8")

    assert globals_read_from_outside(moved) == WRAPPER_GLOBALS


def test_the_header_of_planguard_lists_the_same_functions_and_globals() -> None:
    moved = PLANGUARD_SH.read_text(encoding="utf-8")

    assert listed_in_header(moved, "functions") == WRAPPER_FUNCTIONS
    assert listed_in_header(moved, "globals") == WRAPPER_GLOBALS
    assert listed_in_header(moved, "local-state globals") == LOCAL_STATE_GLOBALS


def function_bodies(text: str) -> dict[str, str]:
    """Each function's text, from its ``name() {`` line to the first ``}`` that
    stands alone at the start of a line (the style of these files)."""
    bodies: dict[str, str] = {}
    for match in FUNCTION_DEFINITION.finditer(text):
        end = re.compile(r"^\}", re.MULTILINE).search(text, match.end())
        # A one-line function ends on its own line.
        line_end = text.index("\n", match.start())
        one_line = text[match.start() : line_end].rstrip().endswith("}")
        bodies[match.group(1)] = (
            text[match.start() : line_end]
            if one_line
            else text[match.start() : end.end() if end else len(text)]
        )
    return bodies


def test_the_function_splitter_cuts_a_long_and_a_one_line_function() -> None:
    text = "one() {\n  echo ${A}\n}\ntwo() { echo ${B}; }\nthree() {\n  echo ${C}\n}\n"

    bodies = function_bodies(text)

    assert set(bodies) == {"one", "two", "three"}
    assert "A" in bodies["one"] and "B" not in bodies["one"]
    assert "B" in bodies["two"] and "C" not in bodies["two"]
    assert "C" in bodies["three"]


@pytest.mark.parametrize("name", sorted(LOCAL_STATE_GLOBALS))
def test_a_local_state_global_is_read_only_inside_the_local_state_functions(
    name: str,
) -> None:
    moved = PLANGUARD_SH.read_text(encoding="utf-8")
    reading = re.compile(r"\$\{?[!#]?" + name + r"\b")
    bodies = function_bodies(moved)
    outside_the_functions = moved
    for body in bodies.values():
        outside_the_functions = outside_the_functions.replace(body, "")

    readers = {
        function for function, body in bodies.items() if reading.search(code_of(body))
    }

    assert readers == {"prepare_state"}
    assert readers <= LOCAL_STATE_FUNCTIONS
    assert reading.search(code_of(outside_the_functions)) is None


def test_the_local_state_globals_are_among_the_wrapper_globals() -> None:
    assert LOCAL_STATE_GLOBALS < WRAPPER_GLOBALS


def test_the_wrapper_defines_every_function_and_global_planguard_needs() -> None:
    wrapper = AWS_SH.read_text(encoding="utf-8")
    code = code_of(wrapper)

    unassigned = [n for n in sorted(WRAPPER_GLOBALS) if not re.search(rf"\b{n}=", code)]
    assert defined_in(wrapper) >= WRAPPER_FUNCTIONS
    assert unassigned == []


# The one sentence of the moved code that names the environment by a variable the
# wrapper sets: the words the wrapper's messages had before they were a variable.
@pytest.mark.parametrize("module", MODULES, ids=MODULE_IDS)
def test_the_state_directory_refusal_prints_the_same_sentence_as_before(
    module: Module, tmp_path: Path
) -> None:
    tree = with_local_file(make_tree(tmp_path, module))

    done = tree.run("plan", HOME="")

    assert done.returncode == 1
    assert done.stderr.splitlines()[-1] == (
        "error: HOME is not set, and the state of the AWS environment is kept in "
        f"a directory under it ({module.readme}, State)"
    )


def test_the_wrapper_names_the_environment_in_one_read_only_variable() -> None:
    code = code_of(AWS_SH.read_text(encoding="utf-8"))

    assert 'readonly ENVIRONMENT_WORDS="the AWS environment"' in code.splitlines()


# The two things the wrapper of a remote backend changes, held byte for byte for
# the wrapper of the local state before anything moves: the whole sentence of the
# refusal of another workspace, and the whole argument list of the init call.
@pytest.mark.parametrize("module", MODULES, ids=MODULE_IDS)
def test_another_workspace_is_refused_with_this_whole_sentence(
    module: Module, tmp_path: Path
) -> None:
    tree = with_local_file(make_tree(tmp_path, module))
    (tree.module / ".terraform").mkdir()
    (tree.module / ".terraform" / "environment").write_text("w1")

    done = tree.run("plan")

    assert done.returncode == 1
    assert done.stderr.splitlines()[-1] == (
        "error: the module's directory is not on Terraform's default workspace, "
        "or .terraform/environment cannot be read: Terraform would keep the state "
        "in terraform.tfstate.d/ in the checkout and not in the state under your "
        "home, and a removal would find it empty. Get back with: terraform "
        f"-chdir=infra/terraform/{module.directory} workspace select default "
        f"({module.readme}, State)"
    )


@pytest.mark.parametrize("subcommand", ["plan", "destroy"])
@pytest.mark.parametrize("module", MODULES, ids=MODULE_IDS)
def test_the_init_call_has_exactly_these_arguments_in_this_order(
    module: Module, subcommand: str, tmp_path: Path
) -> None:
    tree = with_local_file(make_tree(tmp_path, module))

    done = tree.run(subcommand, terminal=subcommand == "destroy")

    assert done.returncode == 0, done.stdout + done.stderr
    assert tree.terraform_calls("init") == [
        f"terraform -chdir={tree.module} init -no-color -input=false "
        f"-reconfigure -lockfile=readonly -backend-config=path={tree.state_path}"
    ]


# ── a move, not a copy ───────────────────────────────────────────────────────


def test_no_name_is_defined_in_two_of_the_three_files() -> None:
    texts = {
        path.name: path.read_text(encoding="utf-8")
        for path in (AWS_SH, COMMON_SH, PLANGUARD_SH)
    }
    names = {
        name: defined_in(text) | set(READONLY_DEFINITION.findall(text))
        for name, text in texts.items()
    }

    assert names["planguard.sh"] != set()
    assert names["planguard.sh"] & names["aws.sh"] == set()
    assert names["planguard.sh"] & names["common.sh"] == set()
    assert names["aws.sh"] & names["common.sh"] == set()


# ── the order of the source line ─────────────────────────────────────────────


def test_the_wrapper_sources_planguard_after_common_and_before_a_moved_name() -> None:
    wrapper_code = code_of(AWS_SH.read_text(encoding="utf-8")).splitlines()
    moved = PLANGUARD_SH.read_text(encoding="utf-8")
    moved_names = defined_in(moved) | set(READONLY_DEFINITION.findall(moved))
    uses = re.compile(r"\b(?:" + "|".join(sorted(moved_names)) + r")\b")

    sourcing = [n for n, line in enumerate(wrapper_code) if "planguard.sh" in line]
    common = [n for n, line in enumerate(wrapper_code) if "common.sh" in line]
    first_use = [
        n
        for n, line in enumerate(wrapper_code)
        if uses.search(line) and n not in sourcing
    ]

    assert [wrapper_code[n] for n in sourcing] == [source_line("planguard.sh")]
    assert [wrapper_code[n] for n in common] == [source_line("common.sh")]
    assert first_use != []  # the wrapper does use what moved
    assert common[0] < sourcing[0] < first_use[0]


def test_the_source_line_has_the_hint_that_lets_shellcheck_follow_it() -> None:
    lines = AWS_SH.read_text(encoding="utf-8").splitlines()
    at = lines.index(source_line("planguard.sh"))

    assert lines[at - 1] == "# shellcheck source=planguard.sh"
