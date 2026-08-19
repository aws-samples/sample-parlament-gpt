import { App } from "aws-cdk-lib";
import { Template, Match } from "aws-cdk-lib/assertions";
import { NetworkStack } from "../lib/network-stack";

const env = { account: "111111111111", region: "eu-central-1" };

describe("Network egress lockdown", () => {
  const app = new App();
  const stack = new NetworkStack(app, "TestNet", { env });
  const t = Template.fromStack(stack);

  test("no Network Firewall is created (removed for cost)", () => {
    t.resourceCountIs("AWS::NetworkFirewall::Firewall", 0);
    t.resourceCountIs("AWS::NetworkFirewall::FirewallPolicy", 0);
    t.resourceCountIs("AWS::NetworkFirewall::RuleGroup", 0);
  });

  test("no NAT gateways are created (removed for cost)", () => {
    t.resourceCountIs("AWS::EC2::NatGateway", 0);
    t.resourceCountIs("AWS::EC2::EIP", 0);
  });

  test("workload subnets are isolated: no default route to the internet", () => {
    // No route in this stack should send 0.0.0.0/0 to a NAT gateway or firewall endpoint.
    const all = t.findResources("AWS::EC2::Route");
    for (const [, route] of Object.entries(all)) {
      const props = (route as any).Properties;
      if (props.DestinationCidrBlock === "0.0.0.0/0") {
        expect(props.NatGatewayId).toBeUndefined();
        expect(props.VpcEndpointId).toBeUndefined();
      }
    }
  });

  test("interface VPC endpoints exist for required AWS services", () => {
    const eps = t.findResources("AWS::EC2::VPCEndpoint");
    const services = Object.values(eps).map((e: any) => e.Properties.ServiceName);
    const joined = JSON.stringify(services);
    for (const svc of ["bedrock", "secretsmanager", "logs", "ecr", "sts"]) {
      expect(joined.toLowerCase()).toContain(svc);
    }
  });
});
