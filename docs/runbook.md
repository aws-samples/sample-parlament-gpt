# Runbook

## 0. Prerequisites
- AWS account with Bedrock model access for the configured model — default
  `global.anthropic.claude-sonnet-4-6` (`modelId` context, see README Configuration) —
  enabled in your region (default `eu-central-1`).
- Tools: Node ≥20, Python ≥3.11, Docker, AWS CDK v2 (`npm i -g aws-cdk`), AWS CLI configured.
- A Bundestag DIP API key.

## 1. Verify locally (no AWS cost)
```bash
make test       # python (mock HTTP) + CDK assertion tests
make synth      # renders CloudFormation into infra/cdk.out
make build      # next build + docker images (optional; needs Docker)
```

## 2. Bootstrap & deploy
```bash
cd infra
npx cdk bootstrap aws://<ACCOUNT_ID>/eu-central-1     # once per account/region
cd ..
make deploy                                            # deploys all 4 stacks
```
Deploy order is handled by stack dependencies:
`BundestagNetwork` → `BundestagSecurity` → `BundestagAgent` → `BundestagFrontend`.

> The agent and frontend container images are built by CDK (DockerImageAsset) and pushed to ECR
> during deploy. Ensure Docker is running.

## 3. Provide the DIP API key
The secret is created empty. Fill it (value never goes in the repo):
```bash
make fill-secret KEY=<YOUR_DIP_API_KEY>
# or:
aws secretsmanager put-secret-value --region eu-central-1 \
  --secret-id parlamentgpt/dip-api-key --secret-string '{"apiKey":"<YOUR_DIP_API_KEY>"}'
```
The AgentCore runtime reads the key at request time; no redeploy needed.

## 4. Get the URL
```bash
aws cloudformation describe-stacks --stack-name BundestagFrontend \
  --query "Stacks[0].Outputs[?OutputKey=='CdnDomainName'].OutputValue" --output text
```
This is the **CloudFront** URL (HTTPS). The origin Lambda Function URL uses IAM auth and is only
reachable via CloudFront (OAC-signed); direct access without SigV4 credentials is refused.
Access is further limited to the IPs in the `allowedIps` context by a CloudFront Function (any
other source IP gets a `403` at the edge). Open the CloudFront URL, type a question, submit.

> **Region:** CloudFront and CloudFront Functions are global, so the stacks can deploy to any
> region. (The old us-east-1 requirement was only for the now-removed CLOUDFRONT-scope WAF.)

## 5. Resolved configuration (record after deploy)
Collect outputs:
```bash
for s in BundestagNetwork BundestagSecurity BundestagAgent BundestagFrontend; do
  echo "== $s =="; aws cloudformation describe-stacks --stack-name $s \
    --query "Stacks[0].Outputs" --output table; done
```
Key outputs: `GuardrailIdOut`, `GuardrailVersionOut`, `DipSecretArn`, `AgentRuntimeArn`,
`CdnDomainName`, `FunctionUrl`, `AllowedFqdn` (= `search.dip.bundestag.de`).

## 6. Egress proof test (in deployed env)
Confirm external egress is restricted to the DIP API. From the agent runtime (or a temporary
task in the workload subnets), then:
```bash
# Allowed — should succeed:
curl -sS -o /dev/null -w '%{http_code}\n' https://search.dip.bundestag.de/api/v1/

# Denied — should fail: the workload subnets have no internet route, and the DIP
# client host-pins to search.dip.bundestag.de.
curl -sS --max-time 8 https://example.com ; echo "exit=$?"
curl -sS --max-time 8 https://169.254.169.254/latest/meta-data/ ; echo "exit=$?"
```
Expected: the first returns an HTTP status; the others fail (no route / connection dropped).
Note: external egress is now restricted at the application layer only (the Network Firewall was
removed for cost); the DIP client refuses any host other than `search.dip.bundestag.de`.

## 7. Guardrail / refusal check
On the website ask an off-topic question (e.g. *"Wie programmiere ich in Python?"*). Expected
answer: **"Ich beantworte ausschließlich Fragen zu Reden im Deutschen Bundestag."**
An on-topic question (e.g. *"Was wurde in der 20. Wahlperiode zum Klimaschutz gesagt?"*) returns
a sourced answer with speaker/faction/date/session citations.

## 8. Tear down
```bash
make destroy
```
The DIP secret has `RemovalPolicy.RETAIN` (so the key is not accidentally destroyed). Delete it
manually if desired:
```bash
aws secretsmanager delete-secret --secret-id parlamentgpt/dip-api-key --force-delete-without-recovery
```

## Troubleshooting
- **AgentCore CreateAgentRuntime fails / API not found:** your account/region may not yet expose
  the `bedrock-agentcore-control` API to the custom resource SDK. Build+push the image (CDK does
  this) and create the runtime with the `agentcore` CLI using the same image URI, role, env vars,
  and VPC config (see `docs/architecture.md`), then set `AGENT_RUNTIME_ARN` on the frontend stack.
- **Bedrock AccessDenied:** enable model access for the configured model in the Bedrock console; verify
  the inference-profile ARN matches the IAM resource pattern in `infra/lib/agent-stack.ts`.
- **Frontend 502:** check `AGENT_RUNTIME_ARN` env on the task and the task role's
  `InvokeAgentRuntime` permission; inspect CloudWatch logs `/frontend` and the agent log group.
