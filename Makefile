.PHONY: build up down restart logs deploy clean ps

# Build image (no cache, pull fresh base)
build:
	docker compose build --no-cache --pull

# Start all services in background
up:
	docker compose up -d

# Stop and remove containers (keep volumes)
down:
	docker compose down --remove-orphans

# Stop then start
restart: down up

# Follow web container logs
logs:
	docker compose logs -f web

# Full clean rebuild: stop → remove old image → build fresh → start → prune dangling
deploy:
	docker compose down --remove-orphans
	docker image rm frameiq-app:latest 2>/dev/null || true
	docker compose build --no-cache --pull
	docker compose up -d
	docker image prune -f
	@echo "Done. App running at https://frameiq.studio"

# Nuclear option: wipe containers + volumes (loses DB data)
clean:
	docker compose down --volumes --remove-orphans
	docker image rm frameiq-app:latest 2>/dev/null || true
	docker image prune -f

# Show running containers for this project
ps:
	docker compose ps
