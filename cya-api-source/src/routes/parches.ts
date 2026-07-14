import { Router } from 'express';
import { pool } from '../db';
import { requireAuth } from './auth';

export const parchesRouter = Router();

function code(){ return Math.random().toString(36).slice(2,7).toUpperCase(); }

/** POST /parches  { name, myName? } — create a parche and join it */
parchesRouter.post('/', requireAuth, async (req: any, res) => {
  const { name, myName } = req.body;
  if (myName) await pool.query(`UPDATE users SET display_name = $1 WHERE id = $2`, [String(myName).slice(0, 24), req.user.id]);
  const c = await pool.query(
    `INSERT INTO parches (name, join_code, created_by) VALUES ($1,$2,$3) RETURNING id, join_code`,
    [name || 'Mi Parche', code(), req.user.id]);
  await pool.query(`INSERT INTO parche_members (parche_id, user_id) VALUES ($1,$2)`, [c.rows[0].id, req.user.id]);
  res.json({ parcheId: c.rows[0].id, joinCode: c.rows[0].join_code });
});

/** POST /parches/join  { joinCode, myName? } — join via the invite code/link */
parchesRouter.post('/join', requireAuth, async (req: any, res) => {
  const { joinCode, myName } = req.body;
  if (myName) await pool.query(`UPDATE users SET display_name = $1 WHERE id = $2`, [String(myName).slice(0, 24), req.user.id]);
  const p = await pool.query(`SELECT id FROM parches WHERE join_code = $1`, [String(joinCode || '').toUpperCase()]);
  if (!p.rows.length) return res.status(404).json({ error: 'parche_not_found' });
  await pool.query(
    `INSERT INTO parche_members (parche_id, user_id) VALUES ($1,$2) ON CONFLICT DO NOTHING`,
    [p.rows[0].id, req.user.id]);
  res.json({ ok: true, parcheId: p.rows[0].id, joinCode: String(joinCode).toUpperCase() });
});

/**
 * GET /parches/:id — the group view: members with lifetime meals (leaderboard),
 * the shared total, the goal, and who is walking right now (live presence).
 */
parchesRouter.get('/:id', requireAuth, async (req: any, res) => {
  const head = await pool.query(`SELECT name, join_code FROM parches WHERE id = $1`, [req.params.id]);
  if (!head.rows.length) return res.status(404).json({ error: 'parche_not_found' });
  const members = await pool.query(
    `SELECT u.display_name AS name,
            COALESCE(NULLIF(TRIM(u.city),''),'') AS city,
            m.walking_now, COALESCE(m.walking_steps,0)::int AS walking_steps,
            (SELECT COUNT(*) FROM donations d WHERE d.user_id = m.user_id)::int AS meals,
            COALESCE((SELECT SUM(a.steps) FROM activities a WHERE a.user_id = m.user_id),0)::int AS total_steps
     FROM parche_members m JOIN users u ON u.id = m.user_id
     WHERE m.parche_id = $1
     ORDER BY total_steps DESC, meals DESC`, [req.params.id]);
  // Ranking position by total steps (most-walked first) → competitive leaderboard.
  const ranked = members.rows.map((m: any, i: number) => ({ ...m, rank: i + 1 }));
  const total = ranked.reduce((a: number, m: any) => a + Number(m.meals), 0);
  res.json({
    name: head.rows[0].name,
    joinCode: head.rows[0].join_code,
    members: ranked,
    totalMeals: total,
    goal: ranked.length * 7,
    walkingNow: ranked.filter((m: any) => m.walking_now).map((m: any) => m.name),
  });
});

/** POST /parches/:id/presence  { walking, steps? } — live "caminando ahora" + live step count */
parchesRouter.post('/:id/presence', requireAuth, async (req: any, res) => {
  const steps = Math.max(0, parseInt(req.body.steps, 10) || 0);
  await pool.query(
    `UPDATE parche_members SET walking_now = $1, walking_steps = $2 WHERE parche_id = $3 AND user_id = $4`,
    [!!req.body.walking, steps, req.params.id, req.user.id]);
  res.json({ ok: true });
});
