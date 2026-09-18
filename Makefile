.PHONY: up down demo test lint typecheck check

up:
	cd deploy && docker compose up -d
	@echo "Grafana:    http://localhost:3001"
	@echo "Tempo:      http://localhost:3200"
	@echo "Prometheus: http://localhost:9090"

down:
	cd deploy && docker compose down

demo:
	OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 uv run uvicorn agenttrace.harness.server:app --port 8765 --log-level warning & \
	echo $$! > /tmp/agenttrace_demo_server.pid; \
	sleep 2; \
	OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 uv run python -m agenttrace.demo \
		--fanout 3 --depth 2 --runner-plan inproc,subprocess,http --http-base-url http://127.0.0.1:8765 \
		| tee /tmp/agenttrace_demo_output.json; \
	kill `cat /tmp/agenttrace_demo_server.pid` 2>/dev/null; \
	TRACE_ID=`uv run python -c "import json;print(json.load(open('/tmp/agenttrace_demo_output.json'))['trace_id'])"`; \
	sleep 2; \
	uv run python -m agenttrace.verify --trace-id $$TRACE_ID

test:
	uv run pytest -q

lint:
	uv run ruff check src/agenttrace tests

typecheck:
	uv run mypy

check: lint typecheck test
