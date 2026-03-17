# Trip Planner -- Makefile
# Single-file Python CLI (stdlib only, no pip dependencies)

PYTHON = python3
RUFF   = ruff

all: help

# ---------------------------------------------------------------------------
# Lint
# ---------------------------------------------------------------------------

lint:  ## lint Python code with ruff
	$(RUFF) check trip_planner.py

format:  ## format Python code with ruff
	$(RUFF) format trip_planner.py

format-check:  ## check formatting without modifying
	$(RUFF) format --check trip_planner.py

# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

test:  ## run tests with pytest
	$(PYTHON) -m pytest tests/ -v --tb=short

test-cov:  ## run tests with coverage
	$(PYTHON) -m pytest tests/ -v --cov=. --cov-report=term-missing --cov-fail-under=50

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

run:  ## run trip planner (pass ARGS, e.g. make run ARGS='--from Oxford --to London')
	$(PYTHON) trip_planner.py $(ARGS)

serve:  ## serve browser app at http://localhost:8080 (fixes CORS for local APIs)
	@echo "Open http://localhost:8080/trip_planner.html"
	$(PYTHON) -m http.server 8080

# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

check:  ## syntax check
	$(PYTHON) -c "import py_compile; py_compile.compile('trip_planner.py', doraise=True); print('OK')"

clean:  ## remove generated files and caches
	rm -rf __pycache__ .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage
	rm -f trip_*.md trip_*.html

pre-commit-install:  ## install pre-commit hooks
	pre-commit install
	pre-commit install --hook-type commit-msg

pre-commit-run:  ## run all pre-commit hooks
	pre-commit run --all-files

help:  ## print help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

.PHONY: all lint format format-check test test-cov run serve check clean \
        pre-commit-install pre-commit-run help
