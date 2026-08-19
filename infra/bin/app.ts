#!/usr/bin/env node
import "source-map-support/register";
import * as cdk from "aws-cdk-lib";
import { NetworkStack } from "../lib/network-stack";
import { SecurityStack } from "../lib/security-stack";
import { GatewayStack } from "../lib/gateway-stack";
import { AgentStack } from "../lib/agent-stack";
import { FrontendStack } from "../lib/frontend-stack";
import { CertificateStack } from "../lib/certificate-stack";
import { enabledJurisdictions } from "../lib/jurisdictions";

const app = new cdk.App();

const region = app.node.tryGetContext("region") || process.env.CDK_DEFAULT_REGION || "eu-central-1";
const account = process.env.CDK_DEFAULT_ACCOUNT;
const env = { account, region };

const suffix = app.node.tryGetContext("suffix") || "sample";

// Global cross-region inference-profile ID (not an account-scoped ARN).
const modelId = app.node.tryGetContext("modelId") || "global.anthropic.claude-sonnet-4-6";

const network = new NetworkStack(app, `ParlamentGptNetwork-${suffix}`, { env });

const security = new SecurityStack(app, `ParlamentGptSecurity-${suffix}`, {
  env,
  // Re-tunable without a code change; see the caution note in security-stack.ts about
  // multilingual / machine-translated retrieved content.
  groundingThreshold: Number(app.node.tryGetContext("groundingThreshold") ?? 0.7),
  relevanceThreshold: Number(app.node.tryGetContext("relevanceThreshold") ?? 0.7),
});

// US Congress is opt-in: its (free) GovInfo key must be requested at api.data.gov, so a
// default deploy leaves the source out entirely (no Lambda, no target, no empty secret).
const enableUsCongress =
  String(app.node.tryGetContext("enableUsCongress") ?? "").toLowerCase() === "true";

// AgentCore Gateway + one fetcher Lambda per government. Per-source secrets (DIP, GovInfo) are
// created here, empty, for the operator to fill post-deploy.
const gateway = new GatewayStack(app, `ParlamentGptGateway-${suffix}`, { env, suffix, enableUsCongress });

const agent = new AgentStack(app, `ParlamentGptAgent-${suffix}`, {
  env,
  vpc: network.vpc,
  workloadSubnets: network.workloadSubnets,
  endpointSecurityGroup: network.endpointSecurityGroup,
  modelId,
  guardrailId: security.guardrailId,
  guardrailVersion: app.node.tryGetContext("guardrailVersion") || "",
  gatewayArn: gateway.gatewayArn,
  gatewayMcpUrl: gateway.gatewayUrl,
  suffix,
});
agent.addDependency(network);
agent.addDependency(security);
agent.addDependency(gateway);

// Optional custom domain (both context keys required together): serves the app on
// `domainName` inside the Route 53 zone `hostedZoneDomain`, with an ACM certificate.
// CloudFront accepts certificates only from us-east-1, hence the separate cert stack and
// the cross-region reference into the (regional) frontend stack.
const domainName: string | undefined = app.node.tryGetContext("domainName");
const hostedZoneDomain: string | undefined = app.node.tryGetContext("hostedZoneDomain");
if ((domainName && !hostedZoneDomain) || (!domainName && hostedZoneDomain)) {
  throw new Error("Set BOTH domainName and hostedZoneDomain context keys, or neither.");
}
const certStack =
  domainName && hostedZoneDomain
    ? new CertificateStack(app, `ParlamentGptCertificate-${suffix}`, {
        env: { account, region: "us-east-1" },
        crossRegionReferences: true,
        domainName,
        hostedZoneDomain,
      })
    : undefined;

const frontend = new FrontendStack(app, `ParlamentGptFrontend-${suffix}`, {
  env,
  crossRegionReferences: true,
  vpc: network.vpc,
  agentRuntimeArn: agent.agentRuntimeArn,
  suffix,
  // The UI advertises exactly the provisioned sources (baked in at image build time).
  enabledJurisdictionKeys: enabledJurisdictions({ enableUsCongress }).map((j) => j.key),
  // Sign-up restriction: comma-separated e-mail domain patterns ("example.com",
  // "*.example.com", "amazon.*"). Unset or "*" = open sign-up, so the sample stays
  // organisation-agnostic; a deployment opts into a restriction via context.
  signupAllowedEmailDomains: app.node.tryGetContext("signupAllowedEmailDomains"),
  // Self sign-up on the hosted UI (default true); set "false" for operator-created users only.
  selfSignUpEnabled: String(app.node.tryGetContext("selfSignUpEnabled") ?? "true") !== "false",
  // Optional SES sender for Cognito's verification mails (default: Cognito's own sender).
  signupEmailFrom: app.node.tryGetContext("signupEmailFrom"),
  // Demo deployments show the technical trace to new users by default; off otherwise.
  defaultDebugMode: String(app.node.tryGetContext("defaultDebugMode") ?? "") === "true",
  domainName,
  hostedZoneDomain,
  certificate: certStack?.certificate,
});
frontend.addDependency(network);
frontend.addDependency(agent);
if (certStack) frontend.addDependency(certStack);

cdk.Tags.of(app).add("project", "parlamentgpt");
app.synth();
