.PHONY: sync hooks lint fmt test sandbox-test synth-test secret-scan check bootstrap synth deploy \
	diagrams help require-target require-account require-region

UV ?= uv
CDK ?= npx -y aws-cdk@2.1144.0
# The AWS Region to bootstrap, synthesise and deploy. Keep in step with DEFAULT_REGION in
# packages/awx_common/src/awx_common/regions.py (a test pins them together).
REGION ?= us-west-2
# Context for synth and deploy: ACCOUNT and ALERT_EMAIL are required; KITS (k1,k4), MODEL_ID and
# FALLBACK_MODEL_ID are optional (the model IDs default to REGION's inference profiles).
CDK_CONTEXT = -c account=$(ACCOUNT) -c alert_email=$(ALERT_EMAIL) -c region=$(REGION_VALUE) \
	$(if $(KITS),-c kits=$(KITS)) $(if $(MODEL_ID),-c model_id=$(MODEL_ID)) \
	$(if $(FALLBACK_MODEL_ID),-c fallback_model_id=$(FALLBACK_MODEL_ID))
# Extra CDK CLI arguments for synth and deploy, for example CDK_ARGS="--require-approval never"
# in a shell with no terminal, where cdk deploy cannot ask a person to approve IAM changes.
CDK_ARGS ?=

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
	$(UV) run --all-packages pytest -m sandbox -rs

synth-test: ## cdk synth and the synth-time security tests
	$(UV) run --all-packages pytest -m synth

secret-scan: ## Scan the tree for secrets
	$(UV) run --all-packages python scripts/secret_scan.py

check: sync lint secret-scan test ## sync, lint, secret scan and offline tests

# Runs from the repository root, which has no cdk.json, so the CDK CLI does not load the app
# (the app needs ALERT_EMAIL; bootstrapping does not).
bootstrap: require-account require-region ## Bootstrap CDK once per account and Region: make bootstrap ACCOUNT=<12-digit id> [REGION=...]
	$(CDK) bootstrap aws://$(ACCOUNT)/$(REGION_VALUE)

synth: require-target require-region ## Synthesise every stack: make synth ACCOUNT=111122223333 ALERT_EMAIL=you@example.com [REGION=...] [KITS=k1,k4] [MODEL_ID=...] [FALLBACK_MODEL_ID=...] [CDK_ARGS=...]
	cd infra && $(CDK) synth --all $(CDK_CONTEXT) $(CDK_ARGS)

deploy: require-target require-region ## Deploy every stack: make deploy ACCOUNT=<12-digit id> ALERT_EMAIL=<address> [REGION=...] [KITS=k1,k4] [MODEL_ID=...] [FALLBACK_MODEL_ID=...] [CDK_ARGS=...]
	cd infra && $(CDK) deploy --all --outputs-file cdk-outputs.json $(CDK_CONTEXT) $(CDK_ARGS)

diagrams: ## Regenerate the architecture diagrams [DRAWIO=/path/to/drawio to render PNGs]
	python3.12 docs/architecture/generate.py $(if $(DRAWIO),--drawio $(DRAWIO))
	$(if $(DRAWIO),$(UV) run --no-project --with pillow==12.3.0 python docs/architecture/optimise_pngs.py)

require-target:
	@if [ -z "$(ACCOUNT)" ] || [ -z "$(ALERT_EMAIL)" ]; then \
		echo "error: set ACCOUNT and ALERT_EMAIL, for example:" >&2; \
		echo "  make $(MAKECMDGOALS) ACCOUNT=111122223333 ALERT_EMAIL=you@example.com" >&2; \
		exit 2; \
	fi

require-account:
	@if [ -z "$(ACCOUNT)" ]; then \
		echo "error: set ACCOUNT, for example:" >&2; \
		echo "  make $(MAKECMDGOALS) ACCOUNT=111122223333" >&2; \
		exit 2; \
	fi

# REGION checks, the same as target_region in infra/app.py, written in make and shell so that
# bootstrap needs no virtual environment. The pattern, prefixes and messages match
# REGION_CODE, OTHER_PARTITION_PREFIXES, REGION_CODE_MESSAGE and OTHER_PARTITION_MESSAGE in
# packages/awx_common/src/awx_common/regions.py (a test pins them together).
REGION_CODE := ^[a-z]{2}(-[a-z]+)+-[0-9]+$$
OTHER_PARTITION_PREFIXES := cn- us-gov- us-iso- us-isob- us-isof- eu-isoe-
# $(value REGION) is the text the caller passed, unexpanded, so make runs no $(...) in it.
REGION_VALUE = $(strip $(value REGION))
REGION_CODE_MESSAGE = REGION=$(REGION_VALUE) is not an AWS Region code such as us-west-2
OTHER_PARTITION_MESSAGE = REGION=$(REGION_VALUE) is not in the commercial AWS partition (aws). This app supports only that partition
# The characters a Region code is made of, and a function that removes each of them from $(1).
# It returns text only when $(1) holds another character.
REGION_CHARACTERS := a b c d e f g h i j k l m n o p q r s t u v w x y z 0 1 2 3 4 5 6 7 8 9 -
region_other_characters = $(if $(2),$(call region_other_characters,$(subst $(firstword $(2)),,$(1)),$(wordlist 2,$(words $(2)),$(2))),$(1))

# An empty REGION (REGION=$REGION with REGION unset in the shell) overrides the default above.
# require-region refuses it, a value that is not a Region code, and a Region in another
# partition, before cdk bootstraps aws://<id>/<value> or synthesises for it. The checks run
# as make expands the recipe, so $(error) also stops `make -n`. Make functions first check that
# the value is one word made only of a-z, 0-9 and -, so no other character reaches the shell
# that checks the pattern.
require-region:
	$(if $(REGION_VALUE),,$(error REGION is empty. Set it first, for example: export REGION=us-west-2))
	$(if $(filter 1,$(words $(REGION_VALUE))),,$(error $(REGION_CODE_MESSAGE)))
	$(if $(call region_other_characters,$(REGION_VALUE),$(REGION_CHARACTERS)),$(error $(REGION_CODE_MESSAGE)))
	$(if $(shell printf '%s\n' '$(REGION_VALUE)' | grep -Eqx '$(REGION_CODE)' && echo ok),,$(error $(REGION_CODE_MESSAGE)))
	$(if $(filter $(addsuffix %,$(OTHER_PARTITION_PREFIXES)),$(REGION_VALUE)),$(error $(OTHER_PARTITION_MESSAGE)))
	@:

help: ## List every target
	@awk 'BEGIN {FS = ":.*## "} /^[a-z][a-z-]*:.*## / {printf "  %-14s %s\n", $$1, $$2}' $(MAKEFILE_LIST)
