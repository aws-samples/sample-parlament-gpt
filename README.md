# ParlamentGPT

AI assistant for researching **debates and speeches across multiple parliaments**, answered
exclusively from each parliament's official open-data service. Started as a German
Bundestag (DIP) assistant and now covers six parliaments,
with four more adapters built and gated behind licensing/ingest decisions.

> ⚠️ **This is sample code, for demonstration purposes only — it is not intended for
> production use without further review and hardening.** Have your security and legal
> teams review this sample before using it in a production or customer-facing setting.
> Deploying it creates AWS resources that **incur ongoing cost** (see [Costs](#costs)).
> Read [Security considerations](#security-considerations),
> [docs/threat-model.md](docs/threat-model.md) and
> [Before running this in production](#before-running-this-in-production) first.

## Covered parliaments

| Parliament | Source | Status |
|---|---|---|
| 🇩🇪 German Bundestag | DIP API | enabled |
| 🇬🇧 UK Parliament | Hansard API | enabled |
| 🇪🇺 European Parliament | EP Open Data Portal | enabled |
| 🇨🇭 Swiss Parliament | Parlamentsdienste OData | enabled |
| 🇦🇹 Austrian Parliament | Parlament Österreich Open Data | enabled |
| 🇺🇸 US Congress | GovInfo (Congressional Record) | opt-in (`enableUsCongress=true` + self-requested API key) |
| 🇨🇦 House of Commons of Canada | Hansard | built, disabled (licensing) |
| 🇦🇺 Parliament of Australia | APH Hansard | built, disabled (licensing) |
| 🇫🇷 Assemblée nationale | comptes rendus | built, disabled (batch ingest pending) |
| 🇳🇱 Tweede Kamer | Open Data | built, disabled (batch ingest pending) |

The single source of truth for this table is `infra/lib/jurisdictions.ts` (egress
allowlists, secrets, tool descriptions) mirrored for display in
`frontend/src/lib/jurisdictions.ts` (labels, attribution). Per-source research notes live
in `docs/multi-gov/source-profiles/`.

## Architecture

```
User → CloudFront (TLS)
     → ALB (locked to CloudFront via prefix list + secret header)
     → Next.js on Fargate (Cognito-authenticated, SSE pass-through)
                ↓                       ↘ DynamoDB (chat sessions, user settings)
                ↓
        Bedrock AgentCore Runtime (Strands agent + Bedrock Guardrail)
                ↓  MCP (SigV4)
        AgentCore Gateway
                ↓  one Lambda target per parliament
        lambdas/<jurisdiction>  →  official parliament API (egress-pinned)
```

- **Frontend**: Next.js 14 (standalone ARM64 container) on Fargate behind CloudFront;
  Cloudscape-inspired design system without a component-library dependency
- **Agent**: Python (Strands SDK) on Bedrock AgentCore Runtime; consumes
  `<jurisdiction>___search_debates` / `<jurisdiction>___get_debate_text` tools over MCP
- **Gateway**: Amazon Bedrock AgentCore Gateway; per-government Lambda targets share the
  `gov_debates` layer (result contract, egress-pinned HTTP client, pagination, text
  normalisation)
- **Model**: configurable via Bedrock inference profile (with Guardrail attached to every
  invocation)

Details: [docs/architecture.md](docs/architecture.md) ·
[docs/security-model.md](docs/security-model.md) ·
[docs/multi-gov/ADR-001-multi-government.md](docs/multi-gov/ADR-001-multi-government.md) ·
[docs/chat-improvements.md](docs/chat-improvements.md) · [CHANGELOG.md](CHANGELOG.md)

## Prerequisites

1. **AWS account** with CLI access configured
2. **Bedrock model access** for your chosen model/inference profile in the
   [Bedrock console](https://console.aws.amazon.com/bedrock/home#/modelaccess)
3. **API keys** for the keyed sources (UK, EU, CH, AT need no key):
   - **DIP (Bundestag)** — two options:
     - *Public key:* the Bundestag publishes a temporary shared key on the
       [DIP API help page](https://dip.bundestag.de/über-dip/hilfe/api), rotated roughly
       yearly (the key published as of August 2026 is valid until the end of May 2027).
       Fine for demos and evaluation.
     - *Individual key:* request one (free) by mail to
       `parlamentsdokumentation@bundestag.de` — use this for anything long-running.
     - Never commit a key. Locally it belongs in a git-ignored `.env`
       (`agent/.env`, see `agent/.env.example`); a deployment reads it from the
       `parlamentgpt/dip-api-key` secret (step 3 below).
   - **GovInfo (US Congress)**: https://api.govinfo.gov/docs/
4. **Docker** (container images), **Node.js ≥ 18**, **Python ≥ 3.11** (tests)

## Deployment

### 1. Bootstrap CDK (once per account/region)

```bash
cd infra
npm install
npx cdk bootstrap aws://ACCOUNT_ID/REGION
```

### 2. Deploy all stacks

```bash
npx cdk deploy --all --require-approval never
```

This creates 5 stacks:

- `ParlamentGptNetwork-SUFFIX` — VPC, subnets, VPC endpoints
- `ParlamentGptSecurity-SUFFIX` — Bedrock Guardrail
- `ParlamentGptGateway-SUFFIX` — AgentCore Gateway, per-government Lambdas, per-source secrets
- `ParlamentGptAgent-SUFFIX` — AgentCore runtime (container + IAM)
- `ParlamentGptFrontend-SUFFIX` — Fargate (Next.js) + ALB + CloudFront + Cognito user pool + DynamoDB sessions table

### 3. Fill the source API-key secrets

The secrets are created empty; fill them after deploy:

```bash
make fill-secret KEY=YOUR_DIP_API_KEY                                          # Germany (DIP)
make fill-secret KEY=YOUR_GOVINFO_KEY SECRET=parlamentgpt/govinfo-api-key      # US (GovInfo) — only with enableUsCongress=true
```

Keys live ONLY in Secrets Manager (never in env vars or code): each source Lambda is
granted read on exactly its own secret and caches the value for the container lifetime,
so a changed key takes effect on the next cold start.

### 4. Access the app

The CloudFront URL is printed in the stack outputs:

```
ParlamentGptFrontend-SUFFIX.CdnDomainName = https://dXXXXXXXXXX.cloudfront.net
```

## Configuration

Via CDK context in `infra/cdk.json` or `--context` on the CLI:

| Context key | Default | Description |
|---|---|---|
| `region` | `us-east-1` (cdk.json) | AWS region |
| `suffix` | `sample` | Stack-name suffix for isolated deployments |
| `enableUsCongress` | `false` | Provision the US Congress source. Opt-in because its (free) [api.data.gov key](https://api.data.gov/signup/) must be requested by you; fill `parlamentgpt/govinfo-api-key` after deploying |
| `modelId` | `global.anthropic.claude-sonnet-4-6` | Bedrock model ID or inference-profile ARN. The default **global** profile routes invocations to any supported commercial Region worldwide; if data residency matters, set a geographic profile (e.g. an `eu.` one) or a single-Region model (threat model I9) |
| `groundingThreshold` | `0.7` | Guardrail contextual-grounding threshold |
| `relevanceThreshold` | `0.7` | Guardrail relevance threshold |
| `guardrailVersion` | latest | Pin a specific Guardrail version |
| `signupAllowedEmailDomains` | unset | Comma-separated e-mail domain patterns allowed to sign up, enforced by a Cognito PreSignUp trigger: `example.com` (exact), `*.example.com` (domain + subdomains), `amazon.*` (any TLD, incl. `co.uk`). Unset or `*` = open sign-up |
| `selfSignUpEnabled` | `true` | Set `false` to disable self sign-up entirely (operator-created users only) |
| `defaultDebugMode` | `false` | `true` makes new users start with the technical trace visible (demo deployments); users can toggle it either way |
| `signupEmailFrom` | unset | Optional SES sender for Cognito's verification mails. Unset = Cognito's default sender (~50 mails/day, no SES setup needed). When inside `hostedZoneDomain`, an SES identity with DKIM is created. An SES sandbox account only delivers to verified recipients |
| `domainName` / `hostedZoneDomain` | unset | Optional custom domain: CloudFront alias + us-east-1 ACM certificate + Route 53 alias records |

Deploy an isolated copy with a different suffix:

```bash
npx cdk deploy --all --context suffix=mytest
```

> **Careful with existing deployments:** the context values above are part of the stack
> names. Deploying with different values (e.g. a bare `make deploy`, which uses the sample
> defaults `us-east-1`/`sample`) creates a **second, parallel system** instead of updating
> the existing one. Pin the context per environment — the `make deploy-demo` target reads
> your pinned values from an untracked `Makefile.local` and does exactly that.

### Authentication & users

Authentication is Amazon Cognito (hosted UI, OAuth 2.0 authorization-code flow). Visitors
sign up themselves on the hosted UI (e-mail verification code included); the optional
`signupAllowedEmailDomains` context restricts which e-mail domains may register. The app
keeps the verified Cognito ID token in an httpOnly cookie (12 h).

Create users manually (e.g. when self sign-up is disabled):

```bash
make create-user USER=alice@example.com PASS='initial-password-12ch' SUFFIX=... REGION=...
```

### Chat sessions, Confidential & Debug modes

Completed conversations are stored per user in DynamoDB (90-day TTL) and can be reopened
from the sidebar to continue working. Two per-account switches in the header:

- **Confidential** — while on, nothing new is persisted (server-enforced); previously
  saved chats remain listed and untouched.
- **Debug** — while on, every answer carries the full pipeline trace: model reasoning,
  each tool call with parameters, result counts, guardrail interventions, and persistence
  events. Off = a normal chat (answers + citations only).

## Testing

Everything runs locally, no AWS credentials or cost:

```bash
make test              # all suites
make test-shared       # shared Lambda layer (contract, egress pin, pagination, …)
make test-lambdas      # every per-government adapter (fixtures from live responses)
make test-agent        # agent: guardrail, gateway auth, in-process MCP e2e loop
make test-frontend     # jest: route, rate limit, agent client, jurisdictions
make test-infra        # CDK assertions: egress absence, gateway shape
make gen-types-check   # generated frontend Source type is in sync with the Python contract
```

Python suites need ≥ 3.11 (`make test PYTHON=python3.13`, or create the venvs with `uv`).

## Project structure

```
├── agent/                        # Strands agent on AgentCore Runtime
│   └── src/parlamentgpt_agent/
│       ├── main.py               # entrypoint: streaming, history replay, text-fallback
│       ├── agent.py              # model + guardrail + Gateway tools assembly
│       ├── gateway.py            # long-lived MCP client (SigV4/Cognito auth seam)
│       ├── prompts.py            # system prompt (jurisdiction selection, citations)
│       └── config.py             # settings from env vars
├── frontend/                     # Next.js chat UI
│   └── src/
│       ├── app/page.tsx          # chat (SSE consumer, reasoning trace, citations)
│       ├── app/globals.css       # design system (Cloudscape-inspired tokens)
│       ├── app/api/ask/          # rate limit + AgentCore/local-agent streaming
│       ├── app/api/auth/         # Cognito OAuth code flow (login/callback/logout)
│       ├── app/api/sessions|settings # chat persistence + per-user toggles
│       └── lib/                  # auth (jose), session store, jurisdiction metadata
├── lambdas/                      # one adapter per parliament + shared layer
│   ├── shared/python/gov_debates # contracts, pinned HTTP client, pagination, ingest
│   ├── germany/ uk/ europarl/ switzerland/ austria/ uscongress/
│   └── canada/ australia/ france/ netherlands/   # built, disabled
├── infra/                        # CDK (5 stacks)
│   └── lib/
│       ├── jurisdictions.ts      # jurisdiction registry (single source of truth)
│       ├── gateway-stack.ts      # AgentCore Gateway + Lambda targets + secrets
│       └── network|security|agent|frontend-stack.ts
└── docs/                         # architecture, security model, runbook, multi-gov ADR
```

## Costs

This sample runs **always-on infrastructure** — it does not scale to zero. A deployed
stack bills continuously for an ECS Fargate task (1× ARM64, always running), an
Application Load Balancer, CloudFront, a NAT-less VPC with interface endpoints, DynamoDB
(on-demand), the Bedrock AgentCore Runtime and Gateway, and up to 11 Lambda functions
(billed per invocation), plus Bedrock model invocations per question asked. Tear it down
when you are done:

```bash
make destroy   # or: cd infra && npx cdk destroy --all
```

## Before running this in production

The defaults here suit a demo/sample deployment. The threat model
([docs/threat-model.md](docs/threat-model.md)) records every residual risk; these are the
ones whose fix is a **deployment decision rather than a code change**, so they are left to
the operator:

| # | Do this for production | Why |
|---|---|---|
| 1 | Leave `defaultDebugMode` unset (the default) | New users then start without the technical trace. The demo sets `--context defaultDebugMode=true` to show the pipeline; the trace exposes model ids, token counts and raw upstream payloads — internal detail, not secrets |
| 2 | Shorten the ID-token lifetime (`idTokenValidity`, currently 12 h) | Sign-out is enforced server-side (revocation marker checked on every data/model call), but the token stays cryptographically valid until it expires. A shorter lifetime narrows that window; add a refresh-token flow if the shorter session is disruptive |
| 3 | Move rate limiting to a shared store (DynamoDB/ElastiCache) before scaling past one task | The in-memory limiter counts per task, so N tasks allow N× the quota. It keys on the verified user, so it is not header-spoofable |
| 4 | Enable access + invocation logging: CloudFront/ALB access logs to S3, Bedrock model-invocation logging, and (if required) an application audit trail | The app deliberately does not log prompts or answers, so attribution is otherwise coarse (threat model R1) |
| 5 | Re-measure the Guardrail grounding/relevance thresholds on real traffic (`groundingThreshold`, `relevanceThreshold`) | They were tuned on German-only text; several sources now serve machine translation, and a silent grounding block looks like a bad answer |
| 6 | Keep chat history server-side instead of accepting it from the client | Today the client replays prior turns (size-capped and validated). Trusted history removes that input surface entirely — see [docs/chat-improvements.md](docs/chat-improvements.md) |
| 7 | Set `RemovalPolicy.RETAIN` on the sessions table and the Cognito user pool | Both are `DESTROY` here so a demo stack cleans up after itself |
| 8 | Review the DynamoDB TTL (90 days) and Confidential-mode UX against your data-retention policy | Stored conversations can reveal what a named user researched |
| 9 | Raise the edge read timeout (or add SSE keep-alives) if you raise any Lambda timeout above ~55 s | Lambda timeouts are capped just below the 60 s CloudFront/ALB origin read timeout so slow queries fail fast instead of burning time on a response nobody receives |
| 10 | Add a network-level egress control (NAT + allowlist, or VPC endpoints) around the adapter Lambdas | Host pinning is enforced in the application layer today, so it is the only egress control (threat model M2) |

## Security considerations

What the deployed stacks actually implement — and, just as deliberately, what they do
**not**: there is **no AWS WAF and no edge rate limiting or bot protection** in this
sample (removed to keep demo cost down). The edge control set is CloudFront TLS plus the
origin lockdown below; per-user rate limiting happens in the application. The full
analysis, including every accepted residual risk, is in
[docs/threat-model.md](docs/threat-model.md) and
[docs/security-model.md](docs/security-model.md).

- **Access control**: Amazon Cognito (hosted UI); the middleware verifies the ID token
  (JWKS signature, issuer, audience, expiry) on every request — unauthenticated pages
  redirect to sign-in, API calls get 401. Sign-up can be domain-restricted via a
  PreSignUp trigger
- **Egress pinning**: every adapter Lambda may only reach its allowlisted hostnames
  (`ALLOWED_HOSTS` injected from the registry, enforced by the shared pinned HTTP client)
- **Guardrail**: Bedrock Guardrail (topic filter, contextual grounding, prompt-injection
  handling) attached to every model invocation, plus a reinforcing system prompt
- **Origin protection**: ALB accepts only CloudFront's origin-facing prefix list and a
  CloudFront-injected secret header
- **No secrets in code**: all source API keys and auth material in Secrets Manager
- **Compliance**: per-source attribution/licence texts are rendered in the UI; machine
  translation and uncorrected transcripts are labelled (see `docs/multi-gov/COMPLIANCE.md`)

## Local development

```bash
# Agent (needs AWS creds for Bedrock + a reachable Gateway, see agent/.env.example)
cd agent
pip install -e '.[dev]'
PYTHONPATH=src python -m parlamentgpt_agent.main

# Frontend against the local agent
cd frontend
npm install
AGENT_LOCAL_URL=http://localhost:8080 npm run dev
```

## License

This sample is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.
