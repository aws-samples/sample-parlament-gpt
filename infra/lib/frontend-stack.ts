import { Stack, StackProps, CfnOutput, Duration, RemovalPolicy } from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as ecrAssets from "aws-cdk-lib/aws-ecr-assets";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as logs from "aws-cdk-lib/aws-logs";
import * as cognito from "aws-cdk-lib/aws-cognito";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import * as cloudfront from "aws-cdk-lib/aws-cloudfront";
import * as origins from "aws-cdk-lib/aws-cloudfront-origins";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import * as route53 from "aws-cdk-lib/aws-route53";
import * as route53Targets from "aws-cdk-lib/aws-route53-targets";
import * as ses from "aws-cdk-lib/aws-ses";
import * as cr from "aws-cdk-lib/custom-resources";
import { Construct } from "constructs";
import * as crypto from "crypto";
import * as fs from "fs";
import * as path from "path";

export interface FrontendStackProps extends StackProps {
  readonly vpc: ec2.IVpc;
  readonly agentRuntimeArn: string;
  /** Stack suffix; also used for the (globally unique per region) Cognito domain prefix. */
  readonly suffix: string;
  /**
   * Optional custom domain for the CloudFront distribution. All three fields must be
   * provided together: the FQDN to serve on, a us-east-1 ACM certificate covering it
   * (see CertificateStack), and the hosted zone to create the alias records in.
   */
  readonly domainName?: string;
  readonly certificate?: acm.ICertificate;
  readonly hostedZoneDomain?: string;
  /**
   * Comma-separated e-mail domain patterns allowed to sign up, enforced by a Cognito
   * PreSignUp trigger BEFORE any account or verification mail exists. Semantics:
   * "example.com" (exact), "*.example.com" (domain + subdomains), "amazon.*" (any TLD,
   * including multi-label like co.uk), "*" or unset/empty (no restriction).
   */
  readonly signupAllowedEmailDomains?: string;
  /** Allow self sign-up on the hosted UI (default true). false = operator-created users only. */
  readonly selfSignUpEnabled?: boolean;
  /**
   * Whether NEW users start with the technical debug trace enabled (default false).
   * Useful for demo deployments that want the pipeline visible out of the box; leave off
   * for anything customer-facing. Users can flip it themselves either way.
   */
  readonly defaultDebugMode?: boolean;
  /**
   * Optional SES sender for Cognito's verification/invitation e-mails. When unset,
   * Cognito's default sender is used (no SES setup needed; capped at ~50 mails/day —
   * fine for demos). When set and the domain lies in `hostedZoneDomain`, an SES domain
   * identity with DKIM records is created. Note: an SES account in sandbox mode only
   * delivers to verified addresses.
   */
  readonly signupEmailFrom?: string;
  /**
   * Gateway target keys of the provisioned sources (from enabledJurisdictions()). Baked
   * into the frontend image at build time so the UI advertises exactly what is deployed.
   */
  readonly enabledJurisdictionKeys?: string[];
}

/**
 * Public frontend: Next.js (standalone) container on AWS Fargate behind an internet-facing
 * ALB, fronted by CloudFront. Authentication is Amazon Cognito (hosted UI, OAuth 2.0
 * authorization-code flow); chat sessions and per-user settings live in DynamoDB.
 *
 *   Internet -> CloudFront (TLS, HTTP/2+3)
 *            -> [secret header + SG locked to CloudFront prefix list] ALB
 *            -> Fargate (Next.js) -> Bedrock AgentCore
 *                                 -> DynamoDB (chat sessions, user settings)
 *   Sign-in:    /api/auth/login -> Cognito hosted UI -> /api/auth/callback (code -> tokens)
 *
 * Resource ordering is deliberate: ALB -> CloudFront -> Cognito -> task definition ->
 * service. The Cognito client must register real callback URLs (custom domain and/or the
 * CloudFront domain), and the task definition needs the Cognito client id — creating the
 * distribution before the pool client and the client before the task definition keeps
 * that dependency chain acyclic.
 *
 * Why ALB+Fargate instead of a Lambda Function URL + OAC: this account's AWS Organization
 * guardrail denies access to Lambda Function URLs from any non-org principal (the CloudFront
 * OAC service principal and anonymous callers are both blocked). ALB -> Fargate is pure
 * networking with no resource-based policy, so the guardrail does not apply, and it supports
 * SSE response streaming end to end.
 *
 * The origin is private: the ALB security group only admits CloudFront's managed origin-facing
 * prefix list, and CloudFront injects a secret header that an ALB listener rule requires.
 */
export class FrontendStack extends Stack {
  constructor(scope: Construct, id: string, props: FrontendStackProps) {
    super(scope, id, props);

    const region = Stack.of(this).region;

    // Secret shared between CloudFront and the ALB so only CloudFront-originated requests
    // are served. Deterministic per account+stack so both sides always agree across deploys.
    const originSecret = crypto
      .createHash("sha256")
      .update(`${this.account}:${id}:parlamentgpt-origin`)
      .digest("hex");
    const ORIGIN_SECRET_HEADER = "x-origin-secret";

    const cluster = new ecs.Cluster(this, "Cluster", { vpc: props.vpc, containerInsightsV2: ecs.ContainerInsights.ENABLED });

    const image = new ecrAssets.DockerImageAsset(this, "FrontendImage", {
      directory: path.join(__dirname, "..", "..", "frontend"),
      platform: ecrAssets.Platform.LINUX_ARM64,
      buildArgs: props.enabledJurisdictionKeys?.length
        ? { NEXT_PUBLIC_ENABLED_JURISDICTIONS: props.enabledJurisdictionKeys.join(",") }
        : undefined,
    });

    // --- Internet-facing ALB, locked to CloudFront (created before the service so the
    // --- CloudFront domain exists when the Cognito callback URLs are registered) ---
    const albSg = new ec2.SecurityGroup(this, "AlbSg", {
      vpc: props.vpc,
      description: "ALB: ingress only from CloudFront origin-facing prefix list",
      allowAllOutbound: true,
    });

    // Resolve CloudFront's managed origin-facing prefix list id (region-specific) at deploy time.
    const cfPrefixList = new cr.AwsCustomResource(this, "CfOriginPrefixList", {
      onUpdate: {
        service: "EC2",
        action: "describeManagedPrefixLists",
        parameters: {
          Filters: [{ Name: "prefix-list-name", Values: ["com.amazonaws.global.cloudfront.origin-facing"] }],
        },
        physicalResourceId: cr.PhysicalResourceId.of("cf-origin-facing-prefix-list"),
      },
      policy: cr.AwsCustomResourcePolicy.fromSdkCalls({ resources: cr.AwsCustomResourcePolicy.ANY_RESOURCE }),
    });
    const cfPrefixListId = cfPrefixList.getResponseField("PrefixLists.0.PrefixListId");
    albSg.addIngressRule(ec2.Peer.prefixList(cfPrefixListId), ec2.Port.tcp(80), "CloudFront origin-facing only");

    const alb = new elbv2.ApplicationLoadBalancer(this, "Alb", {
      vpc: props.vpc,
      internetFacing: true,
      securityGroup: albSg,
      vpcSubnets: { subnetType: ec2.SubnetType.PUBLIC },
    });

    const listener = alb.addListener("Http", {
      port: 80,
      protocol: elbv2.ApplicationProtocol.HTTP,
      // Don't auto-open 0.0.0.0/0; ingress is restricted to the CloudFront prefix list above.
      open: false,
      // Default: refuse anything that didn't come through CloudFront (missing/incorrect secret).
      defaultAction: elbv2.ListenerAction.fixedResponse(403, {
        contentType: "text/plain",
        messageBody: "Forbidden",
      }),
    });

    // Standalone target group; the Fargate service registers into it further down.
    const targetGroup = new elbv2.ApplicationTargetGroup(this, "FargateTg", {
      vpc: props.vpc,
      port: 3000,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targetType: elbv2.TargetType.IP,
      deregistrationDelay: Duration.seconds(15),
      healthCheck: {
        path: "/",
        // Unauthenticated requests redirect to the sign-in flow; 3xx is healthy.
        healthyHttpCodes: "200-399",
        interval: Duration.seconds(15),
        timeout: Duration.seconds(5),
      },
    });

    // Only serve requests carrying the CloudFront-injected secret header.
    listener.addTargetGroups("Fargate", {
      targetGroups: [targetGroup],
      priority: 1,
      conditions: [elbv2.ListenerCondition.httpHeader(ORIGIN_SECRET_HEADER, [originSecret])],
    });

    // --- CloudFront in front of the (CloudFront-locked) ALB ---
    const distribution = new cloudfront.Distribution(this, "Cdn", {
      comment: "ParlamentGPT frontend (CloudFront -> ALB -> Fargate)",
      defaultBehavior: {
        origin: new origins.LoadBalancerV2Origin(alb, {
          protocolPolicy: cloudfront.OriginProtocolPolicy.HTTP_ONLY,
          httpPort: 80,
          readTimeout: Duration.seconds(60),
          customHeaders: { [ORIGIN_SECRET_HEADER]: originSecret },
        }),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
        // Off so SSE responses stream through unbuffered/unmodified.
        compress: false,
        cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
        originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER,
        responseHeadersPolicy: cloudfront.ResponseHeadersPolicy.SECURITY_HEADERS,
      },
      httpVersion: cloudfront.HttpVersion.HTTP2_AND_3,
      minimumProtocolVersion: cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
      // Optional custom domain: alias + us-east-1 certificate (see CertificateStack).
      ...(props.domainName && props.certificate
        ? { domainNames: [props.domainName], certificate: props.certificate }
        : {}),
    });

    const zone = props.hostedZoneDomain
      ? route53.HostedZone.fromLookup(this, "Zone", { domainName: props.hostedZoneDomain })
      : undefined;

    // SES domain identity (with DKIM records in the zone) when Cognito should send its
    // verification mails from a custom address whose domain lies in the hosted zone.
    if (props.signupEmailFrom && zone) {
      const senderDomain = props.signupEmailFrom.split("@")[1] ?? "";
      if (senderDomain === zone.zoneName || senderDomain.endsWith(`.${zone.zoneName}`)) {
        new ses.EmailIdentity(this, "SignupSenderIdentity", {
          identity: ses.Identity.publicHostedZone(zone),
          mailFromDomain: `mail.${zone.zoneName}`,
        });
      }
    }

    // Alias records so the custom domain resolves to this distribution.
    if (props.domainName && zone) {
      const target = route53.RecordTarget.fromAlias(
        new route53Targets.CloudFrontTarget(distribution),
      );
      new route53.ARecord(this, "AliasA", { zone, recordName: props.domainName, target });
      new route53.AaaaRecord(this, "AliasAaaa", { zone, recordName: props.domainName, target });
      new CfnOutput(this, "CustomDomainUrl", { value: `https://${props.domainName}` });
    }

    // --- Cognito: user pool + hosted UI + confidential OAuth client ---

    // PreSignUp trigger: enforce the e-mail domain allowlist before any account exists.
    const preSignUpFn = new lambda.Function(this, "PreSignUpFn", {
      runtime: lambda.Runtime.NODEJS_20_X,
      architecture: lambda.Architecture.ARM_64,
      handler: "index.handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "..", "lambda", "pre-signup")),
      description: "Rejects sign-ups whose e-mail domain is not on the allowlist.",
      environment: {
        ALLOWED_EMAIL_DOMAINS: props.signupAllowedEmailDomains ?? "",
      },
      logRetention: logs.RetentionDays.ONE_MONTH,
    });

    const userPool = new cognito.UserPool(this, "UserPool", {
      selfSignUpEnabled: props.selfSignUpEnabled ?? true,
      signInAliases: { email: true },
      autoVerify: { email: true },
      standardAttributes: {
        email: { required: true, mutable: false },
      },
      passwordPolicy: { minLength: 12 },
      accountRecovery: cognito.AccountRecovery.EMAIL_ONLY,
      lambdaTriggers: { preSignUp: preSignUpFn },
      email: props.signupEmailFrom
        ? cognito.UserPoolEmail.withSES({ fromEmail: props.signupEmailFrom, sesRegion: region })
        : cognito.UserPoolEmail.withCognito(),
      // Demo/sample hygiene: the pool goes away with the stack.
      removalPolicy: RemovalPolicy.DESTROY,
    });

    // Hosted UI domain. The prefix is globally unique per region; derive it from the suffix.
    const domainPrefix = `parlamentgpt-${props.suffix}`.toLowerCase().replace(/[^a-z0-9-]/g, "-").slice(0, 63);
    const userPoolDomain = userPool.addDomain("HostedUi", {
      cognitoDomain: { domainPrefix },
      managedLoginVersion: cognito.ManagedLoginVersion.NEWER_MANAGED_LOGIN,
    });

    // Public base URLs this app is served on; each gets callback/logout registrations.
    // localhost is included for local dev against the same pool.
    const appUrls = [
      ...(props.domainName ? [`https://${props.domainName}`] : []),
      `https://${distribution.distributionDomainName}`,
      "http://localhost:3000",
    ];

    const userPoolClient = userPool.addClient("Web", {
      generateSecret: true,
      // USER_PASSWORD_AUTH stays enabled for scripted E2E tests (SECRET_HASH still required).
      authFlows: { userPassword: true },
      oAuth: {
        flows: { authorizationCodeGrant: true },
        scopes: [cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL, cognito.OAuthScope.PROFILE],
        callbackUrls: appUrls.map((u) => `${u}/api/auth/callback`),
        logoutUrls: appUrls.map((u) => `${u}/`),
      },
      idTokenValidity: Duration.hours(12),
      accessTokenValidity: Duration.hours(12),
      refreshTokenValidity: Duration.days(30),
      preventUserExistenceErrors: true,
    });

    // Branding for the newer managed login: the Cognito default style document with the
    // app's design tokens patched in (layout background f2f3f3, 16px form radius, pill
    // buttons) plus the product logo. infra/branding/managed-login-settings.json was
    // derived from `describe-managed-login-branding-by-client --return-merged-resources`
    // so the schema matches what the service actually stores.
    const brandingSettings = JSON.parse(
      fs.readFileSync(path.join(__dirname, "..", "branding", "managed-login-settings.json"), "utf8"),
    );
    const logoAsset = (file: string, colorMode: "LIGHT" | "DARK") => ({
      category: "FORM_LOGO",
      colorMode,
      extension: "SVG",
      // Synth-time read of two literal filenames from this repo, no runtime input
      // (scanner false positive on the dynamic-looking parameter).
      // nosemgrep: path-join-resolve-traversal, detect-non-literal-fs-filename
      bytes: fs.readFileSync(path.join(__dirname, "..", "branding", file)).toString("base64"),
    });
    new cognito.CfnManagedLoginBranding(this, "ManagedLoginBranding", {
      userPoolId: userPool.userPoolId,
      clientId: userPoolClient.userPoolClientId,
      settings: brandingSettings,
      assets: [logoAsset("logo-light.svg", "LIGHT"), logoAsset("logo-dark.svg", "DARK")],
    });

    // The client secret never appears in the task definition: wrap it in Secrets Manager
    // and inject it as an ECS secret.
    const clientSecret = new secretsmanager.Secret(this, "CognitoClientSecret", {
      description: "ParlamentGPT Cognito app-client secret (used for the token exchange).",
      secretStringValue: userPoolClient.userPoolClientSecret,
    });

    // --- DynamoDB: chat sessions + per-user settings ---
    const sessionsTable = new dynamodb.Table(this, "Sessions", {
      partitionKey: { name: "pk", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "sk", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: "expiresAt",
      // Demo/sample hygiene; flip to RETAIN for a long-lived deployment.
      removalPolicy: RemovalPolicy.DESTROY,
    });

    // --- Fargate task + service ---
    const taskDef = new ecs.FargateTaskDefinition(this, "TaskDef", {
      cpu: 512,
      memoryLimitMiB: 1024,
      runtimePlatform: {
        cpuArchitecture: ecs.CpuArchitecture.ARM64,
        operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
      },
    });

    // Least privilege: invoke ONLY the one AgentCore runtime.
    taskDef.addToTaskRolePolicy(
      new iam.PolicyStatement({
        sid: "InvokeAgentRuntimeOnly",
        actions: ["bedrock-agentcore:InvokeAgentRuntime"],
        resources: [props.agentRuntimeArn, `${props.agentRuntimeArn}/*`],
      }),
    );
    sessionsTable.grantReadWriteData(taskDef.taskRole);

    // Own the app log group explicitly so a metric filter can hang off it (below).
    const webLogGroup = new logs.LogGroup(this, "WebLogs", {
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    // Tripwire for degraded sign-out revocation (threat model S7): the fail-open read
    // path and a failed revocation write each log one of these exact phrases. The custom
    // metric costs nothing until alarmed on; a prolonged non-zero level means revocation
    // is effectively OFF while tokens stay valid (up to 12 h).
    new logs.MetricFilter(this, "RevocationDegradedMetric", {
      logGroup: webLogGroup,
      filterPattern: logs.FilterPattern.anyTerm(
        "revocation check unavailable",
        "failed to record sign-out revocation",
      ),
      metricNamespace: "ParlamentGPT",
      metricName: "RevocationDegraded",
      metricValue: "1",
    });

    const container = taskDef.addContainer("web", {
      image: ecs.ContainerImage.fromDockerImageAsset(image),
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: "frontend",
        logGroup: webLogGroup,
      }),
      environment: {
        AWS_REGION: region,
        AGENT_RUNTIME_ARN: props.agentRuntimeArn,
        SESSIONS_TABLE: sessionsTable.tableName,
        COGNITO_ISSUER: `https://cognito-idp.${region}.amazonaws.com/${userPool.userPoolId}`,
        COGNITO_CLIENT_ID: userPoolClient.userPoolClientId,
        COGNITO_DOMAIN: `https://${domainPrefix}.auth.${region}.amazoncognito.com`,
        ...(props.defaultDebugMode ? { DEFAULT_DEBUG_MODE: "true" } : {}),
        PORT: "3000",
        HOSTNAME: "0.0.0.0",
      },
      secrets: {
        COGNITO_CLIENT_SECRET: ecs.Secret.fromSecretsManager(clientSecret),
      },
    });
    container.addPortMappings({ containerPort: 3000 });

    const serviceSg = new ec2.SecurityGroup(this, "ServiceSg", {
      vpc: props.vpc,
      description: "Frontend Fargate tasks",
      allowAllOutbound: true, // reaches AgentCore/Cognito/DynamoDB over the internet via the IGW
    });

    const service = new ecs.FargateService(this, "Service", {
      cluster,
      taskDefinition: taskDef,
      desiredCount: 1,
      // Public subnets + public IP: no NAT, tasks reach AgentCore / ECR directly via the IGW.
      vpcSubnets: { subnetType: ec2.SubnetType.PUBLIC },
      assignPublicIp: true,
      securityGroups: [serviceSg],
      minHealthyPercent: 100,
      maxHealthyPercent: 200,
    });
    targetGroup.addTarget(service);

    new CfnOutput(this, "CdnDomainName", {
      value: `https://${distribution.distributionDomainName}`,
      description: "Public URL of the application (CloudFront).",
    });
    new CfnOutput(this, "AlbDnsName", {
      value: alb.loadBalancerDnsName,
      description: "Origin ALB DNS (reachable only from CloudFront).",
    });
    new CfnOutput(this, "UserPoolId", { value: userPool.userPoolId });
    new CfnOutput(this, "UserPoolClientId", { value: userPoolClient.userPoolClientId });
    new CfnOutput(this, "CognitoHostedUiDomain", {
      value: `https://${domainPrefix}.auth.${region}.amazoncognito.com`,
      description: "Cognito managed-login domain (sign-in and sign-up UI).",
    });
    new CfnOutput(this, "SessionsTableName", { value: sessionsTable.tableName });
    // Referenced so the domain resource is never orphaned even without other consumers.
    new CfnOutput(this, "CognitoDomainPrefix", { value: userPoolDomain.domainName });
  }
}
