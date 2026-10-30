.PHONY: setup dev check lint type security test dast profile openapi notes

setup:
	uv sync --frozen
	git config core.hooksPath .githooks

dev: setup
	uv run --frozen gxr-studio

lint:
	uv run --frozen ruff check src tests scripts
	uv run --frozen ruff format --check src tests scripts

type:
	uv run --frozen mypy

security:
	uv run --frozen python scripts/scan_secrets.py
	uv run --frozen bandit -q -r src/gxr_studio -c pyproject.toml

test:
	uv run --frozen pytest -q --durations=20 --reruns 1 --cov=gxr_studio --cov-report=term-missing --cov-fail-under=85

dast:
	uv run --frozen pytest -q tests/test_security.py tests/test_ops.py

check: lint type security
	uv run --frozen python scripts/dead_flags.py
	uv run --frozen python scripts/check_agents_md.py
	uv run --frozen python scripts/large_files.py
	uv run --frozen python scripts/export_openapi.py --check
	uv run --frozen lint-imports
	uv run --frozen deptry src
	uv run --frozen vulture src/gxr_studio
	uv run --frozen pylint --disable=all --enable=duplicate-code src/gxr_studio
	$(MAKE) test

profile:
	uv run --frozen python scripts/profile_hot.py

openapi:
	uv run --frozen python scripts/export_openapi.py

notes:
	uv run --frozen python scripts/release_notes.py
