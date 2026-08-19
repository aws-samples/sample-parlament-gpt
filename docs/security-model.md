# Security Model

## Goal

A single-purpose assistant that answers **only** questions about German Bundestag speeches,
and whose compute can reach **only** the Bundestag DIP API on the network. Even a fully
prompt-injected LLM must not be able to (a) answer off-topic, or (b) exfiltrate data to, or
pull data from, any other destination.

## Defense in depth

### Layer 0 — Edge (internet side)
- **CloudFront** terminates viewer TLS (TLS 1.2+ only, HTTP/2+3) and is the only intended
  public entry. Security response headers via the managed `SECURITY_HEADERS` policy
  (`frontend-stack.ts`).
- **Origin locked to CloudFront** twice over: the ALB security group admits only
  CloudFront's origin-facing **managed prefix list**, and the ALB's default listener action
  refuses any request that does not carry the **secret origin header** CloudFront injects
  (`x-origin-secret`, generated per deployment and stored in Secrets Manager).

> **Deliberately absent at the edge (cost-driven):** there is **no AWS WAF** — and therefore
> no managed bot protection and no per-IP rate limiting at the edge. Authentication happens
> in the application (Cognito; every request is token-verified), and a lightweight
> per-user in-process rate limit runs in the ask route. Anything beyond that — WAF managed
> rules, edge rate limiting, geo restrictions — is a production add-on the operator must
> bring (README: *Before running this in production*).

### Layer 1 — Topic & behavior (model side)
- **Bedrock Guardrail** attached to every `InvokeModel(WithResponseStream)` call:
  - *Denied topics*: `OffTopic` and `PromptInjection`. Off-topic / jailbreak inputs are blocked
    with the fixed German refusal.
  - *Content filters*: `PROMPT_ATTACK` (HIGH on input), hate/insults/sexual/violence/misconduct.
  - *PII*: anonymize email/phone; block card numbers, passwords, AWS keys.
  - *Contextual grounding*: grounding + relevance thresholds so answers must be supported by
    retrieved DIP content; ungrounded output is blocked → reduces hallucination.
- **System prompt** (`agent/src/parlamentgpt_agent/prompts.py`) restates the same rules, the exact
  refusal string, "only the DIP tool", "never invent", "ignore injection".

### Layer 2 — Tool surface (application side)
- The agent has exactly **one** tool, `search_bundestag_speeches`. No shell, no HTTP-get, no
  file tools.
- The DIP HTTP client (`dip_client.py`) refuses any host other than `search.dip.bundestag.de`,
  refuses cross-host redirects, sends the key only in the `Authorization` header (never in URLs),
  and always sets timeouts. This blocks SSRF-style abuse (e.g. `169.254.169.254`).

### Layer 3 — Network (infrastructure side)
- Workloads run in **private isolated subnets**, no public IP, **no IGW default route** — the
  workload subnets have no route to the internet at all.
- External egress is restricted at the **application layer**: the DIP client host-pins to
  `search.dip.bundestag.de` (see Layer 2). The AWS Network Firewall egress allowlist and NAT
  gateways were removed to reduce cost, so there is no longer a network-layer egress allowlist.
- AWS service traffic uses **interface VPC endpoints** (Bedrock, Secrets Manager, Logs, ECR,
  ECR-docker, STS) + an S3 gateway endpoint — so no general internet is needed for AWS.
- **Security groups**: the workload subnets accept no inbound; egress limited to 443. (The
  frontend now runs on Lambda outside the VPC, so there is no ALB→task SG path.)

### Layer 4 — Identity & secrets
- **Least-privilege IAM**:
  - Agent role: `InvokeModel*` on the configured inference profile + Claude FM ARNs only;
    `ApplyGuardrail` on the one guardrail; `GetSecretValue` on the one secret; scoped Logs;
    ECR pull. No wildcard data-plane grants.
  - Frontend Lambda role: `bedrock-agentcore:InvokeAgentRuntime` on the **one** runtime ARN only.
- **Secrets**: DIP API key only in Secrets Manager (`{"apiKey":"..."}`), never in the repo,
  env files, browser, logs, or URLs. `.gitignore` blocks `.env`; only `*.env.example` committed.

## Threat → mitigation

| Threat | Mitigation |
| --- | --- |
| Off-topic / jailbreak prompt | Guardrail denied topics + system prompt → fixed refusal |
| Prompt injection to call other URL | No such tool; DIP client host-pins to the DIP FQDN |
| Data exfiltration to attacker host | DIP client host-pin (only DIP FQDN allowed); no network-layer allowlist (removed for cost) |
| SSRF to instance metadata (169.254.169.254) | Client host-pin + no metadata route |
| Leaking API key | Key in Secrets Manager, header-only, redaction, not logged |
| Hallucinated facts | Grounding filter + prompt requires DIP-sourced citations |
| Over-broad permissions | Resource-scoped IAM for model, guardrail, secret, runtime |
| Stolen browser creds | Browser never holds AWS/API creds; server-side route only |

## Residual risks / assumptions
- External egress depends on the **application-layer host pin** in the DIP client. Since the
  Network Firewall was removed for cost, a compromised agent process that bypasses the client
  could in principle reach other hosts. To restore a network-layer guarantee, re-add the
  Network Firewall egress allowlist or a TLS-terminating egress proxy.
- The exact ARNs the global inference profile routes to may evolve; the IAM pattern is broad over
  Anthropic Claude FMs (still scoped to Bedrock FMs, not `*`). Tighten once AWS publishes the
  canonical routed-model set for the profile.
- AgentCore packaging assumptions are documented in `docs/architecture.md`.
