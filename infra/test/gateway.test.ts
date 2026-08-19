import { App } from "aws-cdk-lib";
import { Template, Match } from "aws-cdk-lib/assertions";
import { GatewayStack } from "../lib/gateway-stack";
import { JURISDICTIONS, enabledJurisdictions } from "../lib/jurisdictions";

const env = { account: "111111111111", region: "eu-central-1" };

// Skip asset bundling in tests: the shared layer bundles in a container only (no host-pip
// path anymore), and assertions never need real asset content.
const NO_BUNDLING = { context: { "aws:cdk:bundling-stacks": [] } };

describe("Gateway stack", () => {
  const app = new App(NO_BUNDLING);
  const stack = new GatewayStack(app, "TestGw", { env, suffix: "test" });
  const t = Template.fromStack(stack);
  const enabled = enabledJurisdictions();

  test("creates exactly one Gateway with MCP + AWS_IAM auth + semantic search", () => {
    t.resourceCountIs("AWS::BedrockAgentCore::Gateway", 1);
    t.hasResourceProperties("AWS::BedrockAgentCore::Gateway", {
      AuthorizerType: "AWS_IAM",
      ProtocolType: "MCP",
      ProtocolConfiguration: { Mcp: { SearchType: "SEMANTIC" } },
    });
  });

  test("creates one Gateway target per enabled jurisdiction", () => {
    t.resourceCountIs("AWS::BedrockAgentCore::GatewayTarget", enabled.length);
  });

  test("US Congress is opt-in: absent by default because its api.data.gov key must be self-requested", () => {
    // Default deploy: no US Congress Lambda, target, or (empty) GovInfo secret.
    expect(enabled.some((j) => j.key === "uscongress")).toBe(false);
    const fns = t.findResources("AWS::Lambda::Function");
    for (const [, fn] of Object.entries(fns)) {
      expect((fn as any).Properties.Environment.Variables.ALLOWED_HOSTS ?? "").not.toContain("govinfo");
    }

    // Opt-in deploy: exactly one more target/Lambda, plus the GovInfo secret.
    const appUs = new App(NO_BUNDLING);
    const stackUs = new GatewayStack(appUs, "TestGwUs", { env, suffix: "test", enableUsCongress: true });
    const tUs = Template.fromStack(stackUs);
    tUs.resourceCountIs("AWS::BedrockAgentCore::GatewayTarget", enabled.length + 1);
    tUs.hasResourceProperties("AWS::SecretsManager::Secret", {
      Name: "parlamentgpt/govinfo-api-key",
    });
  });

  test("each target uses the GATEWAY_IAM_ROLE outbound credential provider", () => {
    t.hasResourceProperties("AWS::BedrockAgentCore::GatewayTarget", {
      CredentialProviderConfigurations: [{ CredentialProviderType: "GATEWAY_IAM_ROLE" }],
    });
  });

  test("creates one fetcher Lambda per enabled jurisdiction, all ARM64 Python 3.13", () => {
    t.resourceCountIs("AWS::Lambda::Function", enabled.length);
    const fns = t.findResources("AWS::Lambda::Function");
    for (const [, fn] of Object.entries(fns)) {
      const p = (fn as any).Properties;
      expect(p.Runtime).toBe("python3.13");
      expect(p.Architectures).toEqual(["arm64"]);
    }
  });

  test("SECURITY: fetcher Lambdas run OUTSIDE the VPC (no VpcConfig) — a deliberate choice", () => {
    // The workload subnets are isolated (no NAT), so in-VPC Lambdas could not reach any
    // parliament API. Egress is pinned per-Lambda at the app layer instead. This assertion
    // keeps the out-of-VPC decision conscious (ADR risk R2).
    const fns = t.findResources("AWS::Lambda::Function");
    for (const [, fn] of Object.entries(fns)) {
      expect((fn as any).Properties.VpcConfig).toBeUndefined();
    }
  });

  test("each fetcher Lambda is pinned to its jurisdiction's ALLOWED_HOSTS", () => {
    for (const j of enabled) {
      t.hasResourceProperties("AWS::Lambda::Function", {
        Environment: { Variables: Match.objectLike({ ALLOWED_HOSTS: j.hosts.join(",") }) },
      });
    }
  });

  test("only credentialed jurisdictions get a secret, and it is RETAINed", () => {
    const withSecret = enabled.filter((j) => j.secretName);
    t.resourceCountIs("AWS::SecretsManager::Secret", withSecret.length);
    for (const j of withSecret) {
      t.hasResource("AWS::SecretsManager::Secret", {
        DeletionPolicy: "Retain",
        Properties: Match.objectLike({ Name: j.secretName }),
      });
    }
  });

  test("disabled jurisdictions create NO resources at all", () => {
    // Canada, France, the Netherlands and Australia are built and unit-tested but must not be
    // deployed until their licensing / robots.txt / ingest questions are settled.
    const disabled = JURISDICTIONS.filter((j) => !j.enabled);
    expect(disabled.length).toBeGreaterThan(0);
    const rendered = JSON.stringify(t.toJSON());
    for (const j of disabled) {
      expect(rendered).not.toContain(`Fn${j.pascal}`);
      for (const host of j.hosts) {
        expect(rendered).not.toContain(host);
      }
    }
  });

  test("no index bucket or schedule exists while every enabled source is live-API", () => {
    // The batch-ingest machinery must not be provisioned speculatively.
    const anyBatch = enabled.some((j) => j.batchIngest);
    if (!anyBatch) {
      t.resourceCountIs("AWS::S3::Bucket", 0);
      t.resourceCountIs("AWS::Events::Rule", 0);
    }
  });
});

describe("Gateway stack with a batch-ingest jurisdiction enabled", () => {
  // Prove the ingest wiring works without shipping it: build a stack from a stubbed table that
  // enables one batch source. This keeps the real table honest (all four still disabled) while
  // still covering the code path.
  const jurisdictionsModule = require("../lib/jurisdictions");
  const original = jurisdictionsModule.JURISDICTIONS.slice();

  let t2: Template;
  beforeAll(() => {
    const france = original.find((j: any) => j.key === "france");
    jurisdictionsModule.JURISDICTIONS.length = 0;
    jurisdictionsModule.JURISDICTIONS.push({ ...france, enabled: true });
    const app = new App(NO_BUNDLING);
    const stack = new GatewayStack(app, "TestGwBatch", { env, suffix: "batch" });
    t2 = Template.fromStack(stack);
  });

  afterAll(() => {
    jurisdictionsModule.JURISDICTIONS.length = 0;
    jurisdictionsModule.JURISDICTIONS.push(...original);
  });

  test("creates an encrypted, private, RETAINed index bucket", () => {
    t2.resourceCountIs("AWS::S3::Bucket", 1);
    t2.hasResource("AWS::S3::Bucket", {
      DeletionPolicy: "Retain",
      Properties: Match.objectLike({
        PublicAccessBlockConfiguration: Match.objectLike({ BlockPublicAcls: true }),
        BucketEncryption: Match.anyValue(),
      }),
    });
  });

  test("creates a query Lambda AND a separate scheduled ingest Lambda", () => {
    // Two functions for one jurisdiction: the ingest job downloads tens of MB and can never sit in
    // a request path.
    t2.resourceCountIs("AWS::Lambda::Function", 2);
    const handlers = Object.values(t2.findResources("AWS::Lambda::Function")).map(
      (fn: any) => fn.Properties.Handler,
    );
    expect(handlers).toContain("handler.lambda_handler");
    expect(handlers).toContain("handler.ingest_lambda_handler");
  });

  test("the ingest job runs on a schedule", () => {
    t2.resourceCountIs("AWS::Events::Rule", 1);
    t2.hasResourceProperties("AWS::Events::Rule", {
      ScheduleExpression: "rate(1 day)",
    });
  });

  test("both Lambdas know the index bucket, and both stay out of the VPC", () => {
    const fns = t2.findResources("AWS::Lambda::Function");
    for (const [, fn] of Object.entries(fns)) {
      const props = (fn as any).Properties;
      expect(props.Environment.Variables.INDEX_BUCKET).toBeDefined();
      expect(props.VpcConfig).toBeUndefined();
    }
  });
});
