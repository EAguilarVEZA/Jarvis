import { Router } from 'express';
import { pool } from '../db';
import { requireAuth } from './auth';

export const offersRouter = Router();

const ACTIVATION_WINDOW_SEC = 300; // 5-minute in-store window (measured from activated_at)

/** GET /offers — the user's wallet (their coupons), with status + days left */
offersRouter.get('/', requireAuth, async (req: any, res) => {
  // Finalize coupons whose 5-minute activation window has elapsed → mark them used.
  await pool.query(
    `UPDATE coupons
       SET redeemed_at = activated_at + interval '${ACTIVATION_WINDOW_SEC} seconds'
     WHERE user_id = $1 AND activated_at IS NOT NULL AND redeemed_at IS NULL
       AND activated_at + interval '${ACTIVATION_WINDOW_SEC} seconds' <= now()`,
    [req.user.id]
  );
  const r = await pool.query(
    `SELECT c.code, c.expires_at, c.redeemed_at, c.activated_at, c.reward_message, o.title, o.retailer, s.name AS sponsor,
            CEIL(EXTRACT(EPOCH FROM (c.expires_at - now()))/86400)::int AS days_left,
            GREATEST(0, CEIL(EXTRACT(EPOCH FROM (c.activated_at + interval '${ACTIVATION_WINDOW_SEC} seconds' - now()))))::int AS activation_left,
            CASE WHEN c.redeemed_at IS NOT NULL THEN 'usado'
                 WHEN c.expires_at <= now() THEN 'vencido'
                 WHEN c.activated_at IS NOT NULL
                      AND c.activated_at + interval '${ACTIVATION_WINDOW_SEC} seconds' > now() THEN 'en_uso'
                 ELSE 'activo' END AS status
     FROM coupons c
     JOIN offers o ON o.id = c.offer_id
     JOIN campaigns cp ON cp.id = o.campaign_id
     JOIN sponsors s ON s.id = cp.sponsor_id
     WHERE c.user_id = $1
     ORDER BY (c.redeemed_at IS NOT NULL), c.expires_at ASC`,
    [req.user.id]
  );
  res.json(r.rows);
});

/** GET /offers/catalog — vendor coupons the user can choose from (active campaigns) */
offersRouter.get('/catalog', async (_req, res) => {
  const r = await pool.query(
    `SELECT MIN(o.id::text) AS id, o.title, o.retailer, MIN(o.category_id) AS category_id
     FROM offers o JOIN campaigns cp ON cp.id = o.campaign_id
     WHERE cp.active AND CURRENT_DATE BETWEEN cp.starts_at AND cp.ends_at
     GROUP BY o.title, o.retailer ORDER BY o.title`
  );
  res.json(r.rows);
});

/** GET /offers/credits — how many coupon credits the user can still claim (1 per walk) */
offersRouter.get('/credits', requireAuth, async (req: any, res) => {
  const r = await pool.query(
    `SELECT GREATEST(0, (SELECT COUNT(*) FROM donations WHERE user_id=$1)
                      - (SELECT COUNT(*) FROM coupons   WHERE user_id=$1))::int AS credits`,
    [req.user.id]
  );
  res.json({ credits: r.rows[0].credits });
});

/** POST /offers/choose  { offerId } — spend a credit to add a vendor coupon to the wallet (7-day) */
offersRouter.post('/choose', requireAuth, async (req: any, res) => {
  const { offerId } = req.body;
  const userId = req.user.id;
  const client = await pool.connect();
  try {
    await client.query('BEGIN');
    const c = await client.query(
      `SELECT (SELECT COUNT(*) FROM donations WHERE user_id=$1)
            - (SELECT COUNT(*) FROM coupons   WHERE user_id=$1) AS credits`, [userId]);
    if ((c.rows[0].credits | 0) <= 0) { await client.query('ROLLBACK'); return res.status(400).json({ error: 'no_credit' }); }
    const o = await client.query(`SELECT id, title, retailer FROM offers WHERE id = $1`, [offerId]);
    if (!o.rows.length) { await client.query('ROLLBACK'); return res.status(404).json({ error: 'offer_not_found' }); }
    const code = 'CYA-' + Math.random().toString(36).slice(2, 10).toUpperCase();
    const ins = await client.query(
      `INSERT INTO coupons (offer_id, user_id, code, expires_at)
       VALUES ($1,$2,$3, now() + interval '7 days') RETURNING code, expires_at`,
      [offerId, userId, code]);
    await client.query('COMMIT');
    res.json({ chosen: true, coupon: { code: ins.rows[0].code, expiresAt: ins.rows[0].expires_at, title: o.rows[0].title, retailer: o.rows[0].retailer } });
  } catch (e: any) {
    await client.query('ROLLBACK');
    res.status(500).json({ error: 'internal' });
  } finally { client.release(); }
});

/**
 * POST /offers/activate  { code }
 * Starts the in-store redemption window. The 5-minute window is stamped ONCE
 * (activated_at) and measured from that moment — re-opening the screen returns the
 * REMAINING time, it never resets, so the window can't be gamed by closing/reopening.
 * When the window elapses the coupon is finalized as used (single-use), counted as
 * redeemed on the dashboard, and reconciled later with the retailer's offline report.
 */
offersRouter.post('/activate', requireAuth, async (req: any, res) => {
  const { code } = req.body;
  const c = await pool.query(
    `SELECT c.id, c.activated_at, c.redeemed_at, c.expires_at,
            o.title, o.retailer, o.retailer_code
     FROM coupons c JOIN offers o ON o.id = c.offer_id
     WHERE c.code = $1 AND c.user_id = $2`,
    [code, req.user.id]
  );
  if (!c.rows.length) return res.status(404).json({ error: 'not_found' });
  const row = c.rows[0];
  if (row.redeemed_at) return res.json({ activated: false, expired: true, reason: 'used', offer: { title: row.title, retailer: row.retailer } });
  if (new Date(row.expires_at) <= new Date()) return res.json({ activated: false, expired: true, reason: 'coupon_expired', offer: { title: row.title, retailer: row.retailer } });

  // Stamp activated_at the first time only; the window is measured from it and never resets.
  let activatedAt = row.activated_at;
  if (!activatedAt) {
    const u = await pool.query(
      `UPDATE coupons SET activated_at = now(),
         redeemed_store = COALESCE(redeemed_store, $2 || ' (activación app)')
       WHERE id = $1 RETURNING activated_at`,
      [row.id, row.retailer]
    );
    activatedAt = u.rows[0].activated_at;
  }
  const windowEnd = new Date(new Date(activatedAt).getTime() + ACTIVATION_WINDOW_SEC * 1000);
  const remainingSec = Math.round((windowEnd.getTime() - Date.now()) / 1000);
  if (remainingSec <= 0) {
    // Window finished → finalize as used (assume redeemed in store).
    await pool.query(`UPDATE coupons SET redeemed_at = $2 WHERE id = $1 AND redeemed_at IS NULL`,
      [row.id, windowEnd.toISOString()]);
    return res.json({ activated: false, expired: true, reason: 'window_over', offer: { title: row.title, retailer: row.retailer } });
  }
  res.json({
    activated: true,
    windowSec: remainingSec,               // REMAINING seconds — resumes, never resets to 300
    expiresAt: windowEnd.toISOString(),
    offer: { title: row.title, retailer: row.retailer },
    // What the cashier scans: the retailer's own POS code when provided, else our code.
    barcodeValue: row.retailer_code || code,
  });
});

/**
 * POST /offers/test  { code }
 * Sponsor validation (dry-run): confirms a code is live and redeemable in our system
 * WITHOUT consuming it. Once retailer POS integration exists this also pings the
 * retailer's system. For now it validates existence + not-used + not-expired.
 */
offersRouter.post('/test', async (req, res) => {
  const { code } = req.body;
  const r = await pool.query(
    `SELECT c.redeemed_at, c.expires_at, o.title, o.retailer
     FROM coupons c JOIN offers o ON o.id = c.offer_id WHERE c.code = $1`, [code]);
  if (!r.rows.length) return res.json({ valid: false, reason: 'no_existe', message: 'El código no existe en el sistema.' });
  const row = r.rows[0];
  if (row.redeemed_at) return res.json({ valid: false, reason: 'usado', message: 'El código ya fue usado.' });
  if (new Date(row.expires_at) <= new Date()) return res.json({ valid: false, reason: 'vencido', message: 'El código está vencido.' });
  res.json({ valid: true, message: 'Código activo y listo para que un usuario lo redima.', offer: { title: row.title, retailer: row.retailer } });
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
