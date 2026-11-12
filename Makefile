.PHONY: setup dev check lint type security test test-serial dast profile openapi notes web \
	core core-test core-wasm mobile-dev mobile-build mobile-check

# core/rust-toolchain.toml 里的版本。PATH 上 Homebrew 的 cargo 没有 wasm32 标准库，所以先放 rustup 的目录。
CORE_TOOLCHAIN := $(shell awk -F'"' '/^channel/ {print $$2; exit}' core/rust-toolchain.toml)

define with_rust
	@if ! command -v rustup >/dev/null 2>&1; then \
		echo "要编手机核心：先安装 rustup，再执行 rustup toolchain install $(CORE_TOOLCHAIN)"; \
		exit 1; \
	fi
	@PATH="$$(dirname "$$(rustup which --toolchain $(CORE_TOOLCHAIN) cargo)"):$$HOME/.cargo/bin:$$PATH" $(1)
endef

setup:
	uv sync --frozen
	git config core.hooksPath .githooks
	@if command -v rustup >/dev/null 2>&1; then \
		rustup target add wasm32-unknown-unknown; \
	else \
		echo "要编手机核心：先安装 rustup，再执行 rustup toolchain install $(CORE_TOOLCHAIN) && rustup target add wasm32-unknown-unknown"; \
	fi

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

check: lint type security core core-test core-wasm mobile-check
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
	GXR_REQUIRE_CORE=1 $(MAKE) test

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

# 原生对拍工具。Python 测试没编过它就跳过；make check 会先编，并设 GXR_REQUIRE_CORE=1。
core:
	$(call with_rust,cargo build --manifest-path core/Cargo.toml --release -p gxr-cli)

core-test:
	$(call with_rust,cargo fmt --manifest-path core/Cargo.toml --all --check)
	$(call with_rust,cargo clippy --manifest-path core/Cargo.toml --all-targets -- -D warnings)
	$(call with_rust,cargo test --manifest-path core/Cargo.toml)

# profile.wasm 继承 release，并且 panic=abort。产物给 wasm-bindgen 打成网页能 import 的包。
core-wasm:
	$(call with_rust,cargo build --manifest-path core/Cargo.toml --profile wasm --target wasm32-unknown-unknown -p gxr-wasm)
	$(call with_rust,wasm-bindgen --target web --out-dir mobile/src/wasm-pkg core/target/wasm32-unknown-unknown/wasm/gxr_wasm.wasm)

mobile-dev: core-wasm
	pnpm -C mobile exec vite --host 127.0.0.1 --port 5199 --strictPort

mobile-build: core-wasm
	pnpm -C mobile run build

mobile-check: mobile-build
	pnpm -C mobile exec tsc --noEmit -p .
	@if grep -R -n -F '__dev' mobile/dist; then \
		echo "dev endpoints leaked into the mobile build"; \
		exit 1; \
	fi
