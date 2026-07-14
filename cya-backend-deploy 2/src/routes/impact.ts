import { Router } from 'express';
import { pool } from '../db';
import { requireAuth } from './auth';

export const impactRouter = Router();

impactRouter.get('/me', requireAuth, async (req: any, res) => {
  const r = await pool.query(
    `SELECT COUNT(*) AS meals,
            COALESCE(SUM(steps_at_claim),0) AS total_steps
     FROM donations WHERE user_id = $1`, [req.user.id]);
  res.json(r.rows[0]);
});

impactRouter.get('/community', async (_req, res) => {
  const r = await pool.query(`SELECT * FROM community_impact`);
  res.json(r.rows[0]);
});
