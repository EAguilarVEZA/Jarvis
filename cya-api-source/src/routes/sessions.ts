import { Router } from 'express';
import { pool } from '../db';
import { requireAuth } from './auth';
import { validateActivity } from '../services/fraud';

export const sessionsRouter = Router();

const GOAL_STEPS = 8000;
const MIN_GPS_RATIO = 0.35;  // GPS meters must be >= 35% of (steps * 0.55m): anti-shake

/** POST /sessions/start — begin a single continuous walk session */
sessionsRouter.post('/start', requireAuth, async (req: any, res) => {
  // Close any stale active session first (user abandoned it).
  await pool.query(
    `UPDATE walk_sessions SET status='abandoned', ended_at=now()
     WHERE user_id=$1 AND status='active'`, [req.user.id]);
  const r = await pool.query(
    `INSERT INTO walk_sessions (user_id) VALUES ($1) RETURNING id, started_at`, [req.user.id]);
  res.json({ sessionId: r.rows[0].id, goal: GOAL_STEPS });
});

/** POST /sessions/:id/progress  { steps, gpsMeters } — live update during the walk */
sessionsRouter.post('/:id/progress', requireAuth, async (req: any, res) => {
  const { steps, gpsMeters } = req.body;
  const check = validateActivity({ steps, windowMinutes: 1, source: 'app' as any });
  if (!check.ok) return res.status(422).json({ error: 'rejected', reason: check.reason });
  const r = await pool.query(
    `UPDATE walk_sessions SET steps=$1, gps_meters=$2
     WHERE id=$3 AND user_id=$4 AND status='active' RETURNING steps`,
    [steps, Math.round(gpsMeters ?? 0), req.params.id, req.user.id]);
  if (!r.rows.length) return res.status(409).json({ error: 'session_not_active' });
  res.json({ ok: true, steps: r.rows[0].steps, goal: GOAL_STEPS });
});

/** POST /sessions/:id/abandon — user left mid-session; no meal, nothing accumulates */
sessionsRouter.post('/:id/abandon', requireAuth, async (req: any, res) => {
  await pool.query(
    `UPDATE walk_sessions SET status='abandoned', ended_at=now()
     WHERE id=$1 AND user_id=$2 AND status='active'`, [req.params.id, req.user.id]);
  res.json({ ok: true });
});

/**
 * POST /sessions/:id/complete — the session reached the goal in one shot.
 * Verifies the goal AND real GPS movement, marks the session completed, then
 * claims the daily meal donation tied to THIS session.
 */
sessionsRouter.post('/:id/complete', requireAuth, async (req: any, res) => {
  const client = await pool.connect();
  try {
    await client.query('BEGIN');
    const s = await client.query(
      `SELECT steps, gps_meters FROM walk_sessions
       WHERE id=$1 AND user_id=$2 AND status='active' FOR UPDATE`,
      [req.params.id, req.user.id]);
    if (!s.rows.length) { await client.query('ROLLBACK'); return res.status(409).json({ error: 'session_not_active' }); }

    const { steps, gps_meters } = s.rows[0];
    if (steps < GOAL_STEPS) { await client.query('ROLLBACK'); return res.status(400).json({ error: 'goal_not_reached' }); }
    if (gps_meters < steps * 0.55 * MIN_GPS_RATIO) {
      await client.query('ROLLBACK');
      return res.status(400).json({ error: 'insufficient_movement', message: 'GPS no confirma movimiento real' });
    }

    await client.query(`UPDATE walk_sessions SET status='completed', ended_at=now() WHERE id=$1`, [req.params.id]);

    // pick a sponsor campaign with budget left (same atomic logic as donations.ts)
    const camp = await client.query(
      `SELECT id FROM campaigns WHERE active AND meals_donated < meals_budget
       AND CURRENT_DATE BETWEEN starts_at AND ends_at
       ORDER BY meals_donated::float/meals_budget ASC LIMIT 1 FOR UPDATE SKIP LOCKED`);
    if (!camp.rows.length) { await client.query('ROLLBACK'); return res.status(409).json({ error: 'no_sponsor_budget_today' }); }
    const campaignId = camp.rows[0].id;

    await client.query(
      `INSERT INTO donations (user_id, campaign_id, session_id, donation_date, steps_at_claim)
       VALUES ($1,$2,$3,CURRENT_DATE,$4)`, [req.user.id, campaignId, req.params.id, steps]);
    await client.query(`UPDATE campaigns SET meals_donated=meals_donated+1 WHERE id=$1`, [campaignId]);

    await client.query('COMMIT');
    res.json({ donated: true, sessionId: req.params.id });
  } catch (e: any) {
    await client.query('ROLLBACK');
    if (e.code === '23505') return res.status(409).json({ error: 'already_donated_today' });
    console.error(e);
    res.status(500).json({ error: 'internal' });
  } finally {
    client.release();
  }
});
