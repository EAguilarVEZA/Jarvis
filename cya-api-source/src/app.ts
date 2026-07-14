import 'express-async-errors'; // forwards async route errors to the error handler (prevents hung requests)
import express from 'express';
import cors from 'cors';
import { authRouter } from './routes/auth';
import { activityRouter } from './routes/activity';
import { donationsRouter } from './routes/donations';
import { offersRouter } from './routes/offers';
import { impactRouter } from './routes/impact';
import { sponsorRouter } from './routes/sponsor';
import { preferencesRouter } from './routes/preferences';
import { sessionsRouter } from './routes/sessions';
import { parchesRouter } from './routes/parches';
import { adminRouter } from './routes/admin';
import { pasiRouter } from './routes/pasi';
import { ttsRouter } from './routes/tts';
import { ensureInit } from './init';

// Builds the Express app. Kept separate from listen() so the same app can run
// locally / on Render (index.ts calls listen) and on Vercel (api/index.ts exports it).
export function buildApp() {
  const app = express();
  app.use(cors());
  app.use(express.json());

  // Health check never touches the DB.
  app.get('/health', (_req, res) => res.json({ ok: true }));

  // Self-initialize the database on the first request after a cold start.
  app.use(async (_req, res, next) => {
    try { await ensureInit(); next(); }
    catch (e: any) { res.status(500).json({ error: 'db_init_failed', detail: String(e?.message || e) }); }
  });

  app.use('/auth', authRouter);
  app.use('/activity', activityRouter);
  app.use('/donations', donationsRouter);
  app.use('/offers', offersRouter);
  app.use('/impact', impactRouter);
  app.use('/sponsor', sponsorRouter);
  app.use('/preferences', preferencesRouter);
  app.use('/sessions', sessionsRouter);
  app.use('/parches', parchesRouter);
  app.use('/admin', adminRouter);
  app.use('/pasi', pasiRouter);
  app.use('/tts', ttsRouter);

  // Global error handler: any thrown/rejected handler returns JSON instead of hanging.
  app.use((err: any, _req: express.Request, res: express.Response, _next: express.NextFunction) => {
    console.error('handler_error', err);
    if (res.headersSent) return;
    res.status(500).json({ error: 'internal', detail: String(err?.message || err) });
  });
  return app;
}
