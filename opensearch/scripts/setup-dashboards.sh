#!/bin/bash
# Set up custom LLM observability dashboards in OpenSearch Dashboards

set -e

DASHBOARDS_HOST="http://localhost:5601"
OS_HOST="http://localhost:9200"
OSD_API="$DASHBOARDS_HOST/api"

echo "Waiting for OpenSearch Dashboards to be ready..."
until curl -s "$DASHBOARDS_HOST/api/status" | grep -q '"overall"'; do
    echo "  Waiting..."
    sleep 5
done
echo "OpenSearch Dashboards is ready!"

# 1. Create index pattern for trace spans
echo "Creating index pattern for otel-v1-apm-span-*..."
curl -s -X POST "$OSD_API/saved_objects/index-pattern/otel-v1-apm-span-*" \
    -H "osd-xsrf: true" \
    -H "Content-Type: application/json" \
    -d '{
        "attributes": {
            "title": "otel-v1-apm-span-*",
            "timeFieldName": "startTime"
        }
    }' | python3 -m json.tool
echo ""

# 2. Create index pattern for service map
echo "Creating index pattern for otel-v1-apm-service-map..."
curl -s -X POST "$OSD_API/saved_objects/index-pattern/otel-v1-apm-service-map" \
    -H "osd-xsrf: true" \
    -H "Content-Type: application/json" \
    -d '{
        "attributes": {
            "title": "otel-v1-apm-service-map",
            "timeFieldName": "startTime"
        }
    }' | python3 -m json.tool
echo ""

# 3. Import custom dashboard if NDJSON file exists
if [ -f "dashboards/llm-cost-dashboard.ndjson" ]; then
    echo "Importing LLM cost dashboard..."
    curl -s -X POST "$OSD_API/saved_objects/_import?overwrite=true" \
        -H "osd-xsrf: true" \
        --form file=@dashboards/llm-cost-dashboard.ndjson \
        | python3 -m json.tool
    echo ""
fi

echo "Dashboard setup complete!"
echo "Access OpenSearch Dashboards at: $DASHBOARDS_HOST"
echo "  - Trace Analytics: $DASHBOARDS_HOST/app/observability-traces"
echo "  - Discover:        $DASHBOARDS_HOST/app/discover"
