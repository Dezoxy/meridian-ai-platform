## Code layering and import rules

The views show what runs and what talks to what. This page shows a rule that
no view can: which Python package may import which. It is the level below the
containers, and it is a rule about source code, not about processes.

**Implemented.** The rules are nine import-linter contracts in
[pyproject.toml](../../../pyproject.toml), under `[tool.importlinter]`: six
from before, and three added with the sign-in modules (S021). `make lint` runs
them, and the `python` workflow runs `make lint` on every pull request. On
2026-10-08, with the sign-in branch and main (v0.94) merged, `uv run
lint-imports` printed "Analyzed 247 files, 1523 dependencies" and "Contracts: 9
kept, 0 broken"; on 2026-10-07, on `main` at ae94424, it had printed "Analyzed
237 files, 1446 dependencies" and "Contracts: 6 kept, 0 broken". The contracts
read the packages under `src/meridian/` and nothing else: a test file may
import anything.

### What imports what

The three layers, the gateway inside the lowest one, and the two families of
libraries a contract guards. An arrow reads "imports". The diagram draws the
imports the code has today, counted on the same day; the table below says
which of the arrows that are missing a contract refuses.

```mermaid
flowchart TD
    workloads["meridian.workloads<br/>claims_triage, claim_brief"]
    runtime["meridian.runtime<br/>hosts, clients, runs"]
    frameworks(["langgraph, langchain_core,<br/>agent_framework"])
    sdks(["openai, azure"])
    subgraph platform["meridian.platform"]
        shared["cli, common, evaluation, guardrails,<br/>knowledge_mcp, migrations, policy_mcp,<br/>registry, toolserver"]
        subgraph gateway["meridian.platform.gateway"]
            rest["every module but the adapter"]
            adapter["providers.azure_openai"]
        end
    end
    workloads --> runtime
    workloads --> shared
    runtime --> shared
    workloads --> frameworks
    runtime --> frameworks
    gateway --> shared
    rest --> adapter
    adapter --> sdks
```

Four things the diagram leaves out. The packages inside `meridian.platform`
import each other, and no contract orders them. The gateway's rate store
imports `redis`, which the third contract keeps out of every other package.
No package outside the gateway imports the gateway today, although the
contracts refuse such an import only when its chain ends in a provider's
library, or when the importer is `meridian.platform.common` (the ninth
contract). And two modules of `common`, `signin` and `signinkeys`, import the
JWT library (`jwt`), which the seventh contract keeps out of every other
package; the diagram does not draw it.

### The contracts

"Chain" says whether an import through other modules counts, or only a direct
one.

| Contract, as `pyproject.toml` names it | Refuses | Chain | Why |
|---|---|---|---|
| workloads build on the platform, never the reverse | `meridian.platform` importing `meridian.runtime` or `meridian.workloads`, and `meridian.runtime` importing `meridian.workloads` | Counts | A workload is replaceable and the platform is not built around one |
| platform never imports the agent framework (ADR 2) | Any module under `meridian.platform` importing `langgraph`, a `langchain` distribution, `agent_framework` or one of its adapters | Counts | [ADR 2](../decisions/0002-langgraph-behind-a-framework-agnostic-contract.md) and [ADR 9](../decisions/0009-run-a-second-agent-framework-behind-the-same-host-protocol.md): the platform's contract does not depend on a framework, so a second one could be hosted |
| only the gateway imports a provider SDK (hard rule 4, T-19) | The workloads, the runtime and the nine platform packages that are not the gateway importing `openai`, `azure`, `mistralai`, `anthropic`, `boto3`, `botocore`, `litellm`, a framework adapter that calls a model, or `redis` | Counts | [ADR 3](../decisions/0003-build-a-thin-model-gateway.md): every model call passes the gateway's routing, redaction, budgets and audit |
| inside the gateway only the Azure OpenAI adapter imports a provider SDK | A gateway module other than `providers.azure_openai` importing one of those libraries | Direct only | The provider's types stay in one file, so a second provider is a second adapter |
| the gateway imports neither the evaluation package nor the CLI | `meridian.platform.gateway` importing `meridian.platform.evaluation` or `meridian.platform.cli` | Counts | A deployed service does not depend on the package that loads workloads and holds a client of the gateway (T-78) |
| the claim brief imports no HTTP client (S037) | `meridian.workloads.claim_brief` importing `httpx`, `requests` or `aiohttp` | Direct only | A step of that workload cannot post to a model by itself |
| only the sign-in modules import the JWT library (S021) | The twelve other packages (the workloads, the runtime and ten under `meridian.platform`, the gateway and `common` among them) importing `jwt`; the two sign-in modules that parse and verify a token, `common.signin` and `common.signinkeys`, are the only ignored edges | Counts, except through the two ignored edges | A person's token is parsed and its signature checked in one place; PyJWT was already installed through `mcp` and `msal`, and an import elsewhere would be a second, unreviewed check |
| services and graph code do not import the sign-in modules (S021) | The gateway, the runtime, the three tool servers, the claim brief and the Claims Triage graph and MCP server importing `common.signin`, `signinkeys`, `signinsession` or `signinguard` | Counts | People sign in at the Claims API; services identify each other by certificate (ADR 4), and a person's token, cookie or principal never crosses a service boundary. The Claims API's own modules may import them |
| the shared platform code does not import the gateway | `meridian.platform.common` importing `meridian.platform.gateway` | Counts | The gateway imports `common` (its settings, HTTP and identity code), so the reverse is a cycle. A setting both need, the environment's variable name, is repeated in `common` and a test ties the two copies |

`tests/meridian/test_import_contracts.py` breaks each of the first six
contracts in a copy of the tree and expects the linter to say so, and fails
when a new package under `src/meridian/platform/` is missing from the third
contract's list. `tests/meridian/test_import_contracts_signin.py` does the same
for the three added by S021: it plants `import jwt` in other packages
(and checks that the two sign-in modules keep their import), plants an import
of a sign-in module in the gateway, a tool server, the claim brief and the
Claims Triage graph (and checks that the Claims API and `common` may import
them), and plants an import of the gateway in `common`; it checks that the
`jwt` contract ignores exactly the two modules, that the services contract
forbids exactly the four sign-in modules, and that the `jwt` contract names
every package under `src/meridian/platform/`.

### Inside one container: the Agent Runtime

The import rules say what a package may reach. What sits inside one running
service is a view of the model. One exists, of the Agent Runtime, where the
layering is a decision
([ADR 2](../decisions/0002-langgraph-behind-a-framework-agnostic-contract.md),
[ADR 9](../decisions/0009-run-a-second-agent-framework-behind-the-same-host-protocol.md)):
two hosts behind one protocol, and one client each for the model and the
tools. **Implemented**, and read from the code, not run. The other containers
have no component view.

![Component view: the responsibilities inside the Agent Runtime and the containers each one reaches](embed:RuntimeComponents)

A component is a responsibility of the running service, not a file. The view
register in the architecture README says which parts of the package are left
out and why.

**Update trigger:** a contract in `pyproject.toml` is added, removed or
changed, or a module of the runtime gains or loses a responsibility. The
plan's step S082 adds a contract per service and moves the workloads' graphs
into a package of their own, which changes the diagram, the table and the
view.
