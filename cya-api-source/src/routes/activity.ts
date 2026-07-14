import { Router } from 'express';
import { pool } from '../db';
import { requireAuth } from './auth';
import { validateActivity } from '../services/fraud';

export const activityRouter = Router();

/** POST /activity/sync  { steps, distanceM, windowMinutes, source } */
activityRouter.post('/sync', requireAuth, async (req: any, res) => {
  const { steps, distanceM, windowMinutes = 0, source = 'pedometer' } = req.body;
  const check = validateActivity({ steps, windowMinutes, source });
  if (!check.ok) return res.status(422).json({ error: 'rejected', reason: check.reason });

  await pool.query(
    `INSERT INTO activities (user_id, activity_date, steps, distance_m, source)
     VALUES ($1, CURRENT_DATE, $2, $3, $4)
     ON CONFLICT (user_id, activity_date)
     DO UPDATE SET steps = GREATEST(activities.steps, EXCLUDED.steps),
                   distance_m = EXCLUDED.distance_m, synced_at = now()`,
    [req.user.id, steps, distanceM ?? null, source]
  );
  res.json({ ok: true });
});
