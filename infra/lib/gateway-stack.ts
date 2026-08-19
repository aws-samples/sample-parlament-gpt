import { Stack, StackProps, CfnOutput, Duration, RemovalPolicy } from "aws-cdk-lib";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as logs from "aws-cdk-lib/aws-logs";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import * as agentcore from "aws-cdk-lib/aws-bedrockagentcore";
import * as events from "aws-cdk-lib/aws-events";
import * as targets from "aws-cdk-lib/aws-events-targets";
import * as s3 from "aws-cdk-lib/aws-s3";
import { Construct } from "constructs";

import * as path from "path";
import { JURISDICTIONS, Jurisdiction, enabledJurisdictions } from "./jurisdictions";
import { toolDefinitions } from "./tool-schema";

const SHARED_DIR = path.join(__dirname, "..", "..", "lambdas", "shared");

/** Clip a string to a hard limit, marking that it was cut. */
function truncate(value: string, limit: number): string {
  return value.length <= limit ? value : value.slice(0, limit - 1) + "…";
}

/**
 * Bundling for the shared layer: copy the gov_debates package into `python/` and pip-install
 * the third-party deps alongside it. Container bundling ONLY, with a fully literal command —
 * deliberately no `local.tryBundle` host-pip path: that required child_process calls on
 * framework-supplied directories, i.e. exactly the shape SAST flags as command injection.
 * Deploys therefore need a container runtime (CDK_DOCKER=finch works); the test suites skip
 * asset bundling entirely via the `aws:cdk:bundling-stacks` context.
 */
function sharedLayerCode(): lambda.Code {
  return lambda.Code.fromAsset(SHARED_DIR, {
    exclude: ["tests", ".venv", "*.egg-info", "__pycache__", "pyproject.toml"],
    bundling: {
      image: lambda.Runtime.PYTHON_3_13.bundlingImage,
      command: [
        "bash",
        "-c",
        "cp -r python/. /asset-output/python && " +
          "pip install -r layer-requirements.txt -t /asset-output/python",
      ],
    },
  });
}

export interface GatewayStackProps extends StackProps {
  readonly suffix: string;
  /** Provision the opt-in US Congress source (needs a self-requested api.data.gov key). */
  readonly enableUsCongress?: boolean;
}

/**
 * AgentCore Gateway + one Lambda per government (the fetcher targets).
 *
 * Inbound auth is AWS_IAM (SigV4): the agent's runtime role calls the Gateway with
 * `bedrock-agentcore:InvokeGateway`; no Cognito, no token endpoint, no client secret.
 * Outbound (Gateway -> Lambda) uses a SEPARATE gateway execution role (GATEWAY_IAM_ROLE
 * credential provider) that holds `lambda:InvokeFunction` on exactly the deployed functions.
 *
 * Each Lambda runs OUTSIDE the VPC (no vpc/vpcSubnets): the workload subnets are isolated with
 * no NAT, so an in-VPC Lambda could not reach any parliament API. Egress is controlled at the
 * application layer by the shared PinnedHttpClient, pinned to each function's ALLOWED_HOSTS
 * (injected here, never from a caller). This matches the repo's existing app-layer egress
 * posture (Network Firewall/NAT were removed for cost).
 *
 * Uses the L1 CfnGateway/CfnGatewayTarget (version-stable, real drift detection / rollback)
 * rather than the L2 or an AwsCustomResource.
 */
export class GatewayStack extends Stack {
  public readonly gatewayUrl: string;
  public readonly gatewayArn: string;
  public readonly gatewayId: string;
  /** Index bucket for jurisdictions with no queryable API (created only if one is enabled). */
  public readonly indexBucket?: s3.Bucket;

  constructor(scope: Construct, id: string, props: GatewayStackProps) {
    super(scope, id, props);

    const jurisdictions = enabledJurisdictions({ enableUsCongress: props.enableUsCongress });
    // The index is only provisioned when an enabled jurisdiction actually needs it, so a
    // deployment with only live-API sources creates no bucket at all.
    const needsIndex = jurisdictions.some((j) => j.batchIngest);
    if (needsIndex) {
      this.indexBucket = new s3.Bucket(this, "SpeechIndex", {
        blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
        encryption: s3.BucketEncryption.S3_MANAGED,
        enforceSSL: true,
        versioned: false,
        removalPolicy: RemovalPolicy.RETAIN,
      });
      new CfnOutput(this, "SpeechIndexBucket", {
        value: this.indexBucket.bucketName,
        description: "Index for jurisdictions with no queryable debates API.",
      });
    }

    // Shared Lambda layer: the gov_debates package (contract, pinned client, normalizers,
    // gateway dispatch, secrets). Bundled from lambdas/shared/python.
    const sharedLayer = new lambda.LayerVersion(this, "SharedLayer", {
      code: sharedLayerCode(),
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_13],
      compatibleArchitectures: [lambda.Architecture.ARM_64],
      description: "gov_debates shared library + httpx for the parliamentary debate fetchers",
    });

    // The role the Gateway assumes to invoke the Lambda targets (NOT the agent's role).
    const gatewayRole = new iam.Role(this, "GatewayExecutionRole", {
      assumedBy: new iam.ServicePrincipal("bedrock-agentcore.amazonaws.com", {
        conditions: {
          StringEquals: { "aws:SourceAccount": this.account },
          ArnLike: {
            "aws:SourceArn": `arn:aws:bedrock-agentcore:${this.region}:${this.account}:gateway/*`,
          },
        },
      }),
      description: "Role assumed by the AgentCore Gateway to invoke fetcher Lambdas",
    });

    // The Gateway itself: MCP protocol, AWS_IAM inbound auth, semantic search enabled at
    // create time (irreversible; costs nothing until used — preserves the option).
    const gateway = new agentcore.CfnGateway(this, "Gateway", {
      name: `parlamentgpt-gw-${props.suffix}`, // no underscores allowed
      roleArn: gatewayRole.roleArn,
      authorizerType: "AWS_IAM",
      protocolType: "MCP",
      protocolConfiguration: { mcp: { searchType: "SEMANTIC" } },
      // No exceptionLevel: the service default (terse errors) is right for a published
      // sample; flip to "DEBUG" locally when developing new targets.
    });

    this.gatewayArn = gateway.attrGatewayArn;
    this.gatewayId = gateway.attrGatewayIdentifier;
    this.gatewayUrl = gateway.attrGatewayUrl;

    let previousTarget: agentcore.CfnGatewayTarget | undefined;

    // Everything per-jurisdiction is built INLINE in this loop, on purpose: `j` is a
    // for-of binding over the static in-repo JURISDICTIONS registry — never a function
    // parameter and never runtime input — and the asset directory itself is assembled
    // from literals inside the registry (j.lambdaAssetDir). Keeping it out of helper
    // methods means no taint-style "function argument reaches fs" shape exists at all.
    for (const j of jurisdictions) {
      // Query and ingest share one asset (different entrypoints), built once per jurisdiction.
      const code = lambda.Code.fromAsset(j.lambdaAssetDir, {
        exclude: ["tests", ".venv", "*.egg-info", "__pycache__"],
      });

      // --- fetcher Lambda (request path) with host allowlist and (optional) secret ---
      const environment: Record<string, string> = {
        JURISDICTION: j.key,
        ALLOWED_HOSTS: j.hosts.join(","),
      };
      if (j.batchIngest && this.indexBucket) {
        environment.INDEX_BUCKET = this.indexBucket.bucketName;
        environment.INDEX_PREFIX = "speeches";
      }
      // Own the log group explicitly (modern replacement for the deprecated logRetention
      // prop, which also spawns an extra custom-resource Lambda per function).
      const fnLogs = new logs.LogGroup(this, `Fn${j.pascal}Logs`, {
        logGroupName: `/aws/lambda/parlamentgpt-${j.key}-${this.stackName}`,
        retention: logs.RetentionDays.ONE_MONTH,
        removalPolicy: RemovalPolicy.DESTROY,
      });
      const fn = new lambda.Function(this, `Fn${j.pascal}`, {
        runtime: lambda.Runtime.PYTHON_3_13,
        architecture: lambda.Architecture.ARM_64, // repo convention
        handler: "handler.lambda_handler",
        code,
        layers: [sharedLayer],
        timeout: Duration.seconds(j.timeoutS ?? 30),
        memorySize: j.memoryMb ?? 512,
        logGroup: fnLogs,
        environment,
        // NO vpc / vpcSubnets: in-VPC would have no internet route (isolated subnets, no NAT).
      });
      if (j.secretName) {
        // Empty secret; the operator fills the value post-deploy (the ONE manual step).
        const secret = new secretsmanager.Secret(this, `Secret${j.pascal}`, {
          secretName: j.secretName,
          description: `${j.label} API key, JSON {"${j.secretJsonKey ?? "apiKey"}":"..."}`,
          removalPolicy: RemovalPolicy.RETAIN,
        });
        secret.grantRead(fn); // ONLY this jurisdiction's function can read its own key
        fn.addEnvironment("SECRET_ARN", secret.secretArn);
        new CfnOutput(this, `Secret${j.pascal}Arn`, {
          value: secret.secretArn,
          description: `Fill this secret with the ${j.label} API key.`,
        });
      }

      // --- scheduled ingest job (only for sources with no queryable debates API) ---
      // Separate from the query Lambda by necessity: these jobs download tens of MB
      // (France's bulk ZIP is 55.7 MB / 324 MB unpacked) and run for minutes, so they can
      // never sit in a request path. They are the only writers to the index bucket.
      if (j.batchIngest && this.indexBucket) {
        // The query path only READS the index; the ingest job is the only writer.
        this.indexBucket.grantRead(fn);
        const ingest = j.batchIngest;
        const ingestLogs = new logs.LogGroup(this, `Ingest${j.pascal}Logs`, {
          logGroupName: `/aws/lambda/parlamentgpt-ingest-${j.key}-${this.stackName}`,
          retention: logs.RetentionDays.ONE_MONTH,
          removalPolicy: RemovalPolicy.DESTROY,
        });
        const ingestFn = new lambda.Function(this, `Ingest${j.pascal}`, {
          runtime: lambda.Runtime.PYTHON_3_13,
          architecture: lambda.Architecture.ARM_64,
          // The same asset as the query path; a different entrypoint inside it.
          handler: "handler.ingest_lambda_handler",
          code,
          layers: [sharedLayer],
          timeout: Duration.seconds(ingest.timeoutS),
          memorySize: ingest.memoryMb,
          logGroup: ingestLogs,
          environment,
          // Lambda caps descriptions at 256 chars; the full rationale lives on the entry.
          description: truncate(`Ingest ${j.label} debates into the index. ${ingest.reason}`, 256),
        });
        this.indexBucket.grantReadWrite(ingestFn);
        new events.Rule(this, `Ingest${j.pascal}Schedule`, {
          schedule: events.Schedule.expression(ingest.schedule),
          targets: [new targets.LambdaFunction(ingestFn)],
          description: `Scheduled ingest of ${j.label} debates`,
        });
      }

      fn.grantInvoke(gatewayRole); // L1 => explicit grant (appears in review)

      const target = new agentcore.CfnGatewayTarget(this, `Target${j.pascal}`, {
        gatewayIdentifier: gateway.attrGatewayIdentifier,
        name: j.key, // becomes the tool-name prefix: {key}___{tool}
        credentialProviderConfigurations: [{ credentialProviderType: "GATEWAY_IAM_ROLE" }],
        targetConfiguration: {
          mcp: {
            lambda: {
              lambdaArn: fn.functionArn,
              toolSchema: { inlinePayload: toolDefinitions(j) },
            },
          },
        },
      });
      target.addDependency(gateway.node.defaultChild as any ?? gateway);
      // Serialize target creation: CreateGatewayTarget is 5 TPS and capped at 5 concurrent
      // operations per gateway, so chain them to avoid ThrottlingException on wide deploys.
      if (previousTarget) target.addDependency(previousTarget);
      previousTarget = target;
    }

    new CfnOutput(this, "GatewayUrl", { value: this.gatewayUrl });
    new CfnOutput(this, "GatewayArn", { value: this.gatewayArn });
    new CfnOutput(this, "GatewayMcpUrl", { value: `${this.gatewayUrl}` });
  }

}
