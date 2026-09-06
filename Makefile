.PHONY: install run run-proxy sync-client-config test lint typecheck full-check freeze

install:
	uv sync

run:
	uv run uvicorn main:create_app --factory \
	  --host $${ROUTER_SERVER_HOST:-127.0.0.1} \
	  --port $${ROUTER_SERVER_PORT:-8787} \
	  --app-dir src

run-proxy:
	ROUTER_PROXY_ENABLED=true PYTHONPATH=src uv run python -m entrypoint

# Regenerates ~/.claude/open-harness-router.settings.json from routing.yaml:
# the /model picker rows and the unknown-model window enforcement switch.
sync-client-config:
	PYTHONPATH=src uv run python -m cli.sync_client_config

test:
	uv run pytest -q

lint:
	uv run ruff check src tests

typecheck:
	uv run mypy src

full-check: lint typecheck test
