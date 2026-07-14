import { Router } from 'express';
import bcrypt from 'bcryptjs';
import jwt from 'jsonwebtoken';
import { pool } from '../db';

export const authRouter = Router();
const SECRET = process.env.JWT_SECRET ?? 'dev-secret-change-me';

authRouter.post('/register', async (req, res) => {
  const { email, password, displayName, consentHealthData,
          // optional demographics (only stored with consent) — used for AGGREGATE reporting only
          birthYear, sex, city, postalCode, occupation, consentDemographics } = req.body;
  if (!consentHealthData) return res.status(400).json({ error: 'health_data_consent_required' }); // Ley 1581
  const hash = await bcrypt.hash(password, 10);
  const demo = consentDemographics ? {
    birthYear: birthYear ?? null,
    sex: ['F','M','O','NA'].includes(sex) ? sex : null,
    city: city ?? null, postalCode: postalCode ?? null, occupation: occupation ?? null,
  } : { birthYear:null, sex:null, city:null, postalCode:null, occupation:null };
  const r = await pool.query(
    `INSERT INTO users (email, password_hash, display_name, consent_health_data,
                        birth_year, sex, city, postal_code, occupation, consent_demographics)
     VALUES ($1,$2,$3,TRUE,$4,$5,$6,$7,$8,$9) RETURNING id, email, display_name`,
    [email.toLowerCase(), hash, displayName,
     demo.birthYear, demo.sex, demo.city, demo.postalCode, demo.occupation, !!consentDemographics]
  );
  res.json({ token: jwt.sign({ id: r.rows[0].id }, SECRET), user: r.rows[0] });
});

authRouter.post('/login', async (req, res) => {
  const { email, password } = req.body;
  const r = await pool.query(`SELECT * FROM users WHERE email = $1`, [email.toLowerCase()]);
  if (!r.rows.length || !(await bcrypt.compare(password, r.rows[0].password_hash)))
    return res.status(401).json({ error: 'invalid_credentials' });
  res.json({ token: jwt.sign({ id: r.rows[0].id }, SECRET) });
});

export function requireAuth(req: any, res: any, next: any) {
  const token = req.headers.authorization?.replace('Bearer ', '');
  try { req.user = jwt.verify(token, SECRET); next(); }
  catch { res.status(401).json({ error: 'unauthorized' }); }
}

/** GET /auth/me — the signed-in user's profile (for showing/editing in the app) */
authRouter.get('/me', requireAuth, async (req: any, res) => {
  const r = await pool.query(
    `SELECT display_name, birth_year, sex, city, occupation FROM users WHERE id = $1`, [req.user.id]);
  res.json(r.rows[0] || {});
});

/** PATCH /auth/profile — update the user's profile fields (name, age, city, occupation) */
authRouter.patch('/profile', requireAuth, async (req: any, res) => {
  const { displayName, birthYear, sex, city, occupation } = req.body;
  await pool.query(
    `UPDATE users SET
       display_name = COALESCE($1, display_name),
       birth_year   = COALESCE($2, birth_year),
       sex          = COALESCE($3, sex),
       city         = COALESCE($4, city),
       occupation   = COALESCE($5, occupation),
       consent_demographics = TRUE
     WHERE id = $6`,
    [displayName ?? null, birthYear ?? null,
     (['F','M','O','NA'].includes(sex) ? sex : null), city ?? null, occupation ?? null, req.user.id]);
  res.json({ ok: true });
});
