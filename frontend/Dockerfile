# Next.js (standalone output) as a plain HTTP server container, run on ECS Fargate
# behind an ALB. SSE streaming works natively over the ALB; no adapter layer involved.
# Debian pinned to trixie (13, current stable): `node:20-slim` re-targets new Debian releases over
# time, which would swap the whole OS underneath a routine rebuild. Security patches for
# the pinned release are applied explicitly in the runner stage below.
FROM public.ecr.aws/docker/library/node:20-trixie-slim@sha256:abfbe12cc943141a0c9e8c0a57d710df1dadd95d35e8662cc02958b284d1f35b AS deps
WORKDIR /app
COPY package.json package-lock.json ./
# npm ci only: installs are reproducible from the lockfile or fail loudly.
RUN npm ci

FROM public.ecr.aws/docker/library/node:20-trixie-slim@sha256:abfbe12cc943141a0c9e8c0a57d710df1dadd95d35e8662cc02958b284d1f35b AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# Provisioned-source list, baked in by the CDK image build so the UI advertises exactly
# the sources this deployment created (empty = the static defaults in lib/jurisdictions.ts).
ARG NEXT_PUBLIC_ENABLED_JURISDICTIONS=""
ENV NEXT_PUBLIC_ENABLED_JURISDICTIONS=$NEXT_PUBLIC_ENABLED_JURISDICTIONS
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

FROM public.ecr.aws/docker/library/node:20-trixie-slim@sha256:abfbe12cc943141a0c9e8c0a57d710df1dadd95d35e8662cc02958b284d1f35b AS runner
# Apply Debian security updates: the base image lags the security repos, so patched
# package versions (gnutls, glibc, systemd libs, dpkg, ...) are pulled at build time.
RUN apt-get update \
  && apt-get -y upgrade \
  && apt-get clean \
  && rm -rf /var/lib/apt/lists/*
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0
RUN groupadd -g 10001 nodegrp && useradd -u 10001 -g nodegrp nextuser
COPY --from=builder /app/public ./public
COPY --from=builder --chown=nextuser:nodegrp /app/.next/standalone ./
COPY --from=builder --chown=nextuser:nodegrp /app/.next/static ./.next/static
USER nextuser
EXPOSE 3000
# Liveness for local runs and image scanners; ECS/Fargate health checks are configured on
# the target group and ignore this. Any response below 500 (incl. the auth redirect) is OK.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["node", "-e", "fetch('http://127.0.0.1:3000/').then(r=>process.exit(r.status<500?0:1)).catch(()=>process.exit(1))"]
CMD ["node", "server.js"]
