"""The pickle-free MAF checkpoint store (`claimflow.maf_json_store`).

`CheckpointStorage` is a public protocol, so the flow can swap the pickle-based
`FileCheckpointStorage` for a JSON store. These tests pin that it works end to
end, that no pickle is written, and that the type registry is what decides what
a file may turn into.
"""

import asyncio
from pathlib import Path
from typing import get_protocol_members

import pytest
from agent_framework import CheckpointStorage
from agent_framework.exceptions import WorkflowCheckpointException
from claimflow import maf_flow, rules
from claimflow.maf_json_store import JsonCheckpointStorage
from support import OVER_THRESHOLD_CLAIM, run_cli

ADJUSTER = "ADJ-0042"


def _files(store_dir: Path) -> list[Path]:
    return sorted(store_dir.glob("*.json"))


def test_json_store_implements_every_member_of_the_checkpoint_storage_protocol(
    tmp_path: Path,
) -> None:
    # The protocol is public but not runtime-checkable, so compare the member names.
    store = JsonCheckpointStorage(tmp_path, maf_flow.CHECKPOINT_TYPES)

    for member in get_protocol_members(CheckpointStorage):
        assert callable(getattr(store, member)), member


def test_pause_and_resume_in_two_processes_on_the_json_store_write_no_pickle(
    tmp_path: Path,
) -> None:
    store = str(tmp_path / "store")

    paused = run_cli(
        "start", "--framework", "maf", "--maf-store", "json",
        "--claim-id", OVER_THRESHOLD_CLAIM, "--store-dir", store,
    )  # fmt: skip
    done = run_cli(
        "resume", "--framework", "maf", "--maf-store", "json",
        "--run-ref", paused["run_ref"], "--decision", "approve",
        "--adjuster-id", ADJUSTER, "--store-dir", store,
    )  # fmt: skip

    assert paused["status"] == "awaiting_approval"
    assert (done["status"], done["outcome"]) == ("completed", "approve")
    assert done["run_ref"] == paused["run_ref"]
    files = _files(Path(store))
    assert len(files) == 4 + 2  # entry, two supersteps, pause; response entry, done
    assert not any("__pickled__" in file.read_text() for file in files)


def test_json_store_keeps_the_claim_readable_in_the_intermediate_checkpoints(
    store_dir: Path,
) -> None:
    # Without pickle there is also no base64 in front of the personal data: the
    # protection is the store's access control or encryption, as in LangGraph.
    maf_flow.start(OVER_THRESHOLD_CLAIM, store_dir, store="json")

    email = rules.validate(OVER_THRESHOLD_CLAIM).claim["claimant"]["email"]
    assert any(email in file.read_text() for file in _files(store_dir))


def test_json_store_rewrites_every_checkpoint_it_reads_identically(
    tmp_path: Path,
) -> None:
    source, copy = tmp_path / "source", tmp_path / "copy"
    paused = maf_flow.start(OVER_THRESHOLD_CLAIM, source, store="json")
    maf_flow.resume(
        paused.run_ref,
        {"decision": "approve", "adjuster_id": ADJUSTER},
        source,
        store="json",
    )
    reader = maf_flow.open_storage(source, "json")
    writer = maf_flow.open_storage(copy, "json")

    for file in _files(source):
        checkpoint = asyncio.run(reader.load(file.stem))
        asyncio.run(writer.save(checkpoint))

        assert (copy / file.name).read_text() == file.read_text()


def test_json_store_refuses_to_save_a_type_that_is_not_in_the_registry(
    store_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(maf_flow, "CHECKPOINT_TYPES", [])

    # MAF logs the refused save and carries on; the spike's own code raises.
    with pytest.raises(RuntimeError, match="pause was not checkpointed"):
        maf_flow.start(OVER_THRESHOLD_CLAIM, store_dir, store="json")

    assert len(_files(store_dir)) == 1  # only the entry checkpoint, which has no type


def test_json_store_refuses_a_file_that_names_a_type_outside_the_registry(
    store_dir: Path,
) -> None:
    # Where a pickle store would import whatever the file names, this store
    # looks the name up in its registry and stops.
    maf_flow.start(OVER_THRESHOLD_CLAIM, store_dir, store="json")
    tampered = [
        f for f in _files(store_dir) if "claimflow.rules:Validated" in f.read_text()
    ]
    assert tampered  # the checkpoints that carry the claim between steps
    for file in tampered:
        file.write_text(
            file.read_text().replace(
                "claimflow.rules:ValidatedClaim", "subprocess:Popen"
            )
        )
    storage = maf_flow.open_storage(store_dir, "json")

    for file in tampered:
        with pytest.raises(WorkflowCheckpointException, match="subprocess:Popen"):
            asyncio.run(storage.load(file.stem))


def test_json_store_refuses_an_id_that_leaves_the_store_directory(
    store_dir: Path,
) -> None:
    storage = maf_flow.open_storage(store_dir, "json")

    with pytest.raises(WorkflowCheckpointException, match="Invalid checkpoint ID"):
        asyncio.run(storage.load("../outside"))


def test_json_store_delete_reports_whether_it_removed_a_checkpoint(
    store_dir: Path,
) -> None:
    maf_flow.start(OVER_THRESHOLD_CLAIM, store_dir, store="json")
    storage = maf_flow.open_storage(store_dir, "json")
    checkpoint_id = _files(store_dir)[0].stem

    assert asyncio.run(storage.delete(checkpoint_id)) is True
    assert asyncio.run(storage.delete(checkpoint_id)) is False
