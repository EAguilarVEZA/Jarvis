import { Pool } from 'pg';

// Reuse a single pool across warm serverless invocations (avoids connection
// exhaustion on Neon's free tier). A small max keeps us well under the limit.
const g = global as unknown as { __cyaPool?: Pool };
export const pool =
  g.__cyaPool ??
  (g.__cyaPool = new Pool({
    connectionString: process.env.DATABASE_URL,
    max: 3,
    idleTimeoutMillis: 10_000,
    connectionTimeoutMillis: 15_000,
  }));
