# Transcribe — local development
#
# The venv path is the primary one: it needs no docker socket access and an edit
# is picked up by a restart, not a rebuild. `make dev` uses WHISPER_MODEL=tiny so
# a transcription finishes in seconds instead of minutes.
#
# The docker path mirrors what actually ships, and needs your user in the docker
# group (`sudo usermod -aG docker $$USER`, then log out and back in).

PY      := .venv/bin/python
DATA    := .devdata
MODEL   ?= tiny
PORT    ?= 8011
THREADS ?= 2

DEV_ENV := DATA_DIR=$(DATA) WHISPER_MODEL=$(MODEL) WHISPER_COMPUTE=int8 \
           WHISPER_THREADS=$(THREADS) OMP_NUM_THREADS=$(THREADS) PORT=$(PORT)

.DEFAULT_GOAL := help
.PHONY: help venv dev smoke lint clean docker-dev docker-down docker-logs

help: ## Show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk -F':.*?## ' '{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

venv: ## Create .venv and install dependencies (python 3.11 via mise)
	@command -v mise >/dev/null || { echo "mise not found"; exit 1; }
	mise install
	mise exec -- python -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r requirements.txt

dev: ## Run the app locally on $(PORT) with the tiny model
	@test -x $(PY) || { echo "no venv yet — run 'make venv'"; exit 1; }
	@mkdir -p $(DATA)
	$(DEV_ENV) $(PY) app.py

smoke: ## End-to-end test: start, upload a generated clip, assert it transcribes
	@test -x $(PY) || { echo "no venv yet — run 'make venv'"; exit 1; }
	$(DEV_ENV) PY=$(PY) tests/smoke.sh

lint: ## Byte-compile the app and sanity-check the templates
	$(PY) -m py_compile app.py
	@for t in templates/*.html; do \
	  grep -q '{%' "$$t" || { echo "$$t: no jinja blocks, suspicious"; exit 1; }; \
	done
	@echo "lint ok"

docker-dev: ## Build and run via docker (needs docker group membership)
	docker compose -f compose.dev.yml up -d --build
	@echo "http://localhost:$(PORT)"

docker-logs: ## Follow the docker container's logs
	docker compose -f compose.dev.yml logs -f

docker-down: ## Stop the docker dev stack
	docker compose -f compose.dev.yml down

clean: ## Remove the venv, local data and caches
	rm -rf .venv $(DATA) __pycache__ app.pyc
	find . -name '__pycache__' -prune -exec rm -rf {} +
