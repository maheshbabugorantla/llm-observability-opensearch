#!/bin/bash

# ==============================================================================
# Multi-Agentic Restaurant Menu Designer - Test Script
# ==============================================================================
# Demonstrates complex OpenSearch trace analytics with:
# - Parallel execution across multiple AI agents
# - Nested workflows (workflows within workflows)
# - Decision trees (coordinator choices)
# - Error handling and retries
# - Cross-model traces (GPT-5 and Claude)
# ==============================================================================

BASE_URL="http://localhost:5001"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo ""
echo "========================================================================"
echo "  MULTI-AGENTIC RESTAURANT MENU DESIGNER - TRACE DEMO"
echo "========================================================================"
echo ""
echo "This test generates rich, complex traces in OpenSearch demonstrating:"
echo "  ✓ 4 AI Agents working together (Coordinator, Chef, Sommelier, Nutritionist)"
echo "  ✓ Parallel execution (agents running simultaneously)"
echo "  ✓ Nested workflows (3-4 levels deep)"
echo "  ✓ Decision trees (strategic choices)"
echo "  ✓ Error handling & retries"
echo "  ✓ Cross-model traces (GPT-4 + Claude)"
echo ""
echo "========================================================================"
echo ""

# Function to make API call and display results
test_menu_design() {
    local test_name=$1
    local payload=$2

    echo -e "${BLUE}Test: ${test_name}${NC}"
    echo -e "${YELLOW}Request:${NC}"
    echo "$payload" | jq '.' 2>/dev/null || echo "$payload"
    echo ""

    echo -e "${YELLOW}Sending request...${NC}"
    response=$(curl -s -X POST "${BASE_URL}/menu/design" \
        -H "Content-Type: application/json" \
        -d "$payload")

    http_code=$?

    if [ $http_code -eq 0 ]; then
        echo -e "${GREEN}✓ Response received${NC}"
        echo "$response" | jq '{
            success: .success,
            concept: .menu.concept,
            courses: .menu.courses | length,
            approved: .metadata.nutrition_approved_count,
            iterations: .metadata.total_iterations,
            wine_attempts: .metadata.total_wine_attempts,
            duration: .metadata.design_time_seconds,
            agents: .metadata.agents_involved
        }' 2>/dev/null || echo "$response"
    else
        echo -e "${RED}✗ Request failed${NC}"
        echo "$response"
    fi

    echo ""
    echo "------------------------------------------------------------------------"
    echo ""
    sleep 3  # Pause between tests to see trace generation
}

echo -e "${GREEN}Starting tests...${NC}"
echo ""

# ==============================================================================
# TEST 1: Classic Italian Fine Dining (3 courses)
# Simple test with no dietary restrictions
# Expected trace: ~15-20 spans, 30-45s duration
# ==============================================================================
echo ""
echo "TEST 1: Classic Italian Fine Dining"
echo "------------------------------------"
echo "Complexity: Moderate"
echo "Expected Duration: 30-45 seconds"
echo "Expected Spans: 15-20"
echo ""

test_menu_design "Italian Fine Dining (3 courses)" '{
  "cuisine": "Italian",
  "menu_type": "fine_dining",
  "courses": 3,
  "dietary_requirements": [],
  "budget": "premium",
  "season": "spring",
  "occasion": "romantic_dinner"
}'

# ==============================================================================
# TEST 2: French Haute Cuisine with Dietary Requirements (5 courses)
# Complex test with veg option and gluten-free
# Expected trace: ~25-35 spans, 50-75s duration
# Will likely trigger nutritionist modifications (retry traces!)
# ==============================================================================
echo ""
echo "TEST 2: French Haute Cuisine with Dietary Requirements"
echo "------------------------------------------------------"
echo "Complexity: High (includes dietary restrictions)"
echo "Expected Duration: 50-75 seconds"
echo "Expected Spans: 25-35 (includes retry/refinement spans)"
echo "Special: Will demonstrate error handling & retries"
echo ""

test_menu_design "French Haute Cuisine (5 courses, dietary restrictions)" '{
  "cuisine": "French",
  "menu_type": "haute_cuisine",
  "courses": 5,
  "dietary_requirements": ["vegetarian_option", "gluten_free"],
  "budget": "luxury",
  "season": "autumn",
  "occasion": "anniversary_celebration"
}'

# ==============================================================================
# TEST 3: Japanese Omakase Experience (7 courses)
# Most complex test - maximum course count
# Expected trace: ~35-50 spans, 80-120s duration
# Demonstrates deep nesting and extensive parallel work
# ==============================================================================
echo ""
echo "TEST 3: Japanese Omakase Experience"
echo "-----------------------------------"
echo "Complexity: Very High (7 courses)"
echo "Expected Duration: 80-120 seconds"
echo "Expected Spans: 35-50 (deepest nesting)"
echo "Special: Shows maximum complexity for trace demo"
echo ""

test_menu_design "Japanese Omakase (7 courses)" '{
  "cuisine": "Japanese",
  "menu_type": "omakase",
  "courses": 7,
  "dietary_requirements": ["pescatarian"],
  "budget": "premium",
  "season": "winter",
  "occasion": "business_dinner"
}'

# ==============================================================================
# Summary
# ==============================================================================
echo ""
echo "========================================================================"
echo "  TESTS COMPLETED"
echo "========================================================================"
echo ""
echo "View traces in OpenSearch Dashboards:"
echo "  ${GREEN}http://localhost:5601/app/observability-traces${NC}"
echo ""
echo "View service map:"
echo "  ${GREEN}http://localhost:5601/app/observability-traces#/services${NC}"
echo ""
echo "Expected trace patterns to look for:"
echo ""
echo "1. ${BLUE}Main Transaction:${NC} restaurant_menu_design_workflow"
echo "   Duration: 30-120 seconds depending on course count"
echo ""
echo "2. ${BLUE}Nested Workflows (Child Transactions):${NC}"
echo "   - parallel_agent_research_workflow (shows parallel spans)"
echo "   - recipe_refinement_workflow (shows retries if nutritionist rejects)"
echo "   - wine_pairing_workflow (shows sommelier retries if needed)"
echo ""
echo "3. ${BLUE}Individual Agent Tasks (Spans):${NC}"
echo "   - coordinator_plan_menu_structure (GPT-4)"
echo "   - chef_create_course_recipe (Claude) [may have iteration > 1]"
echo "   - sommelier_pair_wine_with_course (GPT-4) [may have attempt > 1]"
echo "   - nutritionist_analyze_course (Claude)"
echo ""
echo "4. ${BLUE}Parallel Execution:${NC}"
echo "   Look for simultaneous spans in parallel_agent_research_workflow"
echo "   Chef, Nutritionist, and Sommelier research all run at once"
echo ""
echo "5. ${BLUE}Error Handling & Retries:${NC}"
echo "   Search for spans with iteration=2 or attempt=2"
echo "   These show the retry logic in action"
echo ""
echo "6. ${BLUE}Cross-Model Traces:${NC}"
echo "   Filter by 'gen_ai.system' to see GPT-4 vs Claude spans"
echo "   Coordinator & Sommelier: GPT-4"
echo "   Chef & Nutritionist: Claude"
echo ""
echo "========================================================================"
echo ""
echo -e "${GREEN}Trace generation complete! Check OpenSearch Dashboards now.${NC}"
echo ""
