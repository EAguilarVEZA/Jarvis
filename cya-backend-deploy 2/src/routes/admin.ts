import { Router } from 'express';
import bcrypt from 'bcryptjs';
import jwt from 'jsonwebtoken';
import { pool } from '../db';

// Sponsor self-service / admin setup + reporting.
export const adminRouter = Router();
const SECRET = process.env.JWT_SECRET ?? 'dev-secret-change-me';

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
adminRouter.post('/foodbanks', async (req, res) => {
  const { name, city, abacoMember = true } = req.body;
  if (!name || !city) return res.status(400).json({ error: 'name_and_city_required' });
  const r = await pool.query(
    `INSERT INTO food_banks (name, city, abaco_member) VALUES ($1,$2,$3) RETURNING id, name, city`,
    [name, city, !!abacoMember]);
  res.json(r.rows[0]);
});

/** POST /admin/sponsors  { name, contactEmail } */
adminRouter.post('/sponsors', async (req, res) => {
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
adminRouter.post('/campaigns', async (req, res) => {
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
adminRouter.post('/offers', async (req, res) => {
  const { campaignId, title, retailer, categoryId = 'comida_saludable', validDays = 7, generateCount = 0 } = req.body;
  if (!title || !retailer) return res.status(400).json({ error: 'title_and_retailer_required' });
  const o = await pool.query(
    `INSERT INTO offers (campaign_id, title, retailer, category_id, valid_days)
     VALUES ($1,$2,$3,$4,$5) RETURNING id, title, retailer, category_id, valid_days`,
    [campaignId, title, retailer, categoryId, validDays]);
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
  const c = camp.rows[0];
  const issued = cp.rows[0].issued, redeemed = cp.rows[0].redeemed;
  res.json({
    campaign: { mealsBudget: c.meals_budget, mealsDonated: c.meals_donated, costPerMealCop: c.cost_per_meal_cop },
    meals: { donated: c.meals_donated, budget: c.meals_budget,
             investmentCop: c.meals_donated * c.cost_per_meal_cop },
    coupons: { issued, redeemed, redemptionRate: issued ? Math.round(redeemed/issued*100) : 0 },
    byCategory: byCat.rows,
  });
});
