# ── FrameIQ VPS operations ────────────────────────────────────────────────────
# Run from /opt/frameiq on the VPS.

COMPOSE = docker compose
APP_CONTAINER = frameiq-web-1
DB_CONTAINER = frameiq-db-1

.PHONY: deploy up down restart logs backup shell db-shell ssl migrate

# ── Deploy ────────────────────────────────────────────────────────────────────
deploy:
	git pull origin main
	$(COMPOSE) up -d --build
	@echo "→ Deployed. Check: $(MAKE) status"

up:
	$(COMPOSE) up -d --build

down:
	$(COMPOSE) down

restart:
	$(COMPOSE) restart web

# ── Logs ──────────────────────────────────────────────────────────────────────
logs:
	$(COMPOSE) logs -f --tail=100 web

logs-db:
	$(COMPOSE) logs -f --tail=50 db

logs-nginx:
	$(COMPOSE) logs -f --tail=50 nginx

# ── Database ──────────────────────────────────────────────────────────────────
backup:
	@bash scripts/backup.sh

db-shell:
	$(COMPOSE) exec db psql -U postgres frameiq

migrate:
	$(COMPOSE) exec web python scripts/migrate.py status

# Apply schema changes. NEVER calls db.create_all(): application startup and
# this target alike must not create tables outside the migration ledger, which
# would make `migrate.py status` unable to say which migration produced what.
migrate-upgrade:
	$(COMPOSE) exec web python scripts/migrate.py upgrade

# ── SSL (one-time setup) ──────────────────────────────────────────────────────
ssl:
	@echo "1. Make sure ports 80 + 443 are open"
	@echo "2. Run: $(COMPOSE) stop nginx"
	@echo "3. Run: certbot certonly --standalone -d frameiq.studio -d www.frameiq.studio"
	@echo "4. Copy certs: cp /etc/letsencrypt/live/frameiq.studio/fullchain.pem nginx/ssl/"
	@echo "              cp /etc/letsencrypt/live/frameiq.studio/privkey.pem nginx/ssl/"
	@echo "5. Uncomment HTTPS block in nginx/nginx.conf"
	@echo "6. Run: $(COMPOSE) up -d nginx"

# ── Status ────────────────────────────────────────────────────────────────────
status:
	$(COMPOSE) ps
	@echo ""
	@curl -s -o /dev/null -w "Health: %{http_code}" http://localhost:8002/agent_health || echo "Health: unreachable"
	@echo ""
