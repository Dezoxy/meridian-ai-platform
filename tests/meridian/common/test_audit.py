"""The audit event and its insert: every field of the event is written (S031)."""

import dataclasses
import re
import uuid

from dbsupport import DatabaseHandle
from servicesupport import owner_rows

from meridian.platform.common.audit import INSERT_EVENT, AuditEvent, write_audit

SERVICE = "agent-runtime"


def test_an_event_names_no_worker_unless_it_is_given_one() -> None:
    plain = AuditEvent(service=SERVICE, event="tool.call", outcome="ok")
    named = AuditEvent(service=SERVICE, event="tool.call", outcome="ok", worker="terms")

    assert plain.worker is None
    assert named.worker == "terms"


def test_the_insert_writes_every_field_of_the_event() -> None:
    fields = {field.name for field in dataclasses.fields(AuditEvent)}

    columns = re.search(r"\(\s*(.*?)\s*\) VALUES", INSERT_EVENT, re.DOTALL)
    placeholders = set(re.findall(r"%\((\w+)\)s", INSERT_EVENT))

    assert columns is not None
    assert placeholders == fields
    assert {name.strip() for name in columns.group(1).split(",")} == fields


def test_the_worker_of_an_event_is_stored_and_none_is_stored_as_null(
    migrated_database: DatabaseHandle,
) -> None:
    named, unnamed = uuid.uuid4(), uuid.uuid4()
    dsn = migrated_database.dsn("agent_runtime")

    write_audit(
        dsn,
        AuditEvent(
            service=SERVICE,
            event="tool.call",
            outcome="refused",
            run_id=named,
            worker="approvals",
        ),
    )
    write_audit(
        dsn,
        AuditEvent(service=SERVICE, event="tool.call", outcome="ok", run_id=unnamed),
    )

    rows = owner_rows(
        migrated_database,
        "SELECT run_id, worker FROM audit.events WHERE run_id = ANY(%s)",
        ([named, unnamed],),
    )
    assert dict(rows) == {named: "approvals", unnamed: None}
