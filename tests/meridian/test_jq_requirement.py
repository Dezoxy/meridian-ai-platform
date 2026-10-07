"""A missing ``jq`` skips on a developer's machine and fails in CI (S073 K10).

``requires_jq`` marks the tests that run the real ``jq`` (``jqsupport.py``). The
lookup and the environment are patched here, so the rule is read both ways on a
machine that has ``jq`` and on one that has not.
"""

import pytest
from jqsupport import CI_ENV, MISSING, stop_without_jq
from test_kind_manifests import requires_jq


@pytest.fixture
def jq_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("jqsupport.shutil.which", lambda name: None)


@pytest.fixture
def jq_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("jqsupport.shutil.which", lambda name: f"/usr/bin/{name}")


@pytest.mark.parametrize("ci", ["true", "false", ""])
def test_an_installed_jq_lets_the_test_run_in_ci_or_out_of_it(
    jq_present: None, monkeypatch: pytest.MonkeyPatch, ci: str
) -> None:
    monkeypatch.setenv(CI_ENV, ci)

    assert stop_without_jq() is None


def test_a_missing_jq_fails_the_test_in_ci(
    jq_absent: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(CI_ENV, "true")

    with pytest.raises(pytest.fail.Exception) as raised:
        stop_without_jq()

    assert MISSING in str(raised.value)


@pytest.mark.parametrize("ci", ["false", "", "1", "True"])
def test_a_missing_jq_skips_the_test_out_of_ci(
    jq_absent: None, monkeypatch: pytest.MonkeyPatch, ci: str
) -> None:
    # Only the exact value GitHub sets is CI: a developer's "1" is not.
    monkeypatch.setenv(CI_ENV, ci)

    with pytest.raises(pytest.skip.Exception) as raised:
        stop_without_jq()

    assert MISSING in str(raised.value)


def test_a_missing_jq_skips_when_the_variable_is_not_set_at_all(
    jq_absent: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(CI_ENV, raising=False)

    with pytest.raises(pytest.skip.Exception):
        stop_without_jq()


def test_requires_jq_is_the_fixture_that_applies_the_rule_and_not_a_skip_mark() -> None:
    # A skipif mark cannot fail: the rule is in the fixture the mark names.
    assert requires_jq.name == "usefixtures"
    assert requires_jq.args == ("jq_installed",)
