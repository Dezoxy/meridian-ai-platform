"""The recorded provider, its deployment and the evaluation judge (S050).

A recorded deployment answers chat from a recording of a real model, so the
registry holds it to what replay is held to and to two more rules: it names the
live deployment that answered the recordings, and it charges that one's price.
"""

from collections.abc import Callable
from pathlib import Path

import pytest

from meridian.platform.registry import load_registry
from meridian.platform.registry.terraform import azure_deployments

Plant = Callable[..., Path]
LoadErrors = Callable[[Path], tuple[str, ...]]
Edit = tuple[str, str, str]

# models.yaml lists the recorded deployment sixth, after the two replay ones.
WHERE = "models.yaml: deployments[5]"
ANCHOR = "    model: recorded-chat\n"
RECORDED_FROM = "    recorded_from: aoai-sdc-gpt-4o\n"
RECORDED_CLASSES = "    data_classes: [synthetic, internal, personal]\n" + RECORDED_FROM
RECORDED_LABEL = "    residency: eu-region\n" + RECORDED_CLASSES
RECORDED_LABEL_GLOBAL = RECORDED_LABEL.replace("eu-region", "global")
RECORDED_ENTRY = "  - purpose: chat\n    deployment: recorded-chat\n"
RECORDED_BLOCK = f"recorded:\n{RECORDED_ENTRY}"


# ── the provider ────────────────────────────────────────────────────────────
def test_the_committed_registry_has_a_recorded_provider(real_registry: Path) -> None:
    registry = load_registry(real_registry)

    provider = registry.provider("recorded")

    assert provider is not None
    assert provider.kind == "recorded"


def test_a_recorded_provider_with_another_id_is_reported(
    plant: Plant, load_errors: LoadErrors
) -> None:
    directory = plant(
        (
            "providers.yaml",
            "providers:\n",
            "providers:\n  - {id: mock, kind: recorded, description: x}\n",
        )
    )

    errors = load_errors(directory)

    assert errors == (
        "providers.yaml: providers[0].id: a provider of kind 'recorded' must have "
        "id 'recorded'",
    )


# ── the deployment: fields it must not carry ───────────────────────────────
@pytest.mark.parametrize(
    ("line", "field"),
    [
        ("    deployment_name: gpt-4o\n", "deployment_name"),
        ("    sku: Standard\n", "sku"),
        ("    region: swedencentral\n", "region"),
        ("    terraform_key: sdc/gpt-4o\n", "terraform_key"),
        (
            "    rate_limits: {requests_per_10_seconds: 1, tokens_per_minute: 1}\n",
            "rate_limits",
        ),
    ],
)
def test_a_recorded_deployment_with_azure_fields_is_reported(
    plant: Plant, load_errors: LoadErrors, line: str, field: str
) -> None:
    errors = load_errors(plant(("models.yaml", ANCHOR, ANCHOR + line)))

    assert (
        f"{WHERE}.{field}: must not be set for provider kind 'recorded' "
        "(deployment 'recorded-chat')"
    ) in errors


def test_the_committed_recorded_deployment_has_no_azure_fields(
    real_registry: Path,
) -> None:
    deployment = load_registry(real_registry).deployment("recorded-chat")

    assert deployment is not None
    assert deployment.deployment_name is None
    assert deployment.sku is None
    assert deployment.region is None
    assert deployment.terraform_key is None
    assert deployment.rate_limits is None


# ── the deployment: model, purpose and label ───────────────────────────────
def test_a_recorded_deployment_with_a_real_model_name_is_reported(
    plant: Plant, load_errors: LoadErrors
) -> None:
    directory = plant(("models.yaml", "model: recorded-chat", "model: gpt-4o"))

    errors = load_errors(directory)

    assert errors == (
        f"{WHERE}.model: a recorded deployment's model must start with "
        "'recorded-' (deployment 'recorded-chat')",
    )


def test_a_recorded_deployment_of_another_model_name_with_the_prefix_is_accepted(
    plant: Plant,
) -> None:
    directory = plant(("models.yaml", "model: recorded-chat", "model: recorded-x"))

    registry = load_registry(directory)

    deployment = registry.deployment("recorded-chat")
    assert deployment is not None
    assert deployment.model == "recorded-x"


def test_a_recorded_deployment_that_is_not_for_chat_is_reported(
    plant: Plant, load_errors: LoadErrors
) -> None:
    directory = plant(
        (
            "models.yaml",
            "    provider: recorded\n    purpose: chat\n",
            "    provider: recorded\n    purpose: embedding\n",
        )
    )

    errors = load_errors(directory)

    assert (
        f"{WHERE}.purpose: a recorded deployment's purpose must be 'chat', "
        "not 'embedding' (deployment 'recorded-chat')"
    ) in errors


def test_a_recorded_deployment_labelled_global_is_reported(
    plant: Plant, load_errors: LoadErrors
) -> None:
    directory = plant(("models.yaml", RECORDED_LABEL, RECORDED_LABEL_GLOBAL))

    errors = load_errors(directory)

    assert any(
        e.startswith(f"{WHERE}.residency: ")
        and "label 'global' does not match the recorded provider runs inside the "
        "platform; expected 'eu-region' (deployment 'recorded-chat')"
        in e
        for e in errors
    ), errors


# ── the deployment: recorded_from ──────────────────────────────────────────
def test_a_recorded_deployment_names_the_deployment_that_answered(
    real_registry: Path,
) -> None:
    deployment = load_registry(real_registry).deployment("recorded-chat")

    assert deployment is not None
    assert deployment.recorded_from == "aoai-sdc-gpt-4o"


def test_a_recorded_deployment_without_recorded_from_is_reported(
    plant: Plant, load_errors: LoadErrors
) -> None:
    errors = load_errors(plant(("models.yaml", RECORDED_FROM, "")))

    assert (
        f"{WHERE}.recorded_from: required for provider kind 'recorded' "
        "(deployment 'recorded-chat')"
    ) in errors


@pytest.mark.parametrize(
    ("target", "fragment"),
    [
        pytest.param(
            "ghost",
            "unknown deployment 'ghost'",
            id="unknown",
        ),
        pytest.param(
            "replay-chat",
            "deployment 'replay-chat' is a replay deployment; a recording is of "
            "a real model",
            id="replay",
        ),
        pytest.param(
            "recorded-chat",
            "deployment 'recorded-chat' is a recorded deployment; a recording is "
            "of a real model",
            id="itself",
        ),
        pytest.param(
            "aoai-sdc-text-embedding-3-large",
            "deployment 'aoai-sdc-text-embedding-3-large' has purpose "
            "'embedding', the recorded deployment's is 'chat'",
            id="another-purpose",
        ),
    ],
)
def test_a_recorded_from_that_is_not_a_real_deployment_of_the_purpose_is_reported(
    plant: Plant, load_errors: LoadErrors, target: str, fragment: str
) -> None:
    directory = plant(("models.yaml", RECORDED_FROM, f"    recorded_from: {target}\n"))

    errors = load_errors(directory)

    assert any(
        e.startswith(f"{WHERE}.recorded_from: ") and fragment in e for e in errors
    ), errors


def test_a_recorded_from_that_is_another_real_chat_deployment_is_accepted(
    plant: Plant,
) -> None:
    # Same model, same price: a recording may name either live deployment.
    directory = plant(
        ("models.yaml", RECORDED_FROM, "    recorded_from: aoai-sdc-gpt-4o-b\n")
    )

    registry = load_registry(directory)

    deployment = registry.deployment("recorded-chat")
    assert deployment is not None
    assert deployment.recorded_from == "aoai-sdc-gpt-4o-b"


@pytest.mark.parametrize(
    ("old", "new", "field"),
    [
        pytest.param(
            "input_per_million_tokens: 3.025\n      output_per_million_tokens: 12.10\n"
            '      source: "The list price',
            "input_per_million_tokens: 3.026\n      output_per_million_tokens: 12.10\n"
            '      source: "The list price',
            "input_per_million_tokens",
            id="input-differs-by-one-digit",
        ),
        pytest.param(
            "input_per_million_tokens: 3.025\n      output_per_million_tokens: 12.10\n"
            '      source: "The list price',
            "input_per_million_tokens: 3.025\n      output_per_million_tokens: 12.11\n"
            '      source: "The list price',
            "output_per_million_tokens",
            id="output-differs-by-one-digit",
        ),
        pytest.param(
            "input_per_million_tokens: 3.025\n      output_per_million_tokens: 12.10\n"
            '      source: "The list price',
            'input_per_million_tokens: 3.025\n      source: "The list price',
            "output_per_million_tokens",
            id="output-missing",
        ),
    ],
)
def test_a_recorded_price_that_differs_from_the_live_one_is_reported(
    plant: Plant, load_errors: LoadErrors, old: str, new: str, field: str
) -> None:
    errors = load_errors(plant(("models.yaml", old, new)))

    assert any(
        e.startswith(f"{WHERE}.price.{field}: ") and "'aoai-sdc-gpt-4o'" in e
        for e in errors
    ), errors


def test_a_recorded_price_has_its_own_source_and_the_live_numbers(
    real_registry: Path,
) -> None:
    registry = load_registry(real_registry)
    recorded = registry.deployment("recorded-chat")
    live = registry.deployment("aoai-sdc-gpt-4o")

    assert recorded is not None
    assert live is not None
    assert recorded.price.source != live.price.source
    assert recorded.price.currency == live.price.currency
    assert recorded.price.input_per_million_tokens == (
        live.price.input_per_million_tokens
    )
    assert recorded.price.output_per_million_tokens == (
        live.price.output_per_million_tokens
    )


@pytest.mark.parametrize(
    ("anchor", "kind"),
    [
        pytest.param("    model: gpt-4o\n", "azure-openai", id="azure"),
        pytest.param("    model: replay-chat\n", "replay", id="replay"),
    ],
)
def test_recorded_from_on_another_kind_of_deployment_is_reported(
    plant: Plant, load_errors: LoadErrors, anchor: str, kind: str
) -> None:
    directory = plant(
        ("models.yaml", anchor, anchor + "    recorded_from: aoai-sdc-gpt-4o-b\n")
    )

    errors = load_errors(directory)

    index = 0 if kind == "azure-openai" else 3
    deployment = "aoai-sdc-gpt-4o" if kind == "azure-openai" else "replay-chat"
    assert (
        f"models.yaml: deployments[{index}].recorded_from: must not be set for "
        f"provider kind '{kind}' (deployment '{deployment}')"
    ) in errors


# ── policies.yaml: recorded ────────────────────────────────────────────────
def test_the_committed_recorded_deployment_is_found_per_purpose(
    real_registry: Path,
) -> None:
    registry = load_registry(real_registry)

    chat = registry.recorded_deployment("chat")

    assert chat is not None
    assert chat.id == "recorded-chat"
    assert registry.recorded_deployment("embedding") is None
    assert registry.recorded_deployment("ghost") is None


def test_policies_without_a_recorded_section_are_accepted(plant: Plant) -> None:
    # Unlike replay, recorded need not cover every purpose.
    directory = plant(("policies.yaml", RECORDED_BLOCK, ""))

    registry = load_registry(directory)

    assert registry.recorded == ()
    assert registry.recorded_deployment("chat") is None


RECORDED_CASES = [
    pytest.param(
        [("policies.yaml", RECORDED_ENTRY, RECORDED_ENTRY + RECORDED_ENTRY)],
        "policies.yaml: recorded: duplicate purpose 'chat'",
        id="purpose-listed-twice",
    ),
    pytest.param(
        [("policies.yaml", "deployment: recorded-chat", "deployment: ghost")],
        "policies.yaml: recorded[0].deployment: unknown deployment 'ghost'",
        id="unknown-deployment",
    ),
    pytest.param(
        [("policies.yaml", "deployment: recorded-chat", "deployment: aoai-sdc-gpt-4o")],
        "policies.yaml: recorded[0].deployment: deployment 'aoai-sdc-gpt-4o' is "
        "not a recorded deployment (provider kind 'azure-openai')",
        id="real-deployment",
    ),
    pytest.param(
        [("policies.yaml", "deployment: recorded-chat", "deployment: replay-chat")],
        "policies.yaml: recorded[0].deployment: deployment 'replay-chat' is not a "
        "recorded deployment (provider kind 'replay')",
        id="replay-deployment",
    ),
    pytest.param(
        [
            (
                "policies.yaml",
                "  - purpose: chat\n    deployment: recorded-chat\n",
                "  - purpose: embedding\n    deployment: recorded-chat\n",
            )
        ],
        "policies.yaml: recorded[0].deployment: deployment 'recorded-chat' has "
        "purpose 'chat', the recorded entry is 'embedding'",
        id="wrong-purpose",
    ),
    pytest.param(
        [
            (
                "policies.yaml",
                "candidates: [aoai-sdc-gpt-4o, aoai-sdc-gpt-4o-b]",
                "candidates: [recorded-chat]",
            )
        ],
        "policies.yaml: routes[0].candidates[0]: deployment 'recorded-chat' is a "
        "recorded deployment; recorded is a gateway mode, never a route candidate",
        id="recorded-as-route-candidate",
    ),
    pytest.param(
        [("policies.yaml", "deployment: replay-chat", "deployment: recorded-chat")],
        "policies.yaml: replay[0].deployment: deployment 'recorded-chat' is not a "
        "replay deployment (provider kind 'recorded')",
        id="recorded-as-replay-entry",
    ),
]


@pytest.mark.parametrize(("edits", "expected"), RECORDED_CASES)
def test_recorded_rule_violation_is_reported(
    plant: Plant, load_errors: LoadErrors, edits: list[Edit], expected: str
) -> None:
    errors = load_errors(plant(*edits))

    assert expected in errors


def test_a_recorded_deployment_that_does_not_allow_a_tenants_class_is_reported(
    plant: Plant, load_errors: LoadErrors
) -> None:
    directory = plant(
        (
            "models.yaml",
            RECORDED_CLASSES,
            "    data_classes: [synthetic]\n" + RECORDED_FROM,
        )
    )

    errors = load_errors(directory)

    for tenant in ("claims-triage", "evaluation"):
        assert (
            "policies.yaml: recorded[0]: deployment 'recorded-chat' does not allow "
            f"data class 'personal' of tenant {tenant!r}, so recorded mode could "
            "not serve it"
        ) in errors
    assert not any("tenant 'development'" in e and "recorded[0]" in e for e in errors)


def test_a_recorded_residency_the_tenants_class_may_not_reach_is_reported(
    plant: Plant, load_errors: LoadErrors
) -> None:
    directory = plant(
        (
            "policies.yaml",
            "  - id: personal\n    residency: [eu-region, eu-zone]\n",
            "  - id: personal\n    residency: [eu-zone]\n",
        )
    )

    errors = load_errors(directory)

    assert (
        "policies.yaml: recorded[0]: deployment 'recorded-chat' has residency "
        "'eu-region', which data class 'personal' of tenant 'claims-triage' may "
        "not reach"
    ) in errors


def test_a_recorded_deployment_without_an_entry_is_not_a_problem(
    plant: Plant,
) -> None:
    directory = plant(("policies.yaml", RECORDED_BLOCK, ""))

    registry = load_registry(directory)

    assert registry.has_deployment("recorded-chat")


# ── Terraform knows nothing of it ──────────────────────────────────────────
def test_terraform_is_not_asked_about_the_recorded_deployment(
    real_registry: Path,
) -> None:
    registry = load_registry(real_registry)

    compared = {d.id for d in azure_deployments(registry)}

    assert "recorded-chat" not in compared
    assert "aoai-sdc-gpt-4o" in compared


# ── the evaluation judge ───────────────────────────────────────────────────
def test_the_judge_is_a_job_that_lists_no_tool(real_registry: Path) -> None:
    judge = load_registry(real_registry).agent("evaluation-judge")

    assert judge is not None
    assert judge.kind == "job"
    assert judge.tools == ()


def test_only_the_evaluation_tenant_may_run_the_judge(real_registry: Path) -> None:
    registry = load_registry(real_registry)

    allowed = {
        tenant.id
        for tenant in registry.tenants
        if registry.tenant_may_run(tenant.id, "evaluation-judge")
    }

    assert allowed == {"evaluation"}
    assert registry.tenant_may_run("evaluation", "claims-triage")


def test_the_judge_with_a_tool_is_reported(
    plant: Plant, load_errors: LoadErrors
) -> None:
    directory = plant(
        (
            "agents.yaml",
            "    description: Grades whether a rationale is grounded in the text it "
            "was drawn from; it calls no tool.\n    kind: job\n    tools: []\n",
            "    description: x\n    kind: job\n    tools: [policy_lookup]\n",
        )
    )

    errors = load_errors(directory)

    assert (
        "agents.yaml: agents[2].tools[0]: job agent 'evaluation-judge' lists tool "
        "'policy_lookup'; a job has no run row, so no tool server could bind its "
        "call"
    ) in errors
