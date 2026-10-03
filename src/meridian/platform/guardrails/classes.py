"""The order of the four data classes, from least to most sensitive."""

from meridian.platform.registry.models import DataClass

DATA_CLASS_ORDER: tuple[DataClass, ...] = (
    "synthetic",
    "internal",
    "personal",
    "special",
)
_RANK: dict[str, int] = {name: rank for rank, name in enumerate(DATA_CLASS_ORDER)}


def higher_class(a: DataClass, b: DataClass) -> DataClass:
    """The more sensitive of two data classes. A request's class can be raised
    by this and never lowered."""
    return a if _RANK[a] >= _RANK[b] else b


def parse_data_class(value: str) -> DataClass | None:
    """The data class named exactly ``value``, or None. Case and surrounding
    space are not forgiven: a label that is nearly right is not a label."""
    for name in DATA_CLASS_ORDER:
        if value == name:
            return name
    return None
