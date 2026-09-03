.DEFAULT_GOAL := help
PY := .venv/bin/python
PIP := .venv/bin/pip

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "};{printf "  \033[1m%-18s\033[0m %s\n",$$1,$$2}'

venv: ## Create the virtualenv and install everything
	python3 -m venv .venv && $(PIP) install -q --upgrade pip && $(PIP) install -q -e ".[dev]"

db-up: ## Start Postgres and Redis only
	docker compose up -d postgres redis

db-wait: ## Block until Postgres is accepting connections
	@until docker compose exec -T postgres pg_isready -U anvil -d anvil >/dev/null 2>&1; do sleep 1; done; echo "postgres ready"

migrate: ## Apply migrations
	.venv/bin/alembic upgrade head

seed: ## Seed a deterministic demo world
	$(PY) -m anvil.simulator.seed

demo: db-up db-wait migrate seed ## Boot the full offline demo
	docker compose up -d
	@echo "console  http://localhost:3000"
	@echo "api docs http://localhost:8000/docs"

SEED ?= 20260902
SIZE ?= 3000

batch: ## Run the seeded batch experiment and print the evidence report
	$(PY) -m anvil.evidence.run_batch --seed $(SEED) --size $(SIZE)

batch-with-model: ## Same batch, with the LLM classifier modelled as available
	$(PY) -m anvil.evidence.run_batch --seed $(SEED) --size $(SIZE) --with-model

tour: ## A guided tour of everything the system does, in one terminal run
	$(PY) scripts/tour.py

console: ## Serve the web console at http://localhost:8000 (no database, no keys)
	.venv/bin/uvicorn anvil.main_api:app --port 8000 --reload

mcp-token: ## Print the Basic auth token for Razorpay's remote MCP server
	@$(PY) scripts/mcp_token.py

mcp-check: ## Confirm the Razorpay MCP server accepts our credentials
	@$(PY) scripts/mcp_token.py > /dev/null && \
	 curl -sS -o /dev/null -w "mcp.razorpay.com -> HTTP %{http_code}\n" \
	   -X POST https://mcp.razorpay.com/mcp \
	   -H "Authorization: Basic $$($(PY) scripts/mcp_token.py)" \
	   -H "Content-Type: application/json" \
	   -H "Accept: application/json, text/event-stream" \
	   -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'

test: ## Unit tests, no database required
	$(PY) -m pytest tests/unit -q

test-all: db-up db-wait migrate ## Every test, including integration and e2e
	$(PY) -m pytest -q

invariants: ## Run only the financial invariant tests
	$(PY) -m pytest -q -m invariant

lint: ## Ruff and mypy
	.venv/bin/ruff check anvil tests && .venv/bin/ruff format --check anvil tests && .venv/bin/mypy anvil

shellcheck: ## Lint the deployment scripts
	shellcheck deploy/*.sh

docs: ## Regenerate the generated documentation
	$(PY) scripts/gen_config_docs.py

fmt: ## Autoformat
	.venv/bin/ruff format anvil tests && .venv/bin/ruff check --fix anvil tests

down: ## Stop everything
	docker compose down

clean: ## Stop everything and delete the database volume
	docker compose down -v

.PHONY: help venv db-up db-wait migrate seed demo batch batch-with-model tour console mcp-token mcp-check test test-all invariants lint fmt shellcheck docs down clean
