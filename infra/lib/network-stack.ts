import { Stack, StackProps, CfnOutput, Tags } from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import { Construct } from "constructs";

export interface NetworkStackProps extends StackProps {}

/**
 * VPC for the workload.
 *
 * Workload subnets are fully isolated (no NAT, no internet route). The only things
 * that live there are the interface VPC endpoints below, so no NAT gateway is needed.
 * The public-facing frontend tasks run in the public subnets with public IPs and reach
 * the internet directly via the IGW; the AgentCore runtime runs outside this VPC.
 *
 * Egress to external parliament hosts is restricted at the application layer, per fetcher
 * Lambda: each is host-pinned to its own ALLOWED_HOSTS via the shared PinnedHttpClient (see
 * `lambdas/shared/python/gov_debates/http/pinned_client.py`). The fetcher Lambdas run OUTSIDE
 * this VPC. The previous AWS Network Firewall egress allowlist and the NAT gateways were
 * removed to reduce cost.
 *
 * AWS service traffic (Bedrock, Secrets Manager, Logs, ECR, STS) uses interface VPC
 * endpoints in the workload subnets.
 */
export class NetworkStack extends Stack {
  public readonly vpc: ec2.Vpc;
  public readonly workloadSubnets: ec2.SubnetSelection;
  public readonly endpointSecurityGroup: ec2.SecurityGroup;

  constructor(scope: Construct, id: string, props: NetworkStackProps) {
    super(scope, id, props);

    const maxAzs = 2;

    this.vpc = new ec2.Vpc(this, "Vpc", {
      maxAzs,
      natGateways: 0, // workload subnets are isolated; no NAT needed
      ipAddresses: ec2.IpAddresses.cidr("10.42.0.0/16"),
      subnetConfiguration: [
        { name: "public", subnetType: ec2.SubnetType.PUBLIC, cidrMask: 26 },
        { name: "workload", subnetType: ec2.SubnetType.PRIVATE_ISOLATED, cidrMask: 22 },
      ],
      restrictDefaultSecurityGroup: true,
    });

    this.workloadSubnets = { subnetGroupName: "workload" };

    // --- Interface VPC endpoints so AWS calls bypass the internet entirely ---
    this.endpointSecurityGroup = new ec2.SecurityGroup(this, "VpcEndpointSg", {
      vpc: this.vpc,
      description: "Allow HTTPS from workloads to interface VPC endpoints",
      allowAllOutbound: false,
    });
    this.endpointSecurityGroup.addIngressRule(
      ec2.Peer.ipv4(this.vpc.vpcCidrBlock),
      ec2.Port.tcp(443),
      "HTTPS from within the VPC",
    );

    const ifaceEndpoints: Record<string, ec2.InterfaceVpcEndpointAwsService> = {
      Bedrock: ec2.InterfaceVpcEndpointAwsService.BEDROCK_RUNTIME,
      // AgentCore data plane (invoke). Falls back to a service name if the enum is absent.
      SecretsManager: ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER,
      Logs: ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS,
      Ecr: ec2.InterfaceVpcEndpointAwsService.ECR,
      EcrDocker: ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER,
      Sts: ec2.InterfaceVpcEndpointAwsService.STS,
    };
    for (const [name, service] of Object.entries(ifaceEndpoints)) {
      this.vpc.addInterfaceEndpoint(`${name}Endpoint`, {
        service,
        subnets: { subnetGroupName: "workload" },
        securityGroups: [this.endpointSecurityGroup],
        privateDnsEnabled: true,
      });
    }
    // S3 (for ECR layer pulls) via gateway endpoint — free and keeps pulls off the internet.
    this.vpc.addGatewayEndpoint("S3Endpoint", {
      service: ec2.GatewayVpcEndpointAwsService.S3,
      subnets: [{ subnetGroupName: "workload" }],
    });

    Tags.of(this.vpc).add("project", "parlamentgpt");

    new CfnOutput(this, "VpcId", { value: this.vpc.vpcId });
    // Per-jurisdiction egress allowlists now live on each fetcher Lambda (ALLOWED_HOSTS),
    // set from the JURISDICTIONS table in the Gateway stack.
  }
}
