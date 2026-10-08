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
MERMAID_IMAGE     ?= minlag/mermaid-cli:12.0.1

# ── paths ────────────────────────────────────────────────────────────────────
ARCH_DIR  ?= docs/architecture
GENERATED := $(ARCH_DIR)/generated
PORT      ?= 8080
# The registry's copy of Terraform's deployment outputs (T-12).
REGISTRY_SNAPSHOT := config/registry/snapshots/terraform-openai-deployments.json
# Where `make secret-scan` starts: it scans the commits from here to HEAD, the
# ones a push would add. CI's secret scan job scans every commit of a pull
# request, so a finding in an early commit is not fixed by a later one.
SECRET_SCAN_BASE ?= origin/main
# The throwaway PostgreSQL of `make pytest-db`; the image is the one the python
# workflow runs as its service container (a test compares the two strings), so
# it is pinned with := and a command line does not override it. It is
# PostgreSQL 17.11 with pgvector 0.8.7 on Debian trixie (the knowledge store,
# S012): the same PostgreSQL and OS release as the CloudNativePG image kind
# runs, and one pgvector patch ahead of it (0.8.6 there, read on 2026-10-04)
# until that image's next release. A run that overlaps another needs its own
# name and port.
PYTEST_DB_IMAGE     := pgvector/pgvector:0.8.7-pg17-trixie@sha256:7a7e9f22015b67edb4bef5c59daeebcd7e74bfa570df6ce60ae01237c8648a84
PYTEST_DB_CONTAINER ?= meridian-pytest-db
PYTEST_DB_PORT      ?= 55432
# The throwaway Redis beside it, for the gateway's shared rate windows (S066):
# Redis 8.10.2, the 8.10 line's second patch (8.10.0 was the General Availability
# release, 2026-07-29; 8.10.2 carries security fixes), on Alpine 3.23. The digest
# is the multi-arch index's. Same string as the python workflow's service
# container, which a test compares; := so a command line does not override it. A
# run that overlaps another needs its own name and port too, outside Linux's
# ephemeral range (32768 to 60999).
PYTEST_REDIS_IMAGE     := redis:8.10.2-alpine@sha256:3811787313eba226a2ef38658c6ccb91cd5e110edc89c37767de373120a0e5a0
PYTEST_REDIS_CONTAINER ?= meridian-pytest-redis
PYTEST_REDIS_PORT      ?= 26379
# Extra pytest arguments for `make pytest` and `make pytest-db`, e.g. one test
# file, or --durations=25 as CI passes.
PYTEST_ARGS         ?=
# COVERAGE=1 makes `make pytest` and `make pytest-db` measure the line coverage
# of src/meridian (pytest-cov, with xdist) and fail under the floor that
# pyproject.toml's [tool.coverage.report] fail_under holds, the one place it is
# written (S074). CI sets it; a run without it measures nothing and cannot fail
# on coverage, so a person running one file is never refused. Anything but 1
# leaves it off. --no-cov-on-fail keeps a run with a failed test from printing
# the floor's failure as well: that run fails once, for the test (a passing run
# below the floor still fails on the floor).
COVERAGE            ?=
# COVERAGE_SHARD=1 beside COVERAGE=1 is a shard's run in CI (S074): it measures
# the lines, writes the data to COVERAGE_FILE, prints no report and applies no
# floor, because a shard runs a part of the suite and a part is always under it.
# `make coverage-floor` combines the shards' data and applies the floor once. It
# is `--cov-fail-under=0` and not the absence of the option, which pytest-cov
# fills in from pyproject.toml (the one place the number is written).
COVERAGE_SHARD      ?=
PYTEST_COVERAGE_ARGS := $(if $(filter 1,$(COVERAGE)),--cov $(if $(filter 1,$(COVERAGE_SHARD)),--cov-report= --cov-fail-under=0,--cov-report=term:skip-covered) --no-cov-on-fail,)
# Where `make coverage-floor` finds the shards' coverage data: one file
# shard-N.coverage for each shard (the python workflow downloads them here).
COVERAGE_SHARDS_DIR ?= .coverage-shards
# Worker processes for `make pytest` and `make pytest-db` (pytest-xdist -n): a
# number, or auto for one per CPU core; 0 runs the tests in one process. Ten,
# the owner's decision of 2026-10-06 for the 12-core development machine,
# where the whole suite takes 1 min 55 s with ten and 2 min 42 s with four.
# CI sets its own (4, its runner's cores) in the workflow, and so does a step
# that runs beside others. On a laptop under Docker Desktop ten dropped
# connections to the database container (S054): pass PYTEST_WORKERS=4 there.
PYTEST_WORKERS      ?= 10
# The machine's test lock (S099, scripts/machine_lock.sh): the recipe's own
# shell takes one exclusive flock and holds it to its end, so a second
# session's run on the machine waits for the first and says who holds it
# (MERIDIAN_LOCK_WAIT seconds, default 1800; then it runs nothing). In front
# of `make pytest`, `make pytest-db` and each container of `make alerts`, and
# nowhere else: `make eval` and `make eval-baseline` reach it through
# `$(MAKE) pytest-db`, and a second lock around them would wait on that one.
# Not `:=`: the target's name is read where the recipe runs. `exec` behind it
# where the recipe is one command: make's shell then IS the run, so a
# SIGTERM to make ends the run and frees the lock, as it did before the
# line had a `&&` and make started a shell for it.
MACHINE_LOCK = MACHINE_LOCK_LABEL=$@ . scripts/machine_lock.sh
# promtool for `make alerts` (S024): the one of the Prometheus that the
# kube-prometheus-stack chart in infra/kind/pins.env runs (chart 91.8.2 runs
# v3.15.0), so a rule is checked by the parser that will load it. The digest is
# the multi-arch index's, so the same line pulls on a laptop and on CI's runner;
# := so an environment variable does not change it, but a variable on make's
# command line (make PROMTOOL_IMAGE=...) CAN override it: only `override` would
# stop that. .github/renovate.json reads it as it reads PYTEST_DB_IMAGE.
PROMTOOL_IMAGE      := quay.io/prometheus/prometheus:v3.15.0-distroless@sha256:b2a413d5a03ea6a76782a508d1c7947440bba3b973931a25676e278431891b01
# Trivy's configuration scan for `make aws-scan` (S036), `make gcp-scan` (S078),
# `make aws-kubeadm-scan` and `make gcp-kubeadm-scan` (S079) and
# `make azure-platform-scan` (S020), one image for all:
# 0.75.0, read on
# 2026-10-06. The digest is the multi-arch index's (`docker buildx imagetools
# inspect` shows an OCI index; `docker pull` of the tag prints the same digest);
# := so an environment variable does not change it, but a variable on make's
# command line (make TRIVY_IMAGE=...) CAN override it: only `override` would
# stop that, so a command line that names it deserves a look. .github/renovate.json
# reads it as it reads PROMTOOL_IMAGE. The scan needs no network: Trivy looks for a newer
# bundle of checks at mirror.gcr.io first, and with the network off and without
# --skip-check-update it waits, fails and falls back to the checks compiled into
# the image; with that flag it goes straight to those checks. So the checks are
# those of the pinned image, and a new image is the only way they change.
TRIVY_IMAGE         := ghcr.io/aquasecurity/trivy:0.75.0@sha256:af6acf9a6b85dfe389a1941505c0ce9efef52a4719635e1a962f022a3d855daa
# The adjuster's decision `make demo` posts for a claim referred to an adjuster:
# approve, reject or request_documents (the script refuses anything else).
DECISION            ?= approve
# How many claims `make demo-seed` posts (the first of data/synthetic/claims.json,
# 1 to 47; all are golden claims, the last seven added later) and the seconds it waits after each
# claim it triaged (0 to 60; 10 keeps a run under the gateway's tenant windows of
# 10 requests in 10 seconds and 10,000 tokens a minute). The script refuses
# anything else.
COUNT               ?= 40
PACE_SECONDS        ?= 10
# The evaluation of the claims workload (S017, S050): the baseline committed in
# Git, the report a run writes (gitignored) and the one test that writes it. It
# replays a real model's recorded answers through the Model Gateway (CI and
# `make eval`); `make eval-record` makes the recording and needs an Azure login.
EVAL_BASELINE       ?= data/evaluation/claims-triage-baseline.json
EVAL_REPORT         ?= .eval/claims-triage-report.json
EVAL_TEST           ?= tests/meridian/test_evaluation_stack.py::test_the_recorded_model_answers_the_golden_set_through_the_gateway
# The injection suite (S032) beside it: its baseline and the summary beside the
# baseline are committed, the report is gitignored, and one test writes all three
# (the summary only when `make eval-baseline` asks for it).
EVAL_INJECTION_BASELINE ?= data/evaluation/claims-triage-injection-baseline.json
EVAL_INJECTION_REPORT   ?= .eval/claims-triage-injection-report.json
EVAL_INJECTION_SUMMARY  ?= data/evaluation/injection-summary.md
EVAL_INJECTION_TEST     ?= tests/meridian/test_injection_stack.py::test_the_injection_cases_run_through_the_stack_with_an_obedient_model
# What a report is made from: a tracked file here that is newer than the report
# makes it stale (`make eval-compare` refuses it).
EVAL_INPUTS         := src config/registry data/synthetic data/evaluation/recordings tests/meridian pyproject.toml uv.lock

STRUCTURIZR := docker run --rm -v "$(CURDIR)/$(ARCH_DIR):/w:ro"

.PHONY: help validate inspect check docs plan-progress test secret-scan view export mermaid-views mermaid-render mermaid pdf pdf-brief clean lint pytest coverage-floor pytest-db alerts eval eval-tests eval-compare eval-baseline eval-record eval-injection-record synthetic up deploy images helm-lint demo demo-seed smoke gateway-upkeep grafana grafana-password identity-passwords cert-renew cluster-holder down azure-state azure-plan azure-apply azure-smoke gateway-live registry-snapshot registry aws-validate aws-scan aws-plan aws-apply aws-destroy gcp-validate gcp-scan aws-kubeadm-validate aws-kubeadm-scan gcp-kubeadm-validate gcp-kubeadm-scan azure-platform-validate azure-platform-scan
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
	python3 scripts/check_plan_files.py

## plan-progress   rewrite the plan's Part F (finished steps, steps in flight) from the step files' status lines
plan-progress:
	python3 scripts/check_plan_files.py --write

## test            unit tests for the checker and the Mermaid and PDF scripts
test:
	python3 -m unittest discover -s tests

## secret-scan     scan the commits a push would add (SECRET_SCAN_BASE..HEAD, default origin/main) for secrets, as CI's secret scan job does; run it before every push (needs gitleaks)
secret-scan:
	@git rev-parse --verify --quiet "$(SECRET_SCAN_BASE)^{commit}" >/dev/null || { echo "secret-scan: git knows no commit $(SECRET_SCAN_BASE); fetch it, or set SECRET_SCAN_BASE" >&2; exit 1; }
	@command -v gitleaks >/dev/null 2>&1 || { echo "secret-scan: gitleaks is not on the PATH; CI's version is in .github/workflows/docs.yml" >&2; exit 1; }
	gitleaks git --log-opts="$(SECRET_SCAN_BASE)..HEAD" --redact

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
	@# The script passes on a folder with no diagram, which is right for a
	@# repository without Mermaid. This one holds several, so none extracted
	@# means the extractor is broken, and that must not pass as a render.
	@ls $(GENERATED)/mermaid-render/*.mmd >/dev/null 2>&1 || { echo "mermaid-render: no Mermaid block was extracted into $(GENERATED)/mermaid-render" >&2; exit 1; }
	MERMAID_IMAGE=$(MERMAID_IMAGE) scripts/render-mermaid.sh $(GENERATED)/mermaid-render

## mermaid         mermaid-views, then mermaid-render
mermaid: mermaid-views mermaid-render

## pdf             the Documentation tab and every view as one PDF, named <project>-architecture-<date>-<edition>.pdf
pdf:
	STRUCTURIZR_IMAGE=$(STRUCTURIZR_IMAGE) PANDOC_IMAGE=$(PANDOC_IMAGE) MERMAID_IMAGE=$(MERMAID_IMAGE) ARCH_DIR=$(ARCH_DIR) scripts/architecture-pdf.sh

## pdf-brief       the brief: without the documents pdf-brief.txt lists, the decisions as an index; named <project>-architecture-brief-<date>-<edition>.pdf
pdf-brief:
	BRIEF=1 STRUCTURIZR_IMAGE=$(STRUCTURIZR_IMAGE) PANDOC_IMAGE=$(PANDOC_IMAGE) MERMAID_IMAGE=$(MERMAID_IMAGE) ARCH_DIR=$(ARCH_DIR) scripts/architecture-pdf.sh

## clean           delete the generated folder (exports and PDFs; all gitignored)
clean:
	rm -rf $(GENERATED)

# ── Python workspace ─────────────────────────────────────────────────────────
# Run through uv, which creates and syncs .venv on first use. The targets above
# keep working without uv; the docs CI job relies on that.

## lint            ruff check, ruff format --check, the import-linter contracts and the file size check (800 lines, scripts/file-size-exceptions.txt)
lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run lint-imports
	uv run python scripts/check_file_sizes.py

## pytest          tests under tests/meridian and tests/synthetic, including the import-contract detection test, run in parallel (PYTEST_WORKERS, default 10; 0 runs them in one process; with COVERAGE=1 a run of a part of the suite fails the coverage floor); takes the machine's test lock, so it waits for another session's run
pytest:
	$(MACHINE_LOCK) && exec uv run pytest -n $(PYTEST_WORKERS) $(PYTEST_COVERAGE_ARGS) $(PYTEST_ARGS)

## coverage-floor  combine the shards' coverage data (COVERAGE_SHARDS_DIR/*.coverage) and fail under the floor of pyproject.toml's [tool.coverage.report], the one place it is written; the shards run with COVERAGE=1 COVERAGE_SHARD=1 and apply none
coverage-floor:
	uv run coverage combine --keep $(COVERAGE_SHARDS_DIR)/*.coverage
	uv run coverage report --skip-covered

## alerts          check Meridian's alert rules (infra/kind/alerts) with promtool and run their unit tests and the cost dashboard's gap tests (needs Docker and uv); each container takes the machine's test lock
alerts:
	uv run python scripts/alert_rules.py extract .alerts
	uv run python scripts/cost_dashboard_gap.py write .alerts
	$(MACHINE_LOCK) && exec docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges --entrypoint /bin/promtool -v "$(CURDIR)/.alerts:/rules:ro" -w /rules $(PROMTOOL_IMAGE) check rules --lint=all --lint-fatal meridian.rules.yaml
	$(MACHINE_LOCK) && exec docker run --rm --network none --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges --entrypoint /bin/promtool -v "$(CURDIR)/.alerts:/rules:ro" -w /rules $(PROMTOOL_IMAGE) test rules meridian.test.yaml gateway-cost.test.yaml

## pytest-db       pytest in parallel (PYTEST_WORKERS, default 10; 0 runs them in one process) with a throwaway PostgreSQL 17 on 127.0.0.1:55432 and a throwaway Redis 8 on 127.0.0.1:26379, neither persisted (needs Docker; takes the machine's test lock, so a second run waits for the first and both may keep the default container names and ports); the database and Redis tests run instead of skipping; with COVERAGE=1 a run of a part of the suite fails the coverage floor
pytest-db:
	@set -e; \
	$(MACHINE_LOCK); \
	docker rm -f $(PYTEST_DB_CONTAINER) $(PYTEST_REDIS_CONTAINER) >/dev/null 2>&1 || true; \
	trap 'docker rm -f $(PYTEST_DB_CONTAINER) $(PYTEST_REDIS_CONTAINER) >/dev/null 2>&1 || true' EXIT; \
	trap 'exit 130' INT; \
	trap 'exit 143' TERM; \
	docker run -d --name $(PYTEST_DB_CONTAINER) \
		--tmpfs /var/lib/postgresql/data -p 127.0.0.1:$(PYTEST_DB_PORT):5432 \
		-e POSTGRES_HOST_AUTH_METHOD=trust $(PYTEST_DB_IMAGE); \
	docker run -d --name $(PYTEST_REDIS_CONTAINER) \
		-p 127.0.0.1:$(PYTEST_REDIS_PORT):6379 $(PYTEST_REDIS_IMAGE) \
		redis-server --save '' --appendonly no; \
	for i in $$(seq 1 60); do \
		docker exec $(PYTEST_DB_CONTAINER) pg_isready -U postgres -h 127.0.0.1 >/dev/null 2>&1 && break; \
		[ $$i -eq 60 ] && { echo "pytest-db: PostgreSQL did not become ready" >&2; exit 1; }; \
		sleep 1; \
	done; \
	for i in $$(seq 1 60); do \
		[ "$$(docker exec $(PYTEST_REDIS_CONTAINER) redis-cli ping 2>/dev/null)" = PONG ] && break; \
		[ $$i -eq 60 ] && { echo "pytest-db: Redis did not become ready" >&2; exit 1; }; \
		sleep 1; \
	done; \
	MERIDIAN_TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:$(PYTEST_DB_PORT)/postgres \
	MERIDIAN_TEST_REDIS_URL=redis://127.0.0.1:$(PYTEST_REDIS_PORT)/0 \
	MERIDIAN_REQUIRE_DB=1 uv run pytest -n $(PYTEST_WORKERS) $(PYTEST_COVERAGE_ARGS) $(PYTEST_ARGS)

## eval            replay the golden set through the stack with the recorded model's answers and the judge, and run the injection cases through it with a model that obeys (needs Docker); write both reports and compare them with their baselines
eval:
	mkdir -p $(dir $(EVAL_REPORT)) $(dir $(EVAL_INJECTION_REPORT))
	rm -f $(EVAL_REPORT) $(EVAL_INJECTION_REPORT)
	MERIDIAN_EVAL_REPORT=$(abspath $(EVAL_REPORT)) \
	MERIDIAN_EVAL_INJECTION_REPORT=$(abspath $(EVAL_INJECTION_REPORT)) \
	$(MAKE) pytest-db PYTEST_WORKERS=0 PYTEST_ARGS="$(EVAL_TEST) $(EVAL_INJECTION_TEST) -q"
	$(MAKE) eval-compare

## eval-tests      run only the two tests that write the evaluation reports, in one process, to the paths MERIDIAN_EVAL_REPORT and MERIDIAN_EVAL_INJECTION_REPORT name in the environment, against the database and Redis MERIDIAN_TEST_DATABASE_URL and MERIDIAN_TEST_REDIS_URL name (CI's evaluation job; make eval starts its own); no comparison
eval-tests:
	uv run pytest -n 0 $(EVAL_TEST) $(EVAL_INJECTION_TEST) -q

## eval-compare    compare the two reports with their baselines (meridian eval compare), both even when one fails; refuses a report older than a tracked file it is made from
eval-compare:
	@for report in "$(EVAL_REPORT)" "$(EVAL_INJECTION_REPORT)"; do \
		test -f "$$report" || { echo "no report at $$report: run make eval" >&2; exit 1; }; \
		newer="$$(git ls-files -- $(EVAL_INPUTS) | while IFS= read -r file; do \
			if [ "$$file" -nt "$$report" ]; then printf '%s\n' "$$file"; break; fi; \
		done)"; \
		if [ -n "$$newer" ]; then echo "the report is older than $$newer ($$report): run make eval" >&2; exit 1; fi; \
	done
	@status=0; \
	uv run meridian eval compare $(EVAL_BASELINE) $(EVAL_REPORT) || status=1; \
	uv run meridian eval compare $(EVAL_INJECTION_BASELINE) $(EVAL_INJECTION_REPORT) || status=1; \
	exit $$status

## eval-baseline   regenerate the golden set's baseline, the injection baseline and its summary after a reviewed prompt, tool, recording, golden-set, screen or case change (needs Docker)
eval-baseline:
	mkdir -p $(dir $(EVAL_BASELINE)) $(dir $(EVAL_INJECTION_BASELINE)) $(dir $(EVAL_INJECTION_SUMMARY))
	MERIDIAN_EVAL_REPORT=$(abspath $(EVAL_BASELINE)) \
	MERIDIAN_EVAL_INJECTION_REPORT=$(abspath $(EVAL_INJECTION_BASELINE)) \
	MERIDIAN_EVAL_INJECTION_SUMMARY=$(abspath $(EVAL_INJECTION_SUMMARY)) \
	$(MAKE) pytest-db PYTEST_WORKERS=0 PYTEST_ARGS="$(EVAL_TEST) $(EVAL_INJECTION_TEST) -q"

## eval-record     SPENDS MONEY: 55 chat calls, EUR 0.12 as measured on 2026-10-03 (the gateway refuses a run past EUR 0.50 for each of the two tenants it charges), on the live Azure models, with the judge, to record the golden set's answers and a variant prompt's; rewrites files under data/evaluation/ (needs az login, Docker; the owner runs it)
eval-record:
	infra/terraform/foundation.sh eval-record

## eval-injection-record  SPENDS MONEY: the injection cases the baseline says reach the model (52 on 2026-10-07, about EUR 0.12 expected; the gateway refuses the run past EUR 0.50), answered by the live Azure model with no judge, to write their recording, live report and summary under data/evaluation/ (needs az login, Docker; the owner runs it)
eval-injection-record:
	infra/terraform/foundation.sh eval-injection-record

## registry        validate config/registry, compare it with the Terraform snapshot and check the generated schemas and the tool-server contracts under api/mcp
registry:
	uv run meridian registry validate --terraform-outputs $(REGISTRY_SNAPSHOT)
	uv run meridian registry schemas --check
	uv run meridian registry contracts --check

## synthetic       regenerate the synthetic data, the golden set and the injection cases under data/synthetic (seeded; reruns are identical)
synthetic:
	PYTHONPATH=data/synthetic uv run python -m generator

# ── Local platform on kind ───────────────────────────────────────────────────
# infra/kind/README.md says what these create. The cluster's credentials stay in
# infra/kind/kubeconfig (gitignored); ~/.kube/config is never touched.

## up              create the kind cluster, install the local platform and provision the Grafana dashboards (needs Docker, kind, kubectl, helm; first run pulls images; stops when another holder has the cluster unless TAKE_CLUSTER=1); MERIDIAN_IDENTITY=keycloak also makes the local sign-in issuer, an add-on that is off by default, seen on kind once, and needs 2,500 MB of memory available (infra/kind/README.md, "The sign-in issuer"); any other value stops with a usage line; with MERIDIAN_SIGNIN=staff as well it also makes the Secret the Claims API's staff sign-in reads (make deploy then uses it); without the add-on it only says in its last lines that the Secret was not made
up:
	infra/kind/up.sh

## deploy          build the image, run the migrations, seed the policy store, install the Helm release with the Claims API, Agent Runtime, Model Gateway and the three tool servers on the kind cluster and ingest the policy wordings (needs make up; the first deploy of an image waits a minute after the ingestion; stops when another holder has the cluster unless TAKE_CLUSTER=1); MERIDIAN_IDENTITY=keycloak MERIDIAN_SIGNIN=staff turns the Claims API's staff sign-in on (the Secret comes from make up with both switches; empty or off turns it off again; any other value stops with a usage line; infra/kind/README.md, "The sign-in issuer")
deploy:
	infra/kind/deploy.sh

## images          list the meridian:* images in the Docker engine and the kind node, each marked in use or unused by a workload, and print the commands that would remove the unused ones; removes nothing (the owner's command)
images:
	infra/kind/images.sh

## demo            deploy, post a synthetic claim at http://claims.meridian.localhost:8088, find its trace across the five services that triage it in Tempo and, when it is referred to an adjuster, decide it and find that trace too (make demo DECISION=reject; approve, reject or request_documents)
demo: deploy
	DECISION="$(DECISION)" infra/kind/demo.sh

## demo-seed       post the first COUNT (default 40, at most 47) synthetic claims through the edge, one at a time, so the adjuster's and the claimant's pages show content; kind only, synthetic data only, the gateway's replay mode is simulated and calls no model so it costs nothing; safe to run twice (a failed triage is tried again); decides nothing; needs make deploy and 2,500 MB of free memory, and refuses while a test database runs (make demo-seed COUNT=47 adds the seven claims after the first forty); PACE_SECONDS (default 10) is the pause between two claims
demo-seed:
	COUNT="$(COUNT)" PACE_SECONDS="$(PACE_SECONDS)" infra/kind/demo-seed.sh

## smoke           prove the edge, pgvector, the policy, knowledge and migration stores, a trace, log and metric reaching Grafana's datasources, the cost dashboard and, once deployed, one call per tool server through the runtime's client, the gateway's series, the adjuster's and claimant's pages, the sweep's last Job and that its schedule has not stopped, that three connections no network policy allows are blocked and one it allows is not, that the gateway refuses a caller with no identity or with another CA's certificate, that the certificate policy stands and the issuer refuses a request from another namespace, and that the alert rules are loaded, healthy and quiet and the health dashboard is served; with MERIDIAN_IDENTITY=keycloak also six lines for the sign-in issuer: its pod, its documents through the edge, the paths the edge keeps closed, and its issuer as the Claims API's pod sees it (one SKIP line otherwise; seen on kind once); with MERIDIAN_SIGNIN=staff the adjuster queue's line expects a 303 to the app's /auth/start and from there a 303 to the issuer, and an unsigned form post and JSON post of a decision to be refused with 401 (still three adjuster-page lines)
smoke:
	infra/kind/smoke.sh

## gateway-upkeep  run the Model Gateway's upkeep command on kind as a Job of its own, under its own database role, and print its output; ARGS is the subcommand and its arguments, letters, digits and . _ = - only (make expands $(...) in ARGS before the script sees it: your own input): make gateway-upkeep ARGS="reservations --older-than 15" (also close ATTEMPT_ID --reason SLUG, credit TENANT --tokens N --reason SLUG, expire --before YYYY-MM --reason SLUG); reservations, and expire without --confirm, only read, every other subcommand changes the ledger (needs make up and make deploy)
gateway-upkeep:
	infra/kind/upkeep.sh

## grafana         port-forward Grafana to http://127.0.0.1:3000 (Ctrl-C stops it)
grafana:
	infra/kind/grafana.sh forward

## grafana-password print the Grafana admin password
grafana-password:
	@infra/kind/grafana.sh password

## identity-passwords print, on the terminal, the user name and password of each test user of the local sign-in issuer (Keycloak on kind, MERIDIAN_IDENTITY=keycloak): prints secrets, disposable ones of this cluster; refuses a cluster it cannot tell is the local one, and a pipe or a file unless MERIDIAN_IDENTITY_SHOW=1 is set; infra/kind/identity.sh users lists the people without them (infra/kind/README.md, "The sign-in issuer")
identity-passwords:
	@infra/kind/identity.sh passwords

## helm-lint       lint the Meridian chart strictly, with kind's values (the rate store on, with the image of PYTEST_REDIS_IMAGE: the pin in infra/kind/pins.env is the same one) and every Job on, the upkeep Job with one argument (needs helm)
helm-lint:
	helm lint --strict infra/helm/meridian -f infra/kind/values/meridian.yaml --set-string image.repository=meridian --set-string image.tag=lint --set-string rateStore.image=$(PYTEST_REDIS_IMAGE) --set jobs.migrate.enabled=true --set jobs.seed.enabled=true --set jobs.ingest.enabled=true --set jobs.upkeep.enabled=true --set-string jobs.upkeep.runSuffix=lint --set-json 'jobs.upkeep.args=["reservations"]'

## cert-renew      ask cert-manager to issue one Certificate of the namespace meridian again now, for after a denied or failed request when cert-manager's own wait (an hour, doubling) would otherwise hold a repaired deploy: make cert-renew CERT=<name> (the name of a Certificate, see kubectl -n meridian get certificate); it sets the Certificate's Issuing condition as cmctl renew does and changes nothing else; needs make up and make deploy; stops when another holder has the cluster unless TAKE_CLUSTER=1; CERT=rate-store restarts the store (503 for one to three minutes); tested against a stub kubectl; seen on kind on 2026-10-07 (the refusals and one renewal of a healthy Certificate); not seen (a denied request, a 409)
cert-renew:
	infra/kind/cert-renew.sh

## cluster-holder  print who holds the kind cluster (the holder, its commit, the time and the state: changing after a make up or make deploy that did not end well), or that there is no record or no cluster; make up, deploy and down stop when another holder has it unless TAKE_CLUSTER=1 is in front of the command (CLUSTER_HOLDER=<name> names a checkout that is not on a branch); a notice, not a lock
cluster-holder:
	infra/kind/holder.sh

## down            delete the kind cluster "meridian" and its credentials file (destructive; for a test that needs a fresh cluster, never to clear a fault; hard rule 8; stops when another holder has the cluster unless TAKE_CLUSTER=1)
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

# ── AWS module (checked, applied once by the owner) ──────────────────────────
# infra/terraform/aws/README.md says what these do. They use the account pinned
# in infra/terraform/local.env-aws (gitignored). Needs terraform, and for plan,
# apply and destroy the aws CLI signed in. Nothing here runs in CI (S022).

## aws-validate    terraform fmt -check, init with no backend and validate of the AWS module; needs no account and changes nothing in AWS
aws-validate:
	infra/terraform/aws.sh validate

## aws-scan        Trivy's configuration scan of the AWS module from the pinned image, network off, read-only; fails on a HIGH or CRITICAL finding that infra/terraform/aws/.trivyignore does not list; changes nothing in AWS (needs Docker)
aws-scan:
	@ls "$(CURDIR)"/infra/terraform/aws/*.tf >/dev/null 2>&1 || { echo "aws-scan: no .tf file in infra/terraform/aws, nothing to scan" >&2; exit 1; }
	docker run --rm --network none --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges -e TRIVY_CACHE_DIR=/tmp/trivy --mount type=bind,source="$(CURDIR)/infra/terraform/aws",target=/work,readonly -w /work $(TRIVY_IMAGE) config --quiet --skip-check-update --skip-version-check --disable-telemetry --skip-dirs .terraform --skip-files aws.tfplan,aws.tfplan.meta,terraform.tfstate,terraform.tfstate.backup --severity HIGH,CRITICAL --exit-code 1 .

## aws-plan        sign-in check against the pinned account, terraform init and plan of the AWS module into aws.tfplan; changes nothing in AWS
aws-plan:
	infra/terraform/aws.sh plan

## aws-apply       SPENDS MONEY: applies the saved plan of the AWS module (EKS, RDS, ECR, network, budget; it bills by the hour until aws-destroy, see the module's README); the owner runs it, after reading the plan
aws-apply:
	infra/terraform/aws.sh apply

## aws-destroy     REMOVES the AWS environment: Terraform asks its own question; the owner runs it, in a terminal, from a sign-in no session can read (the terminal check stops an accident and a plain shell, not a session that makes itself a terminal)
aws-destroy:
	infra/terraform/aws.sh destroy

# ── Google Cloud module (a scaffold: checked, never planned, never applied) ──
# infra/terraform/gcp/README.md says what this is. There is no project and no
# credential, and deliberately no target that plans, applies or removes it.

## gcp-validate    terraform fmt -check, init with no backend and validate of the Google Cloud module; needs no project and no credential and changes nothing in Google Cloud
gcp-validate:
	infra/terraform/aws.sh validate gcp

## gcp-scan        Trivy's configuration scan of the Google Cloud module from the same pinned image as aws-scan: offline, changes nothing in Google Cloud, needs no project and no credential; fails on a HIGH or CRITICAL finding that infra/terraform/gcp/.trivyignore does not list (needs Docker)
gcp-scan:
	@ls "$(CURDIR)"/infra/terraform/gcp/*.tf >/dev/null 2>&1 || { echo "gcp-scan: no .tf file in infra/terraform/gcp, nothing to scan" >&2; exit 1; }
	docker run --rm --network none --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges -e TRIVY_CACHE_DIR=/tmp/trivy --mount type=bind,source="$(CURDIR)/infra/terraform/gcp",target=/work,readonly -w /work $(TRIVY_IMAGE) config --quiet --skip-check-update --skip-version-check --disable-telemetry --skip-dirs .terraform --skip-files gcp.tfplan,terraform.tfstate,terraform.tfstate.backup --severity HIGH,CRITICAL --exit-code 1 .

# ── AWS self-managed cluster module (S079: checked, not yet planned or applied) ─
# infra/terraform/aws-kubeadm/README.md says what this is. These two checks need
# no account and no credential. No target here plans, applies or removes it yet.

## aws-kubeadm-validate terraform fmt -check, init with no backend and validate of the AWS self-managed cluster module; needs no account and changes nothing in AWS
aws-kubeadm-validate:
	infra/terraform/aws.sh validate aws-kubeadm

## aws-kubeadm-scan Trivy's configuration scan of the AWS self-managed cluster module from the same pinned image as aws-scan: offline, changes nothing in AWS, needs no account; fails on a HIGH or CRITICAL finding that infra/terraform/aws-kubeadm/.trivyignore does not list (needs Docker)
aws-kubeadm-scan:
	@ls "$(CURDIR)"/infra/terraform/aws-kubeadm/*.tf >/dev/null 2>&1 || { echo "aws-kubeadm-scan: no .tf file in infra/terraform/aws-kubeadm, nothing to scan" >&2; exit 1; }
	docker run --rm --network none --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges -e TRIVY_CACHE_DIR=/tmp/trivy --mount type=bind,source="$(CURDIR)/infra/terraform/aws-kubeadm",target=/work,readonly -w /work $(TRIVY_IMAGE) config --quiet --skip-check-update --skip-version-check --disable-telemetry --skip-dirs .terraform --skip-files aws-kubeadm.tfplan,aws-kubeadm.tfplan.meta,terraform.tfstate,terraform.tfstate.backup --severity HIGH,CRITICAL --exit-code 1 .

# ── Google Cloud twin of the self-managed cluster (S079: a scaffold, checked, never planned or applied) ─
# infra/terraform/gcp-kubeadm/README.md says what this is. These two checks need
# no project and no credential. No target here plans, applies or removes it, and
# none will: the project applies nothing in Google Cloud.

## gcp-kubeadm-validate terraform fmt -check, init with no backend and validate of the Google Cloud twin of the self-managed cluster module; needs no project and no credential and changes nothing in Google Cloud
gcp-kubeadm-validate:
	infra/terraform/aws.sh validate gcp-kubeadm

## gcp-kubeadm-scan Trivy's configuration scan of the Google Cloud twin of the self-managed cluster module from the same pinned image as aws-scan: offline, changes nothing in Google Cloud, needs no project and no credential; fails on a HIGH or CRITICAL finding that infra/terraform/gcp-kubeadm/.trivyignore does not list (needs Docker)
gcp-kubeadm-scan:
	@ls "$(CURDIR)"/infra/terraform/gcp-kubeadm/*.tf >/dev/null 2>&1 || { echo "gcp-kubeadm-scan: no .tf file in infra/terraform/gcp-kubeadm, nothing to scan" >&2; exit 1; }
	docker run --rm --network none --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges -e TRIVY_CACHE_DIR=/tmp/trivy --mount type=bind,source="$(CURDIR)/infra/terraform/gcp-kubeadm",target=/work,readonly -w /work $(TRIVY_IMAGE) config --quiet --skip-check-update --skip-version-check --disable-telemetry --skip-dirs .terraform --skip-files gcp-kubeadm.tfplan,terraform.tfstate,terraform.tfstate.backup --severity HIGH,CRITICAL --exit-code 1 .

# ── Azure platform module (S020: checked, never planned or applied) ──────────
# infra/terraform/azure/README.md says what this is. These two checks need no
# Azure sign-in, no subscription and no credential, and deliberately no target
# here plans, applies or removes the module: those arrive with their wrapper and
# the guard's rules, in a later change.

## azure-platform-validate terraform fmt -check, init with no backend and validate of the Azure platform module; needs no Azure sign-in and no subscription and changes nothing in Azure
azure-platform-validate:
	infra/terraform/aws.sh validate azure

## azure-platform-scan Trivy's configuration scan of the Azure platform module from the same pinned image as aws-scan: offline, changes nothing in Azure, needs no Azure sign-in and no subscription; fails on a HIGH or CRITICAL finding that infra/terraform/azure/.trivyignore does not list (needs Docker)
azure-platform-scan:
	@ls "$(CURDIR)"/infra/terraform/azure/*.tf >/dev/null 2>&1 || { echo "azure-platform-scan: no .tf file in infra/terraform/azure, nothing to scan" >&2; exit 1; }
	docker run --rm --network none --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges -e TRIVY_CACHE_DIR=/tmp/trivy --mount type=bind,source="$(CURDIR)/infra/terraform/azure",target=/work,readonly -w /work $(TRIVY_IMAGE) config --quiet --skip-check-update --skip-version-check --disable-telemetry --skip-dirs .terraform --skip-files azure.tfplan,terraform.tfstate,terraform.tfstate.backup --severity HIGH,CRITICAL --exit-code 1 .
