#!/bin/bash
# Run tests for energy_planner

set -e

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${BLUE}Energy Planner Test Runner${NC}"
echo "================================"

# Create the virtual environment and install dependencies
echo -e "${BLUE}Syncing dependencies...${NC}"
uv sync --quiet

# Run tests
echo -e "${BLUE}Running tests...${NC}"
echo ""

set +e
# Default: run all tests
if [ $# -eq 0 ]; then
    uv run pytest tests/ -v
else
    # Run with provided arguments
    uv run pytest "$@"
fi

# Capture exit code
TEST_EXIT_CODE=$?

if [ $TEST_EXIT_CODE -eq 0 ]; then
    echo ""
    echo -e "${GREEN}✓ All tests passed!${NC}"
else
    echo ""
    echo -e "${RED}✗ Some tests failed${NC}"
fi

exit $TEST_EXIT_CODE
