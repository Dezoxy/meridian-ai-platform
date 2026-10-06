"""What the Model Gateway decides before it serves a request: whether this mode
and environment may start, which deployments each purpose may use, and the
provider adapters it builds (S009, S010, S050).

Moved out of ``app.py`` as it was, to keep that file under the size ceiling; the
app builds everything here once, in ``create_app``, and re-exports the names the
tests import from it. The Azure adapter, and with it the SDKs, is imported inside
``live_providers`` and nowhere at module level, so a replay process never loads
them (``test_importing_the_services_loads_no_provider_sdk_or_credential_library``).
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from meridian.platform.common.env import SettingsError
from meridian.platform.gateway.providers.base import ModelProvider, ProviderError
from meridian.platform.gateway.providers.recorded import (
    RecordedProvider,
    RecordingError,
    load_recording,
)
from meridian.platform.gateway.replay import ReplayProvider
from meridian.platform.gateway.settings import RECORDINGS_ENV, GatewaySettings
from meridian.platform.registry import Registry
from meridian.platform.registry.models import Deployment

CHAT_PURPOSE = "chat"
EMBEDDING_PURPOSE = "embedding"
REPLAY_ENVIRONMENTS = frozenset({"test", "ci", "kind"})
# A recording is a laptop's or a CI run's, never a cluster's (T-78).
RECORDED_ENVIRONMENTS = frozenset({"test", "ci", "local"})
# The Azure CLI credential is a developer's login, so a gateway that builds its
# own live providers starts on a laptop only.
LIVE_ENVIRONMENT = "local"
AZURE_KIND = "azure-openai"
REPLAY_KIND = "replay"
RECORDED_KIND = "recorded"


def check_start_allowed(
    settings: GatewaySettings, providers: Mapping[str, ModelProvider] | None
) -> None:
    if settings.mode == "replay":
        if settings.environment not in REPLAY_ENVIRONMENTS:
            raise SettingsError(
                "replay mode is refused outside the test, ci and kind environments "
                "(T-39)"
            )
        return
    if settings.mode == "recorded":
        if settings.environment not in RECORDED_ENVIRONMENTS:
            raise SettingsError(
                "recorded mode is refused outside the test, ci and local "
                "environments (T-78)"
            )
        if providers is None and settings.recordings is None:
            raise SettingsError(f"recorded mode needs {RECORDINGS_ENV}")
        return
    if providers is not None:
        return  # the caller brought its own providers (tests)
    if settings.azure_credential != "azure-cli":
        raise SettingsError(
            "live mode needs MERIDIAN_AZURE_CREDENTIAL=azure-cli: the only "
            "credential source is a developer's az login"
        )
    if settings.environment != LIVE_ENVIRONMENT:
        raise SettingsError(
            "live mode with the Azure CLI credential is refused outside the local "
            "environment"
        )


@dataclass(frozen=True, slots=True)
class Route:
    """What one purpose may use: the deployments a request may use before policy
    narrows them (in replay mode its one replay deployment, in recorded mode its
    one recorded or else replay deployment, in live mode the route's candidates
    in order), and, in replay and recorded mode only, that deployment: where the
    call goes is known before policy decides, so a refusal says it too (T-39).
    The field is named ``replay`` for the mode it was made for."""

    considered: tuple[Deployment, ...]
    replay: Deployment | None
    purpose: str


def route_for(settings: GatewaySettings, registry: Registry, purpose: str) -> Route:
    if settings.mode == "replay":
        deployment = registry.replay_deployment(purpose)
        if deployment is None:
            raise SettingsError(f"the registry has no replay deployment for {purpose}")
        return Route((deployment,), deployment, purpose)
    if settings.mode == "recorded":
        # A purpose with no recorded deployment (embedding) is answered by replay.
        recorded = registry.recorded_deployment(purpose)
        deployment = (
            recorded if recorded is not None else registry.replay_deployment(purpose)
        )
        if deployment is None:
            raise SettingsError(
                f"the registry has no recorded or replay deployment for {purpose}"
            )
        return Route((deployment,), deployment, purpose)
    route = registry.route(purpose)
    if route is None:
        return Route((), None, purpose)
    found = tuple(registry.deployment(name) for name in route.candidates)
    if any(d is None for d in found):
        raise SettingsError(
            f"the {purpose} route names a deployment the registry lacks"
        )
    return Route(tuple(d for d in found if d is not None), None, purpose)


def provider_kinds(
    registry: Registry, considered: Sequence[Deployment]
) -> dict[str, str]:
    """Deployment ID to the kind of its provider (the key of ``providers``)."""
    kinds: dict[str, str] = {}
    for deployment in considered:
        provider = registry.provider(deployment.provider)
        if provider is None:
            raise SettingsError(f"deployment {deployment.id} has no known provider")
        kinds[deployment.id] = provider.kind
    return kinds


def check_azure_candidates(
    settings: GatewaySettings,
    considered: Sequence[Deployment],
    kinds: Mapping[str, str],
) -> None:
    """What ``AzureOpenAIProvider.chat`` and ``embed`` would otherwise raise a
    ``ValueError`` for at the first request."""
    for deployment in considered:
        if kinds[deployment.id] != AZURE_KIND:
            continue
        if deployment.terraform_key is None or deployment.deployment_name is None:
            raise SettingsError(
                f"deployment {deployment.id} has no terraform_key or deployment_name"
            )
        if deployment.purpose == EMBEDDING_PURPOSE and deployment.dimensions is None:
            raise SettingsError(f"deployment {deployment.id} has no dimensions")
        location = deployment.terraform_key.partition("/")[0]
        if location not in settings.azure_openai_endpoints:
            raise SettingsError(
                f"deployment {deployment.id} has no endpoint in "
                "MERIDIAN_AZURE_OPENAI_ENDPOINTS for its location"
            )


def live_providers(
    settings: GatewaySettings,
    considered: Sequence[Deployment],
    kinds: Mapping[str, str],
) -> tuple[dict[str, ModelProvider], Callable[[], None]]:
    """The Azure provider and the call that closes it, with one token fetched
    now so a missing ``az login`` stops the start and not the first request.

    The adapter, and with it the SDKs, is imported here and nowhere at module
    level, so a replay process never loads them.
    """
    from meridian.platform.gateway.providers.azure_openai import (
        AzureOpenAIProvider,
        azure_cli_token_provider,
        check_token,
        refuse_sdk_environment,
    )

    try:
        refuse_sdk_environment()
    except ValueError as error:  # names the variable, never its value
        raise SettingsError(str(error)) from None
    if settings.azure_tenant_id is None:
        raise SettingsError(
            "live mode needs MERIDIAN_AZURE_TENANT_ID: the Azure CLI's default "
            "account must not decide which tenant a token is for"
        )
    check_azure_candidates(settings, considered, kinds)
    token_provider = azure_cli_token_provider(settings.azure_tenant_id)
    try:
        check_token(token_provider)
    except ProviderError:
        # Nothing of the cause: it can name the tenant or the account.
        raise SettingsError(
            "no Azure token: run `az login` for the tenant in "
            "MERIDIAN_AZURE_TENANT_ID and start again"
        ) from None
    provider = AzureOpenAIProvider(settings.azure_openai_endpoints, token_provider)
    return {AZURE_KIND: provider}, provider.close


def recorded_providers(settings: GatewaySettings) -> dict[str, ModelProvider]:
    """The recording's provider and the replay one (embeddings). A recording
    that cannot be read stops the start; the error names the variable and none
    of the file."""
    if settings.recordings is None:
        raise SettingsError(f"recorded mode needs {RECORDINGS_ENV}")
    try:
        recording = load_recording(settings.recordings)
    except RecordingError as error:
        raise SettingsError(
            f"{RECORDINGS_ENV} is not a usable recording: {error}"
        ) from None
    return {RECORDED_KIND: RecordedProvider(recording), REPLAY_KIND: ReplayProvider()}
