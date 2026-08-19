# Compliance status — multi-government sources

Status of every source's licence, terms of use and crawl policy, and what each one blocks.

**Scope of this document.** It records what was *verified* against each source (see
`source-profiles/*.json` for the raw evidence, all gathered from live requests on 2026-08-03) and
which questions need a human answer. It is **not legal advice**; the items in
[§2](#2-open-questions--blocking) need a decision from someone who can accept the risk.

**The commercial-use question runs through everything.** An earlier iteration of the product
carried a "Buy me a coffee" button; it has since been removed, and the repo is now a
non-monetised sample under MIT-0. Two sources restrict commercial or non-commercial use
specifically — the removal resolves the monetisation half of that question, but the
derivative-work and "presented as official" halves below still need a human decision.

---

## 1. Per-source status

| Jurisdiction | Licence / terms | Attribution required | Deployed? | Blocker |
|---|---|---|---|---|
| 🇩🇪 Germany (DIP) | Bundestag terms of use | Yes — rendered | ✅ Live | None. Uses a shared demo key today — see [C6](#c6-germany-is-running-on-a-public-demo-api-key) |
| 🇬🇧 UK (Hansard) | Open Parliament Licence v3.0 | Yes — rendered | ✅ Live | [C1](#c1-uk-open-parliament-licence-wording-unverified) — exact wording unverified |
| 🇪🇺 EU Parliament | CC BY 4.0 | Yes — rendered | ✅ Live | None |
| 🇨🇭 Switzerland | Binding, specific terms | Yes — rendered, **but incomplete** | ✅ Live | [C2](#c2-switzerland-requires-a-download-date-we-do-not-display) — download date missing |
| 🇦🇹 Austria | CC BY 4.0 + disclaimer | Yes — rendered | ✅ Live | [C3](#c3-austria-reserves-the-right-to-prohibit-specific-uses) — noted, not blocking |
| 🇺🇸 US Congress | Public domain (GPO) | Courtesy only | ⚙️ Opt-in (`enableUsCongress=true`; the demo deploys it) | None — no licensing constraint; operator requests the free api.data.gov key |
| 🇨🇦 Canada | Speaker's permission — **excludes commercial use** | Yes + "not official" disclaimer | ❌ **Disabled** | [C4](#c4-canada-commercial-use-carve-out) **and** [C5](#c5-canada-robotstxt-disallows-the-search-endpoint) |
| 🇦🇺 Australia | **CC BY-NC-ND 3.0 AU** | Yes | ❌ **Disabled** | [C4](#c4-canada-commercial-use-carve-out)-adjacent: NonCommercial **and** NoDerivatives |
| 🇫🇷 France | Open (public site + static files) | Yes — rendered | ❌ Disabled | Not compliance — needs an ingest run (see [§3](#3-outstanding-work-once-compliance-is-cleared)) |
| 🇳🇱 Netherlands | Open, anonymous | Yes — rendered | ❌ Disabled | Not compliance — needs an ingest run |

Five jurisdictions deploy by default; US Congress is opt-in (self-requested API key). Four are
built, unit-tested and **provisioned nowhere** — an infra test asserts they create zero
CloudFormation resources.

---

## 2. Open questions — blocking

### C4 — Canada: commercial-use carve-out
**Blocks:** Canada entirely. **Also bears on Australia.**

The Speaker's permission allows reproduction of proceedings **only** if "the reproduction is
accurate and is not presented as official", and it explicitly "does not extend to reproduction,
distribution or use for commercial purpose of financial gain". Non-conforming use can be treated as
copyright infringement *or contempt of Parliament*.

Australia is **CC BY-NC-ND 3.0 AU**, where two terms bite independently:
- **NonCommercial** — the same question as Canada.
- **NoDerivatives** — sits badly with snippeting, translation and LLM summarisation, which is
  approximately everything this product does. This may be a blocker *even if* the commercial
  question resolves favourably.

**Decision needed (updated):** the donation button is gone, which satisfies what used to be
option (b) here and defuses the "financial gain" reading — and with it Australia's NC term.
Still open before either jurisdiction ships: Australia's **ND** term versus snippeting and LLM
summarisation, and Canada's "not presented as official" requirement for AI-condensed output.
Both jurisdictions therefore stay disabled until someone accepts those readings.

*Current state:* the Canada adapter refuses by default (`RESPECT_ROBOTS=true`) and neither
jurisdiction is deployed. The required attribution and "not an official version" disclaimer are
already written and rendered when enabled.

### C5 — Canada: robots.txt disallows the search endpoint
**Blocks:** Canada, **independently of C4.**

`www.ourcommons.ca/robots.txt` explicitly disallows `/PublicationSearch/`, `/publicationsearch/`,
`/Search/`, `/search/`, `/Embed/` and `/ParlDataWidgets/` for **all** user-agents — that is, exactly
the endpoint the adapter uses. The endpoint is documented on the Open Data page and answers
unauthenticated requests, but the robots policy is a genuine terms signal.

**The compliant alternative exists and is not disallowed:** the static whole-sitting XML under
`/Content/House/{parliament}{session}/Debates/{sitting}/HAN{sitting}-E.XML`. That is a bulk-ingest
shape, not a request-path one — so honouring robots.txt means Canada becomes a **batch-ingest**
jurisdiction like France, not a live-API one. The ingest framework for that already exists.

**Decision needed:** (a) re-implement Canada against the bulk XML path (est. ~1 day, reuses the
existing ingest framework), or (b) leave disabled.

### C1 — UK: Open Parliament Licence wording unverified
**Blocks:** nothing today, but **should be confirmed before public launch.**

Hansard data is under the Open Parliament Licence and attribution is required. The licence page
itself returned HTTP 403 during verification (Cloudflare), so the *exact* required attribution
wording is unverified. We currently render:

> Contains information licensed under the Open Parliament Licence v3.0.

That is the conventional formulation, but it was not read from the source.

**Action:** read the licence page from a normal browser and confirm the wording. Cheap; just needs
doing by a human who isn't behind the bot challenge.

### C2 — Switzerland requires a download date we do not display
**Blocks:** nothing, but we are **currently not fully compliant** while Switzerland is live.

Swiss terms are binding and specific. Four requirements; we satisfy three:

| Requirement | Status |
|---|---|
| Cite «Parlamentsdienste der Bundesversammlung, Bern» | ✅ Rendered |
| Must not alter content | ✅ Text is excerpted, never rewritten |
| Must not present as an official publication | ✅ No such claim |
| **Must display the download date** | ❌ **Not implemented** |

We do not store a retrieval timestamp per record, so the UI cannot show one.

**Action:** add a retrieval timestamp to the Swiss adapter's results and render it. Small change
(the `extras` field already exists for it), but it is an unmet obligation *today*. See
[§3](#3-outstanding-work-once-compliance-is-cleared) W1.

### C3 — Austria reserves the right to prohibit specific uses
**Blocks:** nothing. Recorded so it isn't a surprise.

Austria's data is CC BY 4.0 with attribution to the Parlamentsdirektion, but the disclaimer:
- states that **not everything** in the datasets is cleared for reuse (per-dataset copyright and
  data-protection carve-outs);
- disclaims all accuracy/completeness liability, even for slight negligence;
- **reserves the right to suspend the offering or prohibit specific uses** "zur Wahrung der Würde
  der parlamentarischen Körperschaften".

**Action:** none required. Be aware access could be withdrawn, and that protocols are published in
preliminary (*vorläufig*) form and later corrected — the adapter already marks those
`text_status: "uncorrected"`.

### C6 — Germany is running on a public demo API key
**Blocks:** nothing functionally, but this is **not a production posture.**

The DIP OpenAPI spec publishes a working API key inside a field description (and their Swagger UI
auto-authorizes with it), and the verification work used it. It is a shared public key on a quota we
do not control and which can be rotated without notice.

The literal value has been **redacted** from `source-profiles/de.json` — it is the Bundestag's own
published demo key rather than a credential of ours, but committing a key-shaped string trips secret
scanners and is a bad pattern regardless of provenance.

**Action:** request an own key from `parlamentsdokumentation@bundestag.de` and put it in the
`parlamentgpt/dip-api-key` secret. Until then Germany's reliability depends on a key we don't own.

### C7 — US GovInfo key *(resolved; kept for the record)*
**Blocks:** nothing anymore (and never was a licensing issue).

No licensing constraint — GPO states Congressional Record material is public domain. The source is
now **opt-in** (`enableUsCongress=true`): the deploy creates the `parlamentgpt/govinfo-api-key`
secret empty, and the operator registers at <https://www.govinfo.gov/api-signup> (email only,
instant; 36,000 req/hour) and fills it. The demo deployment runs with an issued key.

---

## 3. Outstanding work once compliance is cleared

Ordered by dependency. Nothing here is started; all of it is deliberately deferred.

### W1 — Swiss download-date display *(no compliance gate; do this regardless)*
Store a retrieval timestamp per Swiss record and render it beside the attribution. Closes
[C2](#c2-switzerland-requires-a-download-date-we-do-not-display). **Est. 2–3 h.**

### W2 — Fill the two API-key secrets *(no gate)*
Own DIP key ([C6](#c6-germany-is-running-on-a-public-demo-api-key)) and a GovInfo key
([C7](#c7-us-govinfo-key-not-yet-issued)). Operator action, not code. **Est. <1 h + issuance wait.**

### W3 — Confirm the UK attribution wording *(no gate)*
Closes [C1](#c1-uk-open-parliament-licence-wording-unverified). **Est. <1 h.**

### W4 — Run the France and Netherlands ingest jobs
Neither is licence-blocked; they are disabled only because their index is empty, and the query path
deliberately reports `not_indexed` rather than pretending "no results".

1. Enable `france` / `netherlands` in `infra/lib/jurisdictions.ts` (this provisions the S3 index
   bucket, the ingest Lambda and its EventBridge schedule).
2. Deploy, then invoke each ingest Lambda once to backfill.
3. Enable them in `frontend/src/lib/jurisdictions.ts` so the UI advertises them.

Caveats already handled in code but worth knowing: France's bulk ZIP is 55.7 MB (324 MB unpacked,
601 sitting files) and refreshes nightly; the Netherlands ingest downloads one 0.9–3 MB XML per
sitting and dedups ~10 reports per sitting. Neither export carries party affiliation, so `group` and
`party` stay null rather than being guessed.

**Est. 1 day including a first backfill and verification.**

### W5 — Canada, **if [C4](#c4-canada-commercial-use-carve-out) and [C5](#c5-canada-robotstxt-disallows-the-search-endpoint) clear**
Re-implement against the bulk `/Content/House/...` XML (not robots-disallowed) as a batch-ingest
jurisdiction. The existing search-based adapter stays as reference but should not be enabled. Note
that path uses a **different schema** (root `<Hansard>`, `ExtractedInformation/ExtractedItem`) and
lacks the search XML's `Person`/`Caucus` enrichment — so speaker/party attribution needs rethinking,
not just re-pointing. **Est. 1–2 days.**

### W6 — Australia, **only if [C4](#c4-canada-commercial-use-carve-out)'s NC *and* ND terms clear**
Enable the existing adapter and run its ingest. Also note the technical ceiling: verbatim text is
only retrievable from ~2011 onward (search indexing reaches 1901), `parlinfo.aph.gov.au` is
WAF-blocked, and the transcript endpoint is undocumented — so it may break without notice. The
adapter records `full_text_unavailable` for pre-2011 records rather than implying the speech had no
words. **Est. 0.5 day to enable; the ND question may make it moot.**

### W7 — Rate-limit pacing for the EU adapter
Not a licensing item, but a live-traffic risk found during verification. The EP API documents 500
requests / 5 min per endpoint, yet bursting ~10 requests in a few seconds was observed to **drop the
connection with no HTTP response** — no 429, no `Retry-After` — self-healing after 40–60 s.

The adapter now raises `EuThrottled` instead of misreporting a throttle as "no results" (that
distinction is tested). Still to do: a deliberate 3–6 s delay between sequential calls, exponential
backoff starting at 30–60 s, and never fanning out parallel requests to that host. **Est. 2–4 h.**

### W8 — Verify the SigV4-signed MCP transport against a live Gateway *(done)*
**Resolved:** validated against the live Gateway and exercised by the e2e test; see
`agent/src/parlamentgpt_agent/gateway.py` (ADR risk R4 retired). Originally:
the one thing that cannot be checked without deploying. The agent authenticates to the Gateway with
IAM/SigV4; no AWS sample shows a SigV4-signed MCP client, so the two unknowns are whether the SSE
`GET` needs signing as well as the JSON-RPC `POST`, and whether the payload hash works with
streamable-HTTP framing. Unit-tested; a Cognito M2M fallback is wired behind
`GATEWAY_AUTH_MODE=cognito` using the same `httpx.Auth` seam. **Est. 30 min spike; ~2 h if the
fallback is needed.**

### W9 — Re-tune the Guardrail grounding thresholds
`GROUNDING`/`RELEVANCE` sit at 0.7, tuned when every source was German DIP text. Retrieved content
is now multilingual and sometimes machine-translated, so the effective strictness differs per
jurisdiction. A silent grounding block is indistinguishable from a bad answer, so this needs
measuring on real traffic. Overridable via CDK context without a code change. **Est. measurement,
then a one-line change.**

---

## 4. Compliance controls already in place

Not pending — implemented and tested:

- **Per-jurisdiction attribution** rendered in the UI from a single registry, with each source's
  required wording (`frontend/src/lib/jurisdictions.ts`, covered by `jurisdictions.test.ts`).
- **The UI never advertises a jurisdiction we cannot serve.** Example questions, the coverage
  subtitle and the attribution panel are all derived from the enabled list, so a disabled source
  cannot leak into the interface.
- **Disabled means disabled.** An infra test asserts that disabled jurisdictions create no
  CloudFormation resources and that their hostnames appear nowhere in the template.
- **Canada refuses by default** (`RESPECT_ROBOTS=true`), asserted by a test that no HTTP request is
  made while the guard is on.
- **Translation honesty.** `is_translation` / `language_original` are populated wherever a source
  serves machine translation as if it were speech (EU: 23 of 24 language variants; Canada: French
  speeches served as English), and the UI labels it. Quoting MT text as verbatim would be a factual
  claim we cannot support.
- **Transcript-status honesty.** `text_status` distinguishes `final` / `uncorrected` / `scanned`, so
  provisional Austrian and Dutch text and scanned pre-1996 Austrian PDFs are labelled rather than
  presented as final.
- **Party is never guessed.** Where a source doesn't publish affiliation (France, Netherlands,
  Australia) or publishes only a parliamentary group (Germany, Switzerland, EU, Canada), `party`
  stays null instead of being inferred. Group and party are separate fields because in Switzerland
  and the EU they are genuinely different things.
- **API keys never reach a user-visible URL.** The US adapter fetches verbatim text from the keyless
  `www.govinfo.gov` host specifically so citation links cannot carry the key.
- **Per-Lambda egress pinning.** Each fetcher can reach only its own jurisdiction's hosts; a
  compromise of one cannot reach another's source or secret.
