import { Router } from 'express';
import { pool } from '../db';
import { requireAuth } from './auth';

export const offersRouter = Router();

/** GET /offers — user's coupons (active first) */
offersRouter.get('/', requireAuth, async (req: any, res) => {
  const r = await pool.query(
    `SELECT c.code, c.expires_at, c.redeemed_at, o.title, o.retailer, s.name AS sponsor
     FROM coupons c
     JOIN offers o ON o.id = c.offer_id
     JOIN campaigns cp ON cp.id = o.campaign_id
     JOIN sponsors s ON s.id = cp.sponsor_id
     WHERE c.user_id = $1
     ORDER BY c.redeemed_at NULLS FIRST, c.expires_at DESC`,
    [req.user.id]
  );
  res.json(r.rows);
});

/** POST /offers/redeem  { code, store } — called by retailer POS integration or in-store validation app */
offersRouter.post('/redeem', async (req, res) => {
  const { code, store } = req.body;
  const r = await pool.query(
    `UPDATE coupons SET redeemed_at = now(), redeemed_store = $2
     WHERE code = $1 AND redeemed_at IS NULL AND expires_at > now()
     RETURNING id`,
    [code, store ?? 'unknown']
  );
  if (!r.rows.length) return res.status(409).json({ error: 'invalid_or_used' });
  res.json({ redeemed: true });
});
