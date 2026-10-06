"""The claim-brief package imports no HTTP client (S037, F4, medium 2).

A step of the workload's own class can hold something that calls a model over the
network, and the host's rule about step classes cannot see that. The package
therefore may not import an HTTP client: the import contract below holds the
third-party ones (the standard library's cannot be named in a contract, so
``test_brief_import_allowlist.py`` holds those). The contract looks at direct
imports only: the package imports the runtime, and the runtime imports ``httpx``
for its own clients.

Each case copies the repository's sources, as ``test_import_contracts.py`` does,
adds one module and runs ``lint-imports`` there.
"""

from pathlib import Path

import pytest
from test_import_contracts import add_probe_in, run_lint_imports
from test_import_contracts import project_copy as project_copy  # a fixture

CONTRACT = "the claim brief imports no HTTP client (S037)"
PACKAGE = "meridian.workloads.claim_brief"
HTTP_CLIENTS = ("httpx", "requests", "aiohttp")


@pytest.mark.parametrize("client", HTTP_CLIENTS)
@pytest.mark.parametrize(
    "form",
    [
        pytest.param("import {}\n", id="import"),
        pytest.param("from {} import Client\n", id="from-import"),
    ],
)
def test_an_import_of_an_http_client_in_the_claim_brief_breaks_a_contract(
    project_copy: Path, client: str, form: str
) -> None:
    probe = add_probe_in(project_copy, PACKAGE, form.format(client))

    exit_code, output = run_lint_imports(project_copy)

    assert exit_code != 0, output
    assert CONTRACT in output, output
    assert f"{probe} -> {client}" in output, output


@pytest.mark.parametrize(
    "package",
    ["meridian.workloads.claims_triage", "meridian.runtime"],
)
def test_the_http_client_is_free_for_the_packages_the_contract_does_not_name(
    project_copy: Path, package: str
) -> None:
    add_probe_in(project_copy, package, "import httpx\n")

    exit_code, output = run_lint_imports(project_copy)

    assert exit_code == 0, output
