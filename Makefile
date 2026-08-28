.PHONY: help setup dev dev-api dev-web test lint fmt types build docker docker-run demo clean \
        railway-deploy railway-logs data-size data-bundle data-push data-replace data-pull data-reset
.DEFAULT_GOAL := help

BACKEND := backend
FRONTEND := frontend
UV := uv --directory $(BACKEND)

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

setup: ## Install all dependencies (uv resolves Python itself; Node must be installed)
	$(UV) sync --extra dev
	cd $(FRONTEND) && npm ci

dev: ## Run backend (:8000) and frontend (:5173) together
	@echo "api  -> http://127.0.0.1:8000/api/health"
	@echo "web  -> http://127.0.0.1:5173"
	@trap 'kill 0' EXIT INT TERM; \
	  $(MAKE) dev-api & $(MAKE) dev-web & wait

dev-api: ## Run only the backend, with reload
	$(UV) run uvicorn dataq.api.app:app --reload --host 127.0.0.1 --port 8000

dev-web: ## Run only the Vite dev server
	cd $(FRONTEND) && npm run dev

test: ## Run the backend test suite (both storage backends)
	$(UV) run pytest -q

lint: ## Lint and type-check both sides
	$(UV) run ruff check src tests
	$(UV) run mypy src || true
	cd $(FRONTEND) && npm run typecheck && npm run lint

fmt: ## Auto-fix lint issues
	$(UV) run ruff check --fix src tests
	$(UV) run ruff format src tests

types: ## Regenerate frontend types from the live OpenAPI document
	@echo "requires 'make dev-api' running in another shell"
	cd $(FRONTEND) && npm run gen:types

build: ## Build the production frontend bundle
	cd $(FRONTEND) && npm run build

docker: ## Build the single-container image
	docker build -t dataq:latest .

docker-run: ## Run the image with a local data volume
	docker run --rm -p 8000:8000 -v $$(pwd)/data:/data \
		-e DATAQ_STORAGE=$${DATAQ_STORAGE:-parquet} dataq:latest

demo: ## Generate sample taxi + auth datasets under ./sample-data
	$(UV) run python -c "import sys; sys.path.insert(0,'.'); \
from tests.fixtures import write_taxi_csv, write_auth_csv; from pathlib import Path; \
d=Path('../sample-data'); print(write_taxi_csv(d/'taxi.csv', rows=20000)); \
print(write_auth_csv(d/'auth.csv', rows=15000))"

# --- Railway ----------------------------------------------------------------
# Data lives on a Railway volume mounted at /data, so it survives code deploys.
# These targets move data between that volume and ./data on your laptop.
BUNDLE := dataq-data.tgz

railway-deploy: ## Build and deploy the current code to Railway
	railway up

railway-logs: ## Tail the deployed service's logs
	railway logs

data-size: ## Measure ./data against the volume you are paying for
	@echo "Railway volumes: 0.5GB trial, 5GB hobby, 50GB pro"
	@test -d data && du -sh data || echo "no ./data yet - run 'make demo' or import something"
	@test -d data && du -sh data/* 2>/dev/null | sort -h || true

data-bundle: ## Tar ./data into $(BUNDLE)
	@test -d data || { echo "no ./data to bundle"; exit 1; }
	tar czf $(BUNDLE) -C data .
	@ls -lh $(BUNDLE)

data-push: data-bundle ## Ship ./data to Railway, merging into what is there
	railway volume files upload ./$(BUNDLE) /_inbox/$(BUNDLE)
	railway restart
	@echo "pushed. the entrypoint unpacks it before the server starts."

data-replace: ## Ship ./data to Railway, REPLACING everything on the volume
	@test -d data || { echo "no ./data to bundle"; exit 1; }
	tar czf dataq-data.replace.tgz -C data .
	@ls -lh dataq-data.replace.tgz
	@echo "'.replace.' in the name tells the entrypoint to wipe /data first."
	railway volume files upload ./dataq-data.replace.tgz /_inbox/dataq-data.replace.tgz
	railway restart

data-pull: ## Download the deployed data to ./data-from-railway
	@echo "note: catalog.sqlite is in WAL mode. For a guaranteed-clean copy,"
	@echo "      pause the service first. The parquet lake is immutable and"
	@echo "      safe to copy while running."
	mkdir -p data-from-railway
	railway volume files download / ./data-from-railway

data-reset: ## Show how to wipe the deployed data (you must run it yourself)
	@echo "Two ways to reset the deployed data:"
	@echo
	@echo "  1. Ship a fresh copy over the top of it:"
	@echo "       make data-replace"
	@echo
	@echo "  2. Delete the files on the volume directly:"
	@echo "       railway volume files delete /catalog.sqlite"
	@echo "       railway volume files delete /warehouse.duckdb"
	@echo "       railway volume files delete /lake"
	@echo "       railway restart"
	@echo
	@echo "Option 2 is not scripted here on purpose: 'railway volume files"
	@echo "delete' refuses to run when an AI agent invokes it, and a target"
	@echo "that silently fails for me but works for you is worse than none."

clean: ## Remove build artefacts and local state
	rm -rf $(FRONTEND)/dist $(BACKEND)/.pytest_cache $(BACKEND)/.ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -f $(BUNDLE) dataq-data.replace.tgz
