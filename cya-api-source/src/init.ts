import { pool } from './db';
import { SCHEMA_SQL, SEED_SQL } from './sql';

// Ensures the database schema + demo data exist. Idempotent and cached per cold
// start so the very first request to a fresh Vercel Postgres self-initializes.
let done: Promise<void> | null = null;
export function ensureInit(): Promise<void> {
  if (!done) {
    done = (async () => {
      await pool.query(SCHEMA_SQL);
      await pool.query(SEED_SQL);
    })().catch((e) => { done = null; throw e; });
  }
  return done;
}
