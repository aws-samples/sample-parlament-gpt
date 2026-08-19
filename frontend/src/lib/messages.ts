/**
 * UI message catalog — the single source for user-facing strings.
 *
 * Centralising the strings does two things:
 *  1. It is the first real step toward internationalisation: swapping this module (or keying
 *     it by locale) translates the whole UI without touching any component.
 *  2. JSX then contains no raw text literals, which is what i18n linters check for.
 *
 * Components reference entries via static property access (MESSAGES.signOut) — deliberately
 * no lookup function with a dynamic key, so no dynamic property access is introduced.
 */
export const MESSAGES = {
  appTitlePrefix: "Parlament",
  appTitleAccent: "GPT",
  signOut: "Sign out",
  confidentialBanner: "Confidential mode — this conversation is not being saved.",
  noSavedChats: "No saved chats yet.",
  emptyStateTitle: "Ask about parliamentary debates",
  emptyStateHint: "Questions are answered from the official records of each parliament.",
  dataSourcesHeader: "Data sources: official parliamentary records",
  dataSourcesBody:
    "Answers cover debates and speeches only, and come exclusively from the official " +
    "open-data services of each parliament listed below. Required attributions:",
  workingOnIt: "Working on it",
  sourcesHeading: "Sources",
  machineTranslation: "machine translation",
  sourceLink: "Source",
  stageBedrock: "bedrock",
  stageLambda: "lambda",
  guardrailIntervened: "Guardrail intervened",
} as const;
