# Chat Improvements: Context Retention & Persistent Sessions

Status: evaluation + phased plan. The quick win (Phase 0) is implemented;
later phases need scheduling.

## Current state

Conversation context is entirely client-held and replayed per request:

1. The browser keeps the chat in React state (`frontend/src/app/page.tsx`).
2. Each `POST /api/ask` carries `{question, history}` where `history` is the prior visible
   turns as `{role, text}` pairs; the route validates and caps at 12 entries.
3. The agent (`agent/src/parlamentgpt_agent/main.py`, `_coerce_history`) re-validates, caps at
   12 messages / 4,000 characters per message, drops leading assistant turns, and loads the
   result into the Strands agent's message list before running the new prompt.
4. Nothing is persisted server-side. There is no Strands session manager and no AgentCore
   Memory in use; the module-global agent's message list is cleared per request.

### Failure modes

| # | Problem | Effect |
|---|---------|--------|
| 1 | No persistence beyond the tab (before Phase 0: none at all) | Reload/crash loses the conversation |
| 2 | No cross-device/cross-tab continuity | A session exists only where it started |
| 3 | Hard truncation (12 msgs / 4,000 chars) | Long conversations silently lose their oldest context; long answers are cut mid-sentence when replayed |
| 4 | Tool results are not replayed | Follow-ups like "open the third result" cannot resolve references the model made from tool output |
| 5 | History is caller-supplied | The server must treat it as untrusted input forever (spoofable "assistant said X" turns); it can steer but must never authorise anything |
| 6 | Global agent + `messages.clear()` | Safe only while the runtime serialises requests per container; blocks any move to concurrent serving |

## Options evaluated

### A. `sessionStorage` persistence (IMPLEMENTED — Phase 0)

The chat is stored under a versioned key (`pd.chat.v1`), trimmed to the last 24 completed
messages and ~64 KB, and restored on mount. Sources are kept; transient steps/loading are
dropped. Cleared on sign-out.

- Pros: zero backend work, tab-scoped (no cross-user leakage on shared machines, unlike
  `localStorage`), survives reloads and transient crashes.
- Cons: still single-tab, single-device; not a real session.

### B. Server-side session store (DynamoDB)

Opaque session id in a cookie → DynamoDB table (`pk=sessionId`, append-only turns, TTL
7–30 days). `/api/ask` loads the trusted history server-side instead of accepting it from
the client; the client sends only the new question.

- Pros: removes trust in caller-supplied history (failure mode 5), enables cross-device
  continuity, chat list UI, and audit. Pay-per-request pricing fits the low traffic.
- Cons: new infra (table + IAM), auth scoping required (session must be bound to the
  authenticated user), data-retention/GDPR review needed since parliamentary questions may
  reveal user interests. Estimated effort: 2–4 days including infra.

### C. AgentCore Memory / Strands session management

Let the agent own conversation state: Strands `SessionManager` with an AgentCore Memory (or
S3/DynamoDB-backed) store keyed by session id passed through from the frontend.

- Pros: native fit; removes the `messages.clear()` pattern and with it failure mode 6;
  tool-use turns can be retained across requests (fixes failure mode 4); enables
  agent-side summarisation strategies.
- Cons: couples session lifetime to agent infrastructure; needs eviction/TTL policy;
  AgentCore Memory feature maturity must be validated in eu-central-1 first. Estimated
  effort: 3–5 days including a spike.

### D. Context compression (summarisation)

Instead of dropping turns beyond the 12-message window, summarise them into one synthetic
context turn ("Earlier in this conversation the user asked about X; relevant findings: …"),
either client-side (cheap heuristic: keep first user turn + last N) or agent-side (model
summary cached per session — requires B or C).

- Pros: directly improves context retention (failure mode 3) without unbounded token growth.
- Cons: agent-side summaries add latency and cost; summaries can drop details the user
  later refers to.

## Recommendation (phased)

1. **Phase 0 (done):** `sessionStorage` restore + SSE stream finalisation fixes.
   *(Superseded: sessionStorage was replaced by Phase 1's server-side sessions.)*
2. **Phase 1 (done):** Option B (DynamoDB sessions) — implemented with per-account
   Confidential mode (no persistence while on) and reopenable session history; keeps the
   agent stateless.
3. **Phase 2:** Option C spike; adopt if AgentCore Memory is production-ready in region.
   This is also the prerequisite for concurrent serving (failure mode 6).
4. **Phase 3:** Option D agent-side summarisation on top of Phase 1/2 storage.

## Security notes

- History remains untrusted input at every server boundary regardless of phase: size caps,
  role allowlist, and shape validation in `route.ts` and `_coerce_history` must stay.
- A server-side store must scope sessions to the authenticated user (`bt_session` subject)
  and enforce TTL-based deletion; chat content is potentially sensitive personal data.
- Session revocation: the stateless auth cookie outlives logout (12 h). If sessions gain
  server-side state (Phase 1), piggyback a revocation list on the same table.

## Related follow-ups (out of scope here)

- Rename internal `bundestag_*` identifiers (Python package, cookie name, secret names such
  as `bundestag/govinfo-api-key`, `HTTP_USER_AGENT` defaults) to product-neutral names —
  ripples into infra and deployed secrets, so it needs its own migration plan.
- Default model ARN in `agent/src/parlamentgpt_agent/config.py` embeds an account id; move it
  fully to configuration.
- Rate limiting keyed on `x-forwarded-for` is only trustworthy behind CloudFront/ALB;
  revisit if the task is ever directly reachable.
