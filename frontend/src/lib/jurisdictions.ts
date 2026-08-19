/**
 * Display metadata and attribution for each supported jurisdiction.
 *
 * `attribution` is a COMPLIANCE requirement, not decoration: several sources mandate specific
 * wording. Switzerland requires citing the Parliamentary Services plus a download date; the EU
 * is CC BY 4.0; UK Hansard is under the Open Parliament Licence; Canada's Speaker's permission
 * requires that material not be presented as official. Anything shown in the UI must carry its
 * source's required notice.
 *
 * `enabled` mirrors infra/lib/jurisdictions.ts — keep the two in sync. Only enabled
 * jurisdictions are advertised in the UI, so the interface never implies coverage we lack.
 */
export type JurisdictionMeta = {
  key: string;          // matches SpeechResult.jurisdiction (de, uk, eu, …)
  label: string;
  flag: string;
  sourceName: string;
  sourceUrl: string;
  attribution: string;
  enabled: boolean;
};

export const JURISDICTIONS: JurisdictionMeta[] = [
  {
    key: "de",
    label: "German Bundestag",
    flag: "🇩🇪",
    sourceName: "DIP – Dokumentations- und Informationssystem",
    sourceUrl: "https://dip.bundestag.de/über-dip/hilfe/api",
    attribution: "Source: Deutscher Bundestag / DIP.",
    enabled: true,
  },
  {
    key: "uk",
    label: "UK Parliament",
    flag: "🇬🇧",
    sourceName: "Hansard (UK Parliament)",
    sourceUrl: "https://hansard.parliament.uk/",
    attribution: "Contains information licensed under the Open Parliament Licence v3.0.",
    enabled: true,
  },
  {
    key: "eu",
    label: "European Parliament",
    flag: "🇪🇺",
    sourceName: "EP Open Data Portal",
    sourceUrl: "https://data.europarl.europa.eu/",
    attribution: "© European Union, 2026. Reused under CC BY 4.0.",
    enabled: true,
  },
  {
    key: "ch",
    label: "Swiss Parliament",
    flag: "🇨🇭",
    sourceName: "Parlamentsdienste OData Webservice",
    sourceUrl: "https://www.parlament.ch/",
    attribution: "Source: Parlamentsdienste der Bundesversammlung, Bern.",
    enabled: true,
  },
  {
    key: "at",
    label: "Austrian Parliament",
    flag: "🇦🇹",
    sourceName: "Parlament Österreich Open Data",
    sourceUrl: "https://www.parlament.gv.at/recherchieren/open-data/",
    attribution: "Source: Parlamentsdirektion, Republik Österreich.",
    enabled: true,
  },
  {
    key: "us",
    label: "US Congress",
    flag: "🇺🇸",
    sourceName: "GovInfo (Congressional Record)",
    sourceUrl: "https://www.govinfo.gov/",
    attribution: "Source: U.S. Government Publishing Office (GovInfo).",
    // Opt-in source (self-requested api.data.gov key); enabled at image build time via
    // NEXT_PUBLIC_ENABLED_JURISDICTIONS when the infra provisions it.
    enabled: false,
  },
  {
    key: "ca",
    label: "House of Commons of Canada",
    flag: "🇨🇦",
    sourceName: "House of Commons Publications (Hansard)",
    sourceUrl: "https://www.ourcommons.ca/",
    attribution:
      "Source: House of Commons of Canada. Reproduced with permission; this is not an official version.",
    enabled: false,
  },
  {
    key: "au",
    label: "Parliament of Australia",
    flag: "🇦🇺",
    sourceName: "APH Hansard",
    sourceUrl: "https://www.aph.gov.au/",
    attribution: "Source: Parliament of Australia (CC BY-NC-ND 3.0 AU).",
    enabled: false,
  },
  {
    key: "fr",
    label: "Assemblée nationale",
    flag: "🇫🇷",
    sourceName: "Assemblée nationale – comptes rendus",
    sourceUrl: "https://www.assemblee-nationale.fr/",
    attribution: "Source: Assemblée nationale (République française).",
    enabled: false,
  },
  {
    key: "nl",
    label: "Tweede Kamer",
    flag: "🇳🇱",
    sourceName: "Tweede Kamer Open Data",
    sourceUrl: "https://opendata.tweedekamer.nl/",
    attribution: "Source: Tweede Kamer der Staten-Generaal.",
    enabled: false,
  },
];

const BY_KEY = new Map(JURISDICTIONS.map((j) => [j.key, j]));

/**
 * Gateway TARGET names (the `{prefix}___tool` namespace, from infra/lib/jurisdictions.ts) mapped
 * to the two-letter jurisdiction discriminator used in SpeechResult.jurisdiction. They differ
 * where a longer, clearer target name reads better to the model — the model sees
 * `europarl___search_debates` while results carry `jurisdiction: "eu"`.
 */
const TARGET_TO_KEY: Record<string, string> = {
  germany: "de",
  uk: "uk",
  europarl: "eu",
  switzerland: "ch",
  austria: "at",
  uscongress: "us",
  canada: "ca",
  australia: "au",
  france: "fr",
  netherlands: "nl",
};

export function jurisdictionMeta(key: string | null | undefined): JurisdictionMeta | undefined {
  if (!key) return undefined;
  return BY_KEY.get(key) ?? BY_KEY.get(TARGET_TO_KEY[key] ?? "");
}

/**
 * Build-time override: the infra bakes the actually-provisioned source list into the image
 * (NEXT_PUBLIC_ENABLED_JURISDICTIONS, comma-separated Gateway target keys), so the UI always
 * advertises exactly what is deployed. Without it (local dev), the static flags apply.
 */
const BUILD_TIME_ENABLED: string[] = (process.env.NEXT_PUBLIC_ENABLED_JURISDICTIONS ?? "")
  .split(",")
  .map((s) => s.trim())
  .filter(Boolean)
  .map((k) => TARGET_TO_KEY[k] ?? k);

export function enabledJurisdictions(): JurisdictionMeta[] {
  if (BUILD_TIME_ENABLED.length > 0) {
    return JURISDICTIONS.filter((j) => BUILD_TIME_ENABLED.includes(j.key));
  }
  return JURISDICTIONS.filter((j) => j.enabled);
}

/** Label for a result, preferring the label the backend sent. */
export function displayLabel(key: string | null | undefined, fallbackLabel?: string | null): string {
  return jurisdictionMeta(key)?.label ?? fallbackLabel ?? key ?? "Unknown parliament";
}

/** Flag emoji for a jurisdiction key, or a neutral marker. */
export function displayFlag(key: string | null | undefined): string {
  return jurisdictionMeta(key)?.flag ?? "🏛";
}
