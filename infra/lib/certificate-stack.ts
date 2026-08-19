import { Stack, StackProps, CfnOutput } from "aws-cdk-lib";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import * as route53 from "aws-cdk-lib/aws-route53";
import { Construct } from "constructs";

export interface CertificateStackProps extends StackProps {
  /** Fully qualified domain the app is served on, e.g. app.example.com. */
  readonly domainName: string;
  /** Route 53 public hosted zone that contains (or is) the parent of domainName. */
  readonly hostedZoneDomain: string;
}

/**
 * ACM certificate for the CloudFront alias. CloudFront only accepts certificates from
 * us-east-1, so this stack is always deployed there and the certificate is handed to the
 * (regional) frontend stack via CDK cross-region references. Only instantiated when the
 * optional `domainName` context is set.
 */
export class CertificateStack extends Stack {
  public readonly certificate: acm.ICertificate;

  constructor(scope: Construct, id: string, props: CertificateStackProps) {
    super(scope, id, props);

    const zone = route53.HostedZone.fromLookup(this, "Zone", {
      domainName: props.hostedZoneDomain,
    });

    this.certificate = new acm.Certificate(this, "Certificate", {
      domainName: props.domainName,
      validation: acm.CertificateValidation.fromDns(zone),
    });

    new CfnOutput(this, "CertificateArn", { value: this.certificate.certificateArn });
  }
}
