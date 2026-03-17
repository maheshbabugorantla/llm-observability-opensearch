#!/bin/bash
# Apply field type overrides for otel-v1-apm-span-* indices.
#
# WHY THIS EXISTS:
#   Data Prepper's dynamic template maps ALL span.attributes.* fields as
#   `keyword` by default. Two problems arise:
#
#   1. Cost/token fields stored as `keyword` cannot be aggregated (sum, avg)
#      in OpenSearch Dashboards — they silently return 0.
#      Fix: map them explicitly as float/long.
#
#   2. String fields like gen_ai@request@model are mapped as `text` by
#      Data Prepper's dynamic mapping (when data flows in before the template
#      is applied), which creates a text+keyword multi-field. OSD Terms
#      aggregations require the field to be directly aggregatable (keyword).
#      Fix: explicitly map them as `keyword` so OSD can use them directly
#      in Terms aggs without needing a `.keyword` subfield suffix.
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

echo "Applying field type override template: $TEMPLATE_NAME"
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
                                    "gen_ai@cost@total_usd":      { "type": "float" },
                                    "gen_ai@cost@input_usd":      { "type": "float" },
                                    "gen_ai@cost@output_usd":     { "type": "float" },
                                    "gen_ai@usage@input_tokens":  { "type": "long" },
                                    "gen_ai@usage@output_tokens": { "type": "long" },
                                    "gen_ai@usage@total_tokens":  { "type": "long" },
                                    "gen_ai@system":              { "type": "keyword" },
                                    "gen_ai@request@model":       { "type": "keyword" },
                                    "gen_ai@response@model":      { "type": "keyword" },
                                    "gen_ai@cost@provider":       { "type": "keyword" },
                                    "gen_ai@cost@model_resolved": { "type": "keyword" }
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
    echo "Fields now mapped as keyword (directly aggregatable in OSD Terms aggs):"
    echo "  span.attributes.gen_ai@system              → keyword"
    echo "  span.attributes.gen_ai@request@model       → keyword"
    echo "  span.attributes.gen_ai@response@model      → keyword"
    echo "  span.attributes.gen_ai@cost@provider       → keyword"
    echo "  span.attributes.gen_ai@cost@model_resolved → keyword"
    echo ""

    # ----------------------------------------------------------------
    # Fix pre-existing index created by Data Prepper ISM before this
    # template was applied. Data Prepper creates otel-v1-apm-span-000001
    # on startup using its own default template (all keyword). If that
    # index already exists with the wrong field types, we must delete it
    # and let Data Prepper recreate it using our priority-200 template.
    # ----------------------------------------------------------------
    echo "Checking whether existing span index needs to be recreated..."

    EXISTING_TYPE=$(curl -s "$OS_HOST/otel-v1-apm-span-000001/_mapping" | python3 -c "
import sys, json
try:
    m = json.load(sys.stdin)
    idx = list(m.keys())[0]
    attrs = (m[idx]['mappings']['properties']
               .get('span', {}).get('properties', {})
               .get('attributes', {}).get('properties', {}))
    print(attrs.get('gen_ai@cost@total_usd', {}).get('type', 'missing'))
except Exception:
    print('missing')
" 2>/dev/null)

    if [ "$EXISTING_TYPE" = "float" ]; then
        echo -e "${GREEN}Index mapping already correct (float). No action needed.${NC}"
    elif [ "$EXISTING_TYPE" = "missing" ]; then
        echo "No span index yet — template will apply when Data Prepper creates it."
    else
        echo -e "${YELLOW}Index has wrong field type ($EXISTING_TYPE). Recreating...${NC}"

        # Find the actual backing index via the alias (handles any rollover name)
        BACKING_INDEX=$(curl -s "$OS_HOST/_cat/aliases/otel-v1-apm-span?h=index" 2>/dev/null | head -1 | tr -d '[:space:]')
        if [ -z "$BACKING_INDEX" ]; then
            BACKING_INDEX="otel-v1-apm-span-000001"
        fi

        # Delete backing index (ISM alias goes with it)
        curl -s -X DELETE "$OS_HOST/$BACKING_INDEX" > /dev/null
        echo "  Deleted $BACKING_INDEX."

        # Restart Data Prepper so its ISM re-creates the index using our template.
        # Try docker restart; if unavailable, print a manual instruction.
        if docker restart data-prepper > /dev/null 2>&1; then
            echo "  Restarted data-prepper container."
        else
            echo -e "${YELLOW}  Could not restart data-prepper automatically.${NC}"
            echo "  Run: docker restart data-prepper"
            echo "  Then wait ~20 s before running 'make test'."
        fi

        # Wait for Data Prepper to come back and recreate the index
        echo "  Waiting for Data Prepper to reinitialize..."
        sleep 10
        until curl -s "$OS_HOST/otel-v1-apm-span-000001/_mapping" | python3 -c "
import sys, json
m = json.load(sys.stdin)
idx = list(m.keys())[0]
attrs = (m[idx]['mappings']['properties']
           .get('span', {}).get('properties', {})
           .get('attributes', {}).get('properties', {}))
t = attrs.get('gen_ai@cost@total_usd', {}).get('type', 'missing')
if t == 'float':
    sys.exit(0)
sys.exit(1)
" 2>/dev/null; do
            echo "  Waiting for index to be recreated..."
            sleep 5
        done

        echo -e "${GREEN}  Index recreated with correct field types (float/long/keyword).${NC}"
    fi
    echo ""
    echo -e "${YELLOW}Next: run 'make test' to generate spans, then 'make dashboard'.${NC}"
else
    echo -e "${RED}Template apply failed (HTTP $HTTP_CODE).${NC}"
    echo "$BODY" | python3 -m json.tool 2>/dev/null || echo "$BODY"
    exit 1
fi
