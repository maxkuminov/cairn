FROM python:3.12-slim

WORKDIR /app

# Install system dependencies. curl is required for the compose healthcheck
# (GET /healthz). sqlite3 backs `make db-backup`, which runs the SQLite Online
# Backup API (`sqlite3 .backup`) inside this container for a consistent snapshot
# even under WAL with the app writing. The OTS notary subprocesses the `ots`
# CLI, which is provided by the `opentimestamps-client` pip package installed
# below — no apt package needed.
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    curl \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# Add non-root user (the app runs unprivileged; watched corpora are mounted ro)
RUN useradd -m -u 1000 -s /bin/bash appuser

# Install Python dependencies. This pulls in `opentimestamps-client`, which puts
# the `ots` CLI on PATH. De-risked: `ots --version` reports v0.7.2 and
# stamp/upgrade/verify work on Python 3.12 (see DESIGN.md §3 / CLAUDE.md). If you
# change the pin, re-run the smoke test before trusting proofs.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code. Alembic runs on startup when CAIRN_AUTO_MIGRATE=1.
COPY alembic.ini .
COPY alembic/ alembic/
COPY src/ src/

# Install the package itself (deps already installed above, so --no-deps) to put the `cairn`
# console script on PATH for `make shell` / `docker compose exec` ops (scan, accept, verify,
# import-manifest, ...). The uvicorn CMD continues to use /app/src directly.
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir --no-deps .

USER appuser

EXPOSE 8000

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
