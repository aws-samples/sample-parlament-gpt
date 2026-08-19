import * as agentcore from "aws-cdk-lib/aws-bedrockagentcore";
import { Jurisdiction } from "./jurisdictions";

type ToolDefinition = agentcore.CfnGatewayTarget.ToolDefinitionProperty;

/**
 * Builds the inline tool-schema (list of ToolDefinition) for one jurisdiction's Gateway target.
 *
 * The AgentCore tool inputSchema is a CONSTRAINED subset of JSON Schema: no enum, no format, no
 * minimum/maximum, no oneOf/default. Constraints therefore live in the `description` text, which
 * is the only steering channel the model reads. `required` is deliberately empty — which
 * argument combination is legal differs per source, so adapters return a soft `bad_argument`
 * error the model can correct rather than the schema rejecting the call.
 */
export function toolDefinitions(j: Jurisdiction): ToolDefinition[] {
  return [
    {
      name: "search_debates",
      description:
        `Search parliamentary DEBATES AND SPEECHES in the ${j.label}. Returns matching ` +
        `contributions with speaker, party/group, date, chamber and a citation link. ` +
        `${j.queryLanguageNote} Use get_debate_text to read the verbatim text of a result.`,
      inputSchema: {
        type: "object",
        properties: {
          query: {
            type: "string",
            description: `Free-text topic keywords. ${j.queryLanguageNote}`,
          },
          speaker: { type: "string", description: "Member/speaker name to filter by." },
          date_start: { type: "string", description: "Inclusive ISO date YYYY-MM-DD lower bound." },
          date_end: { type: "string", description: "Inclusive ISO date YYYY-MM-DD upper bound." },
          term: { type: "string", description: "Legislative term/parliament/session, as a string." },
          chamber: {
            type: "string",
            description: "Jurisdiction-specific chamber filter; omit unless you know the value.",
          },
          max_results: { type: "integer", description: "Maximum results to return, 1-50." },
          cursor: {
            type: "string",
            description: "Opaque continuation token from a previous call. Never construct one.",
          },
        },
        required: [],
      },
    },
    {
      name: "get_debate_text",
      description:
        `Fetch the verbatim text of a single ${j.label} debate/speech by its doc_id ` +
        `(as returned by search_debates — opaque, never construct one). Returns an excerpt ` +
        `centred on the optional query term.`,
      inputSchema: {
        type: "object",
        properties: {
          doc_id: { type: "string", description: "The doc_id from a search_debates result." },
          query: { type: "string", description: "Optional term to locate the relevant passage." },
          max_chars: { type: "integer", description: "Max characters to return (default 6000)." },
        },
        required: ["doc_id"],
      },
    },
  ];
}
