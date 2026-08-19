// GENERATED FILE — DO NOT EDIT BY HAND.
// Generated from lambdas/shared/python/gov_debates/contracts.py (SpeechResult) by
// lambdas/shared/scripts/gen_source_type.py. Run `make gen-types` after changing the contract;
// `make gen-types-check` fails the build if this file is stale.


/** One normalized parliamentary contribution (speech or intervention). */
export type Source = {
  jurisdiction: string;
  jurisdiction_label: string;
  doc_id: string;
  source_url: string | null;
  title: string;
  date: string;
  snippet?: string | null;
  speaker?: string | null;
  group?: string | null;
  party?: string | null;
  role?: string | null;
  chamber?: string | null;
  term?: string | null;
  session_ref?: string | null;
  language_original?: string | null;
  language_text?: string | null;
  is_translation?: boolean;
  text_status?: string;
  extras?: Record<string, unknown>;
};

/** The envelope every fetcher Lambda returns. */
export type ResultsEnvelope = {
  results: Source[];
  total: number;
  jurisdiction: string;
  truncated: boolean;
  cursor?: string;
};
