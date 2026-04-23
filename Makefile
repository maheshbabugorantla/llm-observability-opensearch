.PHONY: up down clean test test-multiagent logs status dashboard verify build \
        logs-dataprepper logs-collector template

# Start the full OpenSearch observability stack
up: build
	docker compose up -d
	@echo "Waiting for services to be healthy..."
	@sleep 10
	@echo ""
	@echo "Stack is starting. Check status with: make status"
	@echo "OpenSearch Dashboards: http://localhost:5601"
	@echo "Flask App:             http://localhost:5001"
	@echo "OpenSearch API:        http://localhost:9200"

# Build the Flask app image
build:
	docker compose build flask-recipe-app

# Stop the stack
down:
	docker compose down

# Stop and remove all data
clean:
	docker compose down -v --remove-orphans

# Run API tests to generate traces
test:
	@echo "Running API tests to generate traces..."
	bash scripts/test-api.sh

# Run multi-agent workflow test
test-multiagent:
	@echo "Running multi-agent workflow test..."
	bash scripts/test-multiagent.sh

# Tail Flask app logs
logs:
	docker compose logs -f flask-recipe-app

# Show all container statuses
status:
	@echo "=== Container Status ==="
	docker compose ps
	@echo ""
	@echo "=== OpenSearch Cluster Health ==="
	@curl -s http://localhost:9200/_cluster/health | python3 -m json.tool 2>/dev/null || echo "OpenSearch not ready"
	@echo ""
	@echo "=== Data Prepper Pipelines ==="
	@curl -s http://localhost:4900/list/pipelines | python3 -m json.tool 2>/dev/null || echo "Data Prepper not ready"
	@echo ""
	@echo "=== Span Count ==="
	@curl -s http://localhost:9200/otel-v1-apm-span-*/_count 2>/dev/null | python3 -m json.tool 2>/dev/null || echo "No spans yet"

# Verify traces are being stored in OpenSearch
verify:
	bash scripts/verify-traces.sh

# Apply numeric field type overrides (run before first span is indexed)
template:
	bash scripts/setup-index-template.sh

# Import dashboards into OpenSearch Dashboards
dashboard:
	bash scripts/setup-dashboards.sh

# Show Data Prepper logs (useful for debugging)
logs-dataprepper:
	docker compose logs -f data-prepper

# Show OTel Collector logs
logs-collector:
	docker compose logs -f otel-collector
