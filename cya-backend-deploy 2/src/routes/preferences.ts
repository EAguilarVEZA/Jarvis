import { Router } from 'express';
import { pool } from '../db';
import { requireAuth } from './auth';

export const preferencesRouter = Router();

/** GET /preferences/categories — all reward categories the user can choose from */
preferencesRouter.get('/categories', async (_req, res) => {
  const r = await pool.query(`SELECT id, label FROM reward_categories ORDER BY label`);
  res.json(r.rows);
});

/** GET /preferences/me — the categories this user has opted into */
preferencesRouter.get('/me', requireAuth, async (req: any, res) => {
  const r = await pool.query(
    `SELECT category_id FROM user_preferences WHERE user_id = $1`, [req.user.id]);
  res.json(r.rows.map((x: any) => x.category_id));
});

/** PUT /preferences/me  { categories: ['comida_saludable','conciertos'] } — replace the set */
preferencesRouter.put('/me', requireAuth, async (req: any, res) => {
  const { categories } = req.body as { categories: string[] };
  if (!Array.isArray(categories)) return res.status(400).json({ error: 'categories_array_required' });
  const client = await pool.connect();
  try {
    await client.query('BEGIN');
    await client.query(`DELETE FROM user_preferences WHERE user_id = $1`, [req.user.id]);
    for (const c of categories) {
      await client.query(
        `INSERT INTO user_preferences (user_id, category_id) VALUES ($1, $2)
         ON CONFLICT DO NOTHING`, [req.user.id, c]);
    }
    await client.query('COMMIT');
    res.json({ ok: true, categories });
  } catch (e) {
    await client.query('ROLLBACK');
    res.status(500).json({ error: 'internal' });
  } finally {
    client.release();
  }
});
