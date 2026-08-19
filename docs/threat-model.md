# Threat Model — ParlamentGPT (STRIDE)

| | |
|---|---|
| Scope commit | branch `main`, 2026-08-19 |
| Method | STRIDE per trust boundary, then a per-element sweep; every claimed mitigation was verified against the code at the cited location during this review |
| Status legend | ✅ mitigated · 🟡 partial / residual risk · 📋 accepted (documented decision) · 🔴 open |

Companion documents: [security-model.md](security-model.md) (control descriptions),
[multi-gov/COMPLIANCE.md](multi-gov/COMPLIANCE.md) (per-source licensing),
[chat-improvements.md](chat-improvements.md) (known-constraint backlog).

---

## 1. System overview

```
Browser ──HTTPS──▶ CloudFront ──HTTP + secret header──▶ ALB ──▶ Fargate (Next.js)
   │                                                                │      │
   └──OAuth 2.0 code flow──▶ Cognito Hosted UI                      │      └──▶ DynamoDB
                                                                    │           (sessions, settings)
                                          InvokeAgentRuntime (SigV4)│
                                                                    ▼
                                              AgentCore Runtime (Strands agent)
                                                   │                    │
                                       Bedrock model + Guardrail ◀──────┤
                                                                        │ MCP (SigV4)
                                                                        ▼
                                                            AgentCore Gateway
                                                                        │
                                                    one Lambda per parliament
                                                       (germany, uk, europarl,
                                                        switzerland, austria;
                                                        uscongress is opt-in)
                                                          │            │
                             host-pinned egress ──▶ official APIs      └──▶ Secrets Manager
                                                                            (source API keys)
```

### 1.1 Assets

| # | Asset | Why it matters |
|---|---|---|
| A1 | User identity / session (Cognito ID token in `pg_id` cookie) | Account takeover = access to another user's chat history |
| A2 | Stored conversations + per-user settings (DynamoDB) | Reveals what a named employee researched; Confidential mode is a promise about this asset |
| A3 | Source API keys in Secrets Manager (DIP; GovInfo only when the opt-in US Congress source is provisioned — a default deploy creates neither the Lambda nor the empty secret) | Leak = quota abuse and attribution of scraping to us |
| A4 | Cognito app-client secret | Enables minting sessions if combined with an authorization code |
| A5 | Model invocation capability (Bedrock) | Cost abuse; reputational risk from off-topic generation |
| A6 | Answer integrity / citation truthfulness | The product claim is "only official records"; a fabricated or mislabelled quote is the worst-case product failure |
| A7 | Availability of the demo | Used in customer-facing sessions |
| A8 | Origin infrastructure (ALB/Fargate) | Bypassing CloudFront removes edge TLS termination and reaches the origin directly (no WAF exists in this sample; the controls at stake are the prefix-list security group and the secret origin header) |

### 1.2 Trust boundaries

| # | Boundary | Crossing data |
|---|---|---|
| TB1 | Internet → CloudFront/ALB → Fargate | Untrusted HTTP: prompts, history, session ids, settings, cookies |
| TB2 | Browser → Cognito Hosted UI (separate origin) | Credentials, authorization code, `state` |
| TB3 | Fargate → AgentCore Runtime | Prompt + caller-supplied history (SigV4-signed) |
| TB4 | Agent → Bedrock model + Guardrail | Prompt, tool schemas, retrieved third-party text — with the default **global** inference profile this crosses Region boundaries (see I9) |
| TB5 | Agent → Gateway → Lambdas | Model-chosen tool name and arguments (LLM output = untrusted input) |
| TB6 | Lambda → official parliament API | Outbound request; **inbound response is lower-trust data** |
| TB7 | App → DynamoDB / Secrets Manager | Per-user partitioned data; credentials |

### 1.3 Actors

- **Anonymous internet user** — no account; may attempt sign-up.
- **Authenticated user** — any `@amazon.*` address that completed Cognito sign-up. Peer users are a real threat source for A2 (horizontal access).
- **Malicious upstream / MITM on an upstream API** — controls parliament API responses (TB6).
- **The model itself** — treated as an untrusted, promptable component (TB4/TB5), not as a trusted controller.
- **Operator** — holds AWS Admin; out of scope as an attacker, in scope for misconfiguration.

---

## 2. STRIDE by threat

### 2.1 Spoofing

| ID | Threat | Status | Mitigation / evidence |
|---|---|---|---|
| S1 | Forged or tampered session cookie to impersonate a user | ✅ | ID token verified on **every** request: signature against the pool JWKS, `iss`, `aud == clientId`, `token_use == "id"`, expiry — `frontend/src/lib/auth.ts` (`verifyIdToken`), enforced in `src/middleware.ts` and re-checked per API route via `getAuthedUser`. Six negative tests cover wrong audience, wrong issuer, access token, expired, garbage (`test/auth.test.ts`). |
| S2 | Sign-up as someone else / from an outside domain | ✅ (when restricted) | Cognito owns the credential flow (e-mail verification code); a PreSignUp Lambda rejects domains outside `signupAllowedEmailDomains` **before** an account or mail exists (`infra/lambda/pre-signup/`). **The allowlist defaults to OPEN**: unset or `*` means anyone can self-register (a bare `cdk deploy` ships that way; README documents it) — restricting sign-up is a per-deployment decision via the `signupAllowedEmailDomains` context, and D1's cost bound assumes you made it. 7 matcher tests incl. `evilamazon.com`, `example.com.evil.org` (`infra/test/domain-matcher.test.ts`). Live-verified: `mallory@evil.org` → `UserLambdaValidationException`. |
| S3 | Address-shape spoofing (`amazon.de` as a subdomain/suffix trick) | ✅ | Matcher anchors every pattern (`^…$`), and `domain.*` does not imply subdomains — verified by test. |
| S4 | OAuth code interception / login CSRF | ✅ | Random `state` in an httpOnly cookie, compared on callback and used to carry the post-login path; mismatch restarts the flow (`api/auth/callback/route.ts`). Code exchange is server-side with the client secret (confidential client), so a stolen code alone is not enough. |
| S5 | Attacker reaches the origin directly, bypassing CloudFront | ✅ | ALB security group admits only CloudFront's managed origin-facing prefix list, **and** a listener rule requires a per-account/stack secret header; the default action is a 403 fixed response (`infra/lib/frontend-stack.ts`). |
| S6 | Impersonating the agent/Gateway (rogue MCP endpoint) | ✅ | Gateway auth is SigV4 with the runtime's IAM role (`GATEWAY_AUTH_MODE=iam`); the URL comes from the CDK output, not from user input (`agent/src/parlamentgpt_agent/gateway.py`). |
| S7 | Post-logout session reuse | ⚠️ partial (best-effort revocation) | Sign-out records a revocation marker (`sk=REVOCATION`, own TTL) and every authenticated route rejects tokens issued at or before it — `lib/authGuard.ts` (`requireUser`), used by `/api/ask`, `/api/sessions*`, `/api/settings`; 7 tests incl. the same-second edge case (`test/authGuard.test.ts`). Residual 1: the check **fails open** — if the revocation store is unreachable, a cryptographically valid token is accepted for its remaining lifetime (up to 12 h), a deliberate availability trade-off (`authGuard.ts`). Residual 2: the page shell (Edge middleware, signature-only) still renders for a revoked token — it carries no user data. Shortening the 12 h token lifetime remains a production decision (README #2). |

### 2.2 Tampering

| ID | Threat | Status | Mitigation / evidence |
|---|---|---|---|
| T1 | Client edits `history` to fake what "the assistant said" and steer the answer | 📋 (prod: README #6) | Accepted by design and bounded: history is caller-supplied context only, never authority. Validated shape + role allowlist, capped at 12 turns (`api/ask/route.ts`) and again at 12 turns / 4,000 chars per message with a leading-user-turn rule in `_coerce_history` (`agent/main.py`). It can influence phrasing; it cannot grant access or bypass the guardrail. Documented in chat-improvements.md as failure mode 5. |
| T2 | Prompt injection via the user prompt (jailbreak, exfiltrate system prompt) | 🟡 | Layered: Bedrock Guardrail `PromptInjection` denied-topic + `PROMPT_ATTACK` filter at HIGH (`infra/lib/security-stack.ts`), plus rule 8 of the system prompt, plus a 500-char client/route cap and 2,000-char agent cap. Residual: no defence is complete; the blast radius is deliberately tiny — the agent has no write tools, only two read tools per parliament. |
| T3 | **Indirect prompt injection via retrieved parliament text** (a speech contains "ignore your instructions") | ✅ (bounded) | Explicit rule 9 of the system prompt: tool results are DATA, never instructions — instruction-shaped retrieved text must be treated as quoted content and never obeyed (`agent/prompts.py`). Structurally bounded as well: read-only tool surface, no tool accepts a URL from model output (T4), and the Guardrail's prompt-attack filter also scores retrieved content. Residual: a hostile upstream can still influence answer *wording*; that is inherent to summarising third-party text. |
| T4 | Model-driven SSRF: model asks a Lambda to fetch an attacker-chosen host | ✅ | Egress is host-pinned per Lambda from the static registry (`ALLOWED_HOSTS` injected by CDK, never from a caller); the shared client validates every URL, blocks cross-host redirects, and is suffix-confusion tested (`lambdas/shared/python/gov_debates/http/pinned_client.py`, `tests/test_egress.py`). Tool arguments are query parameters, not URLs. |
| T5 | XXE / entity-expansion via upstream XML | ✅ | CA/FR/NL adapters parse through `defusedxml`; hostile payloads are skipped like malformed input (`ourcommons.py`, `syseron.py`, `vlos.py`). Stdlib etree remains imported for Element types only. |
| T6 | Cross-user tampering with stored chats (write into someone else's session) | ✅ | Every store call derives the partition key from the verified token `sub`; the session id from the URL is never used as an identity and is shape-validated (`^[a-zA-Z0-9-]{1,64}$`) — `api/sessions/[id]/route.ts`, `lib/sessionStore.ts`. There is no cross-user index. |
| T7 | Supply-chain tampering (dependency or base image swap) | ✅ | `package.json` pinned to exact versions in frontend + infra, `npm ci` lockfile-strict in the image, base images pinned to **digests**, plus an apt security-upgrade layer (both Dockerfiles). Transitive advisories are also held down: npm `overrides` pin the `postcss`/`sharp` versions Next.js bundles to patched releases, and both manifests audit clean (all Dependabot alerts resolved, incl. the `aws-cdk-lib` <2.260 bundling command injection). Python is pinned exactly too: `agent/requirements.txt` + `pyproject.toml` and the Lambda layer's `layer-requirements.txt` carry `==` versions (required in particular because `gateway.py` relies on a private strands accessor). Residual: pip installs are version-pinned but not hash-checked. |
| T8 | Session-cookie tampering in transit | ✅ | Cookies `httpOnly`, `secure`, `sameSite=lax`; CloudFront redirects HTTP→HTTPS with TLS ≥ 1.2_2021. |
| T9 | Open redirect used to plant a phished continuation | ✅ | `next` is restricted to same-origin paths, rejecting `//` and `/\` (`api/auth/login/route.ts`, plus the same check on the state-carried path in the callback). |
| T10 | Answer/citation tampering: the model fabricates, mislabels or mis-attributes a quote (asset A6 — the worst-case product failure) | ⚠️ partial | Layered, not absolute: the Guardrail's contextual-grounding filter scores answers against tool results (threshold configurable, P4); the system prompt mandates source citations; citations are rendered from structured tool results, not from model prose (`page.tsx` sources come from the `sources` array); a source-extraction failure logs loudly before the canned-fallback path replaces an answer (`main.py`). Residual: grounding is statistical — a fluent, wrongly-nuanced summary above threshold passes; machine-translated sources are labelled but still model-mediated. No mechanical quote-verification exists (that would be an ingest-side diff of quoted spans against the official text). |

### 2.3 Repudiation

| ID | Threat | Status | Mitigation / evidence |
|---|---|---|---|
| R1 | User denies having asked something / no attribution of actions | 🟡 (prod: README #4) | Requests are authenticated, stored sessions carry the owner's `sub` and timestamps, and ECS task logs exist. Prompts are deliberately never logged, so attribution is coarse; access/invocation logging is a deployment decision. |
| R2 | Confidential mode leaves no trace that data was intentionally not stored | 📋 | Accepted: that is the point of the mode. The UI records the decision in the visible trace ("Persistence skipped — confidential mode is on") for the current turn only. |
| R3 | Guardrail block is invisible after the fact | ✅ (UX-level) | Interventions surface as explicit `guardrail` trace events instead of a silent refusal (`agent/main.py`, `_detect_guardrail` + stream `redactContent` handling); Bedrock retains its own invocation logging when enabled. |

### 2.4 Information disclosure

| ID | Threat | Status | Mitigation / evidence |
|---|---|---|---|
| I1 | Reading another user's conversations | ✅ | Partition key is `USER#<sub>` from the verified token; no listing across users; TTL 90 days (`lib/sessionStore.ts`). Live-verified via the E2E script. |
| I2 | Source API keys or Cognito secret leaking | ✅ | All credentials live in Secrets Manager; each Lambda is granted read on **only** its own secret (CDK), values are cached in-process and never logged; the Cognito client secret is injected as an ECS secret, never a task-definition env var. No key in code or in git. |
| I3 | Confidential mode silently ignored by a stale/hostile client | ✅ | Server-side backstop: `POST /api/sessions` and `PUT /api/sessions/:id` re-read the setting and return **409** while confidential — the client is not trusted to enforce it (`api/sessions/*`). Live-verified (409 on both, GET still 200). |
| I4 | Debug trace exposes internals to the wrong audience | ✅ | The default is **off** on every path: `DEFAULT_DEBUG_MODE` env (from the `defaultDebugMode` context) is the single source of the default, honoured also on the no-session-store settings path and in the client's pre-fetch state — no hardcoded `debug: true` remains. Content is our own or public parliament data — no credentials, no cross-user data — and raw payloads are capped at 4000 **characters** (`_RAW_PREVIEW_CHARS` in `_summarise_tool_result` — for non-ASCII content the byte size can exceed that figure). Users toggle it per account. |
| I5 | Verbose errors leaking stack traces to the browser | ✅ | API routes return terse JSON (`{error: …}` / `{answer:"",sources:[]}`) with the detail going to server logs (`api/ask/route.ts`, auth routes). The agent's own stream errors are capped at 200 chars and reduced to exception class + message, full detail in the service log (`main.py`); residual: the capped string may still name internal identifiers (ARNs, model ids) — visible to authenticated users only, same class of detail the opt-in debug trace shows. |
| I6 | User enumeration on sign-in/sign-up | ✅ | `preventUserExistenceErrors: true` on the pool client; PreSignUp rejection is a generic domain message. |
| I7 | Prompt/answer content in logs | ✅ | Neither the route nor the agent logs prompt or answer text; only error objects. |
| I8 | Cross-tenant data in the model context | ✅ | The agent clears its message list per request and only replays the caller's own history (`main.py`); a documented constraint keeps this valid only while the runtime serialises requests per container (see M1). |
| I9 | Data residency: prompts leave the deployment Region | ⚠️ operator decision | The default `modelId` is a **global cross-Region inference profile**, which routes each invocation to any supported commercial AWS Region worldwide — so the content crossing TB4 (questions, replayed history, retrieved parliament text) is processed wherever the profile routes it, regardless of where the stacks run. That matters precisely because of asset A2 (what a named user researched), and **Confidential mode does not change it**: it suppresses persistence, not transmission. Deployers with residency requirements must set `modelId` to a geographic profile (e.g. an `eu.` profile) or a single-Region model — a context parameter, no code change (README: Configuration). |

### 2.5 Denial of service

| ID | Threat | Status | Mitigation / evidence |
|---|---|---|---|
| D1 | Prompt flooding → Bedrock cost / quota exhaustion | ✅ (single task, closed sign-up) | The limiter now keys on the **verified token subject**, not on the spoofable `x-forwarded-for`, so rotating headers buys no quota (`api/ask/route.ts`, asserted in `test/route.test.ts`); 20 req/60 s plus prompt-length caps. `/api/ask` also authenticates itself instead of relying on the middleware alone. **This bound assumes a closed sign-up allowlist (S2)**: with open sign-up an attacker rotates accounts instead of headers, each with its own quota, and the deployer pays for the Bedrock calls. Residual: counts are per task, so scaling out needs a shared store (README #3). |
| D2 | Unbounded memory growth in the limiter | ✅ | Expired buckets are swept once the map exceeds 1,000 entries (`api/ask/rateLimit.ts`). |
| D3 | Oversized upstream responses exhausting Lambda memory | ✅ | Per-source memory/timeout sizing in the registry (e.g. Canada 1 GB for ~5.5 MB stream-parsed responses, Switzerland 150 s for a slow upstream), `max_results` clamped, text truncated with `truncated` flags. |
| D4 | Storage growth / cost from stored chats | ✅ | Per-session caps (50 messages, ~200 KB) and DynamoDB TTL (90 days), PAY_PER_REQUEST. |
| D5 | Next.js DoS advisories (image optimizer, server actions, RSC deserialization) | ✅ | Fixed by the upgrade to Next 15.5.23 (the RSC-deserialization and image-optimizer advisories have no 14.x patch; both are fixed from 15.5.10). |
| D6 | Slow upstream stalls the request path | ✅ | Lambda timeouts are now capped below the 60 s CloudFront/ALB origin read timeout (Switzerland 150 s → 55 s, `infra/lib/jurisdictions.ts`): a very slow query fails fast instead of burning Lambda time on a response the browser can no longer receive. Raising it requires raising the edge timeouts or adding SSE keep-alives first (README #9). |

### 2.6 Elevation of privilege

| ID | Threat | Status | Mitigation / evidence |
|---|---|---|---|
| E1 | Unauthenticated access to app or APIs | ✅ | Middleware protects everything except `/api/auth/*` and static assets; API calls without a valid token get 401, pages redirect to the hosted UI. Live-verified (401 / 307). |
| E2 | Frontend task credentials used beyond their purpose | ✅ | Task role holds exactly: `bedrock-agentcore:InvokeAgentRuntime` on **one** runtime ARN, read/write on the sessions table, read on the Cognito secret (and `ses:SendEmail` only if a custom sender is configured, scoped by `ses:FromAddress` condition). |
| E3 | Lambda reaching another jurisdiction's secret or the index bucket | ✅ | Per-Lambda secret grants; ingest write access only for batch-ingest sources; query Lambdas get read-only. |
| E4 | Model escaping its tool sandbox | ✅ | Only two read tools per enabled parliament exist, discovered from the Gateway; there is no code-execution, file, or write tool. The prose-tool-call recovery path fails **closed** on unknown kwargs (`_parse_search_tool_call` allowlist). |
| E5 | Guardrail bypass by scope drift | ✅ | The denied-topic definition covers any non-parliamentary request and explicitly keeps *every* legislature in scope, with regression tests asserting that no example blocks a foreign parliament (`infra/test/guardrail.test.ts`). |
| E6 | Disabled jurisdictions activated accidentally (licensing/robots exposure) | ✅ | `enabled: false` gates Lambda **and** Gateway target creation — with no Gateway target, no tool exists regardless of what any UI shows; Canada's adapter additionally refuses by default (`RESPECT_ROBOTS=true`). The frontend list is cosmetic on top: baked in at image build time from the provisioned set, falling back to the static flags in local dev. |

---

## 3. Code review findings from this pass

| # | Finding | Action |
|---|---|---|
| F1 | `source_url` from upstream APIs was rendered into an anchor `href` without scheme validation — a `javascript:`/`data:` URL from a compromised or spoofed upstream would have become a clickable XSS sink (T-U2). | **Fixed in this commit**: `safeHttpUrl()` gates citation links to `http(s)` (`frontend/src/app/page.tsx`). Markdown links already route through react-markdown's sanitiser. |
| F2 | Answer text is rendered as markdown; a hostile answer could contain links. | Verified safe: react-markdown does not render raw HTML by default (no `rehype-raw`), and the custom `a` renderer forces `target="_blank" rel="noopener noreferrer"`. |
| F3 | Rate-limit key trusted `x-forwarded-for`, and `/api/ask` was protected by the middleware alone. | **Fixed in this pass**: the route calls `requireUser` itself and the limiter keys on the verified subject. |
| F4 | Module-global Strands agent with per-request `messages.clear()`. | Documented constraint (M1 below); inline comment already present in `main.py`. |
| F5 | Session TTL vs. token lifetime asymmetry (12 h token, no revocation). | **Fixed in this pass** for the data plane via sign-out revocation (`lib/authGuard.ts`); the token-lifetime decision is a production choice (README #2). |

### Known constraints (unchanged, tracked)

- **M1** — ✅ mitigated: access to the module-global agent is serialised by `_AGENT_LOCK` (`agent/main.py`), acquired off the event loop in the streaming path, so a concurrent invocation waits instead of interleaving two users' messages. Normally uncontended (one request per container today). Throughput per container therefore stays at one conversation; moving state off the global is still the long-term fix (chat-improvements.md phase 2).
- **M2** — Application-layer host pinning is the *only* egress control for the Lambdas (no NAT/network backstop). A bug in `pinned_client.py` is therefore a security bug; it is covered by dedicated tests.

---

## 4. Priorities

Everything actionable in code was fixed in this pass. What remains is a deployment
checklist, kept in [README — Before running this in production](../README.md#before-running-this-in-production):

| Priority | Item | Threat |
|---|---|---|
| P1 | Shorten the ID-token lifetime (revocation is already enforced server-side) | S7 |
| P2 | Shared-store rate limiting before scaling past one task | D1 |
| P3 | Enable access/invocation logging | R1 |
| P4 | Re-measure grounding thresholds on real multilingual traffic | A6 |
| P5 | Server-side history instead of client-replayed turns | T1 |
| P6 | Network-level egress backstop for the adapter Lambdas | M2 |

## 5. Out of scope

Operator/insider abuse of AWS Admin, physical/host security of managed AWS services,
availability of upstream parliament APIs, correctness of the parliaments' own records, and
i18n of the UI (deliberately English-only for the sample).
