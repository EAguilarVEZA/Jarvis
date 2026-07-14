import { Router } from 'express';
import bcrypt from 'bcryptjs';
import jwt from 'jsonwebtoken';
import { pool } from '../db';

// Sponsor self-service / admin setup + reporting.
export const adminRouter = Router();
const SECRET = process.env.JWT_SECRET ?? 'dev-secret-change-me';

// Require a valid sponsor token (from /admin/login) for any route that creates
// or changes data. Reporting GETs and login/register stay open.
function requireSponsor(req: any, res: any, next: any) {
  const token = (req.headers.authorization || '').replace('Bearer ', '');
  try { req.sponsor = jwt.verify(token, SECRET); next(); }
  catch { res.status(401).json({ error: 'unauthorized' }); }
}

/** POST /admin/register  { name, email, password } — create a sponsor account */
adminRouter.post('/register', async (req, res) => {
  const { name, email, password } = req.body;
  if (!name || !email || !password) return res.status(400).json({ error: 'name_email_password_required' });
  const hash = await bcrypt.hash(password, 10);
  try {
    const r = await pool.query(
      `INSERT INTO sponsors (name, contact_email, password_hash) VALUES ($1,$2,$3) RETURNING id, name`,
      [name, email.toLowerCase(), hash]);
    res.json({ token: jwt.sign({ sponsorId: r.rows[0].id }, SECRET), sponsor: r.rows[0] });
  } catch (e: any) {
    res.status(500).json({ error: 'internal', detail: String(e.message || e) });
  }
});

/** POST /admin/login  { email, password } */
adminRouter.post('/login', async (req, res) => {
  const { email, password } = req.body;
  const r = await pool.query(`SELECT id, name, password_hash FROM sponsors WHERE contact_email = $1`, [String(email||'').toLowerCase()]);
  if (!r.rows.length || !r.rows[0].password_hash || !(await bcrypt.compare(password, r.rows[0].password_hash)))
    return res.status(401).json({ error: 'invalid_credentials' });
  res.json({ token: jwt.sign({ sponsorId: r.rows[0].id }, SECRET), sponsor: { id: r.rows[0].id, name: r.rows[0].name } });
});

/** GET /admin/foodbanks — list food banks (for setup + campaign dropdowns) */
adminRouter.get('/foodbanks', async (_req, res) => {
  const r = await pool.query(`SELECT id, name, city, abaco_member FROM food_banks ORDER BY name`);
  res.json(r.rows);
});

/** POST /admin/foodbanks  { name, city, abacoMember } — register a food bank */
adminRouter.post('/foodbanks', requireSponsor, async (req, res) => {
  const { name, city, abacoMember = true } = req.body;
  if (!name || !city) return res.status(400).json({ error: 'name_and_city_required' });
  const r = await pool.query(
    `INSERT INTO food_banks (name, city, abaco_member) VALUES ($1,$2,$3) RETURNING id, name, city`,
    [name, city, !!abacoMember]);
  res.json(r.rows[0]);
});

/** PATCH /admin/foodbanks/:id  { name?, city?, abacoMember? } */
adminRouter.patch('/foodbanks/:id', requireSponsor, async (req, res) => {
  const { name, city, abacoMember } = req.body;
  await pool.query(`UPDATE food_banks SET name=COALESCE($1,name), city=COALESCE($2,city), abaco_member=COALESCE($3,abaco_member) WHERE id=$4`,
    [name ?? null, city ?? null, (abacoMember === undefined ? null : !!abacoMember), req.params.id]);
  res.json({ ok: true });
});

/** DELETE /admin/foodbanks/:id */
adminRouter.delete('/foodbanks/:id', requireSponsor, async (req, res) => {
  try { await pool.query(`DELETE FROM food_banks WHERE id=$1`, [req.params.id]); res.json({ deleted: true }); }
  catch (e: any) {
    if (e.code === '23503') return res.status(409).json({ error: 'in_use', message: 'Este banco tiene campañas asociadas. Elimina esas campañas primero.' });
    res.status(500).json({ error: 'internal' });
  }
});

/** POST /admin/sponsors  { name, contactEmail } */
adminRouter.post('/sponsors', requireSponsor, async (req, res) => {
  const { name, contactEmail } = req.body;
  if (!name) return res.status(400).json({ error: 'name_required' });
  const r = await pool.query(
    `INSERT INTO sponsors (name, contact_email) VALUES ($1,$2) RETURNING id, name`,
    [name, contactEmail || null]);
  res.json(r.rows[0]);
});

/**
 * POST /admin/campaigns
 * { sponsorId, foodBankId, mealsBudget, costPerMealCop, minMealsFunded?, campaignType?, startsAt, endsAt }
 * The golden rule (min_meals_funded > 0) is enforced by the DB on every campaign.
 */
adminRouter.post('/campaigns', requireSponsor, async (req, res) => {
  const { sponsorId, foodBankId, mealsBudget, costPerMealCop,
          minMealsFunded = 1, campaignType = 'donacion', startsAt, endsAt } = req.body;
  try {
    const r = await pool.query(
      `INSERT INTO campaigns
         (sponsor_id, food_bank_id, campaign_type, meals_budget, min_meals_funded,
          cost_per_meal_cop, starts_at, ends_at)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id, meals_budget`,
      [sponsorId, foodBankId, campaignType, mealsBudget, minMealsFunded, costPerMealCop, startsAt, endsAt]);
    res.json(r.rows[0]);
  } catch (e: any) {
    if (e.code === '23514') return res.status(400).json({ error: 'golden_rule', message: 'min_meals_funded must be > 0' });
    res.status(500).json({ error: 'internal', detail: String(e.message || e) });
  }
});

/**
 * POST /admin/offers
 * { campaignId, title, retailer, categoryId, validDays, generateCount? }
 * Creates the offer and (optionally) pre-generates single-use coupon codes for
 * printing as barcodes. Returns the offer + any generated codes.
 */
adminRouter.post('/offers', requireSponsor, async (req, res) => {
  const { campaignId, title, retailer, categoryId = 'comida_saludable', validDays = 7, generateCount = 0, retailerCode = null } = req.body;
  if (!title || !retailer) return res.status(400).json({ error: 'title_and_retailer_required' });
  const o = await pool.query(
    `INSERT INTO offers (campaign_id, title, retailer, category_id, valid_days, retailer_code)
     VALUES ($1,$2,$3,$4,$5,$6) RETURNING id, title, retailer, category_id, valid_days, retailer_code`,
    [campaignId, title, retailer, categoryId, validDays, retailerCode || null]);
  const offer = o.rows[0];

  const codes: string[] = [];
  for (let i = 0; i < Math.min(generateCount, 1000); i++) {
    const code = 'CYA-' + Math.random().toString(36).slice(2, 10).toUpperCase();
    codes.push(code);
    // pre-issued, unassigned coupons (user_id null) — claimed/assigned at redemption time
    await pool.query(
      `INSERT INTO coupons (offer_id, user_id, code, expires_at)
       VALUES ($1, NULL, $2, now() + ($3 || ' days')::interval)
       ON CONFLICT DO NOTHING`,
      [offer.id, code, validDays]
    ).catch(() => {});
  }
  res.json({ offer, generatedCodes: codes });
});

/* ===================== MANAGE: create / edit / delete ===================== */

/** GET /admin/sponsors — list all sponsors */
adminRouter.get('/sponsors', async (_req, res) => {
  const r = await pool.query(`SELECT id, name, contact_email FROM sponsors ORDER BY name`);
  res.json(r.rows);
});

/** GET /admin/tree — sponsors → campaigns(promotions) → offers(premios), for the manage UI */
adminRouter.get('/tree', async (_req, res) => {
  const sponsors = await pool.query(`SELECT id, name, contact_email FROM sponsors ORDER BY name`);
  const campaigns = await pool.query(
    `SELECT id, sponsor_id, food_bank_id, campaign_type, meals_budget, meals_donated,
            cost_per_meal_cop, starts_at, ends_at, active FROM campaigns ORDER BY starts_at DESC`);
  const offers = await pool.query(
    `SELECT o.id, o.campaign_id, o.title, o.retailer, o.category_id, o.retailer_code,
            (SELECT COUNT(*) FROM coupons c WHERE c.offer_id = o.id)::int AS coupons,
            (SELECT COUNT(*) FROM coupons c WHERE c.offer_id = o.id AND c.redeemed_at IS NOT NULL)::int AS redeemed
     FROM offers o ORDER BY o.title`);
  const offByCamp: any = {};
  offers.rows.forEach((o: any) => { (offByCamp[o.campaign_id] = offByCamp[o.campaign_id] || []).push(o); });
  const campBySponsor: any = {};
  campaigns.rows.forEach((c: any) => { c.offers = offByCamp[c.id] || []; (campBySponsor[c.sponsor_id] = campBySponsor[c.sponsor_id] || []).push(c); });
  res.json(sponsors.rows.map((s: any) => ({ ...s, campaigns: campBySponsor[s.id] || [] })));
});

/** PATCH /admin/sponsors/:id  { name?, contactEmail? } */
adminRouter.patch('/sponsors/:id', requireSponsor, async (req, res) => {
  const { name, contactEmail } = req.body;
  await pool.query(`UPDATE sponsors SET name = COALESCE($1,name), contact_email = COALESCE($2,contact_email) WHERE id = $3`,
    [name ?? null, contactEmail ?? null, req.params.id]);
  res.json({ ok: true });
});

/** DELETE /admin/sponsors/:id — removes the sponsor + all its campaigns/offers/coupons */
adminRouter.delete('/sponsors/:id', requireSponsor, async (req, res) => {
  const id = req.params.id; const client = await pool.connect();
  try {
    await client.query('BEGIN');
    await client.query(`DELETE FROM coupons WHERE offer_id IN (SELECT o.id FROM offers o JOIN campaigns cp ON cp.id=o.campaign_id WHERE cp.sponsor_id=$1)`, [id]);
    await client.query(`DELETE FROM offers WHERE campaign_id IN (SELECT id FROM campaigns WHERE sponsor_id=$1)`, [id]);
    await client.query(`DELETE FROM campaigns WHERE sponsor_id=$1`, [id]);
    await client.query(`DELETE FROM sponsors WHERE id=$1`, [id]);
    await client.query('COMMIT'); res.json({ deleted: true });
  } catch (e: any) { await client.query('ROLLBACK'); res.status(500).json({ error: 'internal', detail: String(e.message || e) }); }
  finally { client.release(); }
});

/** PATCH /admin/campaigns/:id  { mealsBudget?, costPerMealCop?, startsAt?, endsAt?, active? } */
adminRouter.patch('/campaigns/:id', requireSponsor, async (req, res) => {
  const { mealsBudget, costPerMealCop, startsAt, endsAt, active } = req.body;
  await pool.query(
    `UPDATE campaigns SET meals_budget=COALESCE($1,meals_budget), cost_per_meal_cop=COALESCE($2,cost_per_meal_cop),
       starts_at=COALESCE($3,starts_at), ends_at=COALESCE($4,ends_at), active=COALESCE($5,active) WHERE id=$6`,
    [mealsBudget ?? null, costPerMealCop ?? null, startsAt ?? null, endsAt ?? null, (active === undefined ? null : active), req.params.id]);
  res.json({ ok: true });
});

/** DELETE /admin/campaigns/:id — removes the promotion + its offers/coupons */
adminRouter.delete('/campaigns/:id', requireSponsor, async (req, res) => {
  const id = req.params.id; const client = await pool.connect();
  try {
    await client.query('BEGIN');
    await client.query(`DELETE FROM coupons WHERE offer_id IN (SELECT id FROM offers WHERE campaign_id=$1)`, [id]);
    await client.query(`DELETE FROM offers WHERE campaign_id=$1`, [id]);
    await client.query(`DELETE FROM campaigns WHERE id=$1`, [id]);
    await client.query('COMMIT'); res.json({ deleted: true });
  } catch (e: any) { await client.query('ROLLBACK'); res.status(500).json({ error: 'internal', detail: String(e.message || e) }); }
  finally { client.release(); }
});

/** PATCH /admin/offers/:id  { title?, retailer?, categoryId?, retailerCode? } */
adminRouter.patch('/offers/:id', requireSponsor, async (req, res) => {
  const { title, retailer, categoryId, retailerCode } = req.body;
  await pool.query(
    `UPDATE offers SET title=COALESCE($1,title), retailer=COALESCE($2,retailer),
       category_id=COALESCE($3,category_id), retailer_code=COALESCE($4,retailer_code) WHERE id=$5`,
    [title ?? null, retailer ?? null, categoryId ?? null, retailerCode ?? null, req.params.id]);
  res.json({ ok: true });
});

/** DELETE /admin/offers/:id — removes the premio + its coupon codes */
adminRouter.delete('/offers/:id', requireSponsor, async (req, res) => {
  const id = req.params.id; const client = await pool.connect();
  try {
    await client.query('BEGIN');
    await client.query(`DELETE FROM coupons WHERE offer_id=$1`, [id]);
    await client.query(`DELETE FROM offers WHERE id=$1`, [id]);
    await client.query('COMMIT'); res.json({ deleted: true });
  } catch (e: any) { await client.query('ROLLBACK'); res.status(500).json({ error: 'internal', detail: String(e.message || e) }); }
  finally { client.release(); }
});

/** DELETE /admin/coupons/:code — delete one coupon code */
adminRouter.delete('/coupons/:code', requireSponsor, async (req, res) => {
  await pool.query(`DELETE FROM coupons WHERE code = $1`, [req.params.code]);
  res.json({ deleted: true });
});

/** GET /admin/overview — quick counts for the setup portal */
adminRouter.get('/overview', async (_req, res) => {
  const r = await pool.query(`
    SELECT (SELECT COUNT(*) FROM sponsors)  AS sponsors,
           (SELECT COUNT(*) FROM campaigns) AS campaigns,
           (SELECT COUNT(*) FROM offers)    AS offers,
           (SELECT COUNT(*) FROM coupons)   AS coupons`);
  res.json(r.rows[0]);
});

/** GET /admin/sponsors/:id/campaigns — list a sponsor's campaigns with progress */
adminRouter.get('/sponsors/:id/campaigns', async (req, res) => {
  const r = await pool.query(
    `SELECT id, campaign_type, meals_budget, meals_donated, cost_per_meal_cop, starts_at, ends_at, active
     FROM campaigns WHERE sponsor_id = $1 ORDER BY starts_at DESC`, [req.params.id]);
  res.json(r.rows);
});

/** GET /admin/demographics — AGGREGATE only (age groups, sex, top cities). Never individual. */
adminRouter.get('/demographics', async (_req, res) => {
  const byAge = await pool.query(`SELECT age_group, users, meals FROM demographics_by_age ORDER BY age_group`);
  const bySex = await pool.query(
    `SELECT COALESCE(sex,'NA') AS sex, COUNT(*)::int AS users FROM users GROUP BY sex`);
  const byCity = await pool.query(
    `SELECT COALESCE(city,'(sin dato)') AS city, COUNT(*)::int AS users FROM users GROUP BY city ORDER BY users DESC LIMIT 10`);
  const byJob = await pool.query(
    `SELECT COALESCE(occupation,'(sin dato)') AS occupation, COUNT(*)::int AS users FROM users GROUP BY occupation ORDER BY users DESC LIMIT 10`);
  res.json({ byAge: byAge.rows, bySex: bySex.rows, byCity: byCity.rows, byOccupation: byJob.rows });
});

/* ============== RETO DEL MES: top walkers + reward push ============== */

/**
 * GET /admin/top-walkers?limit=10[&month=YYYY-MM]
 * The "usuarios del mes" leaderboard: top walkers by steps in a calendar month.
 * Used by the sponsor to see who to reward. Aggregate, ranked, configurable N.
 */
adminRouter.get('/top-walkers', async (req: any, res) => {
  const limit = Math.min(Math.max(parseInt(req.query.limit, 10) || 10, 1), 100);
  const month = /^\d{4}-\d{2}$/.test(req.query.month || '') ? req.query.month + '-01' : null;
  const r = await pool.query(
    `SELECT u.id, u.display_name AS name,
            COALESCE(NULLIF(TRIM(u.city),''),'') AS city,
            COALESCE(SUM(a.steps),0)::int AS steps,
            COUNT(DISTINCT a.activity_date)::int AS days,
            (SELECT COUNT(*) FROM donations d WHERE d.user_id = u.id
               AND date_trunc('month', d.created_at) = date_trunc('month', COALESCE($2::date, CURRENT_DATE)))::int AS meals
     FROM users u JOIN activities a ON a.user_id = u.id
     WHERE date_trunc('month', a.activity_date) = date_trunc('month', COALESCE($2::date, CURRENT_DATE))
     GROUP BY u.id, u.display_name, u.city
     ORDER BY steps DESC LIMIT $1`, [limit, month]);
  res.json(r.rows.map((x: any, i: number) => ({ ...x, rank: i + 1 })));
});

/**
 * POST /admin/reward-top
 * { campaignId, limit, title, retailer, categoryId?, validDays?, message?, month? }
 * Sends a special "thank-you" reward coupon to the top N walkers of the month.
 * Creates one special offer and assigns a single-use coupon (with a personal
 * reward_message) to each top user — it lands directly in their wallet.
 */
adminRouter.post('/reward-top', requireSponsor, async (req, res) => {
  const { campaignId, limit = 10, title, retailer,
          categoryId = 'bienestar', validDays = 21, message, month } = req.body;
  if (!campaignId || !title || !retailer)
    return res.status(400).json({ error: 'campaignId_title_retailer_required' });
  const n = Math.min(Math.max(parseInt(limit, 10) || 10, 1), 100);
  const mDate = /^\d{4}-\d{2}$/.test(month || '') ? month + '-01' : null;
  const top = await pool.query(
    `SELECT u.id FROM users u JOIN activities a ON a.user_id = u.id
     WHERE date_trunc('month', a.activity_date) = date_trunc('month', COALESCE($2::date, CURRENT_DATE))
     GROUP BY u.id ORDER BY SUM(a.steps) DESC LIMIT $1`, [n, mDate]);
  if (!top.rows.length) return res.json({ rewarded: 0, recipients: [] });
  const o = await pool.query(
    `INSERT INTO offers (campaign_id, title, retailer, category_id, valid_days)
     VALUES ($1,$2,$3,$4,$5) RETURNING id`,
    [campaignId, title, retailer, categoryId, validDays]);
  const offerId = o.rows[0].id;
  const msg = (message && String(message).slice(0, 240)) ||
    '🎁 ¡Eres usuario del mes! Gracias por caminar tanto y alimentar a más niños.';
  const recipients: any[] = [];
  for (const u of top.rows) {
    const code = 'CYA-' + Math.random().toString(36).slice(2, 10).toUpperCase();
    await pool.query(
      `INSERT INTO coupons (offer_id, user_id, code, expires_at, reward_message)
       VALUES ($1,$2,$3, now() + ($4 || ' days')::interval, $5) ON CONFLICT DO NOTHING`,
      [offerId, u.id, code, validDays, msg]).catch(() => {});
    recipients.push(u.id);
  }
  res.json({ rewarded: recipients.length, offerId, recipients });
});

/** GET /admin/campaigns/:id/report — the reporting dashboard data for one campaign */
adminRouter.get('/campaigns/:id/report', async (req, res) => {
  const id = req.params.id;
  const camp = await pool.query(`SELECT * FROM campaigns WHERE id = $1`, [id]);
  if (!camp.rows.length) return res.status(404).json({ error: 'campaign_not_found' });
  const cp = await pool.query(
    `SELECT COUNT(*)::int AS issued,
            COUNT(*) FILTER (WHERE c.redeemed_at IS NOT NULL)::int AS redeemed
     FROM coupons c JOIN offers o ON o.id = c.offer_id WHERE o.campaign_id = $1`, [id]);
  const byCat = await pool.query(
    `SELECT o.category_id AS category, COUNT(c.id)::int AS coupons,
            COUNT(c.id) FILTER (WHERE c.redeemed_at IS NOT NULL)::int AS redeemed
     FROM offers o LEFT JOIN coupons c ON c.offer_id = o.id
     WHERE o.campaign_id = $1 GROUP BY o.category_id ORDER BY coupons DESC`, [id]);
  // Best stores by verified redemptions
  const byStore = await pool.query(
    `SELECT COALESCE(c.redeemed_store,'(sin dato)') AS store, COUNT(*)::int AS redeemed
     FROM coupons c JOIN offers o ON o.id = c.offer_id
     WHERE o.campaign_id = $1 AND c.redeemed_at IS NOT NULL
     GROUP BY c.redeemed_store ORDER BY redeemed DESC LIMIT 8`, [id]);
  // Best offers by issued + redemption rate
  const byOffer = await pool.query(
    `SELECT o.title, o.retailer,
            COUNT(c.id)::int AS issued,
            COUNT(c.id) FILTER (WHERE c.redeemed_at IS NOT NULL)::int AS redeemed
     FROM offers o LEFT JOIN coupons c ON c.offer_id = o.id
     WHERE o.campaign_id = $1 GROUP BY o.id, o.title, o.retailer
     HAVING COUNT(c.id) > 0
     ORDER BY issued DESC LIMIT 8`, [id]);
  const c = camp.rows[0];
  const issued = cp.rows[0].issued, redeemed = cp.rows[0].redeemed;
  // Traceability: which food bank delivered this campaign's meals.
  const fb = await pool.query(`SELECT name, city, abaco_member FROM food_banks WHERE id = $1`, [c.food_bank_id]);
  const sp = await pool.query(`SELECT name FROM sponsors WHERE id = $1`, [c.sponsor_id]);
  res.json({
    campaign: { mealsBudget: c.meals_budget, mealsDonated: c.meals_donated, costPerMealCop: c.cost_per_meal_cop },
    sponsor: sp.rows[0] ? sp.rows[0].name : null,
    foodBank: fb.rows[0] ? { name: fb.rows[0].name, city: fb.rows[0].city, abaco: fb.rows[0].abaco_member } : null,
    meals: { donated: c.meals_donated, budget: c.meals_budget,
             investmentCop: c.meals_donated * c.cost_per_meal_cop },
    coupons: { issued, redeemed, redemptionRate: issued ? Math.round(redeemed/issued*100) : 0 },
    byCategory: byCat.rows,
    byStore: byStore.rows,
    byOffer: byOffer.rows,
  });
});
