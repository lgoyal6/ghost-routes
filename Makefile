# Ghost Routes developer interface. Every target is local-only and touches only this project.
SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

UV ?= uv
RUN := $(UV) run
MODEL ?= qwen2.5vl:7b
STATE := $(HOME)/.local/state/ghost-routes
PIDS := $(STATE)/run
WEB := apps/web

.PHONY: help setup model dev seed lint test test-integration test-live-model test-e2e benchmark verify demo stop

help: ## List targets
	@grep -E '^[a-z0-9-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-18s %s\n", $$1, $$2}'

setup: ## Locked dependencies, local .env and preflight
	$(UV) sync --frozen
	$(UV) run python scripts/gen_env.py
	@if [ -d $(WEB) ]; then (cd $(WEB) && npm ci); fi
	@command -v docker >/dev/null || { echo "docker is required for PostGIS"; exit 1; }
	@command -v ollama >/dev/null || echo "note: ollama not found; install it for live extraction (see README)"

model: ## Pull the ungated vision model and verify its digest (no account login)
	ollama pull $(MODEL)
	$(RUN) ghost-routes readiness

dev: ## Start PostGIS, apply migrations, start API and worker, print diagnostics
	docker compose up -d --wait db
	$(RUN) ghost-routes db upgrade
	@mkdir -p $(PIDS)
	@if ! kill -0 $$(cat $(PIDS)/api.pid 2>/dev/null) 2>/dev/null; then \
	  nohup $(RUN) ghost-routes serve >$(PIDS)/api.log 2>&1 & echo $$! >$(PIDS)/api.pid; fi
	@if ! kill -0 $$(cat $(PIDS)/worker.pid 2>/dev/null) 2>/dev/null; then \
	  nohup $(RUN) ghost-routes worker >$(PIDS)/worker.log 2>&1 & echo $$! >$(PIDS)/worker.pid; fi
	@if [ -d $(WEB) ] && ! kill -0 $$(cat $(PIDS)/web.pid 2>/dev/null) 2>/dev/null; then \
	  (cd $(WEB) && nohup npm run dev >$(PIDS)/web.log 2>&1 & echo $$! >$(PIDS)/web.pid); fi
	@sleep 2
	$(RUN) ghost-routes readiness

seed: ## Load the intentional synthetic demo (additive; never resets data)
	$(RUN) ghost-routes seed --namespace ashgrove

lint: ## Ruff and mypy (and the frontend's lint and typecheck once it exists)
	$(RUN) ruff check packages/backend tests scripts
	$(RUN) mypy packages/backend/src
	@if [ -d $(WEB) ]; then (cd $(WEB) && npm run lint && npm run typecheck); fi

test: ## Unit and property checks of the domain (no services)
	$(RUN) pytest tests/unit -q

test-integration: ## Real PostGIS, migrations, jobs and API (needs `make dev` or a running db)
	docker compose up -d --wait db
	$(RUN) pytest tests/integration -q -m integration

test-live-model: ## Real image inference with schema and evidence validation; fails if Ollama or the model is missing
	$(RUN) pytest tests/live -q -m live_model

test-e2e: ## Browser through the actual backend and database
	@[ -d $(WEB) ] || { echo "test-e2e: the web studio ($(WEB)) is not built yet"; exit 1; }
	cd $(WEB) && npx playwright test

benchmark: ## Fixed-manifest baseline evaluation
	$(RUN) ghost-routes benchmark

verify: lint test test-integration test-live-model test-e2e benchmark ## Every required check; fails if any is missing or skipped
	@echo "verify: all required checks passed"

demo: dev seed ## Verified local entrypoint plus model and source status
	@echo "API: http://127.0.0.1:$${GHOST_ROUTES_API_PORT:-8077}/docs"
	@[ -d $(WEB) ] && echo "Studio: http://127.0.0.1:$${GHOST_ROUTES_WEB_PORT:-5177}" || true

stop: ## Stop only this project's processes and containers
	@for p in web worker api; do \
	  if [ -f $(PIDS)/$$p.pid ]; then kill $$(cat $(PIDS)/$$p.pid) 2>/dev/null || true; rm -f $(PIDS)/$$p.pid; fi; done
	docker compose stop db
