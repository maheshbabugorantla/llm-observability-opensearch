#!/bin/bash
# Import LLM observability dashboards into OpenSearch Dashboards.
# Imports: 1 index-pattern, 9 visualizations, 1 dashboard (11 saved objects total).

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DASHBOARDS_HOST="${DASHBOARDS_HOST:-http://localhost:5601}"
NDJSON_FILE="$SCRIPT_DIR/../dashboards/llm-cost-dashboard.ndjson"

# Wait for OpenSearch Dashboards to be ready
echo "Waiting for OpenSearch Dashboards to be ready..."
until curl -s "$DASHBOARDS_HOST/api/status" | grep -q '"overall"'; do
    echo "  Not ready yet, retrying in 5s..."
    sleep 5
done
echo "OpenSearch Dashboards is ready."
echo ""

# Import all saved objects (index-pattern + visualizations + dashboard)
echo "Importing LLM cost dashboard (11 saved objects)..."
RESPONSE=$(curl -s -w "\n%{http_code}" -X POST \
    "$DASHBOARDS_HOST/api/saved_objects/_import?overwrite=true" \
    -H "osd-xsrf: true" \
    --form "file=@$NDJSON_FILE")

HTTP_CODE=$(echo "$RESPONSE" | tail -1)
BODY=$(echo "$RESPONSE" | head -1)

echo "Response (HTTP $HTTP_CODE):"
echo "$BODY" | python3 -m json.tool 2>/dev/null || echo "$BODY"
echo ""

# Check success
SUCCESS=$(echo "$BODY" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('success','false'))" 2>/dev/null || echo "false")
COUNT=$(echo "$BODY" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('successCount',0))" 2>/dev/null || echo "0")

if [ "$SUCCESS" = "True" ] || [ "$SUCCESS" = "true" ]; then
    echo "Import successful: $COUNT objects imported."
    echo ""
    echo "Dashboard URL: $DASHBOARDS_HOST/app/dashboards#/view/llm-cost-dashboard"
    echo "Trace Analytics: $DASHBOARDS_HOST/app/observability-traces"
else
    echo "Import may have issues — check the response above."
    echo "You can retry with: make dashboard"
    exit 1
fi
