import { Router } from 'express';
import { pool } from '../db';

export const sponsorRouter = Router();

/** GET /sponsor/dashboard?sponsorId=... — campaign metrics for the brand */
sponsorRouter.get('/dashboard', async (req, res) => {
  const { sponsorId } = req.query;
  const r = await pool.query(
    `SELECT cp.id AS campaign_id, cp.meals_budget, cp.meals_donated,
            cp.cost_per_meal_cop,
            COUNT(DISTINCT c.id) AS coupons_issued,
            COUNT(DISTINCT c.id) FILTER (WHERE c.redeemed_at IS NOT NULL) AS coupons_redeemed
     FROM campaigns cp
     LEFT JOIN offers o ON o.campaign_id = cp.id
     LEFT JOIN coupons c ON c.offer_id = o.id
     WHERE cp.sponsor_id = $1
     GROUP BY cp.id`,
    [sponsorId]
  );
  res.json(r.rows);
});
