"""Load the simulated policy store from the generator's output (S013).

``data/synthetic`` holds a holder, an address and an insured object for each
policy. The store keeps none of them (a tool result enters a prompt, TB-7), so
the models below name only the fields that are stored and ignore the rest.

Everything is checked before anything is written: the manifest must say the
data is synthetic and carry the SHA-256 of each data file (hard rule 2), and
every record must pass its model. An error names the file and the record's
index, never the record, so no claimant-like text reaches a terminal or a log.
Statements use psycopg placeholders only; the caller owns the transaction.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Annotated, Any, Literal

import psycopg
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    ValidationError,
    model_validator,
)

MANIFEST_FILE = "manifest.json"
POLICIES_FILE = "policies.json"
HISTORY_FILE = "claim-history.json"

UPSERT_POLICY = """
INSERT INTO policy.policies (
    policy_number, product, wording_version, start_date, end_date, status,
    lapsed_on, deductible, sum_insured, cover_limit
) VALUES (
    %(policy_number)s, %(product)s, %(wording_version)s, %(start_date)s,
    %(end_date)s, %(status)s, %(lapsed_on)s, %(deductible)s, %(sum_insured)s,
    %(cover_limit)s
)
ON CONFLICT (policy_number) DO UPDATE SET
    product = EXCLUDED.product,
    wording_version = EXCLUDED.wording_version,
    start_date = EXCLUDED.start_date,
    end_date = EXCLUDED.end_date,
    status = EXCLUDED.status,
    lapsed_on = EXCLUDED.lapsed_on,
    deductible = EXCLUDED.deductible,
    sum_insured = EXCLUDED.sum_insured,
    cover_limit = EXCLUDED.cover_limit
"""

UPSERT_HISTORY = """
INSERT INTO policy.claim_history (
    history_id, policy_number, loss_date, peril, paid_amount, status
) VALUES (
    %(history_id)s, %(policy_number)s, %(loss_date)s, %(peril)s,
    %(paid_amount)s, %(status)s
)
ON CONFLICT (history_id) DO UPDATE SET
    policy_number = EXCLUDED.policy_number,
    loss_date = EXCLUDED.loss_date,
    peril = EXCLUDED.peril,
    paid_amount = EXCLUDED.paid_amount,
    status = EXCLUDED.status
"""

# The store mirrors its source: what the source no longer lists is removed,
# history first because it refers to the policy.
DELETE_HISTORY_NOT_LOADED = (
    "DELETE FROM policy.claim_history WHERE history_id <> ALL(%(history_ids)s)"
)
DELETE_POLICIES_NOT_LOADED = (
    "DELETE FROM policy.policies WHERE policy_number <> ALL(%(policy_numbers)s)"
)


class SeedError(Exception):
    """The source cannot be loaded. The message is for the operator and holds
    no record content."""


@dataclass(frozen=True, slots=True)
class SeedCounts:
    policies: int
    claim_history: int


def _require_text(value: object) -> object:
    """A date is an ISO string in the files; a number is not a date."""
    if not isinstance(value, str):
        raise ValueError("a date must be a string")
    return value


IsoDate = Annotated[date, BeforeValidator(_require_text)]
# The cap of the amounts in the tools' output schemas (config/registry/tools.yaml)
# and of the CHECKs of migration 0004: a stored amount is one a tool can return.
Amount = Annotated[StrictInt, Field(ge=0, le=1_000_000_000)]


class PolicyRecord(BaseModel):
    """The stored fields of a policy; the holder and the insured object are
    ignored. ``lapsed_on`` and ``sum_insured`` have no default: the generator
    always writes them, null or not."""

    model_config = ConfigDict(frozen=True)

    policy_number: Annotated[StrictStr, Field(pattern=r"^POL-[0-9]{4}$")]
    product: Annotated[StrictStr, Field(min_length=1, max_length=32)]
    wording_version: Annotated[StrictStr, Field(min_length=1, max_length=16)]
    start_date: IsoDate
    end_date: IsoDate
    status: Literal["active", "lapsed"]
    lapsed_on: IsoDate | None
    deductible: Amount
    sum_insured: Amount | None
    limit: Amount

    @model_validator(mode="after")
    def _dates_and_status_agree(self) -> "PolicyRecord":
        if self.start_date > self.end_date:
            raise ValueError("the policy ends before it starts")
        if (self.status == "lapsed") != (self.lapsed_on is not None):
            raise ValueError("a policy is lapsed exactly when it has a lapse date")
        return self

    def row(self) -> dict[str, Any]:
        return self.model_dump(exclude={"limit"}) | {"cover_limit": self.limit}


class HistoryRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    history_id: Annotated[StrictStr, Field(pattern=r"^HIST-[0-9]{4}$")]
    policy_number: Annotated[StrictStr, Field(pattern=r"^POL-[0-9]{4}$")]
    loss_date: IsoDate
    peril: Annotated[StrictStr, Field(min_length=1, max_length=64)]
    paid_amount: Amount
    status: Annotated[StrictStr, Field(min_length=1, max_length=32)]

    def row(self) -> dict[str, Any]:
        return self.model_dump()


def _read(source: Path, name: str) -> bytes:
    try:
        return (source / name).read_bytes()
    except OSError as exc:
        raise SeedError(f"{name} cannot be read from the source directory") from exc


def _manifest(source: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(_read(source, MANIFEST_FILE))
    except ValueError as exc:
        raise SeedError(f"{MANIFEST_FILE} is not valid JSON") from exc
    if not isinstance(manifest, dict) or manifest.get("synthetic") is not True:
        raise SeedError(
            f"{MANIFEST_FILE} does not say the data is synthetic; only the "
            "generator's output may be loaded (hard rule 2)"
        )
    return manifest


def _verified(source: Path, manifest: dict[str, Any], name: str) -> bytes:
    """The bytes of a data file whose SHA-256 is the manifest's."""
    files = manifest.get("files")
    expected = files.get(name) if isinstance(files, dict) else None
    if not isinstance(expected, str):
        raise SeedError(f"{MANIFEST_FILE} has no hash for {name}")
    data = _read(source, name)
    if hashlib.sha256(data).hexdigest() != expected:
        raise SeedError(
            f"{name} does not match its hash in {MANIFEST_FILE}; "
            "regenerate it with make synthetic"
        )
    return data


def _validated[Model: BaseModel](model: type[Model], item: object) -> Model | None:
    """None when the record is invalid, so the caller raises outside this
    handler and the error carries no cause that holds the record's values."""
    try:
        return model.model_validate(item)
    except ValidationError:
        return None


def _records[Model: BaseModel](
    name: str, data: bytes, model: type[Model]
) -> list[Model]:
    try:
        payload = json.loads(data)
    except ValueError as exc:
        raise SeedError(f"{name} is not valid JSON") from exc
    if not isinstance(payload, list):
        raise SeedError(f"{name} must hold a list of records")
    records: list[Model] = []
    for index, item in enumerate(payload):
        record = _validated(model, item)
        if record is None:
            raise SeedError(f"{name}: record {index} is not valid")
        records.append(record)
    return records


def seed_policies(conn: psycopg.Connection, source: Path) -> SeedCounts:
    """Upsert the policies, then their claim history, then remove the history
    and the policies the source no longer lists, in the caller's transaction
    (the caller commits). The store mirrors the source, and the counts are what
    was loaded.

    Raises ``SeedError`` before any write when the source is not the
    generator's synthetic output or a record is malformed.
    """
    manifest = _manifest(source)
    policies = _records(
        POLICIES_FILE, _verified(source, manifest, POLICIES_FILE), PolicyRecord
    )
    history = _records(
        HISTORY_FILE, _verified(source, manifest, HISTORY_FILE), HistoryRecord
    )
    with conn.cursor() as cursor:
        cursor.executemany(UPSERT_POLICY, [p.row() for p in policies])
        cursor.executemany(UPSERT_HISTORY, [h.row() for h in history])
        cursor.execute(
            DELETE_HISTORY_NOT_LOADED, {"history_ids": [h.history_id for h in history]}
        )
        cursor.execute(
            DELETE_POLICIES_NOT_LOADED,
            {"policy_numbers": [p.policy_number for p in policies]},
        )
    return SeedCounts(policies=len(policies), claim_history=len(history))
