# flask-tenants -- development and release tasks, driven by uv.
#
#   make dev            set up the environment
#   make db-up test     run the suite (needs PostgreSQL; see Testing below)
#   make build check    produce and validate the distributions
#   make publish-test   upload to TestPyPI
#   make publish        upload to PyPI
#
# Every target is safe to re-run. Nothing uploads without an explicit target.

SHELL := /bin/bash
.DEFAULT_GOAL := help

# ---- configuration (override on the command line: make test PG_PORT=5432) ----

UV             ?= uv
PY_VERSION     ?= 3.12
VENV           ?= .venv
PYTHON         := $(VENV)/bin/python
PKG            := flask_tenants
VERSION_FILE   := src/$(PKG)/__init__.py

# Container runtime, auto-detected. `docker` is frequently a shell alias for
# podman, and aliases do not exist in make's non-interactive shell -- so look
# for the real binaries rather than trusting the name.
CONTAINER      ?= $(shell command -v docker 2>/dev/null || command -v podman 2>/dev/null)

PG_CONTAINER   ?= ft-pg
PG_PORT        ?= 55432
PG_IMAGE       ?= postgres:16-alpine
TEST_DATABASE_URL ?= postgresql+psycopg://tenants:tenants@127.0.0.1:$(PG_PORT)/tenants

TESTPYPI_URL       := https://test.pypi.org/legacy/
TESTPYPI_CHECK_URL := https://test.pypi.org/simple/

VERSION = $(shell sed -n 's/^__version__ = "\(.*\)"/\1/p' $(VERSION_FILE))

.PHONY: help
help: ## Show this help
	@echo "flask-tenants $(VERSION)"
	@echo
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "Publishing needs a token:  export UV_PUBLISH_TOKEN=pypi-..."

# ---- environment -------------------------------------------------------------

$(PYTHON):
	$(UV) venv --python $(PY_VERSION) $(VENV)

.PHONY: dev
dev: $(PYTHON) ## Create the venv and install the package with all dev extras
	$(UV) pip install --python $(PYTHON) -e ".[dev]"
	@echo "Ready. Activate with: source $(VENV)/bin/activate"

.PHONY: clean
clean: ## Remove build artefacts and caches (keeps the venv and built docs)
	rm -rf dist build *.egg-info src/*.egg-info
	rm -rf .pytest_cache .ruff_cache .mypy_cache .coverage htmlcov
	find . -type d -name __pycache__ -not -path "./$(VENV)/*" -exec rm -rf {} + 2>/dev/null || true

.PHONY: clean-all
clean-all: clean ## Also remove the virtualenv
	rm -rf $(VENV)

# ---- quality -----------------------------------------------------------------

.PHONY: lint
lint: ## Check formatting and lint rules
	$(UV) run --python $(PYTHON) ruff check src tests
	$(UV) run --python $(PYTHON) ruff format --check src tests

.PHONY: fmt
fmt: ## Apply formatting and autofixable lint rules
	$(UV) run --python $(PYTHON) ruff check --fix src tests
	$(UV) run --python $(PYTHON) ruff format src tests

# ---- testing -----------------------------------------------------------------
#
# The suite needs a real PostgreSQL. SQLite has no schemas, so there is no
# in-memory fallback -- without a database the tenancy tests SKIP, and a green
# run that skipped everything looks exactly like a green run that passed.
# `make test` fails loudly instead.

.PHONY: db-up
db-up: _require-container ## Start the test PostgreSQL (docker or podman)
	@$(CONTAINER) start $(PG_CONTAINER) >/dev/null 2>&1 || \
		$(CONTAINER) run -d --name $(PG_CONTAINER) \
			-e POSTGRES_USER=tenants -e POSTGRES_PASSWORD=tenants -e POSTGRES_DB=tenants \
			-p $(PG_PORT):5432 $(PG_IMAGE) >/dev/null
	@printf "waiting for postgres"
	@for i in $$(seq 1 30); do \
		$(CONTAINER) exec $(PG_CONTAINER) pg_isready -U tenants -q 2>/dev/null && break; \
		printf "."; sleep 1; \
	done; echo " ready on port $(PG_PORT)"

.PHONY: db-down
db-down: _require-container ## Stop and remove the test PostgreSQL
	-@$(CONTAINER) rm -f $(PG_CONTAINER) >/dev/null 2>&1
	@echo "removed $(PG_CONTAINER)"

.PHONY: db-shell
db-shell: _require-container ## Open psql against the test database
	$(CONTAINER) exec -it $(PG_CONTAINER) psql -U tenants -d tenants

.PHONY: _require-db
_require-db:
	@$(PYTHON) -c "import sqlalchemy as sa; \
		sa.create_engine('$(TEST_DATABASE_URL)').connect().close()" 2>/dev/null \
		|| { \
			echo "No PostgreSQL at $(TEST_DATABASE_URL)"; \
			echo; \
			echo "The tenancy tests would SKIP rather than fail, and a run that"; \
			echo "skipped everything is indistinguishable from one that passed."; \
			echo "Start one with:  make db-up"; \
			exit 1; }

.PHONY: test
test: _require-db ## Run the test suite (fails if PostgreSQL is absent)
	@FLASK_TENANTS_TEST_DATABASE_URL=$(TEST_DATABASE_URL) \
		$(PYTHON) -m pytest -q --no-header

.PHONY: test-v
test-v: _require-db ## Run the test suite verbosely
	FLASK_TENANTS_TEST_DATABASE_URL=$(TEST_DATABASE_URL) $(PYTHON) -m pytest -v

.PHONY: leak-test
leak-test: _require-db ## Run only the cross-tenant isolation tests
	@echo "The tests the whole design rests on -- nothing below Python enforces isolation."
	FLASK_TENANTS_TEST_DATABASE_URL=$(TEST_DATABASE_URL) \
		$(PYTHON) -m pytest tests/test_binding.py tests/test_leak_probe_detects_leaks.py -v

# ---- docs --------------------------------------------------------------------
#
# Sources are in docsrc/; output goes to docs/ so GitHub Pages can serve it from
# the "/docs folder on main" setting.

.PHONY: docs
docs: ## Build the documentation site into docs/
	$(PYTHON) -m mkdocs build --strict
	@touch docs/.nojekyll        # GitHub Pages: do not run the output through Jekyll
	@echo "built docs/ from docsrc/"

.PHONY: docs-serve
docs-serve: ## Serve the docs with live reload
	$(PYTHON) -m mkdocs serve

# ---- version -----------------------------------------------------------------
#
# The version lives in exactly one place -- src/flask_tenants/__init__.py --
# and hatchling reads it from there, so a release bumps one file.

.PHONY: version
version: ## Print the current version
	@echo $(VERSION)

.PHONY: bump
bump: ## Set a new version: make bump V=0.2.0
	@test -n "$(V)" || { echo "usage: make bump V=0.2.0"; exit 1; }
	@echo "$(V)" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+([ab.]?[0-9a-z.]*)?$$' \
		|| { echo "'$(V)' does not look like a PEP 440 version"; exit 1; }
	@sed -i.bak 's/^__version__ = ".*"/__version__ = "$(V)"/' $(VERSION_FILE) && rm -f $(VERSION_FILE).bak
	@echo "$(PKG) is now $(V)  (was built from $(VERSION_FILE))"

# ---- packaging ---------------------------------------------------------------

.PHONY: build
build: clean ## Build the sdist and wheel into dist/
	$(UV) build
	@echo
	@ls -lh dist/

.PHONY: check
check: ## Validate the built distributions before uploading
	@test -d dist || { echo "nothing in dist/ -- run: make build"; exit 1; }
	$(UV) run --python $(PYTHON) twine check --strict dist/*
	@echo
	@echo "sdist contents (should carry sources, tests and docs -- no venv, no dist):"
	@tar -tzf dist/*.tar.gz | head -20
	@echo
	@tar -tzf dist/*.tar.gz | grep -qE '(^|/)\.venv/' \
		&& { echo "ERROR: the sdist contains .venv"; exit 1; } || true
	@echo "distributions look publishable."

.PHONY: smoke
smoke: ## Install the built wheel into a throwaway venv and import it without Flask
	@test -n "$$(ls dist/*.whl 2>/dev/null)" || { echo "run: make build"; exit 1; }
	@rm -rf .smoke && $(UV) venv --python $(PY_VERSION) .smoke -q
	@$(UV) pip install -q --python .smoke/bin/python dist/*.whl
	@.smoke/bin/python -c "import importlib.util, flask_tenants as ft; \
		from flask_tenants import tenant_context, SimpleTenant; \
		assert importlib.util.find_spec('flask') is None, 'Flask leaked into the core deps'; \
		ctx = tenant_context(SimpleTenant.for_id('x')); ctx.__enter__(); \
		print('wheel ok:', ft.__version__, '| core imports with no Flask installed')"
	@rm -rf .smoke

# ---- publishing --------------------------------------------------------------
#
# Both targets read UV_PUBLISH_TOKEN from the environment. Create a scoped
# token at https://pypi.org/manage/account/token/ (and test.pypi.org separately
# -- they are different accounts with different tokens).

.PHONY: publish-test
publish-test: _require-token ## Upload to TestPyPI
	$(UV) publish \
		--publish-url $(TESTPYPI_URL) \
		--check-url $(TESTPYPI_CHECK_URL) \
		dist/*
	@echo
	@echo "Verify the upload installs cleanly:"
	@echo "  uv run --with flask-tenants==$(VERSION) \\"
	@echo "    --index https://test.pypi.org/simple/ --index-strategy unsafe-best-match \\"
	@echo "    python -c 'import flask_tenants; print(flask_tenants.__version__)'"

.PHONY: publish
publish: _require-token _confirm-publish ## Upload to PyPI (asks for confirmation)
	$(UV) publish --check-url https://pypi.org/simple/ dist/*
	@echo
	@echo "Published $(PKG) $(VERSION). Tag it:  make tag"

.PHONY: publish-dry-run
publish-dry-run: ## Show what publish would upload, without uploading
	@# --trusted-publishing never: without it uv probes for an OIDC token first
	@# and reports a confusing "Trusted publishing failed" before the dry run.
	$(UV) publish --dry-run --trusted-publishing never dist/*

.PHONY: release
release: lint test build check smoke ## Full pre-release gate: lint, test, build, validate, smoke
	@echo
	@echo "$(PKG) $(VERSION) is ready."
	@echo "  make publish-test   then verify"
	@echo "  make publish        then make tag"

.PHONY: tag
tag: ## Create and push the git tag for the current version
	@git rev-parse --is-inside-work-tree >/dev/null 2>&1 || { echo "not a git repository"; exit 1; }
	git tag -a v$(VERSION) -m "$(PKG) $(VERSION)"
	git push origin v$(VERSION)

# ---- guards ------------------------------------------------------------------

.PHONY: _require-container
_require-container:
	@test -n "$(CONTAINER)" || { \
		echo "Neither docker nor podman was found on PATH."; \
		echo "Note: a shell alias (docker -> podman) is invisible to make."; \
		echo "Point at the binary explicitly:  make db-up CONTAINER=/opt/podman/bin/podman"; \
		exit 1; }

.PHONY: _require-token
_require-token:
	@test -n "$$UV_PUBLISH_TOKEN" || { \
		echo "UV_PUBLISH_TOKEN is not set."; \
		echo "  export UV_PUBLISH_TOKEN=pypi-AgEI...   (PyPI)"; \
		echo "  export UV_PUBLISH_TOKEN=pypi-AgENd...  (TestPyPI -- a different token)"; \
		exit 1; }
	@test -n "$$(ls dist/*.whl 2>/dev/null)" || { echo "nothing to upload -- run: make build"; exit 1; }

.PHONY: _confirm-publish
_confirm-publish:
	@echo "About to publish $(PKG) $(VERSION) to PyPI."
	@echo "A version number can never be reused or reverted there."
	@if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then \
		test -z "$$(git status --porcelain)" \
			|| { echo; echo "ERROR: working tree is dirty. Commit first."; exit 1; }; \
	fi
	@read -p "Type the version to confirm: " v; \
		test "$$v" = "$(VERSION)" || { echo "mismatch -- aborted"; exit 1; }
