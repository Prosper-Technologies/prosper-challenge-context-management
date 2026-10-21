# Prosper Challenge — run everything from the repo root.

VENV := backend/.venv
PYTHON := $(VENV)/bin/python

.PHONY: help install run web test clean

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

install: ## Create the venv and install backend + frontend dependencies
	python3 -m venv $(VENV)
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -r backend/requirements-dev.txt
	npm --prefix frontend ci

run: ## Run the voice agent + builder API (prebuilt client at http://localhost:7860/client)
	$(PYTHON) backend/bot.py

web: ## Run the builder frontend (http://localhost:5173; needs `make run`)
	npm --prefix frontend run dev

test: ## Run the backend tests
	cd backend && .venv/bin/python -m pytest -q

clean: ## Remove the venv and Python caches
	rm -rf $(VENV) frontend/node_modules frontend/dist
	find backend -type d -name __pycache__ -prune -exec rm -rf {} +
