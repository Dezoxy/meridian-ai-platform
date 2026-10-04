# Sample operator entry point for the architecture workspace.
#
# Copy it into a repository that uses this template and adjust the pins below
# once. Every target runs locally with Docker; none of this is CI. Override any
# variable per call, e.g. `make view PORT=8081`.

# ── pins ─────────────────────────────────────────────────────────────────────
# The Structurizr image your viewer runs. Keep it identical to the server's pin
# so the parser here is the parser there. The PNG/SVG export uses its
# -playwright tag.
STRUCTURIZR_IMAGE ?= structurizr/structurizr:2026.09.19
# Pandoc with LaTeX and the Eisvogel template, for `make pdf` (~2 GB).
PANDOC_IMAGE      ?= pandoc/extra:3.11.0.0-debian
# Mermaid CLI, for `make mermaid-render` and `make pdf`: draws Mermaid blocks
# as PNG. The mature 11.x line; runs as your user (~630 MB).
MERMAID_IMAGE     ?= minlag/mermaid-cli:11.17.0

# ── paths ────────────────────────────────────────────────────────────────────
ARCH_DIR  ?= docs/architecture
GENERATED := $(ARCH_DIR)/generated
PORT      ?= 8080
# The registry's copy of Terraform's deployment outputs (T-12).
REGISTRY_SNAPSHOT := config/registry/snapshots/terraform-openai-deployments.json
# The throwaway PostgreSQL of `make pytest-db`; the image is the one the python
# workflow runs as its service container (a test compares the two strings), so
# it is pinned with := and a command line does not override it. It is
# PostgreSQL 17.11 with pgvector 0.8.6 on Debian trixie (the knowledge store,
# S012): the same PostgreSQL, pgvector and OS release as the CloudNativePG
# image kind runs, so the tests see what the cluster will. A run that
# overlaps another needs its own name and port.
PYTEST_DB_IMAGE     := pgvector/pgvector:0.8.6-pg17-trixie@sha256:724a4041afdb1750446e3f6b5cfa8f3b0ac5a2cf538ddfa6bfee4f94c2fa85c6
PYTEST_DB_CONTAINER ?= meridian-pytest-db
PYTEST_DB_PORT      ?= 55432
# Extra pytest arguments for `make pytest-db`, e.g. one test file.
PYTEST_ARGS         ?=
# Worker processes for `make pytest` and `make pytest-db` (pytest-xdist -n): a
# number, or auto for one per CPU core; 0 runs the tests in one process. Four,
# as CI's runner has: with ten on a laptop, connections to the database
# container were dropped before PostgreSQL saw them (Docker Desktop, S054).
PYTEST_WORKERS      ?= 4
# The adjuster's decision `make demo` posts for a claim referred to an adjuster:
# approve, reject or request_documents (the script refuses anything else).
DECISION            ?= approve
# The evaluation of the claims workload (S017, S050): the baseline committed in
# Git, the report a run writes (gitignored) and the one test that writes it. It
# replays a real model's recorded answers through the Model Gateway (CI and
# `make eval`); `make eval-record` makes the recording and needs an Azure login.
EVAL_BASELINE       ?= data/evaluation/claims-triage-baseline.json
EVAL_REPORT         ?= .eval/claims-triage-report.json
EVAL_TEST           ?= tests/meridian/test_evaluation_stack.py::test_the_recorded_model_answers_the_golden_set_through_the_gateway
# What a report is made from: a tracked file here that is newer than the report
# makes it stale (`make eval-compare` refuses it).
EVAL_INPUTS         := src config/registry data/synthetic data/evaluation/recordings tests/meridian pyproject.toml uv.lock

STRUCTURIZR := docker run --rm -v "$(CURDIR)/$(ARCH_DIR):/w:ro"

.PHONY: help validate inspect check docs test view export mermaid-views mermaid-render mermaid pdf clean lint pytest pytest-db eval eval-compare eval-baseline eval-record synthetic up deploy demo smoke grafana grafana-password down azure-state azure-plan azure-apply azure-smoke gateway-live registry-snapshot registry
.DEFAULT_GOAL := help

## help            list the targets
help:
	@awk 'BEGIN { FS = "## " } /^## / { print "  " $$2 }' $(MAKEFILE_LIST)

## validate        parse the workspace with the pinned Structurizr image
validate:
	$(STRUCTURIZR) $(STRUCTURIZR_IMAGE) validate -workspace /w/workspace.dsl

## inspect         list model findings; fails on any ERROR line
inspect:
	@out="$$($(STRUCTURIZR) $(STRUCTURIZR_IMAGE) inspect -workspace /w/workspace.dsl 2>&1)"; \
	printf '%s\n' "$$out"; \
	if printf '%s\n' "$$out" | grep -q 'ERROR'; then echo "inspect: errors found" >&2; exit 1; fi

## check           validate + inspect: run before committing a model change
check: validate inspect

## docs            fail when documentation contradicts the tree (links, indexes, ADRs, view register, IDs, headings)
docs:
	python3 scripts/check_docs_consistency.py

## test            unit tests for the checker and the Mermaid and PDF scripts
test:
	python3 -m unittest discover -s tests

## view            browse the workspace at http://localhost:8080/workspace/1 (PORT=... to change; Ctrl-C stops it)
view:
	docker run --rm -p $(PORT):8080 -v "$(CURDIR)/$(ARCH_DIR):/usr/local/structurizr" $(STRUCTURIZR_IMAGE) local

## export          every view as SVG, PNG and Mermaid, plus the workspace JSON, into the generated folder
export:
	mkdir -p $(GENERATED)
	chmod 777 $(GENERATED)
	$(STRUCTURIZR) -v "$(CURDIR)/$(GENERATED):/out" $(STRUCTURIZR_IMAGE) export -workspace /w/workspace.dsl -format json -output /out
	$(STRUCTURIZR) -v "$(CURDIR)/$(GENERATED):/out" $(STRUCTURIZR_IMAGE) export -workspace /w/workspace.dsl -format mermaid -output /out
	$(STRUCTURIZR) -v "$(CURDIR)/$(GENERATED):/out" $(STRUCTURIZR_IMAGE)-playwright export -workspace /w/workspace.dsl -format svg -output /out
	$(STRUCTURIZR) -v "$(CURDIR)/$(GENERATED):/out" $(STRUCTURIZR_IMAGE)-playwright export -workspace /w/workspace.dsl -format png -output /out
	@echo "exported $$(ls $(GENERATED) | wc -l | tr -d ' ') files to $(GENERATED)"

## mermaid-views   refresh the diagrams marked <!-- mermaid-view: Key --> from the model
mermaid-views:
	mkdir -p $(GENERATED)/mermaid-export
	chmod 777 $(GENERATED)/mermaid-export
	rm -f $(GENERATED)/mermaid-export/*.mmd
	$(STRUCTURIZR) -v "$(CURDIR)/$(GENERATED)/mermaid-export:/out" $(STRUCTURIZR_IMAGE) export -workspace /w/workspace.dsl -format mermaid -output /out
	python3 scripts/mermaid_blocks.py sync

## mermaid-render  render every Mermaid block in the Markdown; fails on a syntax error
mermaid-render:
	python3 scripts/mermaid_blocks.py extract
	MERMAID_IMAGE=$(MERMAID_IMAGE) scripts/render-mermaid.sh $(GENERATED)/mermaid-render

## mermaid         mermaid-views, then mermaid-render
mermaid: mermaid-views mermaid-render

## pdf             the Documentation tab and every view as one PDF, named <project>-architecture-<date>-<edition>.pdf
pdf:
	STRUCTURIZR_IMAGE=$(STRUCTURIZR_IMAGE) PANDOC_IMAGE=$(PANDOC_IMAGE) MERMAID_IMAGE=$(MERMAID_IMAGE) ARCH_DIR=$(ARCH_DIR) scripts/architecture-pdf.sh

## clean           delete the generated folder (exports and PDFs; all gitignored)
clean:
	rm -rf $(GENERATED)

# ── Python workspace ─────────────────────────────────────────────────────────
# Run through uv, which creates and syncs .venv on first use. The targets above
# keep working without uv; the docs CI job relies on that.

## lint            ruff check, ruff format --check and the import-linter contracts
lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run lint-imports

## pytest          tests under tests/meridian and tests/synthetic, including the import-contract detection test, run in parallel (PYTEST_WORKERS, default 4; 0 runs them in one process)
pytest:
	uv run pytest -n $(PYTEST_WORKERS)

## pytest-db       pytest in parallel (PYTEST_WORKERS, default 4; 0 runs them in one process) with a throwaway PostgreSQL 17 on 127.0.0.1:55432 (needs Docker; concurrent runs each need their own PYTEST_DB_CONTAINER and PYTEST_DB_PORT); the database tests run instead of skipping
pytest-db:
	@set -e; \
	docker rm -f $(PYTEST_DB_CONTAINER) >/dev/null 2>&1 || true; \
	trap 'docker rm -f $(PYTEST_DB_CONTAINER) >/dev/null 2>&1 || true' EXIT; \
	trap 'exit 130' INT; \
	trap 'exit 143' TERM; \
	docker run -d --name $(PYTEST_DB_CONTAINER) \
		--tmpfs /var/lib/postgresql/data -p 127.0.0.1:$(PYTEST_DB_PORT):5432 \
		-e POSTGRES_HOST_AUTH_METHOD=trust $(PYTEST_DB_IMAGE); \
	for i in $$(seq 1 60); do \
		docker exec $(PYTEST_DB_CONTAINER) pg_isready -U postgres -h 127.0.0.1 >/dev/null 2>&1 && break; \
		[ $$i -eq 60 ] && { echo "pytest-db: PostgreSQL did not become ready" >&2; exit 1; }; \
		sleep 1; \
	done; \
	MERIDIAN_TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:$(PYTEST_DB_PORT)/postgres \
	MERIDIAN_REQUIRE_DB=1 uv run pytest -n $(PYTEST_WORKERS) $(PYTEST_ARGS)

## eval            replay the golden set through the stack with the recorded model's answers and the judge (needs Docker), write the report and compare it with the baseline
eval:
	mkdir -p $(dir $(EVAL_REPORT))
	rm -f $(EVAL_REPORT)
	MERIDIAN_EVAL_REPORT=$(abspath $(EVAL_REPORT)) $(MAKE) pytest-db PYTEST_WORKERS=0 PYTEST_ARGS="$(EVAL_TEST) -q"
	$(MAKE) eval-compare

## eval-compare    compare a report with the baseline (meridian eval compare); refuses a report older than a tracked file it is made from
eval-compare:
	@test -f "$(EVAL_REPORT)" || { echo "no report at $(EVAL_REPORT): run make eval" >&2; exit 1; }
	@newer="$$(git ls-files -- $(EVAL_INPUTS) | while IFS= read -r file; do \
		if [ "$$file" -nt "$(EVAL_REPORT)" ]; then printf '%s\n' "$$file"; break; fi; \
	done)"; \
	if [ -n "$$newer" ]; then echo "the report is older than $$newer: run make eval" >&2; exit 1; fi
	uv run meridian eval compare $(EVAL_BASELINE) $(EVAL_REPORT)

## eval-baseline   regenerate the baseline after a reviewed prompt, tool, recording or golden-set change (needs Docker)
eval-baseline:
	mkdir -p $(dir $(EVAL_BASELINE))
	MERIDIAN_EVAL_REPORT=$(abspath $(EVAL_BASELINE)) $(MAKE) pytest-db PYTEST_WORKERS=0 PYTEST_ARGS="$(EVAL_TEST) -q"

## eval-record     SPENDS MONEY: about 60 chat calls, under EUR 0.50, on the live Azure models, with the judge, to record the golden set's answers and a variant prompt's; rewrites files under data/evaluation/ (needs az login, Docker; the owner runs it)
eval-record:
	infra/terraform/foundation.sh eval-record

## registry        validate config/registry, compare it with the Terraform snapshot and check the generated schemas and the tool-server contracts under api/mcp
registry:
	uv run meridian registry validate --terraform-outputs $(REGISTRY_SNAPSHOT)
	uv run meridian registry schemas --check
	uv run meridian registry contracts --check

## synthetic       regenerate the synthetic data and golden set under data/synthetic (seeded; reruns are identical)
synthetic:
	PYTHONPATH=data/synthetic uv run python -m generator

# ── Local platform on kind ───────────────────────────────────────────────────
# infra/kind/README.md says what these create. The cluster's credentials stay in
# infra/kind/kubeconfig (gitignored); ~/.kube/config is never touched.

## up              create the kind cluster, install the local platform and provision the Grafana dashboards (needs Docker, kind, kubectl, helm; first run pulls images)
up:
	infra/kind/up.sh

## deploy          build the image, run the migrations, seed the policy store, put the Claims API, Agent Runtime, Model Gateway and the three tool servers on the kind cluster and ingest the policy wordings (needs make up; the first deploy of an image waits a minute after the ingestion)
deploy:
	infra/kind/deploy.sh

## demo            deploy, post a synthetic claim at http://claims.meridian.localhost:8088, find its trace across the five services that triage it in Tempo and, when it is referred to an adjuster, decide it and find that trace too (make demo DECISION=reject; approve, reject or request_documents)
demo: deploy
	DECISION="$(DECISION)" infra/kind/demo.sh

## smoke           prove the edge, pgvector, a trace, log and metric reaching Grafana's datasources, the cost dashboard and, once deployed, one call per tool server through the runtime's client, the gateway's series, the adjuster's and claimant's pages and the sweep's last Job
smoke:
	infra/kind/smoke.sh

## grafana         port-forward Grafana to http://127.0.0.1:3000 (Ctrl-C stops it)
grafana:
	infra/kind/grafana.sh forward

## grafana-password print the Grafana admin password
grafana-password:
	@infra/kind/grafana.sh password

## down            delete the kind cluster "meridian" and its credentials file (destructive; the owner runs it)
down:
	infra/kind/down.sh

# ── Azure foundation ─────────────────────────────────────────────────────────
# infra/terraform/README.md says what these create. They use the subscription
# pinned in infra/terraform/local.env (gitignored); the first azure-state needs
# AZURE_SUBSCRIPTION=<name or id>. Needs az (logged in), terraform, jq and curl.

## azure-state     CREATES Azure resources: the Terraform state storage account (Entra ID only) and infra/terraform/local.env; the owner runs it
azure-state:
	infra/terraform/state.sh

## azure-plan      terraform init and plan of the foundation into foundation.tfplan; changes nothing in Azure
azure-plan:
	infra/terraform/foundation.sh plan

## azure-apply     CREATES Azure resources: applies the saved plan (Key Vault, Azure OpenAI, budget); the owner confirms the plan first
azure-apply:
	infra/terraform/foundation.sh apply

## azure-smoke     prove the foundation: live models match Terraform, key auth is off, one tiny call per deployment (well under EUR 0.01)
azure-smoke:
	infra/terraform/foundation.sh smoke

## gateway-live    four real chat calls and one embedding call through the Model Gateway in live mode on this laptop, one chat call with the first candidate made to fail and two with a response schema: az login, a synthetic prompt, a throwaway PostgreSQL (needs Docker; well under EUR 0.01)
gateway-live:
	infra/terraform/foundation.sh gateway-live

## registry-snapshot refresh the registry's copy of Terraform's deployment outputs (only the compared fields); read-only in Azure
registry-snapshot:
	infra/terraform/foundation.sh outputs > $(REGISTRY_SNAPSHOT).tmp || { rm -f $(REGISTRY_SNAPSHOT).tmp; exit 1; }
	mv $(REGISTRY_SNAPSHOT).tmp $(REGISTRY_SNAPSHOT)
