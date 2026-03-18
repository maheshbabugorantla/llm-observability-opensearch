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

    # Refresh the index pattern field list so OSD discovers the actual field
    # types from OpenSearch (keyword, float, long, etc.). Without this step,
    # the index pattern has no field cache and Terms/Sum aggs show as invalid.
    #
    # OSD 2.x field refresh is a two-step process:
    #   1. GET _fields_for_wildcard  → discovers fields from OpenSearch mapping
    #   2. PUT saved_objects/index-pattern/{id}  → stores updated field list in OSD
    #
    # This only works after data has been indexed (index must exist).
    echo "Refreshing index pattern field list..."
    FIELDS_RAW=$(curl -s \
        "$DASHBOARDS_HOST/api/index_patterns/_fields_for_wildcard?pattern=otel-v1-apm-span-*&meta_fields=_source&meta_fields=_id&meta_fields=_type&meta_fields=_index&meta_fields=_score" \
        -H "osd-xsrf: true")

    # Build the PUT body: OSD saved_objects expects "fields" to be a JSON string
    # (the array double-serialized), not a raw JSON array.
    UPDATE_BODY=$(echo "$FIELDS_RAW" | python3 -c "
import sys, json
data = json.load(sys.stdin)
fields = data.get('fields', [])
if not fields:
    sys.exit(1)
# OSD stores fields as a serialized JSON string inside the attributes object
fields_str = json.dumps(fields, separators=(',', ':'))
body = json.dumps({'attributes': {'fields': fields_str}}, separators=(',', ':'))
print(body)
" 2>/dev/null || echo "")

    if [ -n "$UPDATE_BODY" ]; then
        UPDATE_RESP=$(curl -s -o /dev/null -w "%{http_code}" \
            -X PUT "$DASHBOARDS_HOST/api/saved_objects/index-pattern/otel-apm-span-pattern" \
            -H "osd-xsrf: true" \
            -H "Content-Type: application/json" \
            -d "$UPDATE_BODY")
        if [ "$UPDATE_RESP" = "200" ]; then
            echo "Field list refreshed successfully."
        else
            echo "Field list refresh returned HTTP $UPDATE_RESP (non-fatal — fields may show as unknown until data is indexed)."
        fi
    else
        echo "Field list refresh skipped — no index found yet (run 'make test' first, then 'make dashboard')."
    fi
    echo ""

    echo "Dashboard URL: $DASHBOARDS_HOST/app/dashboards#/view/llm-cost-dashboard"
    echo "Trace Analytics: $DASHBOARDS_HOST/app/observability-traces"
else
    echo "Import may have issues — check the response above."
    echo "You can retry with: make dashboard"
    exit 1
fi
