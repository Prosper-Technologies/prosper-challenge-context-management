# Prosper Challenge — run everything from the repo root.

VENV := backend/.venv
PYTHON := $(VENV)/bin/python

GENERATED := frontend/src/generated
JSON2TS := npm --prefix frontend exec -- json2ts --unreachableDefinitions --unknownAny --no-additionalProperties

.PHONY: help install lock dev run web test eval types check-types clean

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

install: ## Create the venv, install backend + frontend dependencies, create backend/.env
	python3 -m venv $(VENV)
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -r backend/requirements-dev.lock
	npm --prefix frontend ci
	@test -f backend/.env || (cp backend/.env.example backend/.env && \
		echo "Created backend/.env: add your OpenAI and ElevenLabs API keys to it.")

lock: ## Re-resolve the Python lock from requirements*.txt (needs uv; review the diff)
	cd backend && uv pip compile --universal --python-version 3.11 \
		requirements-dev.txt -o requirements-dev.lock

dev: ## Run the backend and the builder together; open http://localhost:5173 (Ctrl+C stops both)
	@$(MAKE) --no-print-directory -j2 run web

run: ## Run the voice agent + builder API (prebuilt client at http://localhost:7860/client)
	@test -f backend/.env || echo "warning: no backend/.env, so test calls will fail (see backend/.env.example)"
	$(PYTHON) backend/bot.py

web: ## Run the builder frontend (http://localhost:5173; needs `make run`)
	npm --prefix frontend run dev

test: ## Run the backend and frontend tests
	cd backend && .venv/bin/python -m pytest -q
	npm --prefix frontend test

eval: ## Run the naive vs tools benchmark (calls the OpenAI API: needs OPENAI_API_KEY and costs usage); options via EVAL_ARGS="--k 1 --scenarios chen_paging"
	cd backend && .venv/bin/python -W ignore -m eval $(EVAL_ARGS)

types: ## Regenerate the frontend's TypeScript types from the backend's JSON Schema
	cd backend && .venv/bin/python -W ignore contract.py > ../$(GENERATED)/contract.schema.json
	$(JSON2TS) -i $(GENERATED)/contract.schema.json -o $(GENERATED)/contract.ts

check-types: ## Fail if the committed schema or TypeScript types are out of date
	@tmp=$$(mktemp -d) && \
	(cd backend && .venv/bin/python -W ignore contract.py) > $$tmp/contract.schema.json && \
	$(JSON2TS) -i $$tmp/contract.schema.json -o $$tmp/contract.ts && \
	diff -u $(GENERATED)/contract.schema.json $$tmp/contract.schema.json && \
	diff -u $(GENERATED)/contract.ts $$tmp/contract.ts && \
	echo "Generated types are up to date."

clean: ## Remove the venv and Python caches
	rm -rf $(VENV) frontend/node_modules frontend/dist
	find backend -type d -name __pycache__ -prune -exec rm -rf {} +
