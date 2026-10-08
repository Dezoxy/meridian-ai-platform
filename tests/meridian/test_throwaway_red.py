"""THROWAWAY (S074): one failing test, pushed to a branch that is never merged,
to see the required check `python` turn red on GitHub. Deleted with its branch."""


def test_this_fails_on_purpose() -> None:
    assert 1 + 1 == 3
