Verification complete. All decision-critical claims tested against the actual repo; two research findings were **wrong for this repo's versions** and materially change the plan. Here is the ADR.

---

# ADR-001: Multi-Government Parliamentary Debate Agent via AgentCore Gateway + One Lambda per Government

**Status:** Proposed / implementation-ready
**Date:** 2026-08-03
**Verified against:** `aws-cdk-lib@2.259.0`, `strands-agents@1.50.2`, `mcp@1.29.0`, `bedrock-agentcore@1.19.0` as installed in this repo.

## 0. Verification corrections to the input research

I re-ran the load-bearing claims. Three results change the plan; record them before reading on.

**CORRECTION 1 — the MCPClient lifecycle risk is NOT a blocker (was flagged "top implementation risk").** The change-map claimed Strands "requires the agent to run inside a `with mcp_client:` context, which conflicts with the module global `_agent = build_agent()`". False on 1.50.2. I confirmed `__enter__` is literally `return self.start()` and `__exit__` is `self.stop(...)` in `.venv/lib/python3.13/site-packages/strands/tools/mcp/mcp_client.py:305-320`, then proved the behaviour end to end against a real local streamable-HTTP MCP server:

```
A. tools after module-scope start(): ['get_debate_text', 'search_debates']
B. session active: True          C. consumer count (explicit-list form): 0
D0/D1/D2. fresh asyncio.run() loop -> status=success active=True   (3 separate event loops)
E. 8 concurrent calls: 8 /8
F. after gc.collect(): active = True
G. after stop(): active = False  H. call after stop raises: MCPClientInitializationError
I. after restart: active = True  J. PRE-EXISTING tool object after reconnect -> status = success
```

`main.py`'s module-scope cold-start build survives as-is. Section 5 is therefore a small change, not a restructure.

**CORRECTION 2 — the MCP tool-result envelope needs NO parser change.** The change map predicted MCP returns `{"content":[{"type":"text","text":"<json>"}]}` requiring extension of `main.py:155-161`, `:309-315`, `:365-366`, else the UI silently shows `✓ 0 hits`. What Strands actually hands back:

```
ENVELOPE keys: ['content', 'isError', 'status', 'toolUseId']
content: [{"text": "{\n  \"results\": [ ... ], \"total\": 1 }"}]
  item keys: ['text'] | -> parsed text is dict: True | top-level keys: ['results', 'total']
```

That is exactly the `{'text': '<json>'}` branch `_extract_sources` and both step-counters already implement. **No change needed** provided every Lambda returns a top-level `results` array. The `results` key remains load-bearing at `main.py:162, 316, 365`.

**CORRECTION 3 — the ToolProvider GC footgun is guarded in 1.50.2, but the explicit-list form is still correct.** `remove_consumer` only calls `stop()` when `not self._consumers and (self._tool_provider_started or self._connection_failed)` (`mcp_client.py:461-486`). Dropping to zero consumers on a manually-started client left the session alive (`active=True`). Use `tools=list(client.list_tools_sync())` anyway — it registers zero consumers, so the guard can never fire.

**Baselines captured** (so new breakage is distinguishable): agent `23 passed`; infra `4 passed`; frontend `2 failed, 10 passed` — the two failures are pre-existing (`route.test.ts` mocks `invokeAgent` while `route.ts:75` calls `streamAgentRuntime`). Do not "fix" them here.

**Also confirmed:** `CfnGateway`, `CfnGatewayTarget`, `Gateway`, `GatewayTarget`, `LambdaTargetConfiguration`, `ToolSchema`, `GatewayAuthorizer`, `McpProtocolConfiguration`, `CfnRuntime` all resolve from `aws-cdk-lib/aws-bedrockagentcore`. `streamablehttp_client` accepts `auth: httpx.Auth | None`; `botocore.auth.SigV4Auth` is importable. Guardrail inversion is real at `infra/lib/security-stack.ts:41` ("other parliaments") and `:46` ("What was said in the US Congress?").

---

## 1. Target architecture

```
Browser
  └─ CloudFront (compress:false, CACHING_DISABLED — SSE passthrough)
      └─ ALB (CF prefix list + secret header)
          └─ Fargate Next.js  frontend/src/app/api/ask/route.ts
              │  InvokeAgentRuntime  (frontend-stack.ts:94-100 — unchanged)
              ▼
          AgentCore Runtime (Strands, networkMode PUBLIC)
          agent/src/parlamentgpt_agent/main.py   _agent built once at cold start
              │                    ├──► Bedrock model + Guardrail  (unchanged path)
              │  MCP streamable-HTTP + SigV4  (bedrock-agentcore:InvokeGateway)
              ▼
          AgentCore Gateway   ONE gateway, protocolType MCP, authorizerType AWS_IAM
              │  assumes GatewayExecutionRole → lambda:InvokeFunction
              ├──► λ germany      → search.dip.bundestag.de + dserver.bundestag.de   [secret]
              ├──► λ europarl     → data.europarl.europa.eu                          [no auth]
              ├──► λ uk           → hansard-api.parliament.uk, members-api…          [no auth]
              ├──► λ switzerland  → ws.parlament.ch                                  [no auth]
              ├──► λ austria      → www.parlament.gv.at                              [no auth]
              ├──► λ uscongress   → api.govinfo.gov + www.govinfo.gov                [secret]
              ├──► λ canada       → www.ourcommons.ca                                [no auth]
              └──► (deferred: france, netherlands, australia)
```

Each Lambda is **outside the VPC** (see §3). Each has its own IAM role, its own host allowlist, and — where needed — read access to *only its own* secret.

### Request path for one tool call, end to end

1. Browser POSTs `{question, history}` to `/api/ask`; `route.ts` truncates to `MAX_LEN=500` and calls `streamAgentRuntime`.
2. AgentCore Runtime invokes `main.py:invoke(payload)`; `stream=true` → `_invoke_stream`.
3. `_agent.stream_async(prompt)` — the agent already holds `MCPAgentTool` objects registered at cold start.
4. Model emits a tool-use block named `germany___search_debates`. `_invoke_stream` yields `{"type":"tool_call","tool":"germany___search_debates","input":{...}}`.
5. `MCPAgentTool.stream()` → `mcp_client.call_tool_async(...)` → marshalled via `run_coroutine_threadsafe` onto the client's **background** event loop (this is why step 3 is loop-agnostic).
6. HTTP POST to `https://{gwId}.gateway.bedrock-agentcore.{region}.amazonaws.com/mcp`, SigV4-signed by an `httpx.Auth` subclass.
7. Gateway validates the caller's IAM identity, validates arguments against the target's declared `inputSchema` (free arg validation), strips nothing, assumes `GatewayExecutionRole`, invokes the λ with `client_context.custom.bedrockAgentCoreToolName = "germany___search_debates"`. **Event is the bare arguments object** — no envelope, no `body`.
8. λ splits on `"___"`, dispatches `search_debates`, fetches the DIP key from its own secret, calls the pinned host through the shared client, normalizes, returns `{"results":[...], "total": n, "jurisdiction":"de"}`.
9. Gateway wraps the JSON into an MCP `CallToolResult`. Strands surfaces it as `{'status':'success','content':[{'text':'<json>'}], 'isError':False}`.
10. `_invoke_stream` json-parses `content[0]['text']`, finds `results` is a list, yields `{"type":"tool_result","count":n}` — **existing code, unchanged**.
11. Model writes a grounded answer; `_extract_sources` harvests `results` into `sources`; `{"type":"answer"}` is emitted; `page.tsx:consumeStream` renders citations.

---

## 2. The normalized result schema

Owned in **one** place, `shared/contracts/speech_result.py` in the Lambda layer, and mirrored to TypeScript by generation (§9). Every Lambda MUST return a top-level `{"results": [...]}` — that literal key is the sentinel at `main.py:162, 316, 365`.

```jsonc
{
  "results": [{
    // --- identity & provenance (required) ---
    "jurisdiction":      "de",             // NEW discriminator: de|eu|uk|us|ch|at|ca|au|fr|nl
    "jurisdiction_label":"German Bundestag",
    "doc_id":            "aktivitaet:1784775@protokoll:5798",
    "source_url":        "https://dserver.bundestag.de/btp/21/21083.pdf#P.10089",

    // --- content (required; snippet may be null) ---
    "title":   "Einleitende Ausführungen und Befragung des Bundesministers …",
    "snippet": "Nach Artikel 6 des Landwirtschaftsgesetzes …",
    "date":    "2026-06-11",               // ISO-8601 date, ALWAYS normalized

    // --- attribution (all nullable) ---
    "speaker": "Hubertus Heil",
    "group":   "SPD",                      // parliamentary group / Fraktion / caucus / political group
    "party":   "SPD",                       // the actual political party — DIFFERENT from group
    "role":    "Bundesminister für Arbeit und Soziales",

    // --- context (all nullable) ---
    "chamber":     "Bundestag",
    "term":        "21",                   // STRING, not int
    "session_ref": "21/83, p. 10089D",

    // --- text fidelity (required) ---
    "language_original": "de",             // what was actually SPOKEN
    "language_text":     "de",             // language of `snippet`
    "is_translation":    false,            // true ⇒ snippet is not the speaker's own words
    "text_status":       "final",          // final | uncorrected | scanned

    // --- escape hatch ---
    "extras": { "aktivitaetsart": "Rede", "protokoll_id": "5798", "page": "10089D" }
  }],
  "total": 9,
  "jurisdiction": "de",
  "truncated": false
}
```

### Justification, field by field, against the 10 field mappings

**`group` and `party` are separate fields, not one.** This is the central schema decision and it is forced by the data. Switzerland proves they are genuinely different concepts: `Transcript.ParlGroupAbbreviation` = `"V"` / `ParlGroupName` = `"Fraktion der Schweizerischen Volkspartei"` is the *Fraktion*, while the actual party requires a `MemberCouncil` join yielding `PartyAbbreviation="GRÜNE"` / `PartyName="GRÜNE Schweiz"` — and the group is `null` for Federal Councillors. The EU has the same split at a different altitude: `participation_in_name_of="org/VERTS_ALE"` is the EU-level political *group*, while the national party lives in `/meps/{id}.hasMembership[]` under `NATIONAL_POLITICAL_GROUP`. Germany's `fraktion` is a group, not a party. Canada's `Caucus/@Abbr="CPC"` is a caucus. Collapsing these into one `party` field would silently mislabel Swiss and EU results, which is exactly the class of error nobody notices. Fill whichever is available; when a source gives only one, populate `group` and leave `party` null (never guess).

**Committee is deliberately NOT a schema field.** It belongs in `extras` because scope is *debates and speeches on the floor*. Only NL (`Vergadering.Soort='Commissie'`), CA (Committee Evidence, a different `PubType`), AU (`chi=4/5/6`) and CH surface committee material at all, and in each case the correct handling is to *filter it out at the adapter* (AU's default `chi=0` mixes committee hearings in, and research's first exploratory AU query came back almost entirely Senate committee hearings). A schema field would invite adapters to pass it through.

**`term` is a string.** DE `wahlperiode=21` is an int, CH requires a `Session→LegislativePeriodNumber` join, US is `congress="119"` (already a string), UK has no term field at all and must be derived from `SittingDate`, AT is a Roman numeral `"XXVII"`, FR is `"17"`, NL is `Vergaderjaar="2025-2026"`. A string is the only type that holds Roman numerals and hyphenated Dutch session-years without lossy coercion.

**`date` is normalized ISO-8601, and this is where adapters will bite.** The raw forms are wildly inconsistent: DE `datum="2026-06-11"` (clean) but DE `dateSeance`-equivalent in FR is `"20260721150000000"` (17-char, YYYYMMDDHHmmssSSS); CH `MeetingDate="20241218"` is a *string* compared lexicographically; UK `"2024-12-19T00:00:00"`; NL `"2026-06-04T00:00:00+02:00"` local-midnight; AT `datetime="2023-12-14T23:00:00.000Z"` which is the **15 December** sitting shifted to UTC (slicing the first 10 chars gives the wrong day); AU `"8/10/2025"` day-first and **not zero-padded**. Normalization is non-optional and belongs in the shared layer with per-country parsers.

**`language_original` / `language_text` / `is_translation` exist because two sources serve machine translation as if it were speech.** The EU returns all 24 languages per speech with `xml:lang="es-t-en-mtec"` — the `-mtec` suffix marks machine translation, and only the variant matching `originalLanguage` (e.g. `.../SPA`) is what the member actually said. Canada's English Hansard contains translated French speeches, distinguishable only by `Content/FloorLanguage/@language`. Switzerland is the inverse trap: `Language` is a *label*-localization axis, and the same speech fetched as DE/FR/IT returns **byte-identical** `Text` (research verified md5 `083bf267f2d3` across all three). A product that quotes MT English as a verbatim quote is making a factual claim it cannot support; these three fields let the UI label it.

**`text_status`** is required by NL (`Status` ∈ `Casco|Ongecorrigeerd|Gecorrigeerd|Gerectificeerd`, where only ~1458 of 23263 `Verslag` rows are `Eindpublicatie`), AU (`Status` `Proof` vs `Final`), AT (*vorläufiges* protocol, later corrected), and FR (`<version>avant_JO</version>`). Recent debates are *mutable* in these systems; caching them as final is a correctness bug.

**`doc_id` is an opaque string, and Germany explains why it must be compound.** DIP has two ID namespaces: `aktivitaet.id="1784775"` works only at `/aktivitaet/{id}`, while the protocol text needs `fundstelle.id="5798"`. Research verified `/plenarprotokoll-text/1697688` → HTTP 404 while `/plenarprotokoll-text/5683` → 200. The repo's current `get_bundestag_document_text` passes the activity id straight in, so **it 404s on every activity-derived call today.** Encoding both (`aktivitaet:…@protokoll:…`) makes the search→fetch composition work. US similarly needs `packageId` + `granuleId`; CA needs `@Id` (for refetch) and `@EventId` (for the `#Int-` anchor) — different values on the same record.

**`speaker` is singular + nullable, with US as the acknowledged compromise.** A US CREC *granule* holds multiple speakers (`members[]` had 3 on the sampled record). The US adapter must either explode one result per speaker or set `speaker` to the primary and list the rest in `extras.speakers[]`. I mandate **explode per speaker** where the source attributes text per speaker, and primary+extras where it does not. UK historic (pre-1900) rows have `MemberName=null`, so nullable is required regardless.

**`extras` is an untyped map and must never be rendered blindly.** It carries the genuinely non-generalizable: DE `aktivitaetsart` (only ~40% of Plenarprotokoll activities are `Rede`), UK column refs (`HC Deb vol 759 c430`), US `pagePrefix`, CH `SortOrder`, AT the V/S/F/G/N↔ÖVP/SPÖ/FPÖ/GRÜNE/NEOS mapping ambiguity, EU the group-slug↔label mismatch (`org/S_D` vs `{id:'org/2953',label:'S&D'}` — different id schemes across endpoints).

---

## 3. Shared Lambda layer, and where the egress guarantee lives

### Layout

```
lambdas/
  shared/                       → published as a single Lambda layer (or vendored per fn)
    contracts/speech_result.py  SpeechResult dataclass + to_results_envelope() + validate()
    http/pinned_client.py       PinnedHttpClient, EgressViolation, assert_allowed()
    http/pagination.py          cursor_pages(), offset_pages(), token_pages(), page_cap
    normalize/dates.py          parse_iso, parse_ddmmyyyy, parse_yyyymmdd, parse_dip_ts, tz_fix
    normalize/text.py           strip_tags, strip_control_codes, nbsp_fix, snippet_around
    gateway/handler.py          tool_name(context), dispatch(), error envelopes
    secrets.py                  get_secret(arn) — @lru_cache KEYED ON ARN
  germany/handler.py  europarl/handler.py  uk/handler.py  …
```

**`PinnedHttpClient` — per-Lambda host pinning.** A direct port of `dip_client.py:26-31` and `:54-57`, parameterized instead of hardcoded, and constructed with the allowlist **baked in per Lambda at deploy time** (from CDK env, not from a caller argument):

```python
class PinnedHttpClient:
    def __init__(self, allowed_hosts: frozenset[str], *, timeout_s: float, auth_header=None):
        if not allowed_hosts:
            raise ValueError("refusing to construct an unpinned client")
        self._allowed = allowed_hosts        # e.g. frozenset({"search.dip.bundestag.de",
                                             #                 "dserver.bundestag.de"})
    def _assert_allowed(self, url: str) -> None:
        host = (urlparse(url).hostname or "").lower()
        if host not in self._allowed:        # EXACT set membership — never endswith()
            raise EgressViolation(f"Blocked egress to {host!r}; allowed={sorted(self._allowed)}")
```

Two properties are non-negotiable and must be preserved verbatim from today's behaviour, because `test_egress.py` proves them: exact-match membership (not suffix matching — `search.dip.bundestag.de.evil.com` must be refused) and `follow_redirects=False` with re-validation of any `Location` (cross-host redirect refusal). Keys go in headers, never the query string — which matters concretely for the US, where `api.govinfo.gov` download links embed `api_key` and **must not** be surfaced to users; route user-visible text fetches to the keyless `www.govinfo.gov/content/...`.

Note Germany needs **two** hosts (`search.dip.bundestag.de` for the API, `dserver.bundestag.de` for every `fundstelle.pdf_url`/`xml_url` citation), and the US needs two (`api.govinfo.gov` keyed search, `www.govinfo.gov` keyless text). A single-host pin would break citations. The allowlist is a set, sized to that Lambda's real needs and nothing more.

**Pagination helpers** — four genuinely different shapes, one helper each, all with a mandatory `max_pages` cap:
- `cursor_pages` — DE (opaque Solr `cursor`; **terminate when the returned cursor equals the one sent**, never on falsy — DIP's `cursor` is a *required* field and is always populated, so a falsy check loops forever), US (`offsetMark`, forward-only, `query`/`pageSize`/`sorts` must stay byte-identical).
- `offset_pages` — EU (`limit`/`offset`, **hard ceiling `offset < 10000`**, `meta.total` saturates at 10000), UK (`skip`/`take`, **`take>100` → HTTP 500**), AT (`page`/`pagesize`, `pages` advisory only), AU (`page`/`ps` ∈ {10,25,50,100}), NL (`$top` max 250 — **`$top=251` is a 400, not a clamp**).
- `token_pages` — NL `@odata.nextLink` / `$skiptoken`, CH `d.__next` (`$top` silently capped at 1000).
- `date_window_pages` — the fallback for sources with no working deep pagination: CA (`Page` **silently ignored** when `xml=1`; verified byte-identical pages 1/2/3 capped at 1000 items against `RecordsFound=2131`) and FR. Slice by day/week and assert `RecordsFound <= cap` per window.

**Error contract.** Two classes, deliberately:
- *Soft* (model can fix it): return `{"results": [], "total": 0, "jurisdiction": …, "error": {"kind": "no_results"|"bad_argument", "message": …}}`. Still a top-level `results` array, so `_extract_sources` and the step-counter stay happy and the UI shows `✓ 0 hits` truthfully rather than crashing.
- *Hard* (genuine fault: upstream 5xx, timeout, `EgressViolation`): **raise**, so it lands in gateway metrics (`metricSystemErrors`) and CloudWatch rather than being laundered into a cheerful tool result.

Do **not** adopt the `{"statusCode": 400, "body": …}` pattern from AWS samples: `statusCode` is not part of the Gateway contract, so a `400` is delivered to the model as a *successful* call whose text happens to say "error". *Must verify at implementation time:* that an unhandled Lambda exception surfaces as `ToolExecutionError` with `isError: true` (research marked this PLAUSIBLE, not confirmed; my local probe showed `isError` is present in the Strands envelope and an unknown tool yields `status:'error'` without raising, which is consistent but is not the same test).

**`secrets.py` must be keyed on the ARN.** The current `secrets.py:14` is `@lru_cache(maxsize=1)` keyed on nothing. Copied into a shared 10-government helper that is a **cross-government credential leak**: Lambda #2 receives Lambda #1's cached key. Use `@lru_cache(maxsize=32)` on `get_secret(arn)`. In practice each Lambda is its own process so this is belt-and-braces — but the helper is shared code and must be safe by construction.

### Where the egress guarantee now lives — honestly

**Today:** exactly one control, in one process — `dip_client.py:_assert_allowed`. There is no network-layer backstop. `network-stack.ts:20-21` says the Network Firewall allowlist and NAT gateways "were removed to reduce cost"; `security-model.md:79-82` names this as accepted residual risk; `infra/test/network.test.ts:12-21` *asserts the absence* of `NetworkFirewall::Firewall|FirewallPolicy|RuleGroup` and of `NatGateway|EIP` (verified: 4/4 green). The documented position is that the pin *is* the egress control.

**After:** N controls, one per Lambda, each in its own process with its own IAM role and its own (or no) credential. The agent process keeps **zero** parliament credentials and **zero** parliament egress — `props.dipSecret.grantRead(role)` at `agent-stack.ts:105` is deleted, and the agent's only outbound target becomes the Gateway (an AWS endpoint).

**The honest claim:** this is *the same kind* of control (application-layer, in-process, no network enforcement) but *strictly better in blast radius*, and better in credential scope:

| | Today | After |
|---|---|---|
| Enforcement layer | app only | app only — **unchanged, still no network backstop** |
| Compromise of the reasoning process reaches | DIP host + the DIP key | the Gateway only; no parliament host, no parliament key |
| Compromise of one fetcher reaches | n/a (one process) | that one jurisdiction's hosts + that one secret |
| Credentials in the agent container | 1 | 0 |

What I am **not** claiming: that a compromised Lambda cannot reach the internet. It can — see the VPC decision below. The pin is a guardrail against SSRF and accidental exfiltration (the `169.254.169.254` and `.evil.com` cases in `test_egress.py:20-29`), not a sandbox. Anyone who wants true network enforcement must fund NAT + Network Firewall (roughly $65+/mo for 2 NAT gateways before firewall endpoints), and that is a deliberate decision recorded in §12/Q3, not something this ADR sneaks in.

**Decision: the 10 Lambdas run OUTSIDE the VPC (no `vpc`/`vpcSubnets` prop).** Forced by arithmetic, not preference: `network-stack.ts:38` sets `natGateways: 0` and workload subnets are `PRIVATE_ISOLATED` (`:42`), so an in-VPC Lambda has **no internet route at all** and literally cannot reach any parliament API. Going in-VPC therefore *requires* re-adding NAT, which breaks `network.test.ts:18-21` and `:23-33` and reverses an explicit cost decision. Running outside the VPC keeps the posture identical to today's (app-layer pin only), costs nothing, and — importantly — **keeps the absence assertions true and meaningful** rather than tempting someone to delete them. I add a new assertion that the Lambdas have no `VpcConfig`, so this stays a conscious choice (§10).

**`test_egress.py` must be ported, not deleted.** All 6 tests currently fail at *collection* (they import `ALLOWED_DIP_HOST`, `DipEgressViolation`, `_assert_allowed` from modules that move), which is precisely how a security test gets deleted instead of migrated. It becomes `lambdas/shared/tests/test_egress.py`, parametrized over the real per-jurisdiction host table, retaining the two pin-bypass cases verbatim: the suffix-confusion host and the instance-metadata IP.

---

## 4. Tool surface

### Decision: one target per Lambda, **two tools per target** — i.e. per-jurisdiction tools, NOT a `jurisdiction` parameter.

Target names (tool-name prefixes; pattern `([0-9a-zA-Z][-]?){1,100}`, **no underscores** — which is exactly what makes `___` an unambiguous delimiter): `germany`, `europarl`, `uk`, `uscongress`, `switzerland`, `austria`, `canada`, `australia`, `france`, `netherlands`.

Resulting MCP-visible names:

```
germany___search_debates        germany___get_debate_text
europarl___search_debates       europarl___get_debate_text
uk___search_debates             uk___get_debate_text
…
```

### Why not a single `search_debates(jurisdiction, …)`?

Because **it is not available.** A gateway target maps to exactly one `lambdaArn` (`targetConfiguration.mcp.lambda.lambdaArn`). Given the user's explicit requirement of one Lambda per government, the tool surface is *necessarily* ≥10 tools. The only way to get one tool with a jurisdiction parameter is an 11th router Lambda that fans out to the other ten — and I reject that:

- It reintroduces a central chokepoint that needs `lambda:InvokeFunction` on all ten, undoing the blast-radius isolation that is the main security win of this migration.
- It doubles invocation latency and stacks two timeouts inside the model's patience budget.
- It makes the 6 MB tool-call payload cap a *shared* budget across jurisdictions instead of a per-jurisdiction one.
- It throws away a genuinely useful property: with per-jurisdiction tools **the model selects the jurisdiction by selecting the tool**, which is a discrete, inspectable choice that shows up in `page.tsx`'s step display. A `jurisdiction` string parameter is a free-text field the model can hallucinate (`"Germany"`, `"DE"`, `"deutschland"`) and the constrained-JSON-Schema subset used by `toolSchema` has **no `enum`** — so we could not even constrain it. That is decisive.

### Why two tools per target rather than one?

`search_debates` returns metadata + snippet; `get_debate_text` returns the full verbatim passage. They are separate because for most sources the full text is a *different, expensive* request: DE needs `/plenarprotokoll-text/{fundstelle.id}` (619k–843k chars per sitting); NL needs a 0.9–3 MB whole-meeting XML blob; US needs a third hop to `www.govinfo.gov/content/...`; AU needs `/api/hansard/transcript?id=…`. Folding them into one tool would either make every search slow and blow the 6 MB cap, or require a mode flag the model would misuse. Where a source embeds full text in search results (UK `ContributionTextFull`, CA `ParaText`, EU `api:xmlFragment`, CH `Text`), `get_debate_text` is a cheap re-fetch by `doc_id` and still worth exposing for consistency.

### Token cost and selection accuracy

At full build-out this is 20 tools. At ~120–180 tokens of schema each that is roughly 2.5–3.5k tokens per request — real, but acceptable against a 2000-char user prompt cap and well inside any modern context. More importantly, the sequencing in §11 means we ship **6 tools** (3 jurisdictions) before ever seeing 20, so we get to measure selection accuracy as it grows rather than guessing. No primary source gives a numeric tool-count threshold where selection degrades; AWS's own framing for needing semantic search is "hundreds". We are an order of magnitude below that.

### Semantic tool search: enable at create time, do not use on the hot path

`protocolConfiguration.mcp.searchType = SEMANTIC` is **create-time only and irreversible** — AWS docs are explicit that you cannot enable it on an existing gateway. Since it costs nothing until the reserved `x_amz_bedrock_agentcore_search` tool is actually called, set it at creation purely to preserve the option. Do **not** route normal tool selection through it: the dedicated quota is **25 transactions/minute** (versus 1000 concurrent connections for ordinary tool calls), so it would become a hard throughput ceiling, and it adds a round-trip before every real call. Revisit only if the surface grows past ~40 tools.

### Input schemas (the `inputSchema` subset has no `enum`, `format`, `minimum`, `oneOf`, `default` — constraints must live in `description` text, which is the only steering mechanism)

`{jurisdiction}___search_debates`:

| property | type | description (this text *is* the contract) |
|---|---|---|
| `query` | string | Free-text topic keywords, in the corpus language. **Per-jurisdiction note injected here** — e.g. Germany: "The German corpus has no full-text search; keywords match subject descriptors and titles only." |
| `speaker` | string | Member name. Germany/EU/France: resolved to an internal id by the tool. |
| `date_start` | string | Inclusive ISO date `YYYY-MM-DD`. |
| `date_end` | string | Inclusive ISO date `YYYY-MM-DD`. |
| `chamber` | string | Jurisdiction-specific; allowed values named in the description (UK: `Commons`\|`Lords`; CH: `N`\|`S`; US: `house`\|`senate`; DE: `Bundestag`). |
| `term` | string | Legislative term/parliament/session as a string. |
| `max_results` | integer | 1–50. Values above the source's page cap are clamped by the tool. |
| `cursor` | string | Opaque continuation token returned by a previous call. Never construct one. |

Required: `[]` — deliberately. Several sources cannot be queried without *something* (AT and AU both return zero results without a keyword; DIP needs date bounds to stay fast), but which combination is legal differs per jurisdiction, and the schema subset cannot express "one of". Adapters validate and return a **soft** `bad_argument` error naming the missing field, which the model can then fix. Making `query` globally required would break the legitimate "all speeches by X in date range" pattern that UK and CH support natively.

`{jurisdiction}___get_debate_text`: `{ doc_id: string (required — as returned by search, opaque, never constructed), max_chars: integer (default clamp 20000) }`.

---

## 5. Agent-side change

**Given Correction 1, `main.py`'s module-scope build stays.** This is the single biggest de-risking of the plan. The structure is:

```python
# agent/src/parlamentgpt_agent/gateway.py  (new)
_client = MCPClient(_transport, startup_timeout=30)   # module scope

def _transport():                                      # re-invoked on every start()
    return streamablehttp_client(GATEWAY_MCP_URL, auth=SigV4Auth_httpx(...),
                                 timeout=60, sse_read_timeout=300)

def ensure_session() -> None:
    """Reconnect if the session died (401, network blip). ~0.03s when already live."""
    if _client._is_session_active():          # no public accessor as of 1.50.2 — pin the version
        return
    try:
        _client.stop(None, None, None)        # re-raises stored cause; swallow it
    except Exception:
        pass
    _client.start()

def list_all_tools():                          # tools/list IS paginated on Gateway
    tools, token = [], None
    while True:
        page = _client.list_tools_sync(pagination_token=token)
        tools.extend(page)
        token = page.pagination_token
        if token is None:
            return tools
```

`agent.py:build_agent()` replaces `tools=[search_bundestag_speeches, get_bundestag_document_text]` with `_client.start()` then `tools=list_all_tools()`. Use the **explicit list**, not `tools=[_client]` — verified zero consumers, so the `remove_consumer`→`stop()` path can never fire even if a transient sub-Agent is created and GC'd.

`main.py` changes, minimally:
1. Add `ensure_session()` as the first line of both `invoke` paths. Cheap when healthy; the only thing standing between a single 401 and a permanently dead container. (A 401 tears down the *whole* session, not just the call — subsequent calls then return `Connection to the MCP server was closed`.)
2. **`_extract_sources`, `_extract_steps` and the `_invoke_stream` counter need no change** (Correction 2) — but add a regression test pinning the `{'text':'<json>'}` shape so a future MCP version bump can't silently zero the citations.
3. `_run_text_tool_call` / `_call_search_tool` (`main.py:251-253`) is a genuine blocker: `getattr(search_bundestag_speeches, "__wrapped__", …)` reaches into a local Strands `@tool` object that ceases to exist. Re-plumb to `_client.call_tool_sync(name=<prefixed>, arguments=…)`, regenerate the kwarg allowlist from the tool schema rather than hand-maintaining it (it is **fail-closed** — `return None` on any unknown kwarg — so a stale allowlist silently disables recovery), and make the regex tolerate `\w+___\w+`. Keep the path: `.env.example` still points at Gemma, and this is what makes non-Claude models usable.
4. `page.tsx:458` renders `🔍 {s.tool}(…)` literally, so strip the `___` prefix and map to a friendly label (§9). Do the stripping agent-side in the `tool_call` event so the SSE contract carries `{"tool":"search_debates","jurisdiction":"de"}` and the frontend does not learn Gateway naming.

**Residual risk (was the top risk, now demoted to #4):** `_is_session_active()` is a private method with no public accessor in 1.50.2. Pin `strands-agents==1.50.2` in both `requirements.txt` and `pyproject.toml` and wrap the call in one helper so an upgrade breaks in exactly one place. Also declare `mcp` explicitly in both files — it is currently present (1.29.0) only transitively via `strands-agents`, and `agent/Dockerfile:15-16` installs `requirements.txt` while tests install `pyproject`'s `.[dev]`, so a missing pin would pass every local test and fail only in the deployed container.

---

## 6. Auth

**Inbound: `AWS_IAM`, not Cognito.** `authorizerType` is required but accepts `AWS_IAM`, which is plain SigV4 with `bedrock-agentcore:InvokeGateway`. The agent already runs with an IAM execution role, so this eliminates a user pool, a resource server, custom scopes, a client secret in Secrets Manager, secret rotation, a token endpoint round-trip, and token-expiry handling. Add to the existing `AgentRole` in `agent-stack.ts:83-121`:

```json
{ "Sid": "AllowGatewayInvocation", "Effect": "Allow",
  "Action": ["bedrock-agentcore:InvokeGateway"],
  "Resource": ["arn:aws:bedrock-agentcore:<region>:<acct>:gateway/<gatewayId>"] }
```

The one cost is that `streamablehttp_client` does not SigV4-sign. I verified the mechanism exists: the signature accepts `auth: httpx.Auth | None`, and `botocore.auth.SigV4Auth` imports fine, so a ~30-line `httpx.Auth` subclass signing for service `bedrock-agentcore` is straightforward.

> **Must verify at implementation time (30-minute spike, before Milestone 2 is merged):** no AWS doc or sample shows a SigV4-signed MCP client against a Gateway — every published sample uses `Authorization: Bearer`. Two specific unknowns: (a) whether signing must cover the SSE `GET` as well as the JSON-RPC `POST`, and (b) whether the payload hash works with streamable-HTTP's request framing. If the spike fails, fall back to `CUSTOM_JWT` + Cognito M2M — `GatewayAuthorizer.usingCognito({userPool, allowedClients})` wires it, and the client-side shape is the *same* `httpx.Auth` subclass with a token provider behind it. **Use `httpx.Auth` either way, never a static `headers={"Authorization": …}` dict** — that dict is snapshotted when the httpx client is built at connect time, so a token refreshed an hour into the container's life never reaches the wire. That is the pattern every AWS sample uses and it is wrong for a long-lived container.

**Outbound: exactly one legal option.** For Lambda targets the auth matrix permits only the gateway service role: `credentialProviderConfigurations: [{"credentialProviderType": "GATEWAY_IAM_ROLE"}]`. Do not include `iamCredentialProvider` (that sub-struct is for MCP-server/OpenAPI targets only). A **separate** `GatewayExecutionRole` is required — distinct from `AgentRole` — trusting `bedrock-agentcore.amazonaws.com` with `aws:SourceAccount` / `aws:SourceArn` conditions, holding `lambda:InvokeFunction` on exactly the deployed function ARNs. Same-account means no Lambda resource policy is needed. Note this role is *shared across all targets*, so its permissions are the upper bound of what any authorized caller can exercise — acceptable here because the ten functions are peers, and each function's *own* role is what scopes secret access.

**Per-country outbound credentials — only 2 of 10 need one:**

| Jurisdiction | Credential | Registration | Live-testable by us? |
|---|---|---|---|
| Germany | API key, `Authorization: ApiKey <k>` | email `parlamentsdokumentation@bundestag.de` | **Partly.** The OpenAPI spec publishes a working demo key in a `description` field; research used it. It is a shared public key on a quota we do not control and can rotate without notice. **Must request our own before any milestone is called done.** |
| US Congress | `api_key` (api.data.gov) | instant, email only, at `govinfo.gov/api-signup` | **No, until registered.** `DEMO_KEY` is `x-ratelimit-limit: 10`/hour and research got locked out mid-verification. Registered keys are 36,000/hr. |
| EU, UK, Switzerland, Austria, Canada, Netherlands, France, Australia | none | none | Yes (subject to the per-source caveats below) |

Not fully live-testable regardless of credentials: **Australia** — every `parlinfo.aph.gov.au` URL returned 403 behind an Azure WAF JS challenge, and its full-text endpoint is undocumented. **France** — the only structured channel is a 55.7 MB ZIP (324 MB unpacked, 601 files) that does not fit a request-path Lambda. **Netherlands** — needs a 1.5 GB backfill and our own index before any query works.

Licence obligations that are product requirements, not footnotes: **Australia is CC BY-NC-ND 3.0 AU** — the NonCommercial and NoDerivatives terms sit badly with a monetised product (a since-removed donation button raised this at the time; the repo is now a non-monetised sample) and with snippeting/summarising; **Canada**'s Speaker's permission excludes "reproduction… for commercial purpose of financial gain" and requires the material not be presented as official; **Switzerland** requires citing «Parlamentsdienste der Bundesversammlung, Bern» plus a download date; **EU** is CC BY 4.0. These need a human decision (§12/Q1).

---

## 7. IaC plan

New stack `BundestagGateway-<suffix>`, inserted between Security and Agent in `infra/bin/app.ts`:

```
Network → Security → Gateway → Agent → Frontend
```

**Use the L1 `CfnGateway` / `CfnGatewayTarget`, not the L2, and not `AwsCustomResource`.** The class doc at `agent-stack.ts:23-27` ("there is no stable L2 construct") is now stale for the Gateway. My reasoning for L1 over L2: the L2 (`Gateway`, `GatewayTarget`, `LambdaTargetConfiguration`, `ToolSchema`) loads fine and would auto-grant `lambda:InvokeFunction`, but research found no stability marker on it, and this repo already carries the scars of betting on new surfaces. L1 is generated from the CloudFormation spec, is version-stable, and still gives real drift detection, rollback and `cdk diff` — everything `AwsCustomResource` does not. The cost is ~6 lines of explicit `grantInvoke`, which I actively prefer here because the grant then appears in review. Do **not** add eleven more `AwsCustomResource`s: `installLatestAwsSdk: true` plus `bedrock-agentcore:*` on `*` (`agent-stack.ts:172-177`) is a wildcard that already violates the stated least-privilege model.

New file `infra/lib/gateway-stack.ts`:

```ts
// One role assumed by Gateway to call the Lambdas — NOT the agent's role.
const gatewayRole = new iam.Role(this, "GatewayExecutionRole", {
  assumedBy: new iam.ServicePrincipal("bedrock-agentcore.amazonaws.com", {
    conditions: {
      StringEquals: { "aws:SourceAccount": account },
      ArnLike: { "aws:SourceArn": `arn:aws:bedrock-agentcore:${region}:${account}:gateway/*` },
    },
  }),
});

const gateway = new agentcore.CfnGateway(this, "Gateway", {
  name: `bundestag-gw-${props.suffix}`,          // no underscores allowed
  roleArn: gatewayRole.roleArn,
  authorizerType: "AWS_IAM",
  protocolType: "MCP",
  protocolConfiguration: { mcp: { searchType: "SEMANTIC" } },  // create-time only, irreversible
  exceptionLevel: "DEBUG",                       // granular errors while developing; drop later
});

for (const j of props.jurisdictions) {           // driven by a typed table, see below
  const fn = new lambda.Function(this, `Fn${j.pascal}`, {
    runtime: lambda.Runtime.PYTHON_3_13,
    architecture: lambda.Architecture.ARM_64,    // repo convention
    timeout: Duration.seconds(j.timeoutS ?? 45), // well under the model's patience
    memorySize: j.memoryMb ?? 512,
    layers: [sharedLayer],
    environment: { JURISDICTION: j.key, ALLOWED_HOSTS: j.hosts.join(","), ... },
    // NO vpc / vpcSubnets — see §3. In-VPC would have no internet route at all.
  });
  j.secret?.grantRead(fn);                        // only germany + uscongress
  fn.grantInvoke(gatewayRole);                    // L1 ⇒ explicit
  new agentcore.CfnGatewayTarget(this, `Target${j.pascal}`, {
    gatewayIdentifier: gateway.attrGatewayIdentifier,
    name: j.key,                                  // becomes the tool-name prefix
    credentialProviderConfigurations: [{ credentialProviderType: "GATEWAY_IAM_ROLE" }],
    targetConfiguration: { mcp: { lambda: {
      lambdaArn: fn.functionArn,
      toolSchema: { inlinePayload: j.toolDefinitions },   // structured list, NOT a JSON string
    }}},
  });
}
```

Serialize target creation with `node.addDependency()` chains: `CreateGatewayTarget` is 5 TPS *and* capped at 5 concurrent target operations per gateway, so ten in one deploy is near the limit.

Changed files:
- `infra/bin/app.ts` — delete `dipFqdn`/`dipSecretName`/`dipBaseUrl` (`:18-20`, note the hardcoded `/api/v1` path is DIP-specific and wrong for the other nine); add a typed `JURISDICTIONS` table (`{key, label, hosts[], secretName?, enabled}`); instantiate `GatewayStack`; extend the dependency chain at `:38-39`. **Do not rename the stacks** — renaming `Bundestag*-<suffix>` makes CloudFormation delete and recreate, orphaning the `RETAIN` secrets.
- `infra/lib/agent-stack.ts` — remove `dipSecret`/`dipBaseUrl` props (`:18-19`), the `DIP_BASE_URL`/`DIP_API_KEY_SECRET_ARN` env vars (`:127,130`), and **`props.dipSecret.grantRead(role)` (`:105`)** — leaving that grant forfeits the whole credential-isolation benefit. Add `GATEWAY_MCP_URL` (from `gateway.attrGatewayUrl` + `/mcp`) and `bedrock-agentcore:InvokeGateway` scoped to the gateway ARN. Fix the now-false SG description at `:55-59` ("restricted to the DIP FQDN at the application layer") — note that SG is not actually attached to the runtime (`networkMode: "PUBLIC"`, `:141`), so it is decorative today.
- `infra/lib/network-stack.ts` — drop the singular `dipFqdn` prop (`:6-8`) and the `AllowedFqdn` CfnOutput (`:87`, referenced by `runbook.md:60`). Update the class doc comment to describe where egress control now lives.
- `infra/lib/security-stack.ts` — guardrail rewrite (§8); the `dipSecretName`/`DipApiKey` secret (`:9,22-26`) becomes two secrets (germany, uscongress), both `RETAIN`.
- `infra/cdk.json` — replace `dipFqdn`/`dipSecretName` (`:7-8`). Note pre-existing drift: `cdk.json:8` says `bundestag/dip-api-key` while `app.ts:19` defaults to the suffixed `bundestag/dip-api-key-${suffix}`, and the `Makefile` `fill-secret` target hardcodes the **unsuffixed** name. Fix rather than multiply this by ten.

Explicitly out of scope: migrating the runtime `AwsCustomResource` to the now-existing `CfnRuntime`. It is tempting and would remove the `bedrock-agentcore:*` wildcard, but it is an independent change with its own replacement risk — separate PR.

---

## 8. Guardrail and system prompt rewrite

**This is the highest-severity single change in the migration.** `security-stack.ts:39-41` currently denies "Any request not about the German Bundestag. Includes general knowledge, coding, **other parliaments**, opinions, and small talk", and `:46` lists **"What was said in the US Congress?"** as a blocked *example*. After this change nine of ten supported jurisdictions *are* "other parliaments" and a US Congress question is a first-class query. This blocks at the Bedrock service layer — before the model runs, before any Gateway tool is reached — so no agent code, prompt, or Lambda can compensate. And **no test anywhere covers the guardrail's topic definition** (infra tests only assert network absences), so it can ship unchanged.

Rewrite, preserving every protection:

```ts
const REFUSAL = "I only answer questions about parliamentary debates and speeches.";

{ name: "OffTopic", type: "DENY",
  definition:
    "Any request that is not about parliamentary or legislative debates, speeches, or " +
    "statements made on the floor of a national or supranational legislature. " +
    "Includes general knowledge, coding, personal advice, opinions, and small talk. " +
    "Requests about any country's parliament, congress, or assembly are IN SCOPE and " +
    "must not be refused.",
  examples: [                       // US Congress example DELETED; replaced with true off-topic
    "How do I program in Python?",
    "What is the capital of France?",
    "Give me some advice about my career.",
    "Write me a poem about the sea.",
    "Tell me a joke.",
  ] },
```

Keep `PromptInjection` **verbatim** (`:52-61`) — it is orthogonal to jurisdiction and is the injection defence. Keep all six content filters at `HIGH` (`:65-74`) and the PII policy (`:77-86`) unchanged.

**Re-tune `contextualGroundingPolicyConfig` (`:88-93`, GROUNDING/RELEVANCE at 0.7) — do not assume 0.7 still holds.** Grounding is scored against retrieved content; that content is now heterogeneous and often non-English (German, French, Italian, Dutch), and some of it is machine-translated. Answers grounded in a French transcript may score differently from ones grounded in DIP German. Measure on real traffic before trusting it; a silent grounding block looks identical to a bad answer.

Because `CfnGuardrailVersion` (`:96-99`) pins a version and `guardrailVersion` is threaded from `app.ts:33` context into `agent-stack.ts:129`, **an operator who edits the guardrail but forgets to bump the context leaves the old blocking guardrail live.** Mitigate two ways: a new CDK assertion test (§10) and a runbook step.

`config.py:REFUSAL_MESSAGE` (`:15`) must change **in the same commit** — it is asserted byte-for-byte at `test_guardrail.py:16`, and it is hand-duplicated with `security-stack.ts:6` with no build-time link.

**System prompt (`prompts.py:4-43`) — full rewrite.** Four changes:
1. **Delete lines 28-29**, the "translate the user's search terms into German" rule. Actively harmful now: it would corrupt queries to nine of ten sources (searching UK Hansard for "Klimaschutz" returns nothing). Per-corpus language guidance belongs in each tool's `description`, which is the only steering channel the schema subset gives us — and it must be per-jurisdiction because DE/AT/CH-de want German, FR wants French, NL wants Dutch, and UK/US/CA/AU want English.
2. **Delete the hardcoded two-tool list (`:19-25`)** and its eight German resource names. Tools come from `tools/list`.
3. **Add jurisdiction-selection guidance** — which does not exist today: infer the jurisdiction from the question; if ambiguous, ask rather than guess; never present one parliament's data as another's; when comparing, call each jurisdiction's tool separately.
4. **Narrow scope to debates and speeches**, dropping Drucksachen/Vorgänge/Personen.

Keep the two behavioural rules that are actually load-bearing: "You must not refuse such requests" (guards against over-refusal of legitimate in-scope questions) and "no other sources, URLs, or tools", restated in Gateway terms. `test_guardrail.py:19-26` asserts the literals `'DIP'`, `'speeches by Hubertus Heil 2026'`, `'no other'` — those assertions must be rewritten alongside, not deleted (§10).

---

## 9. Frontend changes

The good news: `agentClient.ts` is essentially unchanged. The frontend still talks to the AgentCore **Runtime**; the Gateway is one hop deeper. `frontend-stack.ts:94-100` keeps granting only `bedrock-agentcore:InvokeAgentRuntime` — the frontend must **not** be granted `InvokeGateway`.

1. **`Source` type (`page.tsx:5-12`) — regenerate, do not hand-edit.** This is the #1 silent-failure surface: six optional fields hand-mirroring a ten-field Python dataclass, ingested through untyped casts (`event.sources as Source[]` at `:67`, `Array.isArray(data.sources)` at `:140`), rendered via `filter(Boolean).join(" · ")` at `:391-393`. A Python-side rename produces no TS error, no runtime error, and no failing test — citations just render blank. Generate `Source` from the single `speech_result.py` contract (and mirror it into the tool `outputSchema`) as a build step, so drift is a red build.
2. **Jurisdiction badge.** Add `jurisdiction`/`jurisdiction_label` to the rendered citation line and a per-result flag/label chip, so a user can never mistake a Swiss result for a German one. Group the citation list by jurisdiction when a turn spans several.
3. **Translation and status labels.** Where `is_translation` is true, mark the snippet "machine translation — original: {language_original}". Where `text_status != "final"`, mark it "uncorrected transcript". These are honesty requirements, not polish; the EU serves MT for 23 of 24 languages and NL/AU serve provisional text.
4. **Strip the `___` prefix in the step display (`:455-460`).** Raw `germany___search_debates` is user-hostile. The agent emits `{"tool":"search_debates","jurisdiction":"de"}` (§5), so render `🔍 Germany · search_debates(…)`.
5. **`EXAMPLES` (`:29-41`) — replace all ten.** They are all German, and `:35`, `:36`, `:40` reference motions/bills that leave scope entirely, so they would return nothing on a one-click chip on the empty state. Seed 2–3 per live jurisdiction only, and drop the "each returns real DIP results" comment.
6. **Data-source panel (`:264-287`) — rebuild as a compliance artifact, not copy.** Today it claims data comes "exclusively" from DIP and lists five DIP categories. It becomes one attribution row per live jurisdiction with the required licence text and link (CH's mandated citation + download date, EU CC BY 4.0, UK Open Parliament Licence, CA's "not official" disclaimer, AU's NC/ND if we ever enable it). Also update `layout.tsx:5-6` (title/description/SEO), `login/page.tsx:53`, and `page.tsx:189/208-210/217-219`.

---

## 10. Test strategy

**Offline-unit-testable (the large majority — this is where the value is):**
- **Per-country adapter tests with recorded fixtures.** For each jurisdiction, capture one real response per endpoint as a fixture and assert the adapter emits a schema-valid `results` envelope with correct `date` normalization, `speaker`, `group`/`party`, `doc_id` and `source_url`. This is where the verified upstream bugs get pinned: DE `titel` on `/aktivitaet` is the **speaker name**, not the debate title (so `title` must come from `vorgangsbezug[0].titel`); DE party is **absent** from `/aktivitaet` and needs the `/person/{id}` join; AT `datetime` is UTC-shifted so naive slicing yields the wrong day; AU dates are day-first and unpadded; CH `Language` does not translate `Text`.
- **Egress tests, parametrized per jurisdiction** — the ported `test_egress.py`. Must retain the suffix-confusion host and `169.254.169.254`, plus a new test that constructing a client with an empty allowlist raises.
- **Schema conformance, one test across all adapters:** every adapter returns a top-level `results` list; every item validates; `jurisdiction` is always present and matches the Lambda; `extras` never collides with a reserved key.
- **`results` key regression test** pinning the `{'text':'<json>'}` MCP envelope through `_extract_sources` (guards Correction 2 against a future MCP bump).
- **Gateway handler tests:** `___` splitting, unknown tool → error, and — the one research flagged — `context.client_context is None` (a plain `aws lambda invoke` has no ClientContext, so `.custom` raises `AttributeError`); handlers must degrade gracefully so tests need not fake ClientContext.
- **CDK assertion tests, filling three real coverage gaps:** (a) the guardrail `OffTopic` definition does **not** contain "other parliaments" and the examples do **not** contain "US Congress" — the bug with zero coverage today; (b) `config.py:REFUSAL_MESSAGE` equals `security-stack.ts:REFUSAL` (the two-file duplication no test guards); (c) every `AWS::Lambda::Function` in the gateway stack has **no `VpcConfig`**, which is what keeps the §3 decision deliberate.
- Rewrite `test_guardrail.py:19-26` prompt-substring assertions to the new invariants: contains `REFUSAL_MESSAGE`, contains "must not refuse", contains jurisdiction-selection guidance, and **does not** contain "translate … into German".

**Needs a deploy (keep this list short and explicit):**
- SigV4 inbound auth against a real gateway (§6 spike). Unavoidable — no local emulator.
- Whether an unhandled Lambda exception maps to `ToolExecutionError` / `isError: true`.
- Whether the Gateway ignores `statusCode` in a returned dict (asserted from absence of evidence).
- The `supportedVersions` default set — check `GetGateway` output after create.
- `tools/list` pagination actually paginating at 20 tools (the drain loop is written either way).

**Keeping the "absence" assertions meaningful.** `network.test.ts:12-33` is the canary for the entire egress architecture; re-adding those resources must be a deliberate change with the tests updated. By running the Lambdas outside the VPC, all four assertions stay green *and* keep meaning what they say. The only required edit is `:9`'s `dipFqdn` prop when `NetworkStackProps` changes. If a future decision funds NAT + Network Firewall for real network-layer egress control, `:12-21` must be updated **with** a new positive assertion (firewall rule group contains exactly the N jurisdiction domains) — never by deleting the count checks.

**Reference the baselines when judging breakage:** agent 23, infra 4, frontend 10-pass/2-pre-existing-fail.

---

## 11. Build order

Each milestone is independently verifiable and independently shippable.

**M0 — Contract and shared layer, zero AWS.** Create `lambdas/shared/*`: `speech_result.py`, `PinnedHttpClient`, pagination helpers, date/text normalizers, `gateway/handler.py`, ARN-keyed `secrets.py`. Port `test_egress.py` parametrized. *Verify:* new unit suite green; existing 23 agent tests still green (nothing touched yet).

**M1 — Germany behind the Gateway, at parity.** One Lambda (`germany`), one gateway, one target, two tools. Move `dip_client.py`/`secrets.py`/the `tools.py` normalization layer into `lambdas/germany/`. Agent switches to MCP tools; `dipSecret.grantRead` deleted from `AgentRole`. **Fix the four verified DIP bugs rather than porting them:** drop the non-existent `q` param (verified: `?q=Klimaschutz` returns byte-identical results to a bogus param — the primary search never text-filters, and `MAX_DIP_SCAN_PAGES=25` is a 25-round-trip workaround for it); apply `f.person` on `/aktivitaet` where it *does* work (`tools.py:240` has the condition inverted, applying it everywhere *except* the one endpoint that supports it); pass `fundstelle.id` to `/plenarprotokoll-text` (currently 404s on every activity-derived call); take `title` from `vorgangsbezug[0].titel` and party from the `/person/{id}` join. Add `f.zuordnung=BT` and `f.dokumentart=Plenarprotokoll` — without them DIP mixes in Bundesrat protocols and written Kleine-Anfrage co-signatures that nobody spoke. **This is also the milestone that proves the SigV4 spike.** *Verify:* the rewritten e2e test drives the real agent loop against a fake MCP server and gets German citations; guardrail/prompt still German-only at this point so no guardrail change is needed yet — deliberately keeping M1 to one variable.

**M2 — Guardrail + prompt + frontend multi-jurisdiction, still one country.** Land the guardrail rewrite, the prompt rewrite, the generated `Source` type, the jurisdiction badge, and the new CDK assertions **before** a second country exists. *Verify:* a US-Congress-shaped question is no longer blocked at the guardrail (it will correctly answer "no source for that jurisdiction yet"), and off-topic/injection are still refused. This ordering means the guardrail inversion is fixed and *tested* before it can ever block real traffic.

**M3 — UK + EU (`verdict: ready`, no credentials).** Chosen deliberately as country #2 and #3: both are genuinely no-auth, both embed full text in the search response (UK `ContributionTextFull`, EU `api:xmlFragment`), and both are English-first, so they exercise multi-jurisdiction plumbing without also exercising translation edge cases. Handle UK's `take<=100`→500 cap and party-via-`Members/History` join; handle EU's `offset<10000` ceiling, HTTP 204 empty results (**zero-byte body — `json.loads` throws**), mandatory `Accept: application/ld+json` (else you silently get RDF/XML), comma-joined `include-output`, and the `-mtec` MT labelling. *Verify:* 6 tools listed; a comparison question calls two jurisdictions and cites both with correct badges.

**M4 — Switzerland + Austria (no credentials, German-language, group≠party).** CH validates the `group`/`party` split properly (Fraktion vs `MemberCouncil.PartyAbbreviation`) and forces the latency discipline: an unbounded `substringof(...,Text)` measured **43–85 s cold**, so the adapter must always AND a `MeetingDate` range and set a >120 s socket timeout. AT validates the undocumented-endpoint canary pattern and the strict `date_range` ISO-with-milliseconds format (8 other formats silently 500 or return unfiltered).

**M5 — US + Canada (US needs a registered key).** US is the N+1 stress case: one 100-hit page costs 1 + 100 + 100 = 201 requests, so cap concurrency and route text through the keyless host. CA needs the `xml=1` pagination defect worked around by date-window slicing.

### Recommended DEFERRALS

**France — defer.** `verdict: hard`, and correctly so. There is no official queryable API; the search endpoint returns **HTML only** (`Accept: application/json` still returns `text/html`; `/json` returns 500), so it needs a scraper that breaks on any redesign. `seance_date` is **inert** — eight format variants all returned unfiltered totals, a silent-wrong-results trap — and there is no date-range query at all. `limit` is inert too. The structured alternative is a 55.7 MB ZIP (324 MB unpacked, 601 XML files) that does not fit a request-path Lambda and needs a separate batch ingest, and it carries no `sourceUrl`, so citation links require scraping the HTML index anyway to learn each sitting's slug. Revisit only with a budgeted ingest pipeline.

**Netherlands — defer.** OData v4 looks inviting and is a trap: **`$search` is accepted with HTTP 200 and silently ignored** (verified identical `$count` of 99117 for `$search=klimaat`, `$search=zzzznonsensexyz`, and no `$search`). No field anywhere holds speech text, so free-text search over debates is *impossible* server-side — it requires downloading 0.9–3 MB whole-meeting XML per sitting (~1.5 GB for final reports) and building our own index. Plus ~10× `Verslag` duplication per sitting that must be de-duped, and every GUID inside the XML 404s against OData.

**Australia — defer.** Full text depends on an **undocumented internal AJAX endpoint** (`/api/hansard/transcript`, found by reading the site's own JS) with no contract; `parlinfo.aph.gov.au` is **WAF-blocked** (403 JS challenge) so the historic fallback is unverifiable from a Lambda; full text is **empty for every pre-2011 record** while search reaches 1901 (a coverage cliff that reads as a parser bug); and the CC BY-**NC-ND** licence is a genuine legal blocker for a monetised product.

That gives **7 live jurisdictions** (DE, UK, EU, CH, AT, US, CA) = 14 tools, comfortably inside the token and selection-accuracy budget, with the three highest-effort/lowest-confidence sources parked behind an explicit decision rather than half-built.

---

## 12. Top 5 risks

**R1 — The guardrail blocks ~90% of legitimate traffic (highest severity, loudest).** `security-stack.ts:41` denies "other parliaments" and `:46` blocks a US Congress question, enforced at the Bedrock layer before any of our code runs. No test covers it. *Mitigation:* land the rewrite in **M2, before country #2 exists**; add the CDK assertion that the definition lacks "other parliaments" and the examples lack "US Congress"; add the `REFUSAL_MESSAGE` cross-file equality test; add a runbook step for bumping `guardrailVersion` context (a forgotten bump leaves the old guardrail live even after the code is fixed).

**R2 — The egress guarantee silently drops to zero (quietest, therefore most dangerous).** `dip_client.py` is today's *only* egress control at any layer. All 6 of its tests fail at *collection* after the move (they import deleted symbols), so the likeliest outcome is deletion rather than migration; and the natural DRY refactor — one client with a 10-host allowlist — destroys blast-radius isolation. *Mitigation:* M0 ports the tests first, parametrized per jurisdiction, keeping the `.evil.com` and metadata-IP cases; `PinnedHttpClient` raises if constructed with an empty allowlist; the allowlist is injected from CDK per function, never from a caller; the new no-`VpcConfig` assertion keeps the out-of-VPC decision deliberate. **Note R1 is loud and R2 is quiet: R1 will be fixed first and will consume the attention R2 deserves.** Assign R2 an owner explicitly.

**R3 — Schema drift → answers with zero citations, no error anywhere.** `_extract_sources` is wrapped in a bare `except Exception: pass` (`main.py:367-368`), the `results` key is an undocumented cross-layer contract, and `page.tsx`'s `Source` is a hand-mirror with all-optional fields behind an untyped cast. Ten independently-authored adapters is the ideal breeding ground. *Mitigation:* one `speech_result.py` owns the contract; the TS type is **generated**; the tool `outputSchema` declares it so the Gateway rejects drift at deploy; a conformance test runs across all adapters; the `{'text':'<json>'}` envelope gets a regression test.

**R4 — SigV4-signed MCP transport may not work (UNVERIFIED).** No AWS doc or sample shows it; all samples use bearer tokens. *Mitigation:* 30-minute spike in M1 before merge. The mechanism is confirmed to exist (`auth: httpx.Auth | None` on `streamablehttp_client`; `botocore.auth.SigV4Auth` importable). Fallback is `CUSTOM_JWT` + Cognito M2M with `GatewayAuthorizer.usingCognito`, and because both paths use the same `httpx.Auth` seam the client code barely changes. Also pin `strands-agents==1.50.2` — `ensure_session()` depends on the private `_is_session_active()`.

**R5 — Per-source fragility and silent-wrong-results.** Several sources fail *open* rather than erroring: DIP silently ignores unknown params (a typo becomes "return everything"), NL's `$search` is ignored, FR's `seance_date`/`limit` are inert, AT ignores unknown body keys, CA ignores `Page` under `xml=1`, AU's `hto` defaults to title-only. Each yields plausible-looking wrong data. *Mitigation:* every adapter ships a **canary test asserting a filtered query returns strictly fewer results than the unfiltered one**, plus a `RecordsFound == len(items)` assertion where the source reports a total; defer FR/NL/AU; run canaries on a schedule so an upstream change is caught by CI rather than by a user.

---

## Open questions needing a human decision

**Q1 — Licensing, and it may be a hard blocker.** Australia is CC BY-**NC-ND**; Canada's Speaker's permission excludes reproduction "for commercial purpose of financial gain" and requires material not be presented as official. (A donation button existed at the time of writing and has since been removed; the commercial-use half of the question is defused.) NoDerivatives also sits badly with snippeting and LLM summarisation. **Needs a real answer before AU (and arguably CA) ships.** Switzerland's mandated citation + download-date and the EU/UK attributions are cheaper but still product requirements.

**Q2 — Is the deferral list accepted?** Shipping 7 of 10 jurisdictions means the UI must not imply ten. If all ten are required, FR/NL/AU need a funded bulk-ingest pipeline (S3 + batch parse + our own search index), which is a materially larger project than the Gateway migration.

**Q3 — Do we fund network-layer egress control?** ~$65+/mo for 2 NAT gateways before Network Firewall. This ADR says no and keeps the app-layer pin, matching the existing decision. If yes, `network.test.ts:12-33` must be rewritten with positive assertions and the Lambdas move in-VPC.

**Q4 — Model choice, and does `_run_text_tool_call` survive?** It exists for non-Claude models (Gemma) and its `__wrapped__` reach-in is a hard blocker. Re-plumbing it to `call_tool_sync` is ~40 lines; deleting it is free but drops Gemma support. Related: model-id defaults disagree across `README.md`, `cdk.json`, `config.py` and `.env.example` — pick one.

**Q5 — Own API keys.** Germany's verification used the **public demo key published in the OpenAPI spec's `description` field** — a shared key on a quota we do not control that can rotate without notice. Someone must request a real key from `parlamentsdokumentation@bundestag.de` and register for a `govinfo` key. Until then M1 and M5 cannot be honestly signed off.

**Q6 — Scope confirmation: debates and speeches only?** This drops DIP's Drucksachen/Vorgänge/Personen, which invalidates `page.tsx` examples `:35/:36/:40`, the five-category list at `:272-277`, and part of the prompt. Confirm before the prompt rewrite.

**Q7 — `exceptionLevel: DEBUG` on the gateway** gives granular errors while developing. Confirm it is removed (or accepted) before anything user-facing, since it may surface internals.

**Q8 — Do we re-tune contextual grounding?** 0.7 was set for a single German corpus. Heterogeneous, partly machine-translated multi-language content may score differently and silently block valid answers. Needs measurement on real traffic, and an owner.

**Q9 — Deployment region and suffix.** The CDK context default is `us-east-1` (`cdk.json`) while the Makefile defaults to `eu-central-1`, so region must be pinned per environment. The Gateway control plane was verified reachable in `eu-central-1` (`list-gateways` → `{"items": []}`); **must verify at implementation time** that it is available in whichever region we actually deploy to.