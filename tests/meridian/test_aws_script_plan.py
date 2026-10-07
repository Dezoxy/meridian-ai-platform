"""The commands around the AWS modules, part two: ``infra/terraform/aws.sh``.

This file holds what the script does with Terraform: the plan, the state, the
saved plan and its record, apply, the removal, and the agreement between the
variables the script exports and the ones each module declares. What it does
before it plans is in ``test_aws_script.py``; ``awsscriptsupport.py`` holds the
stand-in tree and the table of the two modules, and ``module`` runs a test once
on each.
"""

import hashlib
import os
import re
import stat
import subprocess
import time
from pathlib import Path

import pytest
from awsscriptsupport import (
    BUDGET_EMAIL,
    ENDPOINT_CIDR,
    GIT_ENV,
    INSTANCE_SHAPES,
    LOCAL_FILE,
    MANAGED,
    MODULE_IDS,
    MODULES,
    OTHER,
    PINNED,
    PLAN_MAX_AGE_SECONDS,
    QUESTION,
    SELF_MANAGED,
    TF_VAR_NAMES,
    Module,
    Tree,
    assert_no_identifier_printed,
    assert_plan_refused_and_dropped,
    each_module,
    env_names,
    everything_printed,
    git,
    make_tree,
    only_the_self_managed_module,
    run_refusing,
    tree_of,
    with_local_file,
    with_saved_plan,
)
from servicesupport import REPO_ROOT

module_fixture = pytest.fixture(params=MODULES, ids=MODULE_IDS, name="module")(
    each_module
)
tree_fixture = pytest.fixture(name="tree")(tree_of)

# ── plan ─────────────────────────────────────────────────────────────────────


def test_plan_initialises_then_plans_into_a_saved_file_only_the_owner_can_read(
    tree: Tree,
) -> None:
    with_local_file(tree)

    done = tree.run("plan", caller_umask="022")

    assert done.returncode == 0, everything_printed(done)
    assert tree.subcommands() == ["init", "plan"]
    plan_call = tree.terraform_calls("plan")[0]
    assert f"-out={tree.plan_file}" in plan_call
    assert "-input=false" in plan_call
    for name in (tree.plan_file, tree.meta_file):
        mode = stat.S_IMODE((tree.module / name).stat().st_mode)
        assert mode & 0o077 == 0, name


def test_plan_hands_terraform_the_region_the_address_the_email_and_the_account(
    tree: Tree,
) -> None:
    with_local_file(tree)

    tree.run("plan", AWS_DEFAULT_REGION="us-east-1")

    seen = tree.terraform_env()
    assert "TF_VAR_region=eu-central-1" in seen
    assert f"TF_VAR_api_access_cidr={ENDPOINT_CIDR}" in seen
    assert f"TF_VAR_budget_email={BUDGET_EMAIL}" in seen
    # The module's provider refuses any other account (allowed_account_ids), so
    # the pinned one goes in as a variable, and nowhere else.
    assert f"TF_VAR_expected_account_id={PINNED}" in seen
    assert "AWS_REGION=eu-central-1" in seen
    assert "AWS_DEFAULT_REGION" not in seen
    assert f"={PINNED}" not in seen.replace(f"TF_VAR_expected_account_id={PINNED}", "")


def test_plan_prints_none_of_the_four_values_nor_what_terraform_would_leak(
    tree: Tree,
) -> None:
    with_local_file(tree)

    done = tree.run("plan")

    assert_no_identifier_printed(done)


def test_a_failed_plan_fails_the_command(tree: Tree) -> None:
    with_local_file(tree)

    done = tree.run("plan", STUB_PLAN_STATUS="1")

    assert done.returncode == 1


def test_a_failed_plan_leaves_no_saved_plan_to_be_applied(tree: Tree) -> None:
    with_local_file(tree)
    with_saved_plan(tree)

    tree.run("plan", STUB_PLAN_STATUS="1")

    assert not (tree.module / tree.plan_file).exists()
    assert not (tree.module / tree.meta_file).exists()


def test_plan_records_the_module_the_commit_the_time_and_the_plan_files_hash(
    tree: Tree,
) -> None:
    with_local_file(tree)
    before = int(time.time())

    done = tree.run("plan")

    assert done.returncode == 0, everything_printed(done)
    lines = (tree.module / tree.meta_file).read_text().splitlines()
    assert lines[0] == f"module={tree.row.name}"
    assert lines[1] == f"commit={git(tree.root, 'rev-parse', 'HEAD')}"
    assert re.fullmatch(r"time=[1-9][0-9]{9}", lines[2])
    assert before <= int(lines[2].removeprefix("time=")) <= int(time.time())
    digest = hashlib.sha256((tree.module / tree.plan_file).read_bytes()).hexdigest()
    assert lines[3] == f"sha256={digest}"
    assert len(lines) == 4


@pytest.mark.parametrize("subcommand", ["plan", "apply"])
def test_plan_and_apply_say_which_module_they_are_about_before_anything_else(
    tree: Tree, subcommand: str
) -> None:
    with_local_file(tree)
    with_saved_plan(tree)

    done = tree.run(subcommand)

    assert done.returncode == 0, everything_printed(done)
    assert done.stdout.splitlines()[0] == f"==> module: {tree.row.name}"


def test_removal_says_which_module_it_is_about_before_anything_else(
    tree: Tree,
) -> None:
    with_local_file(tree)

    done = tree.run("destroy", terminal=True)

    assert done.returncode == 0, done.stdout
    assert done.stdout.splitlines()[0] == f"==> module: {tree.row.name}"


def test_the_sentence_just_above_the_removals_question_names_the_module(
    tree: Tree,
) -> None:
    """The owner types the confirmation under Terraform's own question, which
    names no module. The last sentence of the script above it does, and so does
    the one that says how many resources the state holds."""
    with_local_file(tree)

    done = tree.run("destroy", terminal=True, STUB_STATE_COUNT="4")

    assert done.returncode == 0, done.stdout
    lines = done.stdout.splitlines()
    asked = next(i for i, line in enumerate(lines) if "Do you really want" in line)
    script_lines = [line for line in lines[:asked] if line.startswith("==> ")]
    assert f"module {tree.row.name}:" in script_lines[-1]
    assert "Terraform asks" in script_lines[-1]
    assert any(
        f"module {tree.row.name}" in line and "4 resources" in line
        for line in script_lines
    )


# ── the state ────────────────────────────────────────────────────────────────


def test_the_state_lives_in_a_directory_under_home_that_only_the_owner_can_enter(
    tree: Tree,
) -> None:
    with_local_file(tree)
    tree.state_dir.mkdir(parents=True, mode=0o755)
    tree.state_dir.chmod(0o755)

    done = tree.run("plan")

    assert done.returncode == 0, everything_printed(done)
    (init,) = tree.terraform_calls("init")
    path = tree.state_path
    assert f"-backend-config=path={path}" in init
    assert stat.S_IMODE(tree.state_dir.stat().st_mode) == 0o700
    assert tree.module not in path.parents  # not in a checkout


def test_a_state_directory_that_does_not_exist_is_made_private(tree: Tree) -> None:
    with_local_file(tree)
    assert not tree.state_dir.exists()

    tree.run("plan", caller_umask="022")

    assert stat.S_IMODE(tree.state_dir.stat().st_mode) == 0o700


def test_validate_makes_no_state_directory_and_names_no_backend_path(
    tree: Tree,
) -> None:
    tree.run("validate")

    assert not tree.state_dir.exists()


@pytest.mark.parametrize("subcommand", ["apply", "destroy"])
def test_apply_and_removal_find_the_same_state(tree: Tree, subcommand: str) -> None:
    with_local_file(tree)
    with_saved_plan(tree)

    done = tree.run(subcommand, terminal=subcommand == "destroy")

    assert done.returncode == 0, everything_printed(done)
    inits = tree.terraform_calls("init")
    if subcommand == "destroy":
        (init,) = inits
        assert f"-backend-config=path={tree.state_path}" in init


def test_without_a_home_the_state_has_no_place_and_the_script_refuses(
    tree: Tree,
) -> None:
    with_local_file(tree)
    env = {k: v for k, v in tree.base_env.items() if k != "HOME"}

    done = subprocess.run(
        tree.command(*tree.words("plan")),
        capture_output=True,
        text=True,
        env=env,
        cwd=tree.root,
        stdin=subprocess.DEVNULL,
        timeout=60,
        check=False,
    )

    assert done.returncode == 1
    assert "HOME" in done.stderr
    assert tree.terraform_calls("plan") == []


@pytest.mark.parametrize("subcommand", ["plan", "destroy"])
def test_init_is_told_to_use_the_scripts_own_state_path_whatever_an_older_init_left(
    tree: Tree, subcommand: str
) -> None:
    """Terraform stops with "Backend configuration changed" when a hand-made init
    named another path, and -reconfigure takes the script's path with no copy of
    a state and no question (read on a scratch directory, with no provider)."""
    with_local_file(tree)

    done = tree.run(subcommand, terminal=subcommand == "destroy")

    assert done.returncode == 0, everything_printed(done)
    (init,) = tree.terraform_calls("init")
    assert "-reconfigure" in init
    assert "-input=false" in init
    assert "-migrate-state" not in init
    assert f"-backend-config=path={tree.state_path}" in init


def test_the_init_of_validate_has_no_backend_and_needs_no_reconfigure(
    tree: Tree,
) -> None:
    tree.run("validate")

    (init,) = tree.terraform_calls("init")
    assert "-backend=false" in init
    assert "-reconfigure" not in init


# What a hand-made workspace leaves: `.terraform/environment` names it, and
# Terraform then keeps the state in terraform.tfstate.d/<name>/ in the module's
# directory (a checkout), not at the path the script gave. Selecting the default
# workspace again leaves the file in place with the word `default` in it.
def with_workspace(tree: Tree, name: str | None) -> Tree:
    (tree.module / ".terraform").mkdir(exist_ok=True)
    if name is not None:
        (tree.module / ".terraform" / "environment").write_text(name)
    return tree


@pytest.mark.parametrize("subcommand", ["plan", "destroy"])
@pytest.mark.parametrize(
    "name",
    [
        "w1",
        "staging",
        "default-2",
        "default\nother",  # the whole content is the name, as Terraform reads it
        "default\nother\n",
        "other\ndefault\n",
        "default default",
        "default\n\nother\n",
    ],
)
def test_another_workspace_is_refused_after_init_and_before_anything_else(
    tree: Tree, subcommand: str, name: str
) -> None:
    with_local_file(tree)
    with_workspace(tree, name)

    done = run_refusing(tree, subcommand)

    printed = everything_printed(done)
    assert done.returncode == 1, printed
    assert "workspace" in printed
    assert "workspace select default" in printed  # how to get back
    assert "terraform.tfstate.d" in printed
    assert tree.subcommands() == ["init"]
    assert tree.terraform_calls(subcommand) == []
    assert_no_identifier_printed(done)


def test_apply_refuses_a_workspace_other_than_the_default_and_keeps_the_plan(
    tree: Tree,
) -> None:
    """apply does no init, so a file left between the plan and the apply would
    send the applied state into the checkout unseen: .terraform/ is ignored."""
    with_local_file(tree)
    with_saved_plan(tree)
    with_workspace(tree, "w1")

    done = tree.run("apply")

    assert done.returncode == 1, everything_printed(done)
    assert "workspace select default" in done.stderr
    assert tree.terraform_calls("apply") == []
    assert (tree.module / tree.plan_file).exists()  # the plan is still good


@pytest.mark.parametrize(
    "name",
    [
        None,
        "default",
        "default\n",
        # Terraform trims the whole content (leading and trailing space, tabs,
        # carriage returns, newlines), and an empty or blank file is the default
        # workspace: observed on Terraform v1.16.5 with `workspace show`.
        " default",
        "  default \r\n\t\n",
        "\n\ndefault\n\n",
        "",
        "  \n",
    ],
)
@pytest.mark.parametrize("subcommand", ["plan", "apply", "destroy"])
def test_no_workspace_file_or_one_that_says_default_is_not_refused(
    tree: Tree, subcommand: str, name: str | None
) -> None:
    with_local_file(tree)
    with_saved_plan(tree)
    with_workspace(tree, name)

    done = tree.run(subcommand, terminal=subcommand == "destroy")

    assert done.returncode == 0, everything_printed(done)


@pytest.mark.parametrize("subcommand", ["plan", "apply", "destroy"])
def test_a_directory_where_the_workspace_file_should_be_is_the_scripts_sentence_only(
    tree: Tree, subcommand: str
) -> None:
    with_local_file(tree)
    with_saved_plan(tree)
    (tree.module / ".terraform" / "environment").mkdir(parents=True)

    done = tree.run(subcommand, terminal=subcommand == "destroy")

    printed = everything_printed(done)
    assert done.returncode == 1, printed
    assert "workspace select default" in printed
    assert "Is a directory" not in printed
    assert "cat:" not in printed  # nor the failed read's own line
    assert tree.terraform_calls(subcommand) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
@pytest.mark.parametrize("subcommand", ["plan", "apply", "destroy"])
def test_a_workspace_file_that_cannot_be_read_is_the_scripts_sentence_only(
    tree: Tree, subcommand: str
) -> None:
    with_local_file(tree)
    with_saved_plan(tree)
    with_workspace(tree, "default")
    (tree.module / ".terraform" / "environment").chmod(0)

    done = tree.run(subcommand, terminal=subcommand == "destroy")

    printed = everything_printed(done)
    assert done.returncode == 1, printed
    assert "workspace select default" in printed
    assert "Permission denied" not in printed
    assert tree.terraform_calls(subcommand) == []


# ── apply ────────────────────────────────────────────────────────────────────


def test_apply_without_a_saved_plan_refuses_before_any_call(tree: Tree) -> None:
    with_local_file(tree)

    done = tree.run("apply")

    assert done.returncode == 1
    assert tree.plan_command in done.stderr
    assert tree.calls() == []
    assert tree.aws_calls() == []


def test_apply_applies_exactly_the_saved_plan_and_removes_it(tree: Tree) -> None:
    with_local_file(tree)
    with_saved_plan(tree)

    done = tree.run("apply")

    assert done.returncode == 0, everything_printed(done)
    (call,) = tree.terraform_calls("apply")
    assert call.split()[-1] == tree.plan_file
    assert "-auto-approve" not in call
    assert not (tree.module / tree.plan_file).exists()
    assert not (tree.module / tree.meta_file).exists()
    assert_no_identifier_printed(done)


def test_apply_removes_the_plan_when_it_fails_and_says_to_plan_again(
    tree: Tree,
) -> None:
    with_local_file(tree)
    with_saved_plan(tree)

    done = tree.run("apply", STUB_APPLY_STATUS="1")

    assert done.returncode == 1
    assert tree.plan_command in done.stderr
    assert not (tree.module / tree.plan_file).exists()
    assert not (tree.module / tree.meta_file).exists()


def test_apply_to_another_account_leaves_the_saved_plan_for_the_right_one(
    tree: Tree,
) -> None:
    with_local_file(tree)
    with_saved_plan(tree)

    done = tree.run("apply", STUB_ACCOUNT=OTHER)

    assert done.returncode == 1
    assert (tree.module / tree.plan_file).exists()


# ── a saved plan is applied only if it is this tree's and fresh ──────────────


def test_a_plan_with_no_record_beside_it_is_not_applied(tree: Tree) -> None:
    with_local_file(tree)
    with_saved_plan(tree)
    (tree.module / tree.meta_file).unlink()

    done = tree.run("apply")

    assert_plan_refused_and_dropped(tree, done, "record")


MODULE_LINE = "module=@MODULE@"  # the tests put the tree's own module name in
COMMIT_LINE = "commit=0123456789abcdef0123456789abcdef01234567"
TIME_LINE = "time=1791305195"
HASH_LINE = "sha256=" + "0" * 64


@pytest.mark.parametrize(
    "text",
    [
        "",
        f"{MODULE_LINE}\ncommit=not-a-commit\n{TIME_LINE}\n{HASH_LINE}\n",
        f"{MODULE_LINE}\n{TIME_LINE}\n{HASH_LINE}\n",
        f"{MODULE_LINE}\n{COMMIT_LINE}\n",
        f"{MODULE_LINE}\n{COMMIT_LINE}\ntime=soon\n{HASH_LINE}\n",
        f"{MODULE_LINE}\n{COMMIT_LINE}\ntime=1\n{HASH_LINE}\n",
        f"{MODULE_LINE}\n{COMMIT_LINE}\n{TIME_LINE}\n",  # no hash
        f"{MODULE_LINE}\n{COMMIT_LINE}\n{TIME_LINE}\nsha256=not-a-hash\n",
        f"{MODULE_LINE}\n{COMMIT_LINE}\n{TIME_LINE}\nsha256={'0' * 63}\n",
        f"{MODULE_LINE}\n{COMMIT_LINE}\n{TIME_LINE}\nsha256={'0' * 65}\n",
        f"{MODULE_LINE}\n{COMMIT_LINE}\n{TIME_LINE}\nsha256={'A' * 64}\n",
        f"{MODULE_LINE}\n{COMMIT_LINE}\n{TIME_LINE}\n{HASH_LINE}\nextra\n",
        f"{MODULE_LINE}\n{COMMIT_LINE}\n{HASH_LINE}\n{TIME_LINE}\n",  # another order
        # The three-line record of the script before it named a module.
        f"{COMMIT_LINE}\n{TIME_LINE}\n{HASH_LINE}\n",
        f"{COMMIT_LINE}\n{MODULE_LINE}\n{TIME_LINE}\n{HASH_LINE}\n",
        f"{MODULE_LINE}\n{MODULE_LINE}\n{COMMIT_LINE}\n{TIME_LINE}\n{HASH_LINE}\n",
        # The module's name, in the other spellings.
        f"{MODULE_LINE}\n\n{COMMIT_LINE}\n{TIME_LINE}\n{HASH_LINE}\n",
        f"{MODULE_LINE} \n{COMMIT_LINE}\n{TIME_LINE}\n{HASH_LINE}\n",
        f"{MODULE_LINE.upper()}\n{COMMIT_LINE}\n{TIME_LINE}\n{HASH_LINE}\n",
        f"module=\n{COMMIT_LINE}\n{TIME_LINE}\n{HASH_LINE}\n",
        # A name that is not a word of lower case and dashes, whatever it would
        # do in a path or a shell.
        f"module=../@MODULE@\n{COMMIT_LINE}\n{TIME_LINE}\n{HASH_LINE}\n",
        f"{MODULE_LINE};id\n{COMMIT_LINE}\n{TIME_LINE}\n{HASH_LINE}\n",
        f"module=$(id)\n{COMMIT_LINE}\n{TIME_LINE}\n{HASH_LINE}\n",
        f"module={'a' * 32}\n{COMMIT_LINE}\n{TIME_LINE}\n{HASH_LINE}\n",  # too long
        # A time that is negative, or one digit too long.
        f"{MODULE_LINE}\n{COMMIT_LINE}\ntime=-1791305195\n{HASH_LINE}\n",
        f"{MODULE_LINE}\n{COMMIT_LINE}\ntime=17913051950\n{HASH_LINE}\n",
        # Line ends and blank lines that are not the script's own.
        f"{MODULE_LINE}\r\n{COMMIT_LINE}\r\n{TIME_LINE}\r\n{HASH_LINE}\r\n",
        f"{MODULE_LINE}\n{COMMIT_LINE}\n{TIME_LINE}\n{HASH_LINE}\r\n",
        f"\n{MODULE_LINE}\n{COMMIT_LINE}\n{TIME_LINE}\n{HASH_LINE}\n",
        f"{MODULE_LINE}\n{COMMIT_LINE}\n{TIME_LINE}\n{HASH_LINE}\n\n",
        f"{MODULE_LINE}\n{COMMIT_LINE}\n{TIME_LINE}\n{HASH_LINE}",  # no last newline
        # The record is the four lines and nothing after them: a fifth line that
        # has no newline (the read of it sees the end of the file), and a NUL, in
        # a line and after the last one.
        f"{MODULE_LINE}\n{COMMIT_LINE}\n{TIME_LINE}\n{HASH_LINE}\njunk",
        f"{MODULE_LINE}\n{COMMIT_LINE}\n{TIME_LINE}\n{HASH_LINE}\n{MODULE_LINE}",
        f"{MODULE_LINE}\n{COMMIT_LINE}\n{TIME_LINE}\n{HASH_LINE}\x00",
        f"{MODULE_LINE}\n{COMMIT_LINE}\n{TIME_LINE}\n{HASH_LINE}\n\x00",
        f"{MODULE_LINE}\n{COMMIT_LINE}\n{TIME_LINE}\n{HASH_LINE}\x00junk\n",
        f"{MODULE_LINE}\n{COMMIT_LINE}\x00\n{TIME_LINE}\n{HASH_LINE}\n",
        f"{MODULE_LINE}\n{COMMIT_LINE}\n{TIME_LINE}\n{HASH_LINE[:20]}\x00{HASH_LINE[20:]}\n",
    ],
)
def test_a_record_that_is_not_a_module_a_commit_a_time_and_a_hash_is_not_trusted(
    tree: Tree, text: str
) -> None:
    with_local_file(tree)
    with_saved_plan(tree)
    (tree.module / tree.meta_file).write_text(text.replace("@MODULE@", tree.row.name))

    done = tree.run("apply")

    # The sentence of the parse itself: the later checks (the plan file's hash,
    # the commit) would refuse these records as well, with words of their own, so
    # a record that got past the parse is told apart by what is said.
    assert_plan_refused_and_dropped(
        tree, done, "is not a module, a commit, a time and a SHA-256"
    )


def test_a_record_that_names_another_module_is_not_applied_though_the_hash_matches(
    tree: Tree,
) -> None:
    """The record is the other module's and the plan file is the one it names:
    the hash fits, the commit fits, the age fits. Only the module differs."""
    other = SELF_MANAGED if tree.row is MANAGED else MANAGED
    with_local_file(tree)
    with_saved_plan(tree, module_name=other.name)

    done = tree.run("apply")

    assert_plan_refused_and_dropped(tree, done, "another module")
    assert "record" in done.stderr


def test_a_plan_of_the_other_module_is_not_applied_though_its_record_is_this_ones(
    tree: Tree,
) -> None:
    """The record says this module, and its hash is the hash of the bytes of a
    plan made for the other: the bytes are the other's, so the plan and the
    record were not made together by this module's plan."""
    with_local_file(tree)
    with_saved_plan(tree)
    other_plan = b"a plan of the other module, made by its own plan"
    (tree.module / tree.plan_file).write_bytes(other_plan)

    done = tree.run("apply")

    assert_plan_refused_and_dropped(tree, done, "SHA-256")


def test_a_saved_plan_of_the_other_module_is_not_looked_at_by_this_one(
    tree: Tree,
) -> None:
    """Each module's plan and record have names of their own, in a directory of
    their own: the managed module's saved plan is no saved plan for the
    self-managed one, whatever the record says, and the other way round. The
    other module's plan is not dropped by a refusal that is not about it."""
    other_row = SELF_MANAGED if tree.row is MANAGED else MANAGED
    other = tree.root / "infra" / "terraform" / other_row.directory
    other.mkdir(exist_ok=True)
    (other / "main.tf").write_text("# a stand-in module\n")
    (other / other_row.plan_file).write_bytes(b"plan")
    (other / other_row.meta_file).write_text("kept\n")
    with_local_file(tree)

    done = tree.run("apply")

    assert done.returncode == 1, everything_printed(done)
    assert f"no {tree.plan_file}" in done.stderr
    assert tree.plan_command in done.stderr
    assert tree.calls() == []
    assert (other / other_row.plan_file).read_bytes() == b"plan"
    assert (other / other_row.meta_file).read_text() == "kept\n"


def test_a_plan_file_written_over_the_one_the_script_made_is_not_applied(
    tree: Tree,
) -> None:
    """A `terraform plan -out=aws.tfplan` run by hand, with a -target or a
    variable, after `make aws-plan`: the record is the script's, the bytes are
    not."""
    with_local_file(tree)
    with_saved_plan(tree)
    (tree.module / tree.plan_file).write_bytes(
        b"a plan made by hand, over the script's"
    )

    done = tree.run("apply")

    assert_plan_refused_and_dropped(tree, done, "SHA-256")


def test_a_plan_file_that_is_the_one_the_record_names_is_applied(tree: Tree) -> None:
    with_local_file(tree)
    plan = tree.run("plan")
    assert plan.returncode == 0, everything_printed(plan)

    done = tree.run("apply")

    assert done.returncode == 0, everything_printed(done)
    assert tree.terraform_calls("apply") != []


@pytest.mark.parametrize(
    "time_text",
    [
        "08",  # not an octal digit: bash stops with "value too great for base"
        "01791305195",  # the same, with a leading zero
        "0",
        "015261222753",  # the octal spelling of the clock: bash read it as fresh
        "+1791305195",
        " 1791305195",
        "1791305195 ",
        "0x1000",
        "17913051950000000000000",
    ],
)
def test_a_time_that_is_not_a_plain_decimal_is_not_read_in_any_other_base(
    tree: Tree, time_text: str
) -> None:
    with_local_file(tree)
    with_saved_plan(tree, time_text=time_text)

    done = tree.run("apply")

    assert_plan_refused_and_dropped(tree, done, "record")
    assert "value too great" not in done.stderr


def test_the_octal_spelling_of_this_very_second_is_not_read_as_fresh(
    tree: Tree,
) -> None:
    # Built here, in the body: a parameter built from the clock at import would
    # give two xdist workers that import the file in different seconds different
    # test IDs, and the run stops at collection.
    octal_now = f"0{oct(int(time.time()))[2:]}"
    with_local_file(tree)
    with_saved_plan(tree, time_text=octal_now)

    done = tree.run("apply")

    assert_plan_refused_and_dropped(tree, done, "record")
    assert "value too great" not in done.stderr


def test_a_plan_made_at_another_commit_is_not_applied(tree: Tree) -> None:
    with_local_file(tree)
    with_saved_plan(tree)
    (tree.module / "main.tf").write_text("# changed and committed\n")
    git(tree.root, "commit", "-q", "-am", "a change after the plan")

    done = tree.run("apply")

    assert_plan_refused_and_dropped(tree, done, "commit")


def test_a_tracked_file_changed_in_the_module_since_the_plan_is_not_applied(
    tree: Tree,
) -> None:
    with_local_file(tree)
    with_saved_plan(tree)
    (tree.module / "main.tf").write_text("# changed, not committed\n")

    done = tree.run("apply")

    assert_plan_refused_and_dropped(tree, done, "uncommitted")


def test_a_new_file_in_the_module_since_the_plan_is_not_applied(tree: Tree) -> None:
    with_local_file(tree)
    with_saved_plan(tree)
    (tree.module / "extra.tf").write_text("# a new resource\n")

    done = tree.run("apply")

    assert_plan_refused_and_dropped(tree, done, "uncommitted")


def test_a_change_outside_the_module_is_not_the_modules_business(tree: Tree) -> None:
    with_local_file(tree)
    with_saved_plan(tree)
    (tree.root / "notes.md").write_text("not in the module\n")

    done = tree.run("apply")

    assert done.returncode == 0, everything_printed(done)


def test_a_plan_older_than_the_bound_is_not_applied(tree: Tree) -> None:
    with_local_file(tree)
    with_saved_plan(tree, age_seconds=PLAN_MAX_AGE_SECONDS + 60)

    done = tree.run("apply")

    assert_plan_refused_and_dropped(tree, done, "old")


def test_a_plan_a_little_younger_than_the_bound_is_applied(tree: Tree) -> None:
    with_local_file(tree)
    with_saved_plan(tree, age_seconds=PLAN_MAX_AGE_SECONDS - 60)

    done = tree.run("apply")

    assert done.returncode == 0, everything_printed(done)
    assert tree.terraform_calls("apply") != []


def test_a_plan_dated_in_the_future_is_not_applied(tree: Tree) -> None:
    with_local_file(tree)
    with_saved_plan(tree, age_seconds=-3600)

    done = tree.run("apply")

    assert_plan_refused_and_dropped(tree, done, "old")


def test_planning_in_a_changed_module_shows_the_plan_but_writes_no_record(
    tree: Tree,
) -> None:
    with_local_file(tree)
    (tree.module / "main.tf").write_text("# not committed\n")

    done = tree.run("plan")

    assert done.returncode == 0, everything_printed(done)
    assert "uncommitted" in done.stdout
    assert "stub terraform plan" in done.stdout  # reading the plan is free
    assert (tree.module / tree.plan_file).exists()
    assert not (tree.module / tree.meta_file).exists()


def test_a_plan_made_from_a_changed_module_is_not_applied_even_after_a_hand_revert(
    tree: Tree,
) -> None:
    """What the warning says becomes true: the plan was made from a tree no
    commit describes, and putting the files back by hand afterwards does not
    make the commit describe it."""
    with_local_file(tree)
    original = (tree.module / "main.tf").read_text()
    (tree.module / "main.tf").write_text("# an edit the plan was made from\n")
    tree.run("plan")
    (tree.module / "main.tf").write_text(original)
    module = f"infra/terraform/{tree.row.directory}"
    assert git(tree.root, "status", "--porcelain", "--", module) == ""

    done = tree.run("apply")

    assert done.returncode == 1, everything_printed(done)
    assert "record" in done.stderr
    assert tree.plan_command in done.stderr
    assert tree.terraform_calls("apply") == []
    assert len(tree.aws_calls()) == 2  # the plan's own, and the apply's, first
    assert not (tree.module / tree.plan_file).exists()


def test_an_untracked_file_in_the_module_at_plan_time_also_writes_no_record(
    tree: Tree,
) -> None:
    with_local_file(tree)
    (tree.module / "extra.tf").write_text("# a new resource\n")

    done = tree.run("plan")

    assert done.returncode == 0, everything_printed(done)
    assert not (tree.module / tree.meta_file).exists()


def test_a_module_that_changes_while_the_plan_runs_gets_no_record(tree: Tree) -> None:
    with_local_file(tree)

    done = tree.run("plan", STUB_PLAN_CHANGES_THE_MODULE="1")

    assert done.returncode == 0, everything_printed(done)
    assert not (tree.module / tree.meta_file).exists()


# ── the plan is judged last, and read from standard input ────────────────────


@pytest.mark.parametrize("fault", ["too old", "not the record's file"])
def test_the_saved_plan_is_judged_after_the_sign_in_call_not_before_it(
    tree: Tree, fault: str
) -> None:
    """The order is observable: when the sign-in call fails, a saved plan that
    would have been refused is still there (nothing read it: neither its hash
    nor its age was taken before a call that can hang), and the sentence is the
    sign-in's."""
    with_local_file(tree)
    if fault == "too old":
        with_saved_plan(tree, age_seconds=PLAN_MAX_AGE_SECONDS + 60)
    else:
        with_saved_plan(tree)
        (tree.module / tree.plan_file).write_bytes(b"a plan made by hand")

    done = tree.run("apply", STUB_AWS_STATUS="1")

    assert done.returncode == 1, everything_printed(done)
    assert "sign in" in done.stderr
    assert "too old" not in done.stderr
    assert "SHA-256" not in done.stderr
    assert len(tree.aws_calls()) == 1
    assert (tree.module / tree.plan_file).exists()
    assert (tree.module / tree.meta_file).exists()


def test_a_plan_that_would_be_refused_is_still_refused_once_signed_in(
    tree: Tree,
) -> None:
    with_local_file(tree)
    with_saved_plan(tree, age_seconds=PLAN_MAX_AGE_SECONDS + 60)

    done = tree.run("apply")

    assert_plan_refused_and_dropped(tree, done, "old")


def test_a_module_path_with_a_backslash_does_not_spoil_the_plans_hash(
    tmp_path: Path, module: Module
) -> None:
    """`sha256sum PATH` puts a backslash before the digest when the path holds
    one, and the record then failed its own shape at apply. The path is built
    here, in the body, and the hash is read from standard input."""
    tree = make_tree(tmp_path / "with\\a\\backslash", module)
    with_local_file(tree)

    plan = tree.run("plan")
    assert plan.returncode == 0, everything_printed(plan)
    record = (tree.module / tree.meta_file).read_text()
    shape = r"commit=\w{40}\ntime=\d{10}\nsha256=[0-9a-f]{64}\n"
    assert re.fullmatch(f"module={module.name}\n{shape}", record)
    done = tree.run("apply")

    assert done.returncode == 0, everything_printed(done)
    assert tree.terraform_calls("apply") != []


# ── an ignore rule cannot hide a Terraform file from the clean-tree check ───

HIDING_PLACES = [
    "the caller's default ignore file",
    "the repository's info/exclude",
    "a .gitignore in the module directory",
]
# What apply says about a file git's own status does not show (the last two) and
# about one it does, once the script's git has the caller's ignore file off.
APPLY_SENTENCE = {
    "the caller's default ignore file": "uncommitted",
    "the repository's info/exclude": "untracked Terraform",
    "a .gitignore in the module directory": "untracked Terraform",
}


def hide_zz_files(tree: Tree, where: str) -> None:
    """An ignore rule that hides every file whose name begins with zz, in one of
    three places the script's git read before. The module's own .gitignore is
    committed first, so that it is not itself the change that is seen."""
    if where == HIDING_PLACES[0]:
        (tree.root / ".config" / "git").mkdir(parents=True)
        (tree.root / ".config" / "git" / "ignore").write_text("zz*\n")
    elif where == HIDING_PLACES[1]:
        (tree.root / ".git" / "info" / "exclude").write_text("zz*\n")
    else:
        (tree.module / ".gitignore").write_text("zz*\n")
        git(tree.root, "add", "-A")
        git(tree.root, "commit", "-q", "-m", "an ignore file in the module")


def plain_status_as_the_caller(tree: Tree) -> str:
    """`git status` of the module with no override of the script's, run with the
    stand-in home as the home, so that its default ignore file is read."""
    env = {k: v for k, v in GIT_ENV.items() if k != "XDG_CONFIG_HOME"}
    env["HOME"] = str(tree.root)
    done = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all", "--", "."],
        cwd=tree.module,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


@pytest.mark.parametrize("name", ["zz_extra.tf", "zz_extra.tf.json"])
@pytest.mark.parametrize("where", HIDING_PLACES)
def test_a_terraform_file_that_an_ignore_rule_hides_gets_the_plan_no_record(
    tree: Tree, where: str, name: str
) -> None:
    with_local_file(tree)
    hide_zz_files(tree, where)
    (tree.module / name).write_text("# a resource that changes the plan\n")
    # The premise: a plain `git status` as this home's owner runs it says nothing.
    assert plain_status_as_the_caller(tree) == ""

    done = tree.run("plan")

    assert done.returncode == 0, everything_printed(done)
    assert "stub terraform plan" in done.stdout  # reading the plan is free
    assert not (tree.module / tree.meta_file).exists()
    assert "zz_extra" not in everything_printed(done)


@pytest.mark.parametrize("where", HIDING_PLACES[1:])
def test_the_plan_names_the_kind_and_the_count_of_the_files_an_ignore_rule_hides(
    tree: Tree, where: str
) -> None:
    with_local_file(tree)
    hide_zz_files(tree, where)
    (tree.module / "zz_one.tf").write_text("# one\n")
    (tree.module / "zz_two.tf.json").write_text("{}\n")

    done = tree.run("plan")

    assert done.returncode == 0, everything_printed(done)
    assert "2 untracked Terraform files" in done.stdout
    assert "zz_one" not in everything_printed(done)
    assert "zz_two" not in everything_printed(done)
    assert not (tree.module / tree.meta_file).exists()


@pytest.mark.parametrize("name", ["zz_extra.tf", "zz_extra.tf.json"])
@pytest.mark.parametrize("where", HIDING_PLACES)
def test_a_plan_made_before_a_hidden_terraform_file_appeared_is_not_applied(
    tree: Tree, where: str, name: str
) -> None:
    with_local_file(tree)
    hide_zz_files(tree, where)
    with_saved_plan(tree)
    (tree.module / name).write_text("# a resource the plan was not made from\n")

    done = tree.run("apply")

    assert_plan_refused_and_dropped(tree, done, APPLY_SENTENCE[where])
    assert "zz_extra" not in everything_printed(done)


def test_the_sentence_for_a_hidden_terraform_file_says_how_to_get_rid_of_it(
    tree: Tree,
) -> None:
    """`commit it` is no advice for a file git will not take."""
    with_local_file(tree)
    hide_zz_files(tree, HIDING_PLACES[1])
    with_saved_plan(tree)
    (tree.module / "zz_extra.tf").write_text("# not committed\n")

    done = tree.run("apply")

    assert "1 untracked Terraform file" in done.stderr
    assert "ignore" in done.stderr
    assert "remove it" in done.stderr


def test_the_modules_own_ignored_files_that_are_not_terraform_do_not_trip_it(
    tree: Tree,
) -> None:
    """The plan, its record, what init downloads (a provider, and a module's own
    .tf files under .terraform/) are ignored and untracked: none is a file the
    plan reads from this directory."""
    with_local_file(tree)
    with_saved_plan(tree)
    providers = tree.module / ".terraform" / "providers" / "registry" / "hashicorp"
    providers.mkdir(parents=True)
    (providers / "terraform-provider-aws").write_text("x\n")
    downloaded = tree.module / ".terraform" / "modules" / "network"
    downloaded.mkdir(parents=True)
    (downloaded / "main.tf").write_text("# downloaded by init\n")
    (tree.module / "terraform.tfstate").write_text("{}\n")

    done = tree.run("apply")

    assert done.returncode == 0, everything_printed(done)
    plan = tree.run("plan")
    assert plan.returncode == 0, everything_printed(plan)
    assert (tree.module / tree.meta_file).exists()


def test_a_terraform_file_in_a_subdirectory_is_not_one_the_plan_reads(
    tree: Tree,
) -> None:
    with_local_file(tree)
    hide_zz_files(tree, HIDING_PLACES[1])
    (tree.module / "zzdir").mkdir()
    (tree.module / "zzdir" / "main.tf").write_text("# not loaded\n")

    done = tree.run("plan")

    assert done.returncode == 0, everything_printed(done)
    assert (tree.module / tree.meta_file).exists()


# ── removal ──────────────────────────────────────────────────────────────────


def test_removal_without_a_terminal_refuses_before_reading_anything(
    tree: Tree,
) -> None:
    with_local_file(tree)

    done = tree.run("destroy")

    assert done.returncode == 1
    assert "needs a terminal" in done.stderr
    assert tree.calls() == []
    assert tree.aws_calls() == []


def test_no_environment_variable_lets_a_removal_run_without_a_terminal(
    tree: Tree,
) -> None:
    with_local_file(tree)

    done = tree.run(
        "destroy",
        TF_CLI_ARGS_destroy="-auto-approve",
        TF_CLI_ARGS="-auto-approve",
        TF_INPUT="0",
        CI="true",
    )

    assert done.returncode == 1
    assert "needs a terminal" in done.stderr
    assert tree.calls() == []


def test_removal_with_a_terminal_lets_terraform_ask_its_own_question(
    tree: Tree,
) -> None:
    with_local_file(tree)

    done = tree.run(
        "destroy",
        terminal=True,
        TF_CLI_ARGS_destroy="-auto-approve",
        TF_CLI_ARGS="-auto-approve",
    )

    assert done.returncode == 0, done.stdout
    # The question reached the terminal (the test types its answer only then),
    # and what was typed reached Terraform on a standard input that is one.
    assert QUESTION in done.stdout
    calls = tree.stub_lines()
    assert "tty_stdin=yes" in calls
    assert "answer=yes" in calls
    (call,) = tree.terraform_calls("destroy")
    assert "-auto-approve" not in call
    assert "-input=false" not in call
    assert "TF_CLI_ARGS" not in tree.terraform_env()
    assert_no_identifier_printed(done)


def test_removal_with_a_terminal_still_checks_the_account_first(tree: Tree) -> None:
    with_local_file(tree)

    done = tree.run("destroy", terminal=True, STUB_ACCOUNT=OTHER)

    assert done.returncode == 1
    assert "not the one pinned in" in done.stdout
    assert tree.terraform_calls("destroy") == []


def test_removal_initialises_first_counts_what_the_state_holds_and_then_removes(
    tree: Tree,
) -> None:
    with_local_file(tree)

    done = tree.run("destroy", terminal=True, STUB_STATE_COUNT="4")

    assert done.returncode == 0, done.stdout
    assert tree.subcommands() == ["init", "state", "destroy", "state"]
    assert "4 resources" in done.stdout
    assert "removed" in done.stdout.splitlines()[-1]


@pytest.mark.parametrize(
    "how", [{"STUB_STATE_COUNT": "0"}, {"STUB_STATE_MISSING": "1"}]
)
def test_removal_over_an_empty_state_refuses_with_a_sentence_and_never_says_removed(
    tree: Tree, how: dict[str, str]
) -> None:
    with_local_file(tree)

    done = tree.run("destroy", terminal=True, **how)

    assert done.returncode == 1, done.stdout
    assert "holds nothing" in done.stdout
    assert "removed." not in done.stdout
    assert "lost" in done.stdout  # says what to do if the state was lost
    assert "README" in done.stdout
    assert tree.terraform_calls("destroy") == []


def test_a_state_that_holds_only_data_sources_holds_nothing_to_remove(
    tree: Tree,
) -> None:
    with_local_file(tree)

    done = tree.run("destroy", terminal=True, STUB_STATE_COUNT="0", STUB_STATE_DATA="1")

    assert done.returncode == 1, done.stdout
    assert "holds nothing" in done.stdout
    assert tree.terraform_calls("destroy") == []


def test_removal_that_cannot_read_the_state_refuses_and_says_so(tree: Tree) -> None:
    with_local_file(tree)

    done = tree.run("destroy", terminal=True, STUB_STATE_STATUS="1")

    assert done.returncode == 1, done.stdout
    assert "cannot read the state" in done.stdout
    assert "Failed to load state" not in done.stdout  # terraform's words stay out
    assert tree.terraform_calls("destroy") == []


def test_removal_does_not_say_removed_while_the_state_still_holds_resources(
    tree: Tree,
) -> None:
    with_local_file(tree)

    done = tree.run("destroy", terminal=True, STUB_REMOVAL_KEEPS_STATE="1")

    assert done.returncode == 1, done.stdout
    assert "removed." not in done.stdout
    assert "still holds" in done.stdout


def test_a_failed_removal_says_what_is_left_and_what_to_do_and_never_says_removed(
    tree: Tree,
) -> None:
    with_local_file(tree)

    done = tree.run("destroy", terminal=True, STUB_DESTROY_STATUS="3")

    assert done.returncode == 1, done.stdout
    assert "removed." not in done.stdout
    assert "failed" in done.stdout
    assert "exit 3" in done.stdout
    assert "still holds what is left" in done.stdout
    assert "state list" in done.stdout  # how to read it
    assert "console" in done.stdout
    assert tree.destroy_command in done.stdout  # run it again
    assert "Removal" in done.stdout  # the README's section
    assert tree.subcommands() == ["init", "state", "destroy"]  # no second count
    assert_no_identifier_printed(done)


# ── the script and each module agree on the variables' names ────────────────


def variables_without_a_default(tree: Tree) -> set[str]:
    """The module's variables that have no ``default`` line of their own: the
    ones a plan stops to ask for. Each ``variable`` block is read on its own, so
    a description that says "no default" or a comment about a default minor
    version is not mistaken for one."""
    text = tree.variables_file.read_text(encoding="utf-8")
    declared = re.split(r'^variable "(\w+)" \{$', text, flags=re.MULTILINE)
    blocks = dict(zip(declared[1::2], declared[2::2], strict=True))
    return {
        name
        for name, body in blocks.items()
        if not re.search(r"^  default\s*=", body, re.MULTILINE)
    }


def variables_declared(tree: Tree) -> set[str]:
    text = tree.variables_file.read_text(encoding="utf-8")
    return set(re.findall(r'^variable "(\w+)" \{$', text, flags=re.MULTILINE))


def variables_the_script_gave(tree: Tree) -> set[str]:
    """The variables a plan of this module was given, as the names in the
    environment the stand-in saw."""
    with_local_file(tree)
    done = tree.run("plan")
    assert done.returncode == 0, everything_printed(done)
    return {
        name.removeprefix("TF_VAR_")
        for name in env_names(tree.terraform_env())
        if name.startswith("TF_VAR_")
    }


def test_the_script_exports_every_module_variable_that_has_no_default(
    tree: Tree,
) -> None:
    needed = variables_without_a_default(tree)

    assert needed >= {
        "api_access_cidr",
        "budget_email",
        "expected_account_id",
    }  # the reader found them
    assert needed - variables_the_script_gave(tree) == set()


def test_the_script_exports_no_name_the_module_does_not_declare(tree: Tree) -> None:
    exported = variables_the_script_gave(tree)

    assert exported - variables_declared(tree) == set()
    assert exported >= {"region", "budget_email"}  # the reader found them


def test_the_names_the_tests_expect_the_script_to_export_are_the_names_it_does(
    tree: Tree,
) -> None:
    assert {f"TF_VAR_{n}" for n in variables_the_script_gave(tree)} == TF_VAR_NAMES


def test_the_same_variables_reach_apply_and_the_removal_of_each_module(
    tree: Tree,
) -> None:
    with_local_file(tree)
    with_saved_plan(tree)

    tree.run("apply")
    tree.run("destroy", terminal=True)

    seen = {n for n in env_names(tree.terraform_env()) if n.startswith("TF_VAR_")}
    assert seen == TF_VAR_NAMES
    assert {n.removeprefix("TF_VAR_") for n in seen} <= variables_declared(tree)


# ── what each module keeps of its own (S079) ─────────────────────────────────


def test_the_two_modules_never_share_a_state_directory_or_a_state_file(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    paths = {}
    for row in MODULES:
        tree = make_tree(tmp_path / row.directory, row)
        tree.base_env["HOME"] = str(home)
        with_local_file(tree)

        done = tree.run("plan")

        assert done.returncode == 0, everything_printed(done)
        (init,) = tree.terraform_calls("init")
        (given,) = re.findall(r"-backend-config=path=(\S+)", init)
        paths[row.name] = Path(given)
        assert paths[row.name] == home / row.state_directory / row.state_file
        assert paths[row.name].parent.is_dir()
        assert stat.S_IMODE(paths[row.name].parent.stat().st_mode) == 0o700
        # Not in this module's directory, nor in any checkout of the tree.
        assert tree.module not in paths[row.name].parents
        assert tree.root / "infra" not in paths[row.name].parents
    assert paths["aws"].parent != paths["aws-kubeadm"].parent
    assert paths["aws"].name != paths["aws-kubeadm"].name
    assert paths["aws"].parent not in paths["aws-kubeadm"].parents
    assert paths["aws-kubeadm"].parent not in paths["aws"].parents


def test_the_two_state_directories_are_named_by_their_modules_literal_names() -> None:
    assert MANAGED.state_directory == ".local/state/meridian-aws"
    assert SELF_MANAGED.state_directory == ".local/state/meridian-aws-kubeadm"
    assert SELF_MANAGED.state_file == "aws-kubeadm.tfstate"
    assert SELF_MANAGED.plan_file == "aws-kubeadm.tfplan"  # what the scan skips
    assert SELF_MANAGED.meta_file == "aws-kubeadm.tfplan.meta"


@pytest.mark.parametrize(
    "name",
    [
        "terraform.tfvars",
        ".auto.tfvars",
        "mine.auto.tfvars.json",
        "override.tf",
        "ZZ_OVERRIDE.TF.JSON",
    ],
)
def test_a_file_that_would_change_the_other_modules_plan_is_not_this_modules_business(
    tree: Tree, name: str
) -> None:
    other = with_the_other_module_directory(tree)
    (other / name).write_text("# would change the other module's plan\n")
    with_local_file(tree)

    done = tree.run("plan")

    assert done.returncode == 0, everything_printed(done)
    assert (tree.module / tree.meta_file).exists()


def test_a_hidden_terraform_file_of_the_other_module_does_not_stop_this_record(
    tree: Tree,
) -> None:
    other = with_the_other_module_directory(tree)
    (other / "zz_extra.tf").write_text("# a file the other module's plan would read\n")
    (tree.root / ".git" / "info" / "exclude").write_text("zz*\n")
    with_local_file(tree)

    done = tree.run("plan")

    assert done.returncode == 0, everything_printed(done)
    assert "untracked Terraform" not in done.stdout
    assert (tree.module / tree.meta_file).exists()


def test_a_workspace_of_the_other_module_is_not_this_modules_business(
    tree: Tree,
) -> None:
    other = with_the_other_module_directory(tree)
    (other / ".terraform").mkdir()
    (other / ".terraform" / "environment").write_text("w1")
    with_local_file(tree)
    with_saved_plan(tree)

    plan = tree.run("plan")
    done = tree.run("apply")

    assert plan.returncode == 0, everything_printed(plan)
    assert done.returncode == 0, everything_printed(done)


def with_the_other_module_directory(tree: Tree) -> Path:
    other_row = SELF_MANAGED if tree.row is MANAGED else MANAGED
    other = tree.root / "infra" / "terraform" / other_row.directory
    other.mkdir()
    (other / "main.tf").write_text("# a stand-in module\n")
    git(tree.root, "add", "-A")
    git(tree.root, "commit", "-q", "-m", "the other module's directory")
    return other


@pytest.mark.parametrize("subcommand", ["plan", "apply", "destroy"])
def test_both_modules_read_the_one_local_file_and_name_it_when_it_is_missing(
    tree: Tree, subcommand: str
) -> None:
    done = run_refusing(tree, subcommand)

    assert done.returncode == 1, everything_printed(done)
    assert f"no {tree.local_file}" in everything_printed(done)
    assert tree.local_file.name == "local.env-aws"
    assert tree.calls() == []


@only_the_self_managed_module
@pytest.mark.parametrize("subcommand", ["plan", "apply", "destroy"])
def test_a_second_local_file_for_the_self_managed_module_is_not_read(
    tree: Tree, subcommand: str
) -> None:
    second = tree.local_file.with_name("local.env-aws-kubeadm")
    second.write_text(LOCAL_FILE)
    second.chmod(0o600)

    done = run_refusing(tree, subcommand)

    assert done.returncode == 1, everything_printed(done)
    assert f"no {tree.local_file}" in everything_printed(done)
    assert tree.aws_calls() == []


def test_the_pin_of_the_account_is_the_one_both_modules_read(tree: Tree) -> None:
    with_local_file(tree)

    done = tree.run("plan", STUB_ACCOUNT=OTHER)

    assert done.returncode == 1
    assert "not the one pinned in" in done.stderr
    assert tree.terraform_calls("plan") == []
    assert not (tree.module / tree.plan_file).exists()


def test_the_sentence_without_a_home_names_this_modules_readme(tree: Tree) -> None:
    with_local_file(tree)
    env = {k: v for k, v in tree.base_env.items() if k != "HOME"}

    done = subprocess.run(
        tree.command(*tree.words("plan")),
        capture_output=True,
        text=True,
        env=env,
        cwd=tree.root,
        stdin=subprocess.DEVNULL,
        timeout=60,
        check=False,
    )

    assert done.returncode == 1
    assert f"({tree.row.readme}, State)" in done.stderr


def test_the_sentence_for_another_workspace_names_this_modules_directory(
    tree: Tree,
) -> None:
    with_local_file(tree)
    with_workspace(tree, "w1")

    done = run_refusing(tree, "plan")

    printed = everything_printed(done)
    assert done.returncode == 1, printed
    directory = f"infra/terraform/{tree.row.directory}"
    assert f"terraform -chdir={directory} workspace select default" in printed
    assert f"({tree.row.readme}, State)" in printed
    other = "aws" if tree.row is SELF_MANAGED else "aws-kubeadm"
    assert f"infra/terraform/{other} " not in printed


def test_the_sentences_of_the_removal_name_this_modules_state_and_readme(
    tree: Tree,
) -> None:
    with_local_file(tree)

    empty = tree.run("destroy", terminal=True, STUB_STATE_COUNT="0")
    failed = tree.run("destroy", terminal=True, STUB_DESTROY_STATUS="3")

    assert empty.returncode == 1, empty.stdout
    assert str(tree.state_path) in empty.stdout
    assert f"{tree.row.readme}, Removal, 'If the state is lost'" in empty.stdout
    assert failed.returncode == 1, failed.stdout
    directory = f"infra/terraform/{tree.row.directory}"
    assert f"terraform -chdir={directory} state list" in failed.stdout
    assert f"({tree.row.readme}, Removal)" in failed.stdout
    assert tree.destroy_command in failed.stdout


def test_what_a_plan_of_instances_prints_is_redacted_on_every_command(
    tree: Tree,
) -> None:
    with_local_file(tree)
    with_saved_plan(tree)

    outputs = [
        tree.run("validate"),
        tree.run("plan"),
        tree.run("apply"),
        tree.run("destroy", terminal=True),
    ]

    for done in outputs:
        printed = everything_printed(done)
        assert "stub instance shapes:" in printed  # the line did come through
        for shape in INSTANCE_SHAPES:
            assert shape not in printed
        assert_no_identifier_printed(done)


def test_the_readme_a_modules_sentences_name_has_the_sections_they_cite(
    module: Module,
) -> None:
    """The script's sentences on the state and the removal cite `State`,
    `Removal` and `If the state is lost` of the module's README (and the sentence
    for a missing home cites `State`): the headings must be there."""
    text = (REPO_ROOT / module.readme).read_text(encoding="utf-8")
    headings = re.findall(r"^#{2,3} (.+)$", text, flags=re.MULTILINE)

    assert "State" in headings
    assert "Removal" in headings
    assert "If the state is lost" in headings


def test_the_self_managed_readme_names_the_command_lines_and_files_of_the_script() -> (
    None
):
    text = (REPO_ROOT / SELF_MANAGED.readme).read_text(encoding="utf-8")
    prose = " ".join(text.replace("*", "").split())

    for literal in (
        SELF_MANAGED.plan_command,
        SELF_MANAGED.apply_command,
        SELF_MANAGED.destroy_command,
        SELF_MANAGED.plan_file,
        SELF_MANAGED.meta_file,
        f"~/{SELF_MANAGED.state_directory}/{SELF_MANAGED.state_file}",
        "module=aws-kubeadm",
        "infra/terraform/local.env-aws",
    ):
        assert literal in prose, literal
    assert "no `make` target for them yet" in prose
