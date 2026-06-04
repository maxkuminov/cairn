# Cairn — file-integrity monitor + OpenTimestamps notary
# Manages build, deploy, and database (SQLite) operations.

# Optional host-specific overrides (gitignored). Set DEPLOY_DIR / DATA_DIR /
# REGISTRY here to deploy from a directory outside the repo.
-include Makefile.local

IMAGE_NAME := cairn
IMAGE_TAG := latest
REGISTRY ?= localhost:5000
FULL_IMAGE := $(REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG)
DEPLOY_DIR ?= .
DATA_DIR ?= ./data
PROOFS_DIR ?= ./proofs
COMPOSE_FILE := $(DEPLOY_DIR)/docker-compose.yml
ENV_FILE := $(DEPLOY_DIR)/.env
COMPOSE := docker compose --project-directory $(DEPLOY_DIR) -f $(COMPOSE_FILE)

GREEN := \033[0;32m
YELLOW := \033[0;33m
RED := \033[0;31m
NC := \033[0m

.PHONY: help init build build-cached push image deploy up down restart logs shell migrate db-backup status clean audit trivy

help:
	@echo "$(GREEN)Cairn — integrity monitor + OTS notary$(NC)"
	@echo ""
	@echo "$(YELLOW)Setup:$(NC)"
	@echo "  make init         - Initial setup (directories, .env with generated secret)"
	@echo ""
	@echo "$(YELLOW)Build & Deploy:$(NC)"
	@echo "  make build        - Build Docker image (no cache)"
	@echo "  make build-cached - Build Docker image (with cache)"
	@echo "  make push         - Push image to registry"
	@echo "  make image        - Build, scan, and push"
	@echo "  make deploy       - Full deploy (build, scan, push, backup, recreate)"
	@echo ""
	@echo "$(YELLOW)Container Management:$(NC)"
	@echo "  make up           - Start container"
	@echo "  make down         - Stop container"
	@echo "  make restart      - Restart container"
	@echo "  make logs         - Tail container logs"
	@echo "  make shell        - Shell into container"
	@echo ""
	@echo "$(YELLOW)Database (SQLite — Alembic auto-migrates on startup):$(NC)"
	@echo "  make migrate      - Run Alembic migrations manually (alembic upgrade head)"
	@echo "  make db-backup    - Consistent online backup of the SQLite DB (gzipped)"
	@echo ""
	@echo "$(YELLOW)Operations:$(NC)"
	@echo "  make status       - Show container and health status"
	@echo "  make clean        - Remove containers and images (data preserved)"
	@echo ""
	@echo "$(YELLOW)Security:$(NC)"
	@echo "  make audit        - Audit Python deps (pip-audit)"
	@echo "  make trivy        - Scan local image for HIGH/CRITICAL CVEs"

init:
	@echo "$(GREEN)Setting up Cairn...$(NC)"
	@sudo mkdir -p $(DATA_DIR)/backups $(PROOFS_DIR)
	@sudo chown -R $(shell id -u):$(shell id -g) $(DATA_DIR) $(PROOFS_DIR)
	@sudo chmod -R 775 $(DATA_DIR) $(PROOFS_DIR)
	@if [ ! -f "$(ENV_FILE)" ]; then \
		echo "$(GREEN)Creating $(ENV_FILE) from template...$(NC)"; \
		cp .env.example $(ENV_FILE); \
		SECRET=$$(openssl rand -hex 32); \
		sed -i "s/^CAIRN_SECRET_KEY=.*/CAIRN_SECRET_KEY=$$SECRET/" $(ENV_FILE); \
		echo "$(GREEN)$(ENV_FILE) created with a generated CAIRN_SECRET_KEY$(NC)"; \
	else \
		echo "$(YELLOW)$(ENV_FILE) already exists$(NC)"; \
	fi
	@echo "$(GREEN)Setup complete.$(NC)"
	@echo "$(YELLOW)Next:$(NC) edit $(ENV_FILE) and the corpus mounts in $(COMPOSE_FILE), then: make deploy"

build:
	@echo "$(GREEN)Building image (no cache)...$(NC)"
	docker build --no-cache --pull -f Dockerfile -t $(IMAGE_NAME):$(IMAGE_TAG) .
	@echo "$(GREEN)Built: $(IMAGE_NAME):$(IMAGE_TAG)$(NC)"

build-cached:
	@echo "$(GREEN)Building image (cached)...$(NC)"
	docker build -f Dockerfile -t $(IMAGE_NAME):$(IMAGE_TAG) .
	@echo "$(GREEN)Built: $(IMAGE_NAME):$(IMAGE_TAG)$(NC)"

push:
	@echo "$(GREEN)Pushing to registry...$(NC)"
	docker tag $(IMAGE_NAME):$(IMAGE_TAG) $(FULL_IMAGE)
	docker push $(FULL_IMAGE)
	@echo "$(GREEN)Pushed: $(FULL_IMAGE)$(NC)"

trivy:
	@echo "$(GREEN)Scanning $(IMAGE_NAME):$(IMAGE_TAG) for HIGH/CRITICAL CVEs...$(NC)"
	@trivy image --severity HIGH,CRITICAL --exit-code 1 --ignore-unfixed --no-progress --scanners vuln $(IMAGE_NAME):$(IMAGE_TAG)
	@echo "$(GREEN)No fixable HIGH/CRITICAL CVEs$(NC)"

image: build trivy push

deploy: image
	@echo "$(GREEN)Deploying Cairn...$(NC)"
	@$(MAKE) db-backup 2>/dev/null || true
	$(COMPOSE) up -d --force-recreate
	@docker image prune -f
	@docker builder prune -f --filter until=168h
	@HOST=$$(grep -E '^CAIRN_HOSTNAME=' $(ENV_FILE) 2>/dev/null | cut -d= -f2); \
	echo "$(GREEN)Deployed! https://$${HOST:-localhost}$(NC)"

up:
	$(COMPOSE) up -d

down:
	$(COMPOSE) down

restart:
	$(COMPOSE) restart cairn

logs:
	$(COMPOSE) logs -f --tail=100 cairn

shell:
	$(COMPOSE) exec cairn bash

migrate:
	@echo "$(GREEN)Running migrations...$(NC)"
	$(COMPOSE) exec cairn alembic upgrade head
	@echo "$(GREEN)Migrations complete$(NC)"

db-backup:
	@mkdir -p $(DATA_DIR)/backups 2>/dev/null || true
	@TIMESTAMP=$$(date +%Y%m%d_%H%M%S); \
	BACKUP_FILE="$(DATA_DIR)/backups/cairn_$$TIMESTAMP.db"; \
	CONTAINER_BACKUP="/app/data/backups/cairn_$$TIMESTAMP.db"; \
	docker exec cairn sqlite3 /app/data/cairn.db ".backup '$$CONTAINER_BACKUP'" 2>/dev/null \
		|| cp $(DATA_DIR)/cairn.db $$BACKUP_FILE 2>/dev/null \
		|| true; \
	gzip -f $$BACKUP_FILE 2>/dev/null || true; \
	echo "$(GREEN)Backup: $$BACKUP_FILE.gz$(NC)"

status:
	@echo "$(GREEN)Cairn Status:$(NC)"
	@$(COMPOSE) ps
	@echo ""
	@echo "$(GREEN)Health:$(NC)"
	@HOST=$$(grep -E '^CAIRN_HOSTNAME=' $(ENV_FILE) 2>/dev/null | cut -d= -f2); \
	URL=$${HOST:+https://$$HOST/healthz}; \
	URL=$${URL:-http://localhost:8000/healthz}; \
	curl -s $$URL | python3 -m json.tool 2>/dev/null || echo "$(RED)Not responding$(NC)"

clean: down
	docker rmi $(IMAGE_NAME):$(IMAGE_TAG) $(FULL_IMAGE) 2>/dev/null || true
	@echo "$(GREEN)Cleaned. Data in $(DATA_DIR) and proofs in $(PROOFS_DIR) preserved.$(NC)"

audit:
	pip-audit -r requirements.txt
