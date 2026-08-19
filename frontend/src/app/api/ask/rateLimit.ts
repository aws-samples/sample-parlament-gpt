// In-memory fixed-window rate limiter. Sufficient for a single small Fargate task.
// For multi-task scale, replace with a shared store (e.g. DynamoDB/ElastiCache).

const WINDOW_MS = 60_000;
const MAX_PER_WINDOW = 20;
// Sweep expired buckets once the map grows past this size, so memory stays bounded even
// when many distinct client keys (IPs) hit the API over the process lifetime.
const SWEEP_THRESHOLD = 1_000;

type Bucket = { count: number; resetAt: number };
const buckets = new Map<string, Bucket>();

function sweepExpired(now: number) {
  for (const [key, bucket] of buckets) {
    if (now > bucket.resetAt) buckets.delete(key);
  }
}

export function rateLimit(key: string): { ok: boolean; retryAfterMs: number } {
  const now = Date.now();
  if (buckets.size >= SWEEP_THRESHOLD) {
    sweepExpired(now);
  }
  const b = buckets.get(key);
  if (!b || now > b.resetAt) {
    buckets.set(key, { count: 1, resetAt: now + WINDOW_MS });
    return { ok: true, retryAfterMs: 0 };
  }
  if (b.count >= MAX_PER_WINDOW) {
    return { ok: false, retryAfterMs: b.resetAt - now };
  }
  b.count += 1;
  return { ok: true, retryAfterMs: 0 };
}
