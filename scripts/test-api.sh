#!/bin/bash

# Test script for Recipe Generator API

set -e

BASE_URL="http://localhost:5001"
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${BLUE}================================${NC}"
echo -e "${BLUE}Recipe Generator API Test Suite${NC}"
echo -e "${BLUE}================================${NC}\n"

# Check if jq is installed
if command -v jq &> /dev/null; then
    JQ_CMD="jq ."
else
    echo -e "${YELLOW}Note: Install 'jq' for pretty JSON${NC}\n"
    JQ_CMD="cat"
fi

make_request() {
    local name=$1
    local method=$2
    local endpoint=$3
    local data=$4

    echo -e "${GREEN}Test: ${name}${NC}"
    echo -e "${BLUE}Endpoint: ${method} ${endpoint}${NC}"

    if [ "$method" = "GET" ]; then
        response=$(curl -s -X GET "${BASE_URL}${endpoint}")
    else
        echo -e "${YELLOW}Request Data:${NC}"
        echo "$data" | $JQ_CMD
        response=$(curl -s -X POST "${BASE_URL}${endpoint}" \
            -H "Content-Type: application/json" \
            -d "$data")
    fi

    echo -e "${YELLOW}Response:${NC}"
    echo "$response" | $JQ_CMD
    echo -e "\n---\n"
    sleep 2
}

# Test 1: Health Check
echo -e "${GREEN}=== Test 1: Health Check ===${NC}\n"
make_request "Health Check" "GET" "/health"

# Test 2: OpenAI Recipe
echo -e "${GREEN}=== Test 2: Generate Recipe with OpenAI ===${NC}\n"
make_request "OpenAI Recipe" "POST" "/recipe/generate" '{
  "provider": "openai",
  "dish_name": "Chicken Tikka Masala",
  "cuisine_type": "Indian",
  "servings": 4
}'

# Test 3: Claude Recipe
echo -e "${GREEN}=== Test 3: Generate Recipe with Claude ===${NC}\n"
make_request "Claude Recipe" "POST" "/recipe/generate" '{
  "provider": "claude",
  "dish_name": "Pad Thai",
  "cuisine_type": "Thai",
  "servings": 2
}'

# Test 4: Compare
echo -e "${GREEN}=== Test 4: Compare Providers ===${NC}\n"
make_request "Compare" "POST" "/recipe/compare" '{
  "dish_name": "Chocolate Chip Cookies",
  "servings": 24
}'

# Test 5: Batch
echo -e "${GREEN}=== Test 5: Batch Generation ===${NC}\n"
make_request "Batch" "POST" "/recipe/batch" '{
  "provider": "openai",
  "recipes": [
    {"dish_name": "Caesar Salad", "servings": 4},
    {"dish_name": "Margherita Pizza", "servings": 2}
  ]
}'

echo -e "${GREEN}================================${NC}"
echo -e "${GREEN}All tests completed!${NC}"
echo -e "${GREEN}================================${NC}\n"

echo -e "${BLUE}View traces at: ${YELLOW}http://localhost:5601${NC}\n"
echo -e "${BLUE}Check indices: ${YELLOW}curl http://localhost:9200/_cat/indices?v${NC}\n"
