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

STRUCTURIZR := docker run --rm -v "$(CURDIR)/$(ARCH_DIR):/w:ro"

.PHONY: help validate inspect check docs test view export mermaid-views mermaid-render mermaid pdf clean lint pytest
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

## pytest          tests under tests/meridian, including the import-contract detection test
pytest:
	uv run pytest
