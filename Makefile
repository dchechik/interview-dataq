.PHONY: help setup dev dev-api dev-web test lint fmt types build docker docker-run demo clean
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

clean: ## Remove build artefacts and local state
	rm -rf $(FRONTEND)/dist $(BACKEND)/.pytest_cache $(BACKEND)/.ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
