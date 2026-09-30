"""The wire base class and the NUL rule every free-text field shares."""

from typing import Annotated

import pytest
from pydantic import StringConstraints, ValidationError

from meridian.platform.common.wire import NoNul, WireModel


class Note(WireModel):
    text: Annotated[str, StringConstraints(min_length=1), NoNul]


def test_a_wire_model_is_frozen_and_refuses_extra_fields() -> None:
    note = Note(text="hello")

    with pytest.raises(ValidationError):
        Note.model_validate({"text": "hello", "more": 1})
    with pytest.raises(ValidationError):
        note.text = "changed"  # type: ignore[misc]


def test_text_without_a_nul_byte_is_accepted() -> None:
    assert Note(text="line\nbreak and emoji \U0001f600").text.startswith("line")


@pytest.mark.parametrize("text", ["\x00", "a\x00b", "tail\x00"])
def test_text_with_a_nul_byte_is_refused_without_echoing_it(text: str) -> None:
    with pytest.raises(ValidationError) as raised:
        Note(text=text)

    (error,) = raised.value.errors(include_input=False)
    assert error["loc"] == ("text",)
    assert "NUL" in error["msg"]
