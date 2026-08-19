import { Stack, StackProps, CfnOutput, Duration } from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as iam from "aws-cdk-lib/aws-iam";
import * as ecrAssets from "aws-cdk-lib/aws-ecr-assets";
import * as logs from "aws-cdk-lib/aws-logs";
import * as cr from "aws-cdk-lib/custom-resources";
import { Construct } from "constructs";
import * as path from "path";

export interface AgentStackProps extends StackProps {
  readonly vpc: ec2.Vpc;
  readonly workloadSubnets: ec2.SubnetSelection;
  readonly endpointSecurityGroup: ec2.SecurityGroup;
  readonly modelId: string;
  readonly guardrailId: string;
  readonly guardrailVersion: string;
  /** ARN of the AgentCore Gateway the agent calls for debate tools. */
  readonly gatewayArn: string;
  /** MCP endpoint URL of the Gateway (…/mcp is appended for the agent). */
  readonly gatewayMcpUrl: string;
  readonly suffix: string;
}

/**
 * Builds the agent container, the least-privilege execution role, and the AgentCore
 * runtime (created via the control-plane API through a custom resource, since there is
 * no stable L2 construct at time of writing).
 */
export class AgentStack extends Stack {
  public readonly agentRuntimeArn: string;
  public readonly agentSecurityGroup: ec2.SecurityGroup;

  constructor(scope: Construct, id: string, props: AgentStackProps) {
    super(scope, id, props);

    const region = this.region;
    const account = this.account;

    // --- Container image (pushed to ECR) ---
    const image = new ecrAssets.DockerImageAsset(this, "AgentImage", {
      directory: path.join(__dirname, "..", "..", "agent"),
      platform: ecrAssets.Platform.LINUX_ARM64,
    });

    // --- Security group: egress only to 443 (VPC endpoints + Gateway). No inbound. ---
    // NOTE: the runtime uses networkMode PUBLIC, so this SG is not attached to it; kept for
    // any in-VPC attachment and documentation. Parliament egress is enforced per fetcher
    // Lambda, not here — the agent only reaches the Gateway (an AWS endpoint).
    this.agentSecurityGroup = new ec2.SecurityGroup(this, "AgentSg", {
      vpc: props.vpc,
      description: "Agent runtime: egress 443 only, no inbound",
      allowAllOutbound: false,
    });
    this.agentSecurityGroup.addEgressRule(
      ec2.Peer.ipv4(props.vpc.vpcCidrBlock),
      ec2.Port.tcp(443),
      "HTTPS to VPC endpoints (AWS services)",
    );
    this.agentSecurityGroup.addEgressRule(
      ec2.Peer.anyIpv4(),
      ec2.Port.tcp(443),
      "HTTPS egress to the AgentCore Gateway / AWS APIs",
    );
    // The endpoint SG already permits 443 ingress from the whole VPC CIDR, so no
    // cross-stack SG rule is needed here (avoids a stack dependency cycle).

    const logGroup = new logs.LogGroup(this, "AgentLogs", {
      retention: logs.RetentionDays.ONE_MONTH,
    });

    // --- Least-privilege execution role ---
    const role = new iam.Role(this, "AgentRole", {
      assumedBy: new iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
      description: "ParlamentGPT agent runtime role (least privilege)",
    });

    // Invoke ONLY the configured model. For inference profiles the SDK resolves to the
    // underlying foundation model, so both the profile ARN and foundation-model ARN are needed.
    // modelId may be a full inference-profile ARN (arn:aws:bedrock:…:inference-profile/…)
    // or a bare profile/model ID (e.g. global.anthropic.claude-sonnet-4-6). Extract the trailing
    // resource name, and strip any cross-region routing prefix (global./us./eu./apac.) to get the
    // real foundation-model id, so both forms work.
    const modelResourceId = props.modelId.includes("/")
      ? props.modelId.split("/").pop()!
      : props.modelId;
    const foundationModelId = modelResourceId.replace(/^(global|us|eu|apac)\./, "");
    role.addToPolicy(
      new iam.PolicyStatement({
        sid: "InvokeConfiguredModelOnly",
        actions: ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
        resources: [
          props.modelId.startsWith("arn:")
            ? props.modelId
            : `arn:aws:bedrock:*:${account}:inference-profile/${props.modelId}`,
          `arn:aws:bedrock:*::foundation-model/${foundationModelId}`,
          `arn:aws:bedrock:*::foundation-model/${modelResourceId}`,
        ],
      }),
    );
    // Apply the guardrail on every invocation.
    role.addToPolicy(
      new iam.PolicyStatement({
        sid: "ApplyGuardrail",
        actions: ["bedrock:ApplyGuardrail"],
        resources: [`arn:aws:bedrock:${region}:${account}:guardrail/${props.guardrailId}`],
      }),
    );
    // Invoke ONLY this Gateway. The agent holds zero parliament credentials and zero
    // parliament egress now — its only outbound target is the Gateway (an AWS endpoint).
    // Each fetcher Lambda reads its own source secret; the agent reads none.
    role.addToPolicy(
      new iam.PolicyStatement({
        sid: "InvokeGatewayOnly",
        actions: ["bedrock-agentcore:InvokeGateway"],
        resources: [props.gatewayArn, `${props.gatewayArn}/*`],
      }),
    );
    // Logs.
    logGroup.grantWrite(role);
    role.addToPolicy(
      new iam.PolicyStatement({
        actions: ["logs:CreateLogStream", "logs:PutLogEvents", "logs:CreateLogGroup"],
        resources: [`arn:aws:logs:${region}:${account}:log-group:/aws/bedrock-agentcore/*`],
      }),
    );
    // Pull the image from ECR.
    image.repository.grantPull(role);
    role.addToPolicy(
      new iam.PolicyStatement({
        actions: ["ecr:GetAuthorizationToken"],
        resources: ["*"],
      }),
    );

    // --- AgentCore runtime via control-plane custom resource ---
    const runtimeEnvVars = {
      BEDROCK_MODEL_ID: props.modelId,
      AWS_REGION: region,
      GUARDRAIL_ID: props.guardrailId,
      GUARDRAIL_VERSION: props.guardrailVersion,
      // The agent reaches debate tools through the Gateway (MCP). "/mcp" is the MCP path.
      GATEWAY_MCP_URL: `${props.gatewayMcpUrl}`,
      GATEWAY_AUTH_MODE: "iam",
    };

    // AgentCore runtime names must match [a-zA-Z][a-zA-Z0-9_]{0,47} — no hyphens. Stack
    // names may carry hyphenated suffixes (e.g. "team-demo"), so sanitise for this one name.
    const runtimeName = `parlamentgpt_agent_${props.suffix}`.replace(/[^a-zA-Z0-9_]/g, "_").slice(0, 48);

    const createRuntime = new cr.AwsCustomResource(this, "AgentRuntime", {
      onCreate: {
        service: "bedrock-agentcore-control",
        action: "CreateAgentRuntime",
        parameters: {
          agentRuntimeName: runtimeName,
          roleArn: role.roleArn,
          networkConfiguration: {
            networkMode: "PUBLIC",
          },
          agentRuntimeArtifact: {
            containerConfiguration: { containerUri: image.imageUri },
          },
          environmentVariables: runtimeEnvVars,
        },
        physicalResourceId: cr.PhysicalResourceId.fromResponse("agentRuntimeId"),
      },
      onUpdate: {
        service: "bedrock-agentcore-control",
        action: "UpdateAgentRuntime",
        parameters: {
          agentRuntimeId: new cr.PhysicalResourceIdReference(),
          roleArn: role.roleArn,
          networkConfiguration: {
            networkMode: "PUBLIC",
          },
          agentRuntimeArtifact: {
            containerConfiguration: { containerUri: image.imageUri },
          },
          environmentVariables: runtimeEnvVars,
        },
        physicalResourceId: cr.PhysicalResourceId.fromResponse("agentRuntimeId"),
      },
      onDelete: {
        service: "bedrock-agentcore-control",
        action: "DeleteAgentRuntime",
        parameters: { agentRuntimeId: new cr.PhysicalResourceIdReference() },
      },
      policy: cr.AwsCustomResourcePolicy.fromStatements([
        new iam.PolicyStatement({
          // Exactly the three lifecycle calls this custom resource makes — NOT
          // bedrock-agentcore:* (that would hand the deploy Lambda invoke/gateway
          // control it never needs). Resources stay "*" because CreateAgentRuntime
          // targets no pre-existing ARN.
          actions: [
            "bedrock-agentcore:CreateAgentRuntime",
            "bedrock-agentcore:UpdateAgentRuntime",
            "bedrock-agentcore:DeleteAgentRuntime",
          ],
          resources: ["*"],
        }),
        new iam.PolicyStatement({
          actions: ["iam:PassRole"],
          resources: [role.roleArn],
        }),
        new iam.PolicyStatement({
          actions: ["iam:CreateServiceLinkedRole"],
          resources: ["arn:aws:iam::*:role/aws-service-role/*"],
        }),
      ]),
      timeout: Duration.minutes(10),
      // Use the Lambda runtime's bundled AWS SDK (deterministic) instead of pulling
      // "latest" from npm at deploy time. The runtime SDK has carried
      // bedrock-agentcore-control for a while; flip to true only if a deploy in a
      // region with an older runtime SDK reports an unknown service.
      installLatestAwsSdk: false,
    });
    createRuntime.node.addDependency(image);

    this.agentRuntimeArn = createRuntime.getResponseField("agentRuntimeArn");

    new CfnOutput(this, "AgentRuntimeArn", { value: this.agentRuntimeArn });
    new CfnOutput(this, "AgentImageUri", { value: image.imageUri });
    new CfnOutput(this, "AgentRoleArn", { value: role.roleArn });
  }
}
