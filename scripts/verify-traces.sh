#!/bin/bash
# Verify that traces are flowing from Flask → OTel Collector → Data Prepper → OpenSearch

set -e

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

OS_HOST="http://localhost:9200"

echo -e "${BLUE}================================${NC}"
echo -e "${BLUE}Trace Verification${NC}"
echo -e "${BLUE}================================${NC}\n"

# 1. Check OpenSearch health
echo -e "${YELLOW}1. OpenSearch Cluster Health${NC}"
curl -s "$OS_HOST/_cluster/health" | python3 -m json.tool
echo ""

# 2. Check span indices exist
echo -e "${YELLOW}2. Span Indices${NC}"
curl -s "$OS_HOST/_cat/indices/otel-v1-apm-*?v&h=index,docs.count,store.size"
echo ""

# 3. Check span count
echo -e "${YELLOW}3. Total Span Count${NC}"
SPAN_COUNT=$(curl -s "$OS_HOST/otel-v1-apm-span-*/_count" | python3 -c "import sys,json; print(json.load(sys.stdin).get('count', 0))" 2>/dev/null || echo "0")
echo "  Spans: $SPAN_COUNT"
echo ""

if [ "$SPAN_COUNT" -eq 0 ]; then
    echo -e "${RED}No spans found! Run 'make test' first to generate traces.${NC}"
    exit 1
fi

# 4. Check for LLM spans with cost data
echo -e "${YELLOW}4. LLM Spans with Cost Data${NC}"
curl -s "$OS_HOST/otel-v1-apm-span-*/_search" \
    -H 'Content-Type: application/json' \
    -d '{
        "size": 1,
        "query": {
            "bool": {
                "must": [
                    { "exists": { "field": "span.attributes.gen_ai@system" } }
                ]
            }
        },
        "_source": [
            "span.attributes.gen_ai@system",
            "span.attributes.gen_ai@request@model",
            "span.attributes.gen_ai@usage@input_tokens",
            "span.attributes.gen_ai@usage@output_tokens",
            "span.attributes.gen_ai@cost@total_usd",
            "span.attributes.gen_ai@cost@input_usd",
            "span.attributes.gen_ai@cost@output_usd",
            "span.attributes.gen_ai@cost@model_resolved"
        ]
    }' | python3 -m json.tool
echo ""

# 5. Check service map
echo -e "${YELLOW}5. Service Map${NC}"
curl -s "$OS_HOST/otel-v1-apm-service-map/_search?size=5" \
    -H 'Content-Type: application/json' \
    -d '{"_source": ["serviceName", "destination.domain", "destination.resource"]}' \
    | python3 -m json.tool
echo ""

echo -e "${GREEN}================================${NC}"
echo -e "${GREEN}Verification complete!${NC}"
echo -e "${GREEN}================================${NC}"
echo -e "\n${BLUE}View traces at: ${YELLOW}http://localhost:5601/app/observability-traces${NC}"
echo -e "${BLUE}View service map at: ${YELLOW}http://localhost:5601/app/observability-traces#/services${NC}"
