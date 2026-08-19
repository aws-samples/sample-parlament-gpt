# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
The project is not yet version-tagged; entries are grouped by merge date.
Per-jurisdiction data-source details live in `docs/multi-gov/CHANGES.md`.

## [Unreleased]

### Security
- Advisory round: token verification decoupled from the code-exchange secret
  (`authConfigured()` vs. new `exchangeConfigured()` — a runtime that can verify but not
  exchange keeps sessions working instead of looping sign-in); answer-fidelity failure
  paths log at ERROR (T10/A6 must not hide below alarm level); a
  `ParlamentGPT/RevocationDegraded` metric makes a revocation-store outage alarmable
  (S7); S6 discloses the dormant Cognito gateway-auth fallback; ADR-001 carries a
  point-in-time header naming its known drift; model-ID references unified to the real
  default (`.env.example`, runbook).
- Threat-model honesty round: S2 now states that the sign-up allowlist **defaults to
  open** (restricting it is a per-deployment context decision), D1 states that its cost
  bound assumes that restriction (with open sign-up an attacker rotates accounts, not
  headers), and a new I9 row covers **data residency** — the default global inference
  profile routes invocations to any supported commercial Region, Confidential mode
  suppresses persistence but not transmission, and the README's `modelId` row explains
  how to pin a geographic profile instead.
- Pre-submission review round (F-1…F-9): the debug-trace default now comes from
  `DEFAULT_DEBUG_MODE` on **every** path (the no-session-store settings response and the
  client pre-fetch state no longer hardcode `true`); the AgentCore custom resource is
  scoped to exactly its three lifecycle calls instead of `bedrock-agentcore:*`, and uses
  the runtime's bundled AWS SDK instead of installing "latest" at deploy time; the
  Gateway no longer ships `exceptionLevel: DEBUG`; `authConfigured()` also requires the
  client secret (clear failure instead of an opaque Cognito 400); the remaining open
  version bounds are pinned (`lambdas/shared/pyproject.toml`,
  `lambdas/germany/requirements.txt`) so the shipped pins no longer depend on a bundling
  exclude list; the threat model gains T10 (answer/citation tampering against asset A6,
  honestly partial) and corrected E6/I4 wording; the README disclaimer now asks for a
  security and legal review before production use.
- Shared-layer bundling is now **container-only with a fully literal command** — the
  host-pip `local.tryBundle` path (child_process on framework-supplied directories) is
  removed; test suites skip asset bundling via CDK context. Per-jurisdiction Lambda asset
  directories are assembled from literals inside the registry and all per-jurisdiction
  resources are built inline in the stack constructor, so no variable path segment or
  function argument ever reaches an fs/asset call.
- **Python dependencies pinned exactly** (`agent/requirements.txt`, `agent/pyproject.toml`,
  `lambdas/shared/layer-requirements.txt`): the agent relies on a private strands-agents
  accessor for MCP session recovery, so containers must run the versions the tests ran
  against (strands-agents 1.52.0). A failed session-state probe now logs a warning instead
  of degrading silently.
- Agent stream errors are capped (exception class + 200 chars) instead of piping the raw
  exception string into the SSE response; malformed tool-result blocks now log a warning
  before the best-effort fallback; the text-recovery parser tolerates pathological nesting
  (`RecursionError`). Threat model I4/I5/T7 updated; stale donation-button references
  removed from the compliance docs (the button no longer exists).
- Resolved all open Dependabot alerts (29 alerts, 23 distinct advisories, all in
  transitive npm dependencies): `aws-cdk-lib` 2.259.0 → 2.265.0 (bundling command
  injection), refreshed lockfiles for the `brace-expansion`, `js-yaml`, `nanoid` and
  `fast-uri` DoS advisories, and pinned `postcss` 8.5.26 + `sharp` 0.35.3 via npm
  overrides (path-traversal/file-read and libvips CVEs in the versions Next.js
  bundles). Both npm manifests audit clean. Threat model updated to match (T7, asset
  A3, system diagram: US Congress opt-in).

### Fixed
- `infra/jest.config.js` was missing from the published sample: the nested
  `infra/.gitignore`'s `*.js` overrides the root-level negation, so fresh clones (and the
  content sync) silently dropped it and the sample could not run its infra tests. The
  negation now lives in `infra/.gitignore`, and the publisher verifies that every
  exported file is actually tracked after the sync.

### Changed
- COMPLIANCE.md currency pass: US Congress row reflects the opt-in reality, C7 (GovInfo
  key) and W8 (SigV4 MCP validation) are marked resolved, and the secret names match the
  deployed `parlamentgpt/*` names.
- The maintainers' publish workflow now runs from `main` and generates the internal
  `public` branch as a cleaned content mirror (file-for-file identical to the GitHub
  sample); internal working files exist on `main` only.
- Naming finalised to **ParlamentGPT** everywhere: the agent package is
  `parlamentgpt_agent`, npm/pip package names and default HTTP User-Agent strings say
  `parlamentgpt`, and the last references to the legacy working title are gone from
  README, docs and source profiles.

### Added
- US Congress is now **opt-in** (`--context enableUsCongress=true`): its free GovInfo
  API key must be requested at api.data.gov by the operator, so a default deploy no
  longer provisions a source that cannot work out of the box. The UI advertises exactly
  the provisioned sources (list baked into the frontend image at build time).
- `make sync-public`: maintainers' publish workflow for the GitHub sample repository
  (branch/cleanliness gates, forbidden-content scan, changelog discipline; the sync
  tool itself is not part of the published sample).
- **Amazon Cognito authentication** (hosted UI, OAuth 2.0 authorization-code flow)
  replacing the entire self-built auth stack. Sign-up domain allowlist with wildcards via
  a PreSignUp trigger (`signupAllowedEmailDomains`: `example.com`, `*.example.com`,
  `amazon.*`; unset/`*` = open). ID token verified per request (jose/JWKS) from an
  httpOnly cookie.
- **Persistent chat sessions** (DynamoDB, 90-day TTL): sidebar with past conversations,
  reopen-and-continue, delete; session id in the URL survives reloads.
- **Confidential mode** (per account, server-enforced): while on, nothing new is
  persisted; previously saved chats remain listed and untouched.
- **Debug mode** (per account): toggles the full pipeline trace — reasoning, tool calls,
  result counts, guardrail interventions, persistence events. Off = plain chat.
- Agent emits explicit `guardrail` trace events (output redaction, guardrail stop
  reason, guardrail-classified stream errors) instead of silently returning the refusal.

### Removed
- Legacy auth: login page, `/api/login`, `/api/logout`, `/api/signup*`, user-store and
  session-signing secrets, scrypt user store, SES verification mailer, create-user
  script, and the `authAllowedUsernamePattern`/`authSelfSignup` contexts (superseded by
  `signupAllowedEmailDomains`/`selfSignUpEnabled`). Client-side sessionStorage chat
  persistence (superseded by server-side sessions).

- Cloudscape-inspired design system (`frontend/src/app/globals.css`): design tokens, dark
  navy top navigation, container cards, badges, focus rings — no component-library
  dependency.
- Conversation persistence across reloads via tab-scoped `sessionStorage` (bounded,
  versioned key; cleared on sign-out).
- `docs/chat-improvements.md`: evaluation and phased plan for context retention and
  persistent chat sessions (DynamoDB session store, agent-side session management,
  context summarisation).
- Design notes for this workstream in `docs/chat-improvements.md`.

### Changed
- Rebranded to **ParlamentGPT** (from the original single-parliament product name) to match the multi-parliament
  coverage: UI (header, login page, HTML metadata), CDK stack names
  (`ParlamentGpt*-<suffix>`), gateway/runtime/guardrail resource names, project tag, and
  the source secret names (`parlamentgpt/dip-api-key`, `parlamentgpt/govinfo-api-key`).
- Optional sign-in restriction `AUTH_ALLOWED_USERNAME_PATTERN` (regex, unset by default;
  set per deployment via CDK context `authAllowedUsernamePattern`).
- Citations now render caveats (machine translation, uncorrected/scanned transcripts) as
  badges; reasoning trace uses text markers instead of emojis.
- Comment hygiene across agent and tests: model vendor/version references and meta
  narration removed; factual operational notes retained. Test double `FakeClaude` renamed
  to `FakeBedrockModel`.

### Fixed
- Open redirect on `/login?next=` (protocol-relative and backslash forms rejected).
- SSE consumer finalises the assistant message when the stream ends without an `answer`
  event (no more permanently hanging "Thinking…" state); residual frame and decoder are
  flushed at stream end.
- Text-fallback answer read a non-existent `session_ref` alias (`session_number`) and
  claimed all results came from the DIP API regardless of jurisdiction.
- In-memory rate limiter now sweeps expired buckets (bounded memory).
- Removed the dead non-streaming agent client path; its tests now cover the streaming
  path including history forwarding.
- Chat messages use stable keys instead of array indexes.

## 2026-08-12 — Multi-government gateway

### Added
- Multi-government architecture: Amazon Bedrock AgentCore Gateway with one Lambda target
  per parliament (`lambdas/<jurisdiction>/`) and a shared `gov_debates` Lambda layer
  (contracts, egress-pinned HTTP client, pagination, normalisation, ingest store).
- Adapters for ten jurisdictions; enabled: Germany (DIP), UK (Hansard), European
  Parliament, Switzerland, Austria, US Congress (GovInfo). Built but disabled pending
  licensing or ingest pipelines: Canada, Australia, France, Netherlands.
- Jurisdiction registries (`infra/lib/jurisdictions.ts`, `frontend/src/lib/jurisdictions.ts`)
  with per-source egress allowlists, attribution/licence texts, and query-language notes.
- `GatewayStack` (AgentCore Gateway, per-source secrets), gateway/guardrail assertion tests,
  generated frontend `Source` type with drift check (`make gen-types-check`).
- Source profiles and compliance documentation under `docs/multi-gov/`.

### Changed
- The agent consumes tools over MCP from the Gateway (SigV4 or Cognito auth) instead of
  local Python tool functions; `dip_client.py`, `tools.py`, and `secrets.py` removed.
- Conversation history is passed per request from the client and replayed into the agent
  (stateless server).

## 2026-07-20 — Authentication

### Added
- Application-level login (username/password against a Secrets Manager user store, scrypt
  hashes), HMAC-signed session cookies (12 h), middleware protecting pages and API routes,
  logout route, and user-management tooling (`make create-user` / `make set-users`).

## 2026-06-16 · 2026-06-23 — Initial releases

### Added
- Initial security-hardened Bundestag speech Q&A agent (Strands SDK on Bedrock AgentCore
  Runtime) with DIP API tools, Bedrock Guardrail, and egress host pinning.
- Chat UI with live reasoning trace (SSE streaming), conversation context, and data-source
  attribution; CloudFront + AWS WAF edge (silent challenge) in front of the Fargate
  frontend.

### Fixed
- Streaming/guardrail-redaction handling, agent custom-resource updates, IAM and timeout
  issues, DIP API integration hardening.
