"""Base types of the services' JSON contracts."""

from pydantic import AfterValidator, BaseModel, ConfigDict


class WireModel(BaseModel):
    """A request or response body: immutable, and no field nobody declared."""

    model_config = ConfigDict(frozen=True, extra="forbid")


def _reject_nul(value: str) -> str:
    # PostgreSQL text cannot hold U+0000. Left alone, it reaches the database
    # as an error that quotes the value, and the caller sees a fake outage.
    if "\x00" in value:
        raise ValueError("must not contain a NUL character")
    return value


# Put it in an ``Annotated`` free-text field: ``Annotated[str, NoNul]``.
NoNul = AfterValidator(_reject_nul)
