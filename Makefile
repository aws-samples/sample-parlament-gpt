SHELL := /bin/bash
REGION ?= eu-central-1

# Optional, untracked per-operator overrides (e.g. the pinned context of YOUR long-lived
# deployment — see the deploy-demo block). Loaded first so plain `VAR = value` lines in
# Makefile.local win over every `?=` default below.
-include Makefile.local

.PHONY: help test test-shared test-lambdas test-agent test-infra test-frontend gen-types gen-types-check build build-frontend build-agent synth deploy deploy-demo sync-public destroy fill-secret create-user user-pool-id dev-agent dev-frontend

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-16s %s\n", $$1, $$2}'

## ---- tests (no AWS, no cost) ----
# The agent + Lambdas require Python >= 3.11; the system python3 may be older. Override with
# `make test PYTHON=python3.13` if needed.
PYTHON ?= python3.13

test: test-shared test-lambdas gen-types-check test-agent test-frontend test-infra ## Run all tests

test-shared: ## Shared Lambda layer: contract, egress pin, pagination, normalize, gateway dispatch
	cd lambdas/shared && $(PYTHON) -m venv .venv && . .venv/bin/activate && \
	  pip install -q -e '.[dev]' && python -m pytest

# Every per-government adapter with a request-path Lambda. Add new ones here as milestones land.
ADAPTERS ?= germany uk europarl switzerland austria uscongress canada france netherlands australia

test-lambdas: test-shared ## Per-government adapter tests (fixtures from verified live responses)
	@set -e; for a in $(ADAPTERS); do \
	  echo "--- lambdas/$$a ---"; \
	  cd lambdas/$$a && $(PYTHON) -m venv .venv >/dev/null && . .venv/bin/activate && \
	    pip install -q -e ../shared'[dev]' && PYTHONPATH=. python -m pytest tests/ -q; \
	  cd ../..; \
	done

test-agent: ## Agent: guardrail, gateway auth (SigV4/Cognito), real-MCP e2e loop
	cd agent && $(PYTHON) -m venv .venv && . .venv/bin/activate && \
	  pip install -q -e '.[dev]' && python -m pytest

test-frontend: ## Frontend jest tests (route, rate limit, agent client)
	cd frontend && npm install && npx jest

test-infra: ## CDK assertion tests (egress absence, gateway shape, no-VPC Lambdas)
	cd infra && npm install && npx jest

## ---- generated code ----
gen-types: ## Regenerate the frontend Source type from the Python contract
	cd lambdas/shared && . .venv/bin/activate && python scripts/gen_source_type.py

gen-types-check: ## Fail if the generated frontend type is stale (drift guard)
	cd lambdas/shared && . .venv/bin/activate && python scripts/gen_source_type.py --check

## ---- build / verify (no deploy) ----
build: build-agent build-frontend synth ## Build everything

build-agent: ## Build the agent container image
	cd agent && docker build -t parlamentgpt-agent:local .

build-frontend: ## Next.js production build + container image
	cd frontend && npm install && npm run build && docker build -t parlamentgpt-frontend:local .

synth: ## Render CloudFormation (no deploy)
	cd infra && npm install && npx cdk synth

## ---- deploy / destroy (INCURS AWS COST — run yourself) ----
# CAUTION: a bare `make deploy` uses the sample defaults from infra/cdk.json and
# bin/app.ts (region us-east-1, suffix "sample"). Against an account that already
# runs a suffixed deployment this CREATES A SECOND, PARALLEL SYSTEM instead of
# updating the existing one. For a long-lived environment, pin its full context
# once (deploy-demo + Makefile.local) and always deploy through that.
deploy: ## cdk deploy --all with SAMPLE defaults — see caution above
	cd infra && npx cdk deploy --all --require-approval never

# Pinned context of YOUR long-lived demo deployment. Put the real values into the
# untracked Makefile.local (loaded at the top); the defaults here are placeholders.
# Every value can also be overridden per invocation: make deploy-demo DEMO_SUFFIX=other
# For the Cognito user targets below, pair with: make create-user SUFFIX=$(DEMO_SUFFIX) REGION=$(DEMO_REGION) ...
DEMO_SUFFIX ?= my-demo
DEMO_REGION ?= eu-central-1
DEMO_SIGNUP_DOMAINS ?= example.com
DEMO_DEBUG ?= true
DEMO_DOMAIN ?=
DEMO_HOSTED_ZONE ?=
# Free-form extra context flags for the pinned deployment (e.g. opt-in sources).
DEMO_EXTRA_CONTEXT ?=
DEMO_CONTEXT = --context region=$(DEMO_REGION) --context suffix=$(DEMO_SUFFIX) \
  --context signupAllowedEmailDomains='$(DEMO_SIGNUP_DOMAINS)' --context defaultDebugMode=$(DEMO_DEBUG) \
  $(DEMO_EXTRA_CONTEXT)
ifneq ($(DEMO_DOMAIN),)
DEMO_CONTEXT += --context domainName=$(DEMO_DOMAIN) --context hostedZoneDomain=$(DEMO_HOSTED_ZONE)
endif

deploy-demo: ## Update your pinned demo deployment (configure via Makefile.local)
	cd infra && npx cdk deploy --all --require-approval never $(DEMO_CONTEXT)

# Maintainers' publish workflow, run FROM MAIN: exports main (export-ignore strips all
# internal files), gates + scans the export, then publishes it as a cleaned content
# mirror to BOTH the internal `public` branch and the GitHub sample repo. The sync tool
# itself never leaves main; in a public checkout this target reports and exits.
sync-public: ## Publish main to the internal public branch + GitHub sample repo (maintainers only)
	@test -x scripts/sync-public.sh \
	  || { echo "sync-public is a maintainers-only tool; not present in this checkout."; exit 1; }
	@scripts/sync-public.sh $(SYNC_ARGS)

destroy: ## cdk destroy --all
	cd infra && npx cdk destroy --all --force

# Which per-source secrets exist is defined in infra/lib/jurisdictions.ts (secretName).
fill-secret: ## Put a source API key into Secrets Manager: make fill-secret KEY=... [SECRET=parlamentgpt/dip-api-key]
	@test -n "$(KEY)" || (echo "Usage: make fill-secret KEY=<api-key> [SECRET=parlamentgpt/dip-api-key|parlamentgpt/govinfo-api-key]"; exit 1)
	aws secretsmanager put-secret-value --region $(REGION) \
	  --secret-id "$(or $(SECRET),parlamentgpt/dip-api-key)" \
	  --secret-string '{"apiKey":"$(KEY)"}'
	@echo "Note: warm Lambda containers cache the key for their lifetime; wait for recycle or bump the function config."

## ---- user management (Cognito) ----
SUFFIX ?= sample

user-pool-id: ## Print the Cognito user pool id from the frontend stack output
	@aws cloudformation describe-stacks --region $(REGION) \
	  --stack-name ParlamentGptFrontend-$(SUFFIX) \
	  --query "Stacks[0].Outputs[?OutputKey=='UserPoolId'].OutputValue" --output text

create-user: ## Create a confirmed Cognito user: make create-user USER=a@example.com PASS=...
	@test -n "$(USER)" -a -n "$(PASS)" || (echo "Usage: make create-user USER=<email> PASS=<password>"; exit 1)
	@POOL=$$($(MAKE) -s SUFFIX=$(SUFFIX) user-pool-id); \
	aws cognito-idp admin-create-user --region $(REGION) --user-pool-id "$$POOL" \
	  --username "$(USER)" --user-attributes Name=email,Value="$(USER)" Name=email_verified,Value=true \
	  --message-action SUPPRESS >/dev/null && \
	aws cognito-idp admin-set-user-password --region $(REGION) --user-pool-id "$$POOL" \
	  --username "$(USER)" --password "$(PASS)" --permanent && \
	echo "User $(USER) created and confirmed."

## ---- local dev ----
dev-agent: ## Run the agent locally (needs DIP_API_KEY + AWS creds for Bedrock)
	cd agent && . .venv/bin/activate && PYTHONPATH=src python -m parlamentgpt_agent.main

dev-frontend: ## Run the Next.js dev server
	cd frontend && npm run dev
