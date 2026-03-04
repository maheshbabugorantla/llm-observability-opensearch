#!/bin/bash
# Apply numeric field type overrides for otel-v1-apm-span-* indices.
#
# WHY THIS EXISTS:
#   Data Prepper's dynamic template maps ALL span.attributes.* fields as
#   `keyword` by default. Cost and token fields stored as `keyword` cannot
#   be aggregated (sum, avg) in OpenSearch Dashboards — they silently return 0.
#
# WHEN TO RUN:
#   BEFORE the first span is indexed. Run immediately after OpenSearch is
#   healthy, before Data Prepper starts writing. If spans already exist,
#   run `make clean && make up && make template` to start fresh.
#
# The template uses priority: 200 to override Data Prepper's built-in
# template (priority 100).

set -e

OS_HOST="${OS_HOST:-http://localhost:9200}"
TEMPLATE_NAME="otel-span-numeric-override"

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo "Waiting for OpenSearch to be ready..."
until curl -sf "$OS_HOST/_cluster/health" | grep -q '"status"'; do
    echo "  Not ready yet, retrying in 3s..."
    sleep 3
done
echo -e "${GREEN}OpenSearch is ready.${NC}"
echo ""

echo "Applying numeric field type override template: $TEMPLATE_NAME"
echo "(priority 200 — overrides Data Prepper default at priority 100)"
echo ""

RESPONSE=$(curl -s -w "\n%{http_code}" -X PUT "$OS_HOST/_index_template/$TEMPLATE_NAME" \
    -H "Content-Type: application/json" \
    -d '{
        "index_patterns": ["otel-v1-apm-span-*"],
        "priority": 200,
        "template": {
            "mappings": {
                "properties": {
                    "span": {
                        "properties": {
                            "attributes": {
                                "properties": {
                                    "gen_ai@cost@total_usd":    { "type": "float" },
                                    "gen_ai@cost@input_usd":    { "type": "float" },
                                    "gen_ai@cost@output_usd":   { "type": "float" },
                                    "gen_ai@usage@input_tokens":  { "type": "long" },
                                    "gen_ai@usage@output_tokens": { "type": "long" },
                                    "gen_ai@usage@total_tokens":  { "type": "long" }
                                }
                            }
                        }
                    }
                }
            }
        }
    }')

HTTP_CODE=$(echo "$RESPONSE" | tail -1)
BODY=$(echo "$RESPONSE" | head -1)

if [ "$HTTP_CODE" = "200" ]; then
    echo -e "${GREEN}Template applied successfully (HTTP 200).${NC}"
    echo ""
    echo "Fields now mapped as numeric types:"
    echo "  span.attributes.gen_ai@cost@total_usd    → float"
    echo "  span.attributes.gen_ai@cost@input_usd    → float"
    echo "  span.attributes.gen_ai@cost@output_usd   → float"
    echo "  span.attributes.gen_ai@usage@input_tokens  → long"
    echo "  span.attributes.gen_ai@usage@output_tokens → long"
    echo "  span.attributes.gen_ai@usage@total_tokens  → long"
    echo ""
    echo -e "${YELLOW}Next: run 'make test' to generate spans, then 'make dashboard'.${NC}"
else
    echo -e "${RED}Template apply failed (HTTP $HTTP_CODE).${NC}"
    echo "$BODY" | python3 -m json.tool 2>/dev/null || echo "$BODY"
    exit 1
fi
