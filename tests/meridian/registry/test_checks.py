"""The cross-file checks: plant one violation in a copy, expect its message."""

from collections.abc import Callable
from pathlib import Path

import pytest

from meridian.platform.registry import load_registry

Plant = Callable[..., Path]
LoadErrors = Callable[[Path], tuple[str, ...]]
Edit = tuple[str, str, str]

# Edits that turn the first deployment (gpt-4o) into a GlobalStandard one.
GLOBAL_SKU: Edit = ("models.yaml", "    sku: Standard\n", "    sku: GlobalStandard\n")
GLOBAL_LABEL: Edit = (
    "models.yaml",
    "    residency: eu-region\n",
    "    residency: global\n",
)
GPT4O_CLASSES = "    data_classes: [synthetic, internal, personal]\n"
FIRST_TOOL_ROW = "    effect: read\n"
# A decision tool needs an idempotency key too, so plant both lines.
DECISION_ROWS = "    effect: decision\n    idempotency_key_required: true\n"

REFERENCE_CASES = [
    pytest.param(
        [("models.yaml", "provider: azure-openai", "provider: ghost")],
        "models.yaml: deployments[0].provider: unknown provider 'ghost'",
        id="deployment-provider",
    ),
    pytest.param(
        [("tools.yaml", "server: policy-mcp", "server: ghost-mcp")],
        "tools.yaml: tools[0].server: unknown server 'ghost-mcp'",
        id="tool-server",
    ),
    pytest.param(
        [("agents.yaml", "      - wording_search\n", "      - ghost_tool\n")],
        "agents.yaml: agents[0].tools[2]: unknown tool 'ghost_tool'",
        id="agent-tool",
    ),
    pytest.param(
        [("tenants.yaml", "agents: [claims-triage]", "agents: [ghost]")],
        "tenants.yaml: tenants[0].agents[0]: unknown agent 'ghost'",
        id="tenant-agent",
    ),
    pytest.param(
        [("policies.yaml", "candidates: [aoai-sdc-gpt-4o]", "candidates: [ghost]")],
        "policies.yaml: routes[0].candidates[0]: unknown deployment 'ghost'",
        id="route-candidate",
    ),
    pytest.param(
        [
            (
                "policies.yaml",
                "  - id: personal\n    residency: [eu-region, eu-zone]\n",
                "",
            )
        ],
        "tenants.yaml: tenants[0].data_class: unknown data class 'personal'",
        id="tenant-data-class",
    ),
    pytest.param(
        [
            (
                "policies.yaml",
                "  - id: personal\n    residency: [eu-region, eu-zone]\n",
                "",
            )
        ],
        "models.yaml: deployments[0].data_classes[2]: unknown data class 'personal'",
        id="deployment-data-class",
    ),
    pytest.param(
        [("agents.yaml", "      - wording_search\n", "      - policy_lookup\n")],
        "agents.yaml: agents[0].tools: tool 'policy_lookup' is listed twice",
        id="agent-tool-listed-twice",
    ),
]


@pytest.mark.parametrize(("edits", "expected"), REFERENCE_CASES)
def test_unresolved_or_repeated_reference_is_reported(
    plant: Plant, load_errors: LoadErrors, edits: list[Edit], expected: str
) -> None:
    errors = load_errors(plant(*edits))

    assert expected in errors


def test_duplicate_id_is_reported(plant: Plant, load_errors: LoadErrors) -> None:
    directory = plant(("providers.yaml", "id: replay", "id: azure-openai"))

    errors = load_errors(directory)

    assert "providers.yaml: providers: duplicate id 'azure-openai'" in errors


def test_incomplete_data_classes_are_reported(
    plant: Plant, load_errors: LoadErrors
) -> None:
    directory = plant(("policies.yaml", "  - id: special\n    residency: []\n", ""))

    errors = load_errors(directory)

    assert errors == ("policies.yaml: data_classes: data class 'special' is missing",)


@pytest.mark.parametrize(
    ("edit", "field"),
    [
        pytest.param(("models.yaml", "    sku: Standard\n", ""), "sku", id="sku"),
        pytest.param(
            ("models.yaml", "    terraform_key: sdc/gpt-4o\n", ""),
            "terraform_key",
            id="terraform-key",
        ),
        pytest.param(
            ("models.yaml", "    retires: 2027-04-14\n", ""), "retires", id="retires"
        ),
        pytest.param(
            ("models.yaml", "    region: swedencentral\n", ""), "region", id="region"
        ),
        pytest.param(
            ("models.yaml", "    deployment_name: gpt-4o\n", ""),
            "deployment_name",
            id="deployment-name",
        ),
    ],
)
def test_azure_deployment_missing_a_required_field_is_reported(
    plant: Plant, load_errors: LoadErrors, edit: Edit, field: str
) -> None:
    errors = load_errors(plant(edit))

    assert (
        f"models.yaml: deployments[0].{field}: required for provider kind "
        "'azure-openai' (deployment 'aoai-sdc-gpt-4o')"
    ) in errors


@pytest.mark.parametrize(
    ("line", "field"),
    [
        ("    region: swedencentral\n", "region"),
        ("    sku: Standard\n", "sku"),
        ("    terraform_key: sdc/gpt-4o\n", "terraform_key"),
    ],
)
def test_replay_deployment_with_azure_fields_is_reported(
    plant: Plant, load_errors: LoadErrors, line: str, field: str
) -> None:
    anchor = "    model: replay-chat\n"
    directory = plant(("models.yaml", anchor, anchor + line))

    errors = load_errors(directory)

    assert (
        f"models.yaml: deployments[2].{field}: must not be set for provider kind "
        "'replay' (deployment 'replay-chat')"
    ) in errors


LABEL_CASES = [
    pytest.param(
        [("models.yaml", "    sku: Standard\n", "    sku: DataZoneStandard\n")],
        "label 'eu-region' does not match sku 'DataZoneStandard' in region "
        "'swedencentral'; expected 'eu-zone'",
        id="datazone-labelled-eu-region",
    ),
    pytest.param(
        [("models.yaml", "region: swedencentral", "region: eastus")],
        "label 'eu-region' does not match sku 'Standard' in region 'eastus'; "
        "expected 'global'",
        id="standard-in-eastus-labelled-eu-region",
    ),
    pytest.param(
        [("models.yaml", "region: swedencentral", "region: switzerlandnorth")],
        "in region 'switzerlandnorth'; expected 'global'",
        id="switzerland-is-not-eu",
    ),
    pytest.param(
        [
            GLOBAL_SKU,
            ("models.yaml", "    residency: eu-region\n", "    residency: eu-zone\n"),
        ],
        "label 'eu-zone' does not match sku 'GlobalStandard' in region "
        "'swedencentral'; expected 'global'",
        id="globalstandard-labelled-eu-zone",
    ),
    pytest.param(
        [
            (
                "models.yaml",
                '    version: "1"\n    residency: eu-region\n',
                '    version: "1"\n    residency: global\n',
            )
        ],
        "label 'global' does not match the replay provider runs inside the "
        "platform; expected 'eu-region'",
        id="replay-labelled-global",
    ),
    pytest.param(
        [("models.yaml", "    residency: eu-region\n", "    residency: eu-zone\n")],
        "label 'eu-zone' does not match sku 'Standard' in region 'swedencentral'; "
        "expected 'eu-region'",
        id="standard-in-eu-labelled-eu-zone",
    ),
]


@pytest.mark.parametrize(("edits", "fragment"), LABEL_CASES)
def test_residency_label_that_contradicts_the_facts_is_reported(
    plant: Plant, load_errors: LoadErrors, edits: list[Edit], fragment: str
) -> None:
    errors = load_errors(plant(*edits))

    assert any(fragment in e and e.startswith("models.yaml: ") for e in errors), errors


def test_personal_data_on_a_global_deployment_is_reported(
    plant: Plant, load_errors: LoadErrors
) -> None:
    directory = plant(GLOBAL_SKU, GLOBAL_LABEL)

    errors = load_errors(directory)

    assert (
        "models.yaml: deployments[0].data_classes[2]: data class 'personal' does "
        "not allow residency label 'global' (deployment 'aoai-sdc-gpt-4o')"
    ) in errors
    assert any("data class 'internal' does not allow" in e for e in errors)
    assert not any("data class 'synthetic' does not allow" in e for e in errors)


def test_special_data_on_any_deployment_is_reported(
    plant: Plant, load_errors: LoadErrors
) -> None:
    directory = plant(
        ("models.yaml", GPT4O_CLASSES, "    data_classes: [synthetic, special]\n")
    )

    errors = load_errors(directory)

    assert (
        "models.yaml: deployments[0].data_classes[1]: data class 'special' does "
        "not allow residency label 'eu-region' (deployment 'aoai-sdc-gpt-4o')"
    ) in errors


def test_write_tool_without_idempotency_key_is_reported(
    plant: Plant, load_errors: LoadErrors
) -> None:
    directory = plant(
        (
            "tools.yaml",
            "idempotency_key_required: true",
            "idempotency_key_required: false",
        )
    )

    errors = load_errors(directory)

    assert errors == (
        "tools.yaml: tools[3].idempotency_key_required: a write tool needs an "
        "idempotency key (tool 'add_claim_note')",
    )


def test_decision_tool_in_an_agent_allowlist_is_reported(
    plant: Plant, load_errors: LoadErrors
) -> None:
    directory = plant(("tools.yaml", FIRST_TOOL_ROW, DECISION_ROWS))

    errors = load_errors(directory)

    assert errors == (
        "agents.yaml: agents[0].tools[0]: agent 'claims-triage' lists decision "
        "tool 'policy_lookup'; only humans decide (T-31)",
    )


def test_decision_tool_outside_every_allowlist_is_accepted(plant: Plant) -> None:
    directory = plant(
        ("tools.yaml", FIRST_TOOL_ROW, DECISION_ROWS),
        ("agents.yaml", "      - policy_lookup\n", ""),
    )

    tool = load_registry(directory).tool("policy_lookup")

    assert tool is not None
    assert tool.effect == "decision"


def test_route_candidate_with_the_wrong_purpose_is_reported(
    plant: Plant, load_errors: LoadErrors
) -> None:
    directory = plant(
        (
            "policies.yaml",
            "candidates: [aoai-sdc-gpt-4o]",
            "candidates: [aoai-sdc-text-embedding-3-large]",
        )
    )

    errors = load_errors(directory)

    assert (
        "policies.yaml: routes[0].candidates[0]: deployment "
        "'aoai-sdc-text-embedding-3-large' has purpose 'embedding', the route is 'chat'"
    ) in errors


def test_duplicate_route_purpose_is_reported(
    plant: Plant, load_errors: LoadErrors
) -> None:
    directory = plant(
        ("policies.yaml", "  - purpose: embedding\n", "  - purpose: chat\n")
    )

    errors = load_errors(directory)

    assert "policies.yaml: routes: duplicate purpose 'chat'" in errors


def test_tenant_with_no_allowed_candidate_is_reported(
    plant: Plant, load_errors: LoadErrors
) -> None:
    directory = plant(
        GLOBAL_SKU,
        GLOBAL_LABEL,
        ("models.yaml", GPT4O_CLASSES, "    data_classes: [synthetic]\n"),
    )

    errors = load_errors(directory)

    unserved = [e for e in errors if "can never be served" in e]
    assert unserved == [
        "policies.yaml: routes[0]: no candidate allows data class 'personal' for "
        "purpose 'chat', so tenant 'claims-triage' can never be served",
        "policies.yaml: routes[0]: no candidate allows data class 'personal' for "
        "purpose 'chat', so tenant 'evaluation' can never be served",
    ]


# ── residency ceiling (hard rule 3, T-35) ───────────────────────────────────
PERSONAL = "  - id: personal\n    residency: [eu-region, eu-zone]"
INTERNAL = "  - id: internal\n    residency: [eu-region, eu-zone]"


@pytest.mark.parametrize(
    ("old", "new", "where", "name", "label"),
    [
        pytest.param(
            PERSONAL,
            PERSONAL[:-1] + ", global]",
            "data_classes[2].residency[2]",
            "personal",
            "global",
            id="personal",
        ),
        pytest.param(
            INTERNAL,
            INTERNAL[:-1] + ", global]",
            "data_classes[1].residency[2]",
            "internal",
            "global",
            id="internal",
        ),
        pytest.param(
            "  - id: special\n    residency: []",
            "  - id: special\n    residency: [eu-region]",
            "data_classes[3].residency[0]",
            "special",
            "eu-region",
            id="special",
        ),
    ],
)
def test_widening_a_data_class_past_its_ceiling_is_reported(
    plant: Plant,
    load_errors: LoadErrors,
    old: str,
    new: str,
    where: str,
    name: str,
    label: str,
) -> None:
    errors = load_errors(plant(("policies.yaml", old, new)))

    assert errors == (
        f"policies.yaml: {where}: label {label!r} is outside the ceiling for "
        f"data class {name!r}; widening this needs a change to the check itself "
        "(hard rule 3)",
    )


def test_narrowing_a_data_class_below_its_ceiling_is_accepted(plant: Plant) -> None:
    directory = plant(
        ("policies.yaml", PERSONAL, "  - id: personal\n    residency: [eu-region]")
    )

    registry = load_registry(directory)

    personal = registry.data_class("personal")
    assert personal is not None
    assert personal.residency == ("eu-region",)


def test_label_listed_twice_in_a_data_class_is_reported(
    plant: Plant, load_errors: LoadErrors
) -> None:
    directory = plant(("policies.yaml", PERSONAL, PERSONAL[:-1] + ", eu-zone]"))

    errors = load_errors(directory)

    assert (
        "policies.yaml: data_classes[2].residency: label 'eu-zone' is listed twice"
        in errors
    )


# ── deployments: terraform key, uniqueness, strings, region ─────────────────
def test_empty_terraform_key_is_rejected(plant: Plant, load_errors: LoadErrors) -> None:
    directory = plant(("models.yaml", "terraform_key: sdc/gpt-4o", 'terraform_key: ""'))

    errors = load_errors(directory)

    assert len(errors) == 1
    assert errors[0].startswith("models.yaml: deployments[0].terraform_key: ")


def test_terraform_key_without_a_location_alias_is_rejected(
    plant: Plant, load_errors: LoadErrors
) -> None:
    directory = plant(
        ("models.yaml", "terraform_key: sdc/gpt-4o", "terraform_key: gpt-4o")
    )

    errors = load_errors(directory)

    assert errors[0].startswith("models.yaml: deployments[0].terraform_key: ")


def test_duplicate_terraform_key_is_reported(
    plant: Plant, load_errors: LoadErrors
) -> None:
    directory = plant(
        (
            "models.yaml",
            "terraform_key: sdc/text-embedding-3-large",
            "terraform_key: sdc/gpt-4o",
        )
    )

    errors = load_errors(directory)

    assert "models.yaml: deployments: duplicate terraform_key 'sdc/gpt-4o'" in errors


def test_duplicate_deployment_name_in_one_region_is_reported(
    plant: Plant, load_errors: LoadErrors
) -> None:
    directory = plant(
        (
            "models.yaml",
            "deployment_name: text-embedding-3-large",
            "deployment_name: gpt-4o",
        )
    )

    errors = load_errors(directory)

    assert (
        "models.yaml: deployments: duplicate deployment name (provider/region/name) "
        "'azure-openai/swedencentral/gpt-4o'"
    ) in errors


def test_same_deployment_name_in_another_region_is_accepted(plant: Plant) -> None:
    directory = plant(
        (
            "models.yaml",
            "deployment_name: text-embedding-3-large",
            "deployment_name: gpt-4o",
        ),
        ("models.yaml", "region: swedencentral", "region: francecentral"),
    )

    registry = load_registry(directory)

    assert registry.deployments[0].region == "francecentral"


@pytest.mark.parametrize(
    ("edit", "path"),
    [
        pytest.param(
            ("models.yaml", "region: swedencentral", "region: Sweden Central"),
            "models.yaml: deployments[0].region",
            id="region-display-name",
        ),
        pytest.param(
            ("models.yaml", "region: swedencentral", 'region: ""'),
            "models.yaml: deployments[0].region",
            id="region-empty",
        ),
        pytest.param(
            (
                "providers.yaml",
                "description: Azure OpenAI; Entra ID only, keys off (S007).",
                'description: ""',
            ),
            "providers.yaml: providers[0].description",
            id="empty-description",
        ),
        pytest.param(
            ("models.yaml", "    model: gpt-4o\n", '    model: ""\n'),
            "models.yaml: deployments[0].model",
            id="empty-model",
        ),
        pytest.param(
            (
                "models.yaml",
                "    deployment_name: gpt-4o\n",
                '    deployment_name: ""\n',
            ),
            "models.yaml: deployments[0].deployment_name",
            id="empty-deployment-name",
        ),
        pytest.param(
            (
                "models.yaml",
                "      source: Runs inside",
                '      source: ""\n      x: Runs inside',
            ),
            "models.yaml: deployments[2].price.source",
            id="empty-price-source",
        ),
        pytest.param(
            ("tools.yaml", "scope: policy:read", 'scope: ""'),
            "tools.yaml: tools[0].scope",
            id="empty-scope",
        ),
        pytest.param(
            ("tools.yaml", "scope: policy:read", 'scope: "policy:*"'),
            "tools.yaml: tools[0].scope",
            id="wildcard-scope",
        ),
        pytest.param(
            ("tools.yaml", "scope: policy:read", "scope: policy"),
            "tools.yaml: tools[0].scope",
            id="scope-without-action",
        ),
        pytest.param(
            ("policies.yaml", "candidates: [aoai-sdc-gpt-4o]", "candidates: []"),
            "policies.yaml: routes[0].candidates",
            id="route-without-candidates",
        ),
    ],
)
def test_constrained_string_rejects_a_bad_value_at_its_path(
    plant: Plant, load_errors: LoadErrors, edit: Edit, path: str
) -> None:
    errors = load_errors(plant(edit))

    assert any(e.startswith(f"{path}: ") for e in errors), errors


# ── tools: approval, idempotency, decision words ────────────────────────────
def test_approval_required_on_a_read_tool_is_reported(
    plant: Plant, load_errors: LoadErrors
) -> None:
    directory = plant(
        ("tools.yaml", FIRST_TOOL_ROW, FIRST_TOOL_ROW + "    approval_required: true\n")
    )

    errors = load_errors(directory)

    assert errors == (
        "tools.yaml: tools[0].approval_required: only a write tool can require "
        "approval (tool 'policy_lookup')",
    )


def test_approval_required_on_a_write_tool_is_accepted(plant: Plant) -> None:
    directory = plant(
        (
            "tools.yaml",
            "    scope: claims:note:write\n",
            "    scope: claims:note:write\n    approval_required: true\n",
        )
    )

    tool = load_registry(directory).tool("add_claim_note")

    assert tool is not None
    assert tool.approval_required is True


def test_decision_tool_without_an_idempotency_key_is_reported(
    plant: Plant, load_errors: LoadErrors
) -> None:
    directory = plant(("tools.yaml", FIRST_TOOL_ROW, "    effect: decision\n"))

    errors = load_errors(directory)

    assert (
        "tools.yaml: tools[0].idempotency_key_required: a decision tool needs an "
        "idempotency key (tool 'policy_lookup')"
    ) in errors


@pytest.mark.parametrize(
    ("edits", "word"),
    [
        pytest.param(
            [
                ("tools.yaml", "id: add_claim_note", "id: record_decision"),
                ("agents.yaml", "- add_claim_note", "- record_decision"),
            ],
            "decision",
            id="write-tool-named-record-decision",
        ),
        pytest.param(
            [
                ("tools.yaml", "id: request_approval", "id: approve_claim"),
                ("agents.yaml", "- request_approval", "- approve_claim"),
            ],
            "approve",
            id="tool-named-approve-claim",
        ),
        pytest.param(
            [
                (
                    "tools.yaml",
                    "scope: claims:approval:request",
                    "scope: claims:decline:request",
                )
            ],
            "decline",
            id="scope-with-decline",
        ),
    ],
)
def test_decision_word_in_an_allowlisted_tool_is_reported_whatever_its_effect(
    plant: Plant, load_errors: LoadErrors, edits: list[Edit], word: str
) -> None:
    errors = load_errors(plant(*edits))

    assert len(errors) == 1
    assert errors[0].startswith("agents.yaml: agents[0].tools[")
    assert f"decision word {word!r}; only humans decide (T-31)" in errors[0]


def test_request_approval_is_not_a_decision_tool(real_registry: Path) -> None:
    registry = load_registry(real_registry)

    assert registry.tool("request_approval") is not None
    assert "request_approval" in registry.agents[0].tools


def test_decision_word_in_a_tool_no_agent_lists_is_accepted(plant: Plant) -> None:
    directory = plant(
        ("tools.yaml", "id: add_claim_note", "id: record_decision"),
        ("agents.yaml", "      - add_claim_note\n", ""),
    )

    assert load_registry(directory).tool("record_decision") is not None


# ── replay trust ────────────────────────────────────────────────────────────
def test_replay_provider_with_another_id_is_reported(
    plant: Plant, load_errors: LoadErrors
) -> None:
    directory = plant(
        (
            "providers.yaml",
            "providers:\n",
            "providers:\n  - {id: mock, kind: replay, description: x}\n",
        )
    )

    errors = load_errors(directory)

    assert errors == (
        "providers.yaml: providers[0].id: a provider of kind 'replay' must have "
        "id 'replay'",
    )


def test_replay_deployment_with_a_real_model_name_is_reported(
    plant: Plant, load_errors: LoadErrors
) -> None:
    directory = plant(("models.yaml", "model: replay-chat", "model: gpt-4o"))

    errors = load_errors(directory)

    assert errors == (
        "models.yaml: deployments[2].model: a replay deployment's model must start "
        "with 'replay-' (deployment 'replay-chat')",
    )


# ── routes: every purpose has one ───────────────────────────────────────────
def test_missing_route_for_a_purpose_is_reported(
    plant: Plant, load_errors: LoadErrors
) -> None:
    directory = plant(
        (
            "policies.yaml",
            "  - purpose: embedding\n"
            "    candidates: [aoai-sdc-text-embedding-3-large]\n",
            "",
        )
    )

    errors = load_errors(directory)

    assert errors == ("policies.yaml: routes: no route for purpose 'embedding'",)


def test_empty_routes_report_every_purpose(
    plant: Plant, load_errors: LoadErrors
) -> None:
    directory = plant(
        (
            "policies.yaml",
            "routes:\n  - purpose: chat\n    candidates: [aoai-sdc-gpt-4o]\n"
            "  - purpose: embedding\n"
            "    candidates: [aoai-sdc-text-embedding-3-large]\n",
            "routes: []\n",
        )
    )

    errors = load_errors(directory)

    assert errors == (
        "policies.yaml: routes: no route for purpose 'chat'",
        "policies.yaml: routes: no route for purpose 'embedding'",
    )


def rename_note_tool(new_id: str) -> list[Edit]:
    """Rename the add_claim_note tool in the tool list and the allowlist."""
    return [
        ("tools.yaml", "id: add_claim_note", f"id: {new_id}"),
        ("agents.yaml", "- add_claim_note", f"- {new_id}"),
    ]


@pytest.mark.parametrize(
    ("tool_id", "word"),
    [
        ("claim_approved", "approved"),
        ("approver_note", "approver"),
        ("deny_claim", "deny"),
        ("claim_denied", "denied"),
        ("rejects_claim", "rejects"),
        ("claim_declined", "declined"),
        ("mark_decided", "decided"),
        ("decides_claim", "decides"),
        ("record_decision", "decision"),
    ],
)
def test_inflected_decision_word_in_a_tool_id_is_reported(
    plant: Plant, load_errors: LoadErrors, tool_id: str, word: str
) -> None:
    errors = load_errors(plant(*rename_note_tool(tool_id)))

    assert len(errors) == 1
    assert f"decision word {word!r}; only humans decide (T-31)" in errors[0]


@pytest.mark.parametrize(
    ("scope", "word"),
    [
        ("claims:approved:write", "approved"),
        ("claims:denied:write", "denied"),
        ("claims:rejected:write", "rejected"),
    ],
)
def test_inflected_decision_word_in_a_scope_is_reported(
    plant: Plant, load_errors: LoadErrors, scope: str, word: str
) -> None:
    errors = load_errors(
        plant(("tools.yaml", "scope: claims:note:write", f"scope: {scope}"))
    )

    assert len(errors) == 1
    assert f"decision word {word!r}; only humans decide (T-31)" in errors[0]


@pytest.mark.parametrize(
    "tool_id",
    [
        "list_approvals",
        "approval_queue",
        "denomination_lookup",
        "declaration_lookup",
        "decimal_lookup",
        "review_note",
    ],
)
def test_words_that_only_look_like_decision_words_are_accepted(
    plant: Plant, tool_id: str
) -> None:
    registry = load_registry(plant(*rename_note_tool(tool_id)))

    assert registry.tool(tool_id) is not None


def test_approval_scopes_are_accepted(plant: Plant) -> None:
    directory = plant(
        ("tools.yaml", "scope: claims:note:write", "scope: claims:approvals:write")
    )

    assert load_registry(directory).tool("add_claim_note") is not None


def test_every_seeded_tool_passes_the_decision_word_check(real_registry: Path) -> None:
    registry = load_registry(real_registry)

    assert set(registry.agents[0].tools) == {t.id for t in registry.tools}
