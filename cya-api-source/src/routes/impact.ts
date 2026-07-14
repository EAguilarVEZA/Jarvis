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

/** GET /impact/by-city — real meals donated per city (for the Impacto Nacional map) */
impactRouter.get('/by-city', async (_req, res) => {
  const r = await pool.query(
    `SELECT COALESCE(NULLIF(TRIM(u.city),''),'(sin ciudad)') AS city, COUNT(*)::int AS meals
     FROM donations d JOIN users u ON u.id = d.user_id
     GROUP BY 1 ORDER BY meals DESC`);
  res.json(r.rows);
});

/**
 * GET /impact/receipts — the walker's traceable "recibos de impacto": each meal they
 * triggered, linked to the sponsor that funded it and the food bank that delivered it.
 */
impactRouter.get('/receipts', requireAuth, async (req: any, res) => {
  const r = await pool.query(
    `SELECT d.id, d.donation_date,
            c.cost_per_meal_cop AS value_cop,
            s.name AS sponsor,
            fb.name AS food_bank, fb.city, fb.abaco_member
     FROM donations d
     JOIN campaigns c ON c.id = d.campaign_id
     JOIN sponsors s ON s.id = c.sponsor_id
     JOIN food_banks fb ON fb.id = c.food_bank_id
     WHERE d.user_id = $1
     ORDER BY d.donation_date DESC, d.created_at DESC`,
    [req.user.id]
  );
  res.json(r.rows.map((x: any, i: number) => ({
    receiptId: 'CYA-' + String(x.id).replace(/-/g, '').slice(0, 8).toUpperCase(),
    date: x.donation_date,
    sponsor: x.sponsor,
    foodBank: x.food_bank,
    city: x.city,
    abaco: x.abaco_member,
    valueCop: x.value_cop,
    meal: r.rows.length - i,   // running meal number (oldest = 1)
  })));
});
