# Multi-government migration — what changed

Converts the agent from German-Bundestag-only to a multi-government parliamentary **debates and
speeches** assistant, using an AgentCore Gateway with one Lambda per government.

Companion documents:
- [`COMPLIANCE.md`](COMPLIANCE.md) — licence/terms status, open questions, and the work queued
  behind them.
- [`ADR-001-multi-government.md`](ADR-001-multi-government.md) — the design rationale.
- [`source-profiles/*.json`](source-profiles/) — per-country API facts, each verified with live
  requests on 2026-08-03.

**Nothing has been deployed.** Everything is verified with `cdk synth` (which makes no AWS calls)
and unit tests, matching this repo's existing practice.

---

## Architecture

```
Fargate (Next.js) → AgentCore Runtime (Strands agent, built once at cold start)
    │  MCP over streamable HTTP, SigV4-signed
    ▼
AgentCore Gateway   (protocolType MCP, authorizerType AWS_IAM, semantic search enabled)
    │  assumes GatewayExecutionRole → lambda:InvokeFunction
    ├─ λ germany      → search.dip.bundestag.de, dserver.bundestag.de     [own secret]
    ├─ λ uk           → hansard-api.parliament.uk, members-api…           [no auth]
    ├─ λ europarl     → data.europarl.europa.eu                           [no auth]
    ├─ λ switzerland  → ws.parlament.ch                                   [no auth]
    ├─ λ austria      → www.parlament.gv.at                               [no auth]
    ├─ λ uscongress   → api.govinfo.gov, www.govinfo.gov                  [own secret]
    └─ (canada, france, netherlands, australia — built, not provisioned)
```

Each Lambda is pinned to only its own jurisdiction's hosts, holds only its own credential (if any),
and runs **outside the VPC** — the workload subnets have no NAT, so an in-VPC Lambda could not reach
any parliament API. Egress control is therefore application-layer, per Lambda, which is the same
*kind* of control as before but with a much smaller blast radius: the agent process now holds zero
parliament credentials and reaches only the Gateway.

**Two tools per jurisdiction**, named `{jurisdiction}___search_debates` and
`{jurisdiction}___get_debate_text` — so the model selects the parliament by selecting the tool,
which is a discrete, inspectable choice rather than a free-text parameter it could hallucinate.

---

## What was added

| Area | Files |
|---|---|
| Shared Lambda library | `lambdas/shared/python/gov_debates/` — normalized contract, host-pinned HTTP client, pagination helpers, date/text normalizers, Gateway dispatch, ARN-keyed secrets |
| Batch-ingest framework | `gov_debates/ingest/` — indexed-document model, S3 month-sharded store, index-backed query adapter |
| Per-government adapters | `lambdas/{germany,uk,europarl,switzerland,austria,uscongress,canada,france,netherlands,australia}/` |
| Gateway infrastructure | `infra/lib/gateway-stack.ts`, `jurisdictions.ts`, `tool-schema.ts` |
| Agent Gateway client | `agent/src/parlamentgpt_agent/gateway.py` — MCP client, SigV4 + Cognito auth, session reconnect |
| Generated types | `lambdas/shared/scripts/gen_source_type.py` → `frontend/src/lib/generated/source.ts` |
| Frontend jurisdiction registry | `frontend/src/lib/jurisdictions.ts` |
| Tests | 19 test files, 495 tests |

## What was removed

`agent/src/parlamentgpt_agent/{tools,dip_client,secrets}.py` and their tests. The logic moved into
`lambdas/germany/` and `lambdas/shared/`; the agent no longer talks to any parliament API directly.

`test_egress.py` was **ported, not deleted** — it is now parametrized across all ten jurisdictions'
host allowlists in `lambdas/shared/tests/test_egress.py`, retaining the original
suffix-confusion (`…bundestag.de.evil.com`) and instance-metadata (`169.254.169.254`) cases.

## What changed

- **Guardrail** (`infra/lib/security-stack.ts`) — the denied-topic definition previously denied
  "other parliaments" and listed *"What was said in the US Congress?"* as a **blocked example**. That
  rejected nine of ten jurisdictions at the Bedrock layer, before the model or any tool ran. Rewritten
  so any legislature is explicitly in scope; the injection defence, all six content filters and the
  PII policy are unchanged. Grounding thresholds are now overridable via CDK context.
- **`REFUSAL_MESSAGE`** broadened to *"I only answer questions about parliamentary debates and
  speeches."* in both `config.py` and `security-stack.ts`, with a test asserting the two match
  byte-for-byte (they are hand-duplicated across languages with no build-time link).
- **System prompt** (`prompts.py`) — dropped the "translate search terms into German" rule (it would
  corrupt queries to nine of ten sources) and the hardcoded DIP tool list; added jurisdiction-selection
  guidance and instructions to disclose machine translation and uncorrected transcripts.
- **Agent** — `build_agent()` now returns `(agent, client)`; `main.py` calls `ensure_session()` first
  in each request path, and the text-fallback recovery path calls the Gateway instead of a deleted
  local tool.
- **Frontend** — `Source` is now generated from the Python contract; jurisdiction badges, translation
  and transcript-status labels, per-source attribution panel, and a coverage-derived subtitle so the
  UI cannot imply coverage we lack.
- **`network-stack.ts` / `security-stack.ts` / `agent-stack.ts`** — DIP-specific props and the
  agent's DIP secret grant removed; Gateway ARN, MCP URL and `bedrock-agentcore:InvokeGateway` added.

---

## Bugs found and fixed

Found by testing against the verified API shapes. Each would have failed silently.

**Shared HTTP client**
1. **A URL's existing query string was dropped** when `params` was passed. httpx replaces the query
   even for an empty dict, so every OData `__next` / `$skiptoken` cursor would have become an
   infinite page-1 loop — presenting as "this source only has one page".
2. **`default_headers` were ignored on an injected client.** For the EU API a missing `Accept` header
   silently returns RDF/XML instead of JSON.
3. **HTML entities were not decoded** (`S&amp;D` leaked into citations).

**European Parliament**
4. **Language selection was inverted.** BCP-47 `es-t-en-mtec` means *source Spanish, delivered
   English*; reading the prefix returned Spanish text labelled as English. Now the correct variant is
   returned and flagged `is_translation`.
5. **Throttling was reported as "no results".** The EP throttle drops the connection or returns 200
   with an empty body — no 429, no `Retry-After`. Treating that as an empty result set told the user
   this parliament never discussed their topic. Now raises `EuThrottled`.

**Germany (DIP)** — four upstream bugs fixed rather than ported: the `q` free-text parameter does not
exist (silently ignored), the `f.person` condition was inverted (applied everywhere *except* the one
endpoint that supports it), full text needs `fundstelle.id` not the activity id (404s otherwise), and
`titel` on `/aktivitaet` is the *speaker*, not the debate title.

**Infrastructure** — Lambda descriptions are capped at 256 characters; the ingest description exceeded
it and would have failed at deploy. Caught by a CDK assertion test.

**Frontend** — the two pre-existing `route.test.ts` failures are fixed. They mocked `invokeAgent`
while the route calls `streamAgentRuntime`, so they could never pass.

---

## Verification

```
shared        160     agent          17
germany        14     frontend       20
uk             24     infra          26
europarl       39     ─────────────────
switzerland    37     TOTAL         495
austria        44
uscongress     37     All 5 stacks synth clean
canada         22     (cdk synth makes no AWS calls)
france         18
netherlands    15
australia      22
```

Run everything with `make test`. Notes:
- `make test` needs Python ≥ 3.11; the target defaults to `PYTHON=python3.13` because the system
  `python3` on the dev machine is 3.9.
- Offline `cdk synth` needs a placeholder account: `CDK_DEFAULT_ACCOUNT=111111111111 npx cdk synth`.
- `make gen-types-check` fails the build if the generated frontend type drifts from the Python
  contract.

## Known limitations

- **The SigV4-signed MCP transport is not live-verified** — it cannot be without deploying. Unit
  tested; a Cognito M2M fallback is wired behind `GATEWAY_AUTH_MODE=cognito` using the same
  `httpx.Auth` seam. See `COMPLIANCE.md` W8.
- **`docs/architecture.md`, `docs/runbook.md` and `docs/security-model.md` remain stale.** They
  described the pre-existing Lambda + Function URL design before this change and are now further out
  of date. Not touched here to keep the diff reviewable; the CDK remains the source of truth.
- **Four jurisdictions are deliberately unreachable.** See `COMPLIANCE.md` §2 for why and §3 for what
  it takes to enable each.
