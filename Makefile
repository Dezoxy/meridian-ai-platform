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
# it is pinned with := and a command line does not override it. A run that
# overlaps another needs its own name and port.
PYTEST_DB_IMAGE     := postgres:17.11@sha256:d74eeac9a635390a49bc21bd49fccd973de707e2a53a76ac49b552b8712ec46f
PYTEST_DB_CONTAINER ?= meridian-pytest-db
PYTEST_DB_PORT      ?= 55432

STRUCTURIZR := docker run --rm -v "$(CURDIR)/$(ARCH_DIR):/w:ro"

.PHONY: help validate inspect check docs test view export mermaid-views mermaid-render mermaid pdf clean lint pytest pytest-db synthetic up deploy demo smoke grafana grafana-password down azure-state azure-plan azure-apply azure-smoke registry-snapshot registry
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

## pytest          tests under tests/meridian and tests/synthetic, including the import-contract detection test
pytest:
	uv run pytest

## pytest-db       pytest with a throwaway PostgreSQL 17 on 127.0.0.1:55432 (needs Docker; concurrent runs each need their own PYTEST_DB_CONTAINER and PYTEST_DB_PORT); the database tests run instead of skipping
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
	MERIDIAN_REQUIRE_DB=1 uv run pytest

## registry        validate config/registry, compare it with the Terraform snapshot and check the generated schemas
registry:
	uv run meridian registry validate --terraform-outputs $(REGISTRY_SNAPSHOT)
	uv run meridian registry schemas --check

## synthetic       regenerate the synthetic data and golden set under data/synthetic (seeded; reruns are identical)
synthetic:
	PYTHONPATH=data/synthetic uv run python -m generator

# ── Local platform on kind ───────────────────────────────────────────────────
# infra/kind/README.md says what these create. The cluster's credentials stay in
# infra/kind/kubeconfig (gitignored); ~/.kube/config is never touched.

## up              create the kind cluster and install the local platform (needs Docker, kind, kubectl, helm; first run pulls images)
up:
	infra/kind/up.sh

## deploy          build the image, run the migrations and put the Claims API, Agent Runtime and Model Gateway on the kind cluster (needs make up)
deploy:
	infra/kind/deploy.sh

## demo            deploy, post a synthetic claim at http://claims.meridian.localhost:8088 and find its one trace across the three services in Tempo
demo: deploy
	infra/kind/demo.sh

## smoke           prove the edge, pgvector and a trace, log and metric reaching Grafana's datasources
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

## azure-smoke     prove the foundation: live models match Terraform, key auth is off, one chat and one embedding call per account (well under EUR 0.01)
azure-smoke:
	infra/terraform/foundation.sh smoke

## registry-snapshot refresh the registry's copy of Terraform's deployment outputs (only the compared fields); read-only in Azure
registry-snapshot:
	infra/terraform/foundation.sh outputs > $(REGISTRY_SNAPSHOT).tmp || { rm -f $(REGISTRY_SNAPSHOT).tmp; exit 1; }
	mv $(REGISTRY_SNAPSHOT).tmp $(REGISTRY_SNAPSHOT)
