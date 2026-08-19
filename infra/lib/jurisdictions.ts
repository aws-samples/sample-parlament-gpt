import * as path from "path";

/** Root of the per-jurisdiction Lambda sources; every asset dir below is a literal child. */
const LAMBDAS_ROOT = path.join(__dirname, "..", "..", "lambdas");

/**
 * The typed registry of government sources. This table is the single place that decides which
 * per-government Lambda + Gateway target gets created, what hosts each may reach (egress
 * allowlist, injected as ALLOWED_HOSTS — never from a caller), whether it needs a credential
 * secret, and whether it is enabled.
 *
 * `enabled: false` means "built but not wired into the Gateway" — used for the four
 * licence-/ingest-gated sources (Canada, Australia, France, Netherlands) and for
 * US Congress, which is merely opt-in (self-requested API key; see enabledJurisdictions).
 *
 * Tool names exposed to the model are `${key}___search_debates` / `${key}___get_debate_text`
 * (three underscores; keys therefore MUST NOT contain underscores — the Gateway uses that as
 * the target/tool delimiter).
 */
export interface Jurisdiction {
  /** Target-name prefix and tool namespace. Lowercase, no underscores. */
  readonly key: string;
  /** PascalCase fragment for CDK construct ids. */
  readonly pascal: string;
  /** Human label surfaced in results and the UI. */
  readonly label: string;
  /**
   * Absolute asset directory of this adapter's handler.py — built from literals right here
   * in the registry, so no variable path segment ever reaches an fs/asset call.
   */
  readonly lambdaAssetDir: string;
  /** Exact hostnames this Lambda may reach (egress allowlist). */
  readonly hosts: string[];
  /** Secrets Manager secret name if this source needs an API key, else undefined. */
  readonly secretName?: string;
  /** JSON key inside the secret holding the key value (default "apiKey"). */
  readonly secretJsonKey?: string;
  /** Whether to create the Lambda + Gateway target. */
  readonly enabled: boolean;
  /** Lambda timeout (s). Full-text endpoints (DE, NL) need more. */
  readonly timeoutS?: number;
  /** Lambda memory (MB). */
  readonly memoryMb?: number;
  /** Corpus language guidance injected into the search tool description. */
  readonly queryLanguageNote: string;
  /** Attribution/licence text shown in the UI (compliance requirement). */
  readonly attribution?: string;
  /**
   * Sources with NO queryable debates API are served from our own index instead of being called at
   * request time. Setting this adds a scheduled ingest Lambda (handler `ingest_lambda_handler`)
   * with write access to the index bucket, and grants the query Lambda read access.
   */
  readonly batchIngest?: {
    /** EventBridge schedule expression, e.g. "rate(1 day)". */
    readonly schedule: string;
    /** Ingest job timeout (s) — these download tens of MB and parse hundreds of files. */
    readonly timeoutS: number;
    /** Ingest job memory (MB). */
    readonly memoryMb: number;
    /** Why this source cannot be queried live (documentation, surfaced in review). */
    readonly reason: string;
  };
}

/**
 * All ten jurisdictions. Historically milestone-gated (M1: germany. M3: uk, eu.
 * M4: switzerland, austria. M5: uscongress. M6: france, netherlands, australia); the
 * flags now mean: five enabled by default, uscongress opt-in via context (operator must
 * request the API key), canada/australia/france/netherlands built-disabled (licensing
 * decisions / ingest pipelines — see docs/multi-gov/COMPLIANCE.md).
 */
export const JURISDICTIONS: Jurisdiction[] = [
  {
    key: "germany",
    pascal: "Germany",
    label: "German Bundestag",
    lambdaAssetDir: path.join(LAMBDAS_ROOT, "germany"),
    hosts: ["search.dip.bundestag.de", "dserver.bundestag.de"],
    secretName: "parlamentgpt/dip-api-key",
    secretJsonKey: "apiKey",
    enabled: true,
    timeoutS: 45, // /plenarprotokoll-text is multi-MB
    memoryMb: 512,
    queryLanguageNote:
      "The German (DIP) corpus has NO full-text search; the query only infers a speaker. " +
      "Prefer the speaker, date_start/date_end and term filters. Query terms must be German.",
    attribution: "Quelle: Deutscher Bundestag / DIP (dip.bundestag.de).",
  },
  {
    key: "uk",
    pascal: "Uk",
    label: "UK Parliament",
    lambdaAssetDir: path.join(LAMBDAS_ROOT, "uk"),
    hosts: ["hansard-api.parliament.uk", "members-api.parliament.uk"],
    enabled: true,
    timeoutS: 30,
    memoryMb: 512,
    queryLanguageNote:
      "Hansard is English. Free-text search is tokenised, not phrase-matched — wrap a phrase in " +
      'double quotes to match it exactly. chamber accepts only "Commons" or "Lords".',
    attribution: "Contains information licensed under the Open Parliament Licence v3.0.",
  },
  {
    key: "europarl",
    pascal: "Europarl",
    label: "European Parliament",
    lambdaAssetDir: path.join(LAMBDAS_ROOT, "europarl"),
    hosts: ["data.europarl.europa.eu", "www.europarl.europa.eu"],
    enabled: true,
    timeoutS: 30,
    memoryMb: 512,
    queryLanguageNote:
      "Covers plenary debates from about July 2021 onward. Query in English by default. Note " +
      "that non-original language versions are MACHINE TRANSLATED — check is_translation before " +
      "quoting a speech as the member's own words. term accepts only 9 or 10.",
    attribution: "© European Union. Reused under CC BY 4.0.",
  },
  {
    key: "switzerland",
    pascal: "Switzerland",
    label: "Swiss Parliament",
    lambdaAssetDir: path.join(LAMBDAS_ROOT, "switzerland"),
    hosts: ["ws.parlament.ch", "www.parlament.ch"],
    enabled: true,
    // This service is SLOW: an unbounded text search measured 43-85s cold. The adapter always
    // bounds the date range, which keeps typical queries well under this budget.
    // Capped just below the 60s CloudFront/ALB origin read timeout on purpose: a longer
    // Lambda would keep burning time on a response the browser can no longer receive, so a
    // very slow query fails fast instead (threat model D6). Raising this requires raising
    // the edge timeouts (or SSE keep-alives) first.
    timeoutS: 55,
    memoryMb: 512,
    queryLanguageNote:
      "Speeches are in the language actually spoken (German, French or Italian) — a German query " +
      "will not match a speech delivered in French. Always narrow with date_start/date_end: " +
      "unbounded searches are very slow. chamber accepts 'N' (Nationalrat) or 'S' (Ständerat).",
    attribution: "Source: Parlamentsdienste der Bundesversammlung, Bern.",
  },
  {
    key: "austria",
    pascal: "Austria",
    label: "Austrian Parliament",
    lambdaAssetDir: path.join(LAMBDAS_ROOT, "austria"),
    hosts: ["www.parlament.gv.at"],
    enabled: true,
    timeoutS: 45,
    memoryMb: 512,
    queryLanguageNote:
      "German-language corpus; a query term is REQUIRED (there is no way to browse without one), " +
      "and there is no speaker filter — a speaker name is matched as free text. term uses Roman " +
      "numerals (e.g. XXVII). chamber accepts 'Nationalrat' or 'Bundesrat'. Records before about " +
      "1996 are scanned images whose text cannot be extracted.",
    attribution: "Source: Parlamentsdirektion, Republik Österreich (CC BY 4.0).",
  },
  {
    key: "uscongress",
    pascal: "UsCongress",
    label: "US Congress",
    lambdaAssetDir: path.join(LAMBDAS_ROOT, "uscongress"),
    // Two hosts: api.govinfo.gov (keyed search/metadata) and www.govinfo.gov (keyless text).
    hosts: ["api.govinfo.gov", "www.govinfo.gov"],
    secretName: "parlamentgpt/govinfo-api-key",
    secretJsonKey: "apiKey",
    // Opt-in (deploy with `--context enableUsCongress=true`): the GovInfo API needs a
    // (free) api.data.gov key the operator must request themselves, so a default deploy
    // must not provision a source that cannot work out of the box.
    enabled: false,
    // The N+1 speaker/party enrichment means several upstream calls per search page.
    timeoutS: 60,
    memoryMb: 512,
    queryLanguageNote:
      "English. Covers the Congressional Record from 1994 onward. Search returns no snippet, so " +
      "call get_debate_text to see any words. term is the Congress number (e.g. 119); chamber " +
      "accepts 'House' or 'Senate'. Note a record may interleave spoken debate with quoted bill " +
      "text — verify a quote is speech before attributing it.",
    attribution: "Source: U.S. Government Publishing Office (GovInfo). Public domain.",
  },
  {
    key: "canada",
    pascal: "Canada",
    label: "House of Commons of Canada",
    lambdaAssetDir: path.join(LAMBDAS_ROOT, "canada"),
    hosts: ["www.ourcommons.ca"],
    // BUILT BUT DISABLED — two independent blockers, both needing a human decision:
    //  1. Licensing: the Speaker's permission excludes reproduction "for commercial purpose of
    //     financial gain" and requires the material not be presented as official. (An earlier
    //     iteration carried a donation button — since removed — but whether an AI summary
    //     counts as "presented as official"/derivative still needs the human call.)
    //  2. robots.txt: ourcommons.ca disallows /PublicationSearch/ for all user-agents. The
    //     compliant bulk route is the whole-sitting XML under /Content/House/ (an ingest
    //     pipeline, not a request-path Lambda).
    // The adapter is complete and unit-tested and refuses by default (RESPECT_ROBOTS=true).
    enabled: false,
    timeoutS: 60,
    memoryMb: 1024,   // responses reach ~5.5 MB and must be stream-parsed
    queryLanguageNote:
      "English (with French-delivered speeches served as English translations — check " +
      "is_translation). Coverage starts around September 2001. Page numbers are ignored by this " +
      "source: narrow date_start/date_end instead (a sitting day is ~240 interventions).",
    attribution:
      "Source: House of Commons of Canada. Reproduced with permission; not an official version.",
  },
  {
    key: "france",
    pascal: "France",
    label: "Assemblée nationale",
    lambdaAssetDir: path.join(LAMBDAS_ROOT, "france"),
    hosts: ["data.assemblee-nationale.fr", "www.assemblee-nationale.fr"],
    // BUILT BUT DISABLED — batch-ingest source; enable once an ingest run has populated the index.
    enabled: false,
    timeoutS: 30,     // query path only reads the index
    memoryMb: 512,
    batchIngest: {
      schedule: "rate(1 day)",
      timeoutS: 900,   // 55.7 MB ZIP -> 324 MB unpacked -> 601 sitting files
      memoryMb: 3008,
      reason:
        "No official queryable debates API: the search endpoint returns HTML only and its " +
        "seance_date/limit params are inert (silent wrong results). Speech-level data exists only " +
        "as a per-legislature bulk ZIP that cannot be fetched per document.",
    },
    queryLanguageNote:
      "French-language corpus, served from a nightly bulk import. Query in French. Party " +
      "affiliation is not in the export and is omitted rather than guessed.",
    attribution: "Source: Assemblée nationale (République française).",
  },
  {
    key: "netherlands",
    pascal: "Netherlands",
    label: "Tweede Kamer",
    lambdaAssetDir: path.join(LAMBDAS_ROOT, "netherlands"),
    hosts: ["gegevensmagazijn.tweedekamer.nl", "opendata.tweedekamer.nl"],
    enabled: false,
    timeoutS: 30,
    memoryMb: 512,
    batchIngest: {
      schedule: "rate(1 day)",
      timeoutS: 900,
      memoryMb: 3008,   // whole-meeting XML blobs run 0.9-3 MB each
      reason:
        "OData $search is accepted with HTTP 200 but SILENTLY IGNORED, and no field holds speech " +
        "text. Transcripts are only available as whole-meeting XML blobs behind a separate " +
        "/resource endpoint, so free-text search requires our own index.",
    },
    queryLanguageNote:
      "Dutch-language corpus, served from a periodic import. Recent reports are often uncorrected " +
      "— check text_status. Party affiliation is not in the transcripts and is omitted.",
    attribution: "Source: Tweede Kamer der Staten-Generaal.",
  },
  {
    key: "australia",
    pascal: "Australia",
    label: "Parliament of Australia",
    lambdaAssetDir: path.join(LAMBDAS_ROOT, "australia"),
    hosts: ["www.aph.gov.au"],
    // BUILT BUT DISABLED — batch-ingest AND a licence question (CC BY-NC-ND 3.0 AU: the
    // NonCommercial term conflicts with a monetised product and NoDerivatives sits badly with
    // snippeting/summarisation). Needs a human decision before enabling.
    enabled: false,
    timeoutS: 30,
    memoryMb: 512,
    batchIngest: {
      schedule: "rate(1 day)",
      timeoutS: 900,
      memoryMb: 1024,
      reason:
        "No API: search is HTML scraping of a WebForms page with no total-results field, and the " +
        "verbatim text comes from an undocumented per-item endpoint that returns nothing for " +
        "records before about 2011.",
    },
    queryLanguageNote:
      "English. Served from a periodic import. Verbatim text is only available from about 2011 " +
      "onward; earlier sittings are citable but their words are only in the official PDF.",
    attribution: "Source: Parliament of Australia (CC BY-NC-ND 3.0 AU).",
  },
];

/**
 * The jurisdictions to actually provision this deploy.
 *
 * US Congress is opt-in on top of the static flags: its (free) api.data.gov key must be
 * requested by the operator, so it only deploys when explicitly asked for
 * (`--context enableUsCongress=true`). The four licence-gated sources (CA/FR/NL/AU)
 * stay hard-disabled here regardless — enabling those is a code change on purpose,
 * because it requires reading docs/multi-gov/COMPLIANCE.md first.
 */
export function enabledJurisdictions(opts?: { enableUsCongress?: boolean }): Jurisdiction[] {
  return JURISDICTIONS.filter(
    (j) => j.enabled || (j.key === "uscongress" && opts?.enableUsCongress === true),
  );
}
