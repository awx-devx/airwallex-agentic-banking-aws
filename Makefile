.PHONY: sync hooks lint fmt test sandbox-test synth-test secret-scan check synth deploy diagrams \
	help require-target

UV ?= uv
CDK ?= npx -y aws-cdk@2.1144.0
# Context for synth and deploy: ACCOUNT and ALERT_EMAIL are required, KITS (k1,k4) is optional.
CDK_CONTEXT = -c account=$(ACCOUNT) -c alert_email=$(ALERT_EMAIL) $(if $(KITS),-c kits=$(KITS))

sync: ## Create or update the uv workspace environment (Python 3.12)
	$(UV) sync --all-packages

hooks: ## Run the secret scan on every commit (sets core.hooksPath to .githooks)
	git config core.hooksPath .githooks

lint: ## Run ruff check and ruff format --check
	$(UV) run --all-packages ruff check .
	$(UV) run --all-packages ruff format --check .

fmt: ## Format and auto-fix with ruff
	$(UV) run --all-packages ruff format .
	$(UV) run --all-packages ruff check --fix .

test: ## Offline tests (no network, no sandbox, no synth)
	$(UV) run --all-packages pytest -m "not sandbox and not synth and not live"

sandbox-test: ## Contract and acceptance tests against the real Airwallex sandbox (the tests take /tmp/awx-sandbox.lock)
	$(UV) run --all-packages pytest -m sandbox

synth-test: ## cdk synth and the synth-time security tests
	$(UV) run --all-packages pytest -m synth

secret-scan: ## Scan the tree for secrets
	$(UV) run --all-packages python scripts/secret_scan.py

check: sync lint secret-scan test ## sync, lint, secret scan and offline tests

synth: require-target ## Synthesise every stack: make synth ACCOUNT=111122223333 ALERT_EMAIL=you@example.com [KITS=k1,k4]
	cd infra && $(CDK) synth --all $(CDK_CONTEXT)

deploy: require-target ## Deploy every stack: make deploy ACCOUNT=<12-digit id> ALERT_EMAIL=<address> [KITS=k1,k4]
	cd infra && $(CDK) deploy --all --outputs-file cdk-outputs.json $(CDK_CONTEXT)

diagrams: ## Regenerate the architecture diagrams [DRAWIO=/path/to/drawio to render PNGs]
	python3.12 docs/architecture/generate.py $(if $(DRAWIO),--drawio $(DRAWIO))
	$(if $(DRAWIO),$(UV) run --no-project --with pillow==12.3.0 python docs/architecture/optimise_pngs.py)

require-target:
	@if [ -z "$(ACCOUNT)" ] || [ -z "$(ALERT_EMAIL)" ]; then \
		echo "error: set ACCOUNT and ALERT_EMAIL, for example:" >&2; \
		echo "  make $(MAKECMDGOALS) ACCOUNT=111122223333 ALERT_EMAIL=you@example.com" >&2; \
		exit 2; \
	fi

help: ## List every target
	@awk 'BEGIN {FS = ":.*## "} /^[a-z][a-z-]*:.*## / {printf "  %-14s %s\n", $$1, $$2}' $(MAKEFILE_LIST)
