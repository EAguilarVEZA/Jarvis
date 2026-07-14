import { Router } from 'express';
import { pool } from '../db';
import { requireAuth } from './auth';

export const parchesRouter = Router();

function code(){ return Math.random().toString(36).slice(2,7).toUpperCase(); }

/** POST /parches  { name } — create a parche and join it */
parchesRouter.post('/', requireAuth, async (req: any, res) => {
  const { name } = req.body;
  const c = await pool.query(
    `INSERT INTO parches (name, join_code, created_by) VALUES ($1,$2,$3) RETURNING id, join_code`,
    [name || 'Mi Parche', code(), req.user.id]);
  await pool.query(`INSERT INTO parche_members (parche_id, user_id) VALUES ($1,$2)`, [c.rows[0].id, req.user.id]);
  res.json({ parcheId: c.rows[0].id, joinCode: c.rows[0].join_code });
});

/** POST /parches/join  { joinCode } — join via the invite link */
parchesRouter.post('/join', requireAuth, async (req: any, res) => {
  const p = await pool.query(`SELECT id FROM parches WHERE join_code = $1`, [req.body.joinCode]);
  if (!p.rows.length) return res.status(404).json({ error: 'parche_not_found' });
  await pool.query(
    `INSERT INTO parche_members (parche_id, user_id) VALUES ($1,$2) ON CONFLICT DO NOTHING`,
    [p.rows[0].id, req.user.id]);
  res.json({ ok: true, parcheId: p.rows[0].id });
});

/**
 * GET /parches/:id — the group view: members with lifetime meals (leaderboard),
 * the shared total, the goal, and who is walking right now (live presence).
 */
parchesRouter.get('/:id', requireAuth, async (req: any, res) => {
  const head = await pool.query(`SELECT name, join_code FROM parches WHERE id = $1`, [req.params.id]);
  if (!head.rows.length) return res.status(404).json({ error: 'parche_not_found' });
  const members = await pool.query(
    `SELECT u.display_name AS name, m.walking_now,
            (SELECT COUNT(*) FROM donations d WHERE d.user_id = m.user_id) AS meals
     FROM parche_members m JOIN users u ON u.id = m.user_id
     WHERE m.parche_id = $1
     ORDER BY meals DESC`, [req.params.id]);
  const total = members.rows.reduce((a: number, m: any) => a + Number(m.meals), 0);
  res.json({
    name: head.rows[0].name,
    joinCode: head.rows[0].join_code,
    members: members.rows,
    totalMeals: total,
    goal: members.rows.length * 7,
    walkingNow: members.rows.filter((m: any) => m.walking_now).map((m: any) => m.name),
  });
});

/** POST /parches/:id/presence  { walking } — set live "caminando ahora" (only true during a session) */
parchesRouter.post('/:id/presence', requireAuth, async (req: any, res) => {
  await pool.query(
    `UPDATE parche_members SET walking_now = $1 WHERE parche_id = $2 AND user_id = $3`,
    [!!req.body.walking, req.params.id, req.user.id]);
  res.json({ ok: true });
});
