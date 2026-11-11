.PHONY: setup dev check lint type security test test-serial dast profile openapi notes web

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
	# Windows 那几段（osplat 的 msvcrt / ctypes 分支）只有按 win32 平台检查才看得到
	uv run --frozen mypy --platform win32

security:
	uv run --frozen python scripts/scan_secrets.py
	uv run --frozen bandit -q -r src/gxr_studio -c pyproject.toml

test:
	uv run --frozen python -I scripts/run_tests.py

test-serial:
	uv run --frozen python -I -m pytest -q --durations=20 --reruns 1 --cov=gxr_studio --cov-report=term-missing --cov-fail-under=85

dast:
	uv run --frozen python -I -m pytest -q tests/test_security.py tests/test_ops.py

check: lint type security
	uv run --frozen python scripts/dead_flags.py
	uv run --frozen python scripts/private_bytes.py --tracked --dist
	uv run --frozen python scripts/check_agents_md.py
	uv run --frozen python scripts/large_files.py
	uv run --frozen python scripts/export_openapi.py --check
	uv run --frozen python scripts/check_frontend.py
	uv run --frozen lint-imports
	uv run --frozen deptry src
	uv run --frozen vulture src/gxr_studio
	# .pyi 是锁定源码的类型存根。pylint 会把它和同名 .py 各算一遍，重复代码检查就会失败。
	uv run --frozen pylint --disable=all --enable=duplicate-code --ignore-patterns='.*\.pyi' src/gxr_studio
	$(MAKE) test

profile:
	uv run --frozen python scripts/profile_hot.py

openapi:
	uv run --frozen python scripts/export_openapi.py

notes:
	uv run --frozen python scripts/release_notes.py

# 显卡预览引擎（frontend/，TypeScript + WGSL）→ web/assets/gpu/。产物要提交；只有改了 frontend/ 才需要。
web:
	pnpm -C frontend install --frozen-lockfile
	pnpm -C frontend run build
