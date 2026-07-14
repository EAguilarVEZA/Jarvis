// Vercel serverless entry point. Vercel routes all requests here (see vercel.json)
// and runs the Express app as a function. Use a POOLED Postgres connection
// (Neon/Supabase pooler) via DATABASE_URL — serverless opens many short connections.
import { buildApp } from '../src/app';
export default buildApp();
