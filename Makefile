.PHONY: help install hooks test test-verbose test-coverage test-cov-html clean lint format check

# Default target
help:
	@echo "Energy Planner Test Makefile"
	@echo "============================"
	@echo ""
	@echo "Available targets:"
	@echo "  make install       - Install dependencies"
	@echo "  make hooks         - Enable the git hooks (checks + Conventional Commits)"
	@echo "  make test          - Run all tests"
	@echo "  make test-verbose  - Run tests with verbose output"
	@echo "  make test-coverage - Run tests with coverage report"
	@echo "  make test-cov-html - Generate HTML coverage report"
	@echo "  make test-fast     - Run tests without coverage"
	@echo "  make test-watch    - Run tests in watch mode (auto-rerun)"
	@echo "  make lint          - Run ruff linter"
	@echo "  make format        - Format code with ruff"
	@echo "  make check         - Run all checks: ruff check, ruff format, ty, tests"
	@echo "  make clean         - Clean test artifacts"
	@echo ""

# Install dependencies
install:
	@echo "Installing dependencies..."
	uv sync
	@echo "✓ Dependencies installed"

# Run all tests
test:
	@echo "Running tests..."
	uv run pytest tests/ -v

# Run tests with verbose output
test-verbose:
	@echo "Running tests (verbose)..."
	uv run pytest tests/ -vv -s

# Run tests with coverage
test-coverage:
	@echo "Running tests with coverage..."
	uv run pytest tests/ -v --cov=custom_components.energy_planner --cov-report=term-missing

# Generate HTML coverage report
test-cov-html:
	@echo "Generating HTML coverage report..."
	uv run pytest tests/ --cov=custom_components.energy_planner --cov-report=html
	@echo "✓ Coverage report generated at htmlcov/index.html"

# Run tests fast (no coverage)
test-fast:
	@echo "Running tests (fast mode)..."
	uv run pytest tests/ -v --tb=short

# Run tests in watch mode (requires pytest-watch)
test-watch:
	@echo "Running tests in watch mode..."
	uv run --with pytest-watch ptw -- tests/ -v

# Run linter
lint:
	@echo "Running ruff linter..."
	uv run ruff check custom_components/energy_planner/
	uv run ruff check tests/

# Format code
format:
	@echo "Formatting code..."
	uv run ruff format custom_components/energy_planner/
	uv run ruff format tests/

# Run checks (lint + test)
check:
	scripts/check.sh

# Enable the git hooks in .githooks
hooks:
	git config core.hooksPath .githooks

# Clean test artifacts
clean:
	@echo "Cleaning test artifacts..."
	rm -rf .pytest_cache
	rm -rf htmlcov
	rm -rf .coverage
	rm -rf .ruff_cache
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	@echo "✓ Cleaned"

# Quick test (for rapid development)
quick:
	@uv run pytest tests/ -x --tb=short

# Test specific file
test-basic:
	@uv run pytest tests/planner/test_basic_planner.py -v

# CI simulation
ci:
	@echo "Running CI pipeline..."
	@make lint
	@make test-coverage
	@echo "✓ CI pipeline completed"

