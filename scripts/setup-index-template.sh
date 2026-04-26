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
#   Step 1 — Install a composable _index_template (priority 500). Per
#   OpenSearch docs and live _simulate verification, when any composable
#   template matches a new index the legacy _template is completely ignored
#   (dynamic_templates included). This guarantees every future ISM rollover
#   index gets the correct field types automatically.
#
#   Step 2 — Belt-and-suspenders: also PUT the explicit field mappings
#   directly onto the current write index (handles the race window where
#   Data Prepper already created the first index before our script ran).
#   Explicit property mappings always win over dynamic_templates within
#   the same index.
#
#   Step 3 — If the current write index already has gen_ai fields mapped
#   as `keyword` (from a Data Prepper ISM rollover that happened before the
#   composable template was installed), force a manual rollover. The new
#   index is minted under the composable template with correct types.
#
# WHEN TO RUN:
#   Part of `make up` — runs automatically after the stack starts.
#   Must complete before `make test` (before any spans are indexed).

set -e

OS_HOST="${OS_HOST:-http://localhost:9200}"
BACKING_INDEX="otel-v1-apm-span-000001"   # first index ISM creates; used only for bootstrap wait
ALIAS="otel-v1-apm-span"
COMPOSABLE_TEMPLATE_NAME="otel-v1-apm-span-genai-overrides"

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
                        "gen_ai@provider@name":       { "type": "keyword" },
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

install_composable_template() {
    local RESP
    RESP=$(curl -s -o /dev/null -w "%{http_code}" -X PUT \
        "$OS_HOST/_index_template/$COMPOSABLE_TEMPLATE_NAME" \
        -H "Content-Type: application/json" \
        -d "{
            \"index_patterns\": [\"otel-v1-apm-span-*\"],
            \"priority\": 500,
            \"template\": { \"mappings\": $FIELD_MAPPINGS }
        }")
    echo "$RESP"
}

get_write_index() {
    # Returns the name of the current ISM write index for the alias.
    # Falls back to BACKING_INDEX if alias doesn't exist yet (fresh stack).
    local IDX
    IDX=$(curl -s "$OS_HOST/_cat/aliases/$ALIAS?h=index,is_write_index" 2>/dev/null \
          | awk '$2=="true"{print $1}' | head -1)
    echo "${IDX:-$BACKING_INDEX}"
}

get_field_type() {
    local IDX="${1:-$BACKING_INDEX}"
    curl -s "$OS_HOST/$IDX/_mapping" | python3 -c "
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
    local IDX="${1:-$BACKING_INDEX}"
    curl -s "$OS_HOST/$IDX/_count" | \
        python3 -c "import sys,json; print(json.load(sys.stdin).get('count',0))" 2>/dev/null || echo "0"
}

apply_mappings() {
    local IDX="${1:-$BACKING_INDEX}"
    local RESP
    RESP=$(curl -s -o /dev/null -w "%{http_code}" -X PUT "$OS_HOST/$IDX/_mapping" \
        -H "Content-Type: application/json" \
        -d "$FIELD_MAPPINGS")
    echo "$RESP"
}

force_rollover() {
    # Force ISM to advance the write index. The new index is created under
    # the composable template already installed, so it gets correct types.
    local RESP
    RESP=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
        "$OS_HOST/$ALIAS/_rollover" \
        -H "Content-Type: application/json" \
        -d '{"conditions": {"max_age": "1s"}}')
    echo "$RESP"
}

wait_for_index() {
    # Poll until BACKING_INDEX exists (max 90 s) — used on fresh stacks.
    for i in $(seq 1 30); do
        STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$OS_HOST/$BACKING_INDEX/_mapping")
        [ "$STATUS" = "200" ] && return 0
        echo "  Waiting for Data Prepper to create the index... (${i}0s)"
        sleep 3
    done
    return 1
}

wait_for_new_write_index() {
    # Poll until the alias's write index changes from the given name (max 30 s).
    local OLD_IDX="$1"
    for i in $(seq 1 10); do
        local NEW_IDX
        NEW_IDX=$(get_write_index)
        [ "$NEW_IDX" != "$OLD_IDX" ] && echo "$NEW_IDX" && return 0
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

# ── Step 2: install composable index template ─────────────────────────────────

echo "Installing composable index template '$COMPOSABLE_TEMPLATE_NAME'..."
echo "  (priority 500 → shadows Data Prepper's legacy _template for all future indices)"
HTTP=$(install_composable_template)
if [ "$HTTP" = "200" ]; then
    echo -e "${GREEN}Composable template installed. All future rollover indices will have correct types.${NC}"
else
    echo -e "${RED}Failed to install composable template (HTTP $HTTP).${NC}"
    exit 1
fi
echo ""

# ── Step 3: wait for Data Prepper to create the backing index ─────────────────

echo "Waiting for Data Prepper to create the span index..."
if ! wait_for_index; then
    echo -e "${RED}Index not created within 90 s. Is Data Prepper running?${NC}"
    echo "Run 'docker compose logs data-prepper' to diagnose."
    exit 1
fi
echo -e "${GREEN}Index $BACKING_INDEX exists.${NC}"
echo ""

# ── Step 4: check current write index and fix if needed ──────────────────────

WRITE_IDX=$(get_write_index)
echo "Current write index: $WRITE_IDX"
echo "Checking field type for gen_ai@cost@total_usd on $WRITE_IDX..."
FIELD_TYPE=$(get_field_type "$WRITE_IDX")

if [ "$FIELD_TYPE" = "float" ]; then
    echo -e "${GREEN}Field types already correct (float/long/keyword). Nothing to do.${NC}"

elif [ "$FIELD_TYPE" = "missing" ]; then
    # Write index exists but gen_ai fields not yet mapped → PUT explicit types now.
    echo "Fields not yet mapped. Applying explicit float/long/keyword types to $WRITE_IDX..."
    HTTP=$(apply_mappings "$WRITE_IDX")
    if [ "$HTTP" = "200" ]; then
        echo -e "${GREEN}Field types applied successfully.${NC}"
    else
        echo -e "${RED}PUT _mapping failed (HTTP $HTTP). Run 'make clean && make up' to retry.${NC}"
        exit 1
    fi

else
    # Write index has gen_ai fields already mapped as `keyword` — happens when an
    # ISM rollover created a new backing index before the composable template was
    # installed. We can't change existing field types in place; force a rollover to
    # mint a fresh index that picks up the now-installed composable template.
    #
    # IMPORTANT: after rollover, also DELETE the bad-typed index. Leaving it in
    # place causes _field_caps to report a type conflict (float vs keyword for the
    # same field across indices), which OSD treats as "field invalid for Sum
    # aggregation" even on indices with correct types.
    DOC_COUNT=$(get_doc_count "$WRITE_IDX")
    echo -e "${YELLOW}Write index '$WRITE_IDX' has fields mapped as '$FIELD_TYPE' ($DOC_COUNT docs).${NC}"
    echo "  Forcing manual rollover to create a fresh index with correct types..."

    HTTP=$(force_rollover)
    if [ "$HTTP" != "200" ]; then
        echo -e "${RED}Rollover failed (HTTP $HTTP).${NC}"
        echo "  Try: curl -X POST \"$OS_HOST/$ALIAS/_rollover\" -H 'Content-Type: application/json' -d '{\"conditions\":{\"max_age\":\"1s\"}}'"
        exit 1
    fi

    echo "  Rollover succeeded. Waiting for new write index to appear..."
    NEW_WRITE_IDX=$(wait_for_new_write_index "$WRITE_IDX")
    if [ $? -ne 0 ]; then
        echo -e "${RED}New write index did not appear within 30 s.${NC}"
        exit 1
    fi
    echo -e "${GREEN}New write index: $NEW_WRITE_IDX${NC}"

    # Belt-and-suspenders: also apply mappings to the new index explicitly.
    FIELD_TYPE2=$(get_field_type "$NEW_WRITE_IDX")
    if [ "$FIELD_TYPE2" = "missing" ]; then
        echo "  Applying explicit float/long/keyword types to $NEW_WRITE_IDX..."
        HTTP=$(apply_mappings "$NEW_WRITE_IDX")
        if [ "$HTTP" = "200" ]; then
            echo -e "${GREEN}  Field types applied successfully.${NC}"
        else
            echo -e "${RED}  PUT _mapping failed (HTTP $HTTP).${NC}"
            exit 1
        fi
    elif [ "$FIELD_TYPE2" = "float" ]; then
        echo -e "${GREEN}  New index already has correct types (composable template applied). Done.${NC}"
    else
        echo -e "${RED}  New index '$NEW_WRITE_IDX' still has wrong type '$FIELD_TYPE2'. This is unexpected.${NC}"
        exit 1
    fi

    # Delete the old bad-typed index so _field_caps across otel-v1-apm-span-*
    # no longer reports a float/keyword conflict for cost/token fields.
    echo "  Deleting '$WRITE_IDX' (keyword-typed cost fields cause _field_caps conflicts)..."
    DEL_HTTP=$(curl -s -o /dev/null -w "%{http_code}" -X DELETE "$OS_HOST/$WRITE_IDX")
    if [ "$DEL_HTTP" = "200" ]; then
        echo -e "${GREEN}  Deleted '$WRITE_IDX'. Type conflict resolved.${NC}"
    else
        echo -e "${YELLOW}  Could not delete '$WRITE_IDX' (HTTP $DEL_HTTP). Dashboard may still show type errors.${NC}"
        echo "  Manual fix: curl -X DELETE $OS_HOST/$WRITE_IDX"
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
echo "  span.attributes.gen_ai@system              → keyword  (older Traceloop)"
echo "  span.attributes.gen_ai@provider@name       → keyword  (current Traceloop/OTel semconv)"
echo "  span.attributes.gen_ai@request@model       → keyword"
echo "  span.attributes.gen_ai@response@model      → keyword"
echo "  span.attributes.gen_ai@cost@provider       → keyword"
echo "  span.attributes.gen_ai@cost@model_resolved → keyword"
echo ""
echo -e "${YELLOW}Next: run 'make test' to generate spans, then 'make dashboard'.${NC}"
