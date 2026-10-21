# Cross-platform note: on Windows without `make`, run the commands directly (they are plain `python -m ...` invocations).
PY ?= python

.PHONY: install dev demo test test-fast test-all lint typecheck check evaluate eval security-test load-test chaos-test chaos production-check production \
        casestudy migrate api ui keygen docker-up

install:            ## runtime + dev dependencies
	$(PY) -m pip install -r requirements-dev.txt

dev:                ## API with auto-reload on :8000 (run `make ui` in a second terminal for the console). Development keys / data only.
	$(PY) -m uvicorn api.main:app --reload --port 8000

demo:               ## end-to-end commercial demo on SYNTHETIC data (tenant -> onboarding -> before/after -> approval -> mock ERP -> trace -> ROI -> reports)
	$(PY) -m enterprise.demo

test:               ## the whole suite except the slow QA matrix
	$(PY) -m pytest -m "not slow"

test-all:           ## everything incl. the 128-case QA matrix (about 2 minutes)
	$(PY) -m pytest

test-fast: test

lint:
	$(PY) -m ruff check .

typecheck:
	$(PY) -m mypy

check: lint typecheck test-all   ## what CI should run

evaluate:           ## 114-scenario benchmark + pre-registered 20-run business experiment + synthetic case studies (add LIVE=3 for 3 real-Ollama workflows)
	$(PY) -m evaluation.runner benchmark --report docs/EVALUATION_REPORT.md $(if $(LIVE),--live-llm $(LIVE),)
	$(PY) -m enterprise.experiments run
	$(PY) -m enterprise.casestudy --profile retailer
	$(PY) -m enterprise.casestudy --profile manufacturer
	$(PY) -m enterprise.casestudy --profile distributor
	$(PY) -m enterprise.casestudy --profile seasonal

eval: evaluate

security-test:      ## authN/Z, tenant isolation, injection, traversal, SSRF, webhook forgery/replay, abuse limits
	$(PY) -m pytest tests/test_security_review.py tests/test_platform_api.py tests/test_security.py tests/test_tenancy.py

load-test:          ## 10 / 50 / 100 concurrent workflows over real HTTP -> evaluation/reports/loadtest.json + docs/LOAD_TEST_REPORT.md (takes a few minutes)
	$(PY) -m evaluation.loadtest

chaos-test:         ## legacy engine fault matrix + platform failure suite (LLM/DB/ERP/supplier down, timeouts, duplicates, worker crash, partial dispatch, invalid data, DR)
	$(PY) -m evaluation.runner chaos
	$(PY) -m evaluation.failure_suite

chaos: chaos-test

production-check:   ## PASS / WARNING / FAIL for this deployment's configuration, data and evidence (exit 1 on FAIL)
	$(PY) -m scripts.production_readiness_check

production:         ## hardened LOCAL launcher: needs SCM_SECRET_KEY, refuses to start on FAIL. NOT a deployment (no TLS / HA) - see docs/DEPLOYMENT.md
	$(PY) -m scripts.run_production

casestudy:
	$(PY) -m enterprise.casestudy --profile $(or $(PROFILE),retailer)

migrate:
	$(PY) -m scm.migrations up

api:
	$(PY) -m uvicorn api.main:app --port 8000

ui:
	$(PY) -m streamlit run app.py

keygen:             ## make keygen ROLE=APPROVER ACTOR=alice   (legacy env keyring; tenant users get keys from POST /api/v1/admin/users/{id}/keys)
	$(PY) -m scm.security gen-key --role $(or $(ROLE),VIEWER) --actor $(or $(ACTOR),api-user)

docker-up:          ## development container only - not a validated production deployment
	docker compose up --build
