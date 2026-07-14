import { Router } from 'express';
import { pool } from '../db';
import { requireAuth } from './auth';

export const donationsRouter = Router();

/**
 * POST /donations/claim
 * Claims today's meal donation. Enforces:
 *  - user reached daily goal (server-side check against activities)
 *  - 1 donation per user per day (DB unique constraint)
 *  - sponsor campaign budget not exceeded (atomic update)
 */
donationsRouter.post('/claim', requireAuth, async (req: any, res) => {
  const userId = req.user.id;
  const client = await pool.connect();
  try {
    await client.query('BEGIN');

    const act = await client.query(
      `SELECT a.steps, u.daily_goal FROM activities a
       JOIN users u ON u.id = a.user_id
       WHERE a.user_id = $1 AND a.activity_date = CURRENT_DATE`,
      [userId]
    );
    if (!act.rows.length || act.rows[0].steps < act.rows[0].daily_goal) {
      await client.query('ROLLBACK');
      return res.status(400).json({ error: 'goal_not_reached' });
    }

    // Pick an active campaign with budget left, lock the row
    const camp = await client.query(
      `SELECT id FROM campaigns
       WHERE active AND meals_donated < meals_budget
         AND CURRENT_DATE BETWEEN starts_at AND ends_at
       ORDER BY meals_donated::float / meals_budget ASC
       LIMIT 1 FOR UPDATE SKIP LOCKED`
    );
    if (!camp.rows.length) {
      await client.query('ROLLBACK');
      return res.status(409).json({ error: 'no_sponsor_budget_today' });
    }
    const campaignId = camp.rows[0].id;

    await client.query(
      `INSERT INTO donations (user_id, campaign_id, donation_date, steps_at_claim)
       VALUES ($1, $2, CURRENT_DATE, $3)`,
      [userId, campaignId, act.rows[0].steps]
    ); // UNIQUE(user_id, donation_date) rejects double claims

    await client.query(
      `UPDATE campaigns SET meals_donated = meals_donated + 1 WHERE id = $1`,
      [campaignId]
    );

    // Coupon issuance is DECOUPLED from the donation: completing a walk earns a
    // "coupon credit". The user then chooses which vendor coupon to add to their
    // wallet via POST /offers/choose. (No coupon is issued here.)
    await client.query('COMMIT');
    res.json({ donated: true, campaignId, creditEarned: true });
  } catch (e: any) {
    await client.query('ROLLBACK');
    if (e.code === '23505') return res.status(409).json({ error: 'already_donated_today' });
    console.error(e);
    res.status(500).json({ error: 'internal' });
  } finally {
    client.release();
  }
});
