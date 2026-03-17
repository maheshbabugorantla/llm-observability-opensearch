#!/bin/bash
# Apply field type overrides for otel-v1-apm-span-* indices.
#
# WHY THIS EXISTS:
#   Data Prepper's legacy _template uses a dynamic_template rule that maps
#   ALL span.attributes.* fields as `keyword`. Two problems arise:
#
#   1. Cost/token fields stored as `keyword` cannot be aggregated (sum, avg)
#      in OpenSearch Dashboards — they silently return 0.
#      Fix: map them explicitly as float/long.
#
#   2. String fields like gen_ai@request@model arrive as `text` via dynamic
#      mapping, creating a text+keyword multi-field. OSD Terms aggregations
#      require a directly aggregatable `keyword` field.
#      Fix: explicitly map them as `keyword`.
#
# HOW IT WORKS:
#   OpenSearch composable index templates (_index_template) do NOT reliably
#   override legacy template (_template) dynamic_templates in OSD 2.17.1.
#   Instead, we directly PUT the explicit field mappings onto the index
#   while it is still empty (no spans indexed yet). Explicit property
#   mappings always win over dynamic_templates within the same index.
#
# WHEN TO RUN:
#   Part of `make up` — runs automatically after the stack starts.
#   Must complete before `make test` (before any spans are indexed).

set -e

OS_HOST="${OS_HOST:-http://localhost:9200}"
BACKING_INDEX="otel-v1-apm-span-000001"

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

FIELD_MAPPINGS='{
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
}'

# ── helpers ──────────────────────────────────────────────────────────────────

get_field_type() {
    curl -s "$OS_HOST/$BACKING_INDEX/_mapping" | python3 -c "
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
" 2>/dev/null
}

get_doc_count() {
    curl -s "$OS_HOST/$BACKING_INDEX/_count" | \
        python3 -c "import sys,json; print(json.load(sys.stdin).get('count',0))" 2>/dev/null || echo "0"
}

apply_mappings() {
    local RESP
    RESP=$(curl -s -o /dev/null -w "%{http_code}" -X PUT "$OS_HOST/$BACKING_INDEX/_mapping" \
        -H "Content-Type: application/json" \
        -d "$FIELD_MAPPINGS")
    echo "$RESP"
}

delete_and_restart() {
    # Find actual backing index (handles rollover)
    local IDX
    IDX=$(curl -s "$OS_HOST/_cat/aliases/otel-v1-apm-span?h=index" 2>/dev/null | head -1 | tr -d '[:space:]')
    [ -z "$IDX" ] && IDX="$BACKING_INDEX"

    curl -s -X DELETE "$OS_HOST/$IDX" > /dev/null
    echo "  Deleted $IDX."

    if docker restart data-prepper > /dev/null 2>&1; then
        echo "  Restarted data-prepper container."
    else
        echo -e "${YELLOW}  Could not restart data-prepper automatically.${NC}"
        echo "  Run: docker restart data-prepper"
        echo "  Then wait ~20 s before running 'make test'."
    fi
}

wait_for_index() {
    # Poll until the backing index exists (max 90 s)
    for i in $(seq 1 30); do
        STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$OS_HOST/$BACKING_INDEX/_mapping")
        [ "$STATUS" = "200" ] && return 0
        echo "  Waiting for Data Prepper to create the index... (${i}0s)"
        sleep 3
    done
    return 1
}

# ── Step 1: wait for OpenSearch ───────────────────────────────────────────────

echo "Waiting for OpenSearch to be ready..."
until curl -sf "$OS_HOST/_cluster/health" | grep -q '"status"'; do
    echo "  Not ready yet, retrying in 3s..."
    sleep 3
done
echo -e "${GREEN}OpenSearch is ready.${NC}"
echo ""

# ── Step 2: wait for Data Prepper to create the backing index ─────────────────

echo "Waiting for Data Prepper to create the span index..."
if ! wait_for_index; then
    echo -e "${RED}Index not created within 90 s. Is Data Prepper running?${NC}"
    echo "Run 'docker compose logs data-prepper' to diagnose."
    exit 1
fi
echo -e "${GREEN}Index $BACKING_INDEX exists.${NC}"
echo ""

# ── Step 3: check current field type and fix ──────────────────────────────────

echo "Checking field type for gen_ai@cost@total_usd..."
FIELD_TYPE=$(get_field_type)

if [ "$FIELD_TYPE" = "float" ]; then
    echo -e "${GREEN}Field types already correct (float/long/keyword). Nothing to do.${NC}"

elif [ "$FIELD_TYPE" = "missing" ]; then
    # Index exists but gen_ai fields not yet mapped → PUT explicit types now,
    # before any span is indexed. Explicit property wins over dynamic_template.
    echo "Fields not yet mapped. Applying explicit float/long/keyword types..."
    HTTP=$(apply_mappings)
    if [ "$HTTP" = "200" ]; then
        echo -e "${GREEN}Field types applied successfully.${NC}"
    else
        echo -e "${RED}PUT _mapping failed (HTTP $HTTP). Run 'make clean && make up' to retry.${NC}"
        exit 1
    fi

else
    # Fields already mapped (probably as keyword from a previous data ingest).
    # Must delete and recreate the index so we can apply the correct mapping.
    DOC_COUNT=$(get_doc_count)
    echo -e "${YELLOW}Fields mapped as '$FIELD_TYPE' ($DOC_COUNT docs). Recreating index...${NC}"

    delete_and_restart

    echo "  Waiting for Data Prepper to recreate the index..."
    sleep 8
    if ! wait_for_index; then
        echo -e "${RED}Index not recreated within 90 s.${NC}"
        exit 1
    fi

    # Apply explicit mappings to the fresh empty index
    echo "  Applying explicit float/long/keyword types to new index..."
    HTTP=$(apply_mappings)
    if [ "$HTTP" = "200" ]; then
        echo -e "${GREEN}  Field types applied successfully.${NC}"
    else
        echo -e "${RED}  PUT _mapping failed (HTTP $HTTP).${NC}"
        exit 1
    fi
fi

echo ""
echo "Fields mapped as numeric types:"
echo "  span.attributes.gen_ai@cost@total_usd    → float"
echo "  span.attributes.gen_ai@cost@input_usd    → float"
echo "  span.attributes.gen_ai@cost@output_usd   → float"
echo "  span.attributes.gen_ai@usage@input_tokens  → long"
echo "  span.attributes.gen_ai@usage@output_tokens → long"
echo "  span.attributes.gen_ai@usage@total_tokens  → long"
echo ""
echo "Fields mapped as keyword (aggregatable in OSD Terms aggs):"
echo "  span.attributes.gen_ai@system              → keyword"
echo "  span.attributes.gen_ai@request@model       → keyword"
echo "  span.attributes.gen_ai@response@model      → keyword"
echo "  span.attributes.gen_ai@cost@provider       → keyword"
echo "  span.attributes.gen_ai@cost@model_resolved → keyword"
echo ""
echo -e "${YELLOW}Next: run 'make test' to generate spans, then 'make dashboard'.${NC}"
