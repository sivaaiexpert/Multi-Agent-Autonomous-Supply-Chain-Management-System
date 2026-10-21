# Deployment

> **Status:** `Dockerfile` and `docker-compose.yml` are syntactically valid (the compose file was parsed) but **were not built or run here** (Docker is not installed in the authoring environment). The API, console, migrations and key tooling *were* exercised directly (see the smoke test in `PRODUCTION_READINESS.md`).

## Bare metal / VM
```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env        # edit; never commit
python -m scm.migrations up
python -m scm.security gen-key --role ADMIN --actor ops    # put the printed SCM_API_KEYS entry into .env
uvicorn api.main:app --host 0.0.0.0 --port 8000
streamlit run app.py --server.port 8501 --server.headless true
```
Put both behind a reverse proxy that terminates TLS, applies rate limiting and (for the console) authentication.

## Docker Compose
```bash
cp .env.example .env
docker compose up --build                  # api :8000, console :8501
docker compose --profile llm up --build    # + local Ollama
docker compose exec ollama ollama pull llama3.2
```
* One image, two commands. Non-root user (uid 10001); the only writable path is the `/data` volume.
* API and console share the SQLite file via the `scm-data` volume. This works on a **local** volume; do not put SQLite on NFS/SMB.
* Health checks: API `/ready`, console `/_stcore/health`; the console waits for a healthy API.

## Configuration
`.env.example` documents every variable. Secrets (API key hashes, webhook secrets) are never in source control; webhook secrets are held in process memory only.

## Release checklist
1. `make check` (ruff + mypy + full pytest incl. QA matrix).
2. `make eval` and commit the refreshed `docs/EVALUATION_REPORT.md`.
3. `python -m scm.migrations up` on a copy of production data; run `GET /ready`.
4. Back up the DB; deploy; verify `/ready`, `/api/v1/audit?verify=true`.

## Rollback
Application: redeploy the previous image. Schema: `python -m scm.migrations down 1` (⚠ drops v2 tables including the audit log — export it first). Prefer rolling forward.
