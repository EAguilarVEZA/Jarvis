// AUTO-GENERATED for self-initializing deploy. Idempotent: safe to run on every cold start.
export const SCHEMA_SQL = `-- Camina y Alimenta — PostgreSQL schema (MVP)
-- gen_random_uuid() is built into PostgreSQL 13+ (no extension needed).

CREATE TABLE IF NOT EXISTS users (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email         TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  display_name  TEXT NOT NULL,
  -- Demographics (optional, with consent) — power aggregate reporting only.
  -- NEVER expose individually; reports return grouped/anonymous data.
  birth_year    INT,                              -- age = current year − birth_year
  sex           TEXT CHECK (sex IN ('F','M','O','NA')),
  city          TEXT,
  postal_code   TEXT,
  occupation    TEXT,                             -- e.g. estudiante, empleado, independiente…
  daily_goal    INT NOT NULL DEFAULT 8000,
  consent_health_data BOOLEAN NOT NULL DEFAULT FALSE, -- Ley 1581 habeas data
  consent_demographics BOOLEAN NOT NULL DEFAULT FALSE,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS food_banks (
  id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name    TEXT NOT NULL,            -- e.g. Banco de Alimentos de Bogotá
  city    TEXT NOT NULL,
  abaco_member BOOLEAN DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS sponsors (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name        TEXT NOT NULL,
  logo_url    TEXT,
  contact_email TEXT,
  password_hash TEXT,                 -- for the sponsor portal login (nullable until set)
  active      BOOLEAN NOT NULL DEFAULT TRUE
);

-- A sponsor buys a campaign: a fixed budget of meals + optional offer.
-- MULTI-TENANT DESIGN DECISION (Phase 5 "Ofertas con Propósito"):
-- the same engine serves any brand's promotions. campaign_type distinguishes
-- donation-driven campaigns from standalone promos; the golden rule is enforced
-- by min_meals_funded > 0 on every campaign, including pure promos.
CREATE TABLE IF NOT EXISTS campaigns (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  sponsor_id     UUID NOT NULL REFERENCES sponsors(id),
  food_bank_id   UUID NOT NULL REFERENCES food_banks(id),
  campaign_type  TEXT NOT NULL DEFAULT 'donacion' CHECK (campaign_type IN ('donacion','promo')),
  meals_budget   INT NOT NULL,                 -- e.g. 50000 meals/month
  meals_donated  INT NOT NULL DEFAULT 0,
  min_meals_funded INT NOT NULL DEFAULT 1 CHECK (min_meals_funded > 0), -- every campaign feeds children
  cost_per_meal_cop INT NOT NULL,              -- e.g. 3000
  starts_at      DATE NOT NULL,
  ends_at        DATE NOT NULL,
  active         BOOLEAN NOT NULL DEFAULT TRUE,
  CHECK (meals_donated <= meals_budget)
);

-- Parches (groups): walk together with family/friends. Cooperative, not toxic.
CREATE TABLE IF NOT EXISTS parches (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name        TEXT NOT NULL,
  join_code   TEXT UNIQUE NOT NULL,            -- short code for the WhatsApp invite link
  created_by  UUID REFERENCES users(id),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS parche_members (
  parche_id   UUID NOT NULL REFERENCES parches(id),
  user_id     UUID NOT NULL REFERENCES users(id),
  joined_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  walking_now BOOLEAN NOT NULL DEFAULT FALSE,  -- live presence, set only during a session
  PRIMARY KEY (parche_id, user_id)
);

-- A single continuous walk session. A meal is donated ONLY when one session
-- reaches the goal in one shot — steps are NOT accumulated across sessions/days.
-- GPS distance is captured to verify real movement (anti-gaming).
CREATE TABLE IF NOT EXISTS walk_sessions (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       UUID NOT NULL REFERENCES users(id),
  started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  ended_at      TIMESTAMPTZ,
  steps         INT NOT NULL DEFAULT 0 CHECK (steps >= 0),
  gps_meters    INT NOT NULL DEFAULT 0,          -- distance from GPS during the session
  status        TEXT NOT NULL DEFAULT 'active'   -- active | completed | abandoned
                CHECK (status IN ('active','completed','abandoned')),
  source        TEXT NOT NULL DEFAULT 'app'
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON walk_sessions (user_id, started_at);

-- Daily activity synced from the phone
CREATE TABLE IF NOT EXISTS activities (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id    UUID NOT NULL REFERENCES users(id),
  activity_date DATE NOT NULL,
  steps      INT NOT NULL CHECK (steps >= 0 AND steps <= 100000),
  distance_m INT,
  source     TEXT NOT NULL DEFAULT 'pedometer', -- pedometer | healthkit | health_connect
  synced_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (user_id, activity_date)
);

-- One meal donation per user per day, tied to a campaign
CREATE TABLE IF NOT EXISTS donations (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     UUID NOT NULL REFERENCES users(id),
  campaign_id UUID NOT NULL REFERENCES campaigns(id),
  session_id  UUID REFERENCES walk_sessions(id),  -- the completed session that earned it
  donation_date DATE NOT NULL,
  steps_at_claim INT NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (user_id, donation_date)               -- hard 1/day cap
);

-- Reward categories users can opt into (declared-intent data)
CREATE TABLE IF NOT EXISTS reward_categories (
  id    TEXT PRIMARY KEY,        -- 'comida_saludable','conciertos','deporte','mercado','bienestar','transporte'
  label TEXT NOT NULL
);

-- User's chosen categories: drives offer matching AND gives sponsors zero-party intent data
CREATE TABLE IF NOT EXISTS user_preferences (
  user_id     UUID NOT NULL REFERENCES users(id),
  category_id TEXT NOT NULL REFERENCES reward_categories(id),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, category_id)
);

-- Offer definitions attached to a campaign
CREATE TABLE IF NOT EXISTS offers (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  campaign_id UUID NOT NULL REFERENCES campaigns(id),
  title       TEXT NOT NULL,                    -- "30% dcto. leche 1L"
  retailer    TEXT NOT NULL,                    -- "Éxito", "D1"
  category_id TEXT NOT NULL REFERENCES reward_categories(id) DEFAULT 'comida_saludable',
  valid_days  INT NOT NULL DEFAULT 7,
  max_issues  INT,
  retailer_code TEXT                              -- the retailer's own POS code; if set, the user's barcode encodes THIS so it scans at checkout
);
-- Idempotent migration for existing databases:
ALTER TABLE offers ADD COLUMN IF NOT EXISTS retailer_code TEXT;
-- Matching rule: when a user claims a donation, issue the coupon whose offer category
-- is in their preferences; fall back to any active offer if no match.

-- Single-use coupons. Either issued to a user on completion, OR pre-generated by
-- a sponsor for printing as barcodes (user_id NULL until assigned/redeemed).
CREATE TABLE IF NOT EXISTS coupons (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  offer_id    UUID NOT NULL REFERENCES offers(id),
  user_id     UUID REFERENCES users(id),        -- NULL = pre-generated, not yet assigned
  code        TEXT UNIQUE NOT NULL,             -- single-use barcode/QR payload
  issued_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at  TIMESTAMPTZ NOT NULL,
  redeemed_at TIMESTAMPTZ,                      -- NULL = not yet used
  redeemed_store TEXT,
  activated_at TIMESTAMPTZ                      -- set when user starts the in-store redemption window
);
-- Idempotent migration for existing databases (column added after first deploy):
ALTER TABLE coupons ADD COLUMN IF NOT EXISTS activated_at TIMESTAMPTZ;
ALTER TABLE parche_members ADD COLUMN IF NOT EXISTS walking_steps INT DEFAULT 0;
ALTER TABLE coupons ADD COLUMN IF NOT EXISTS reward_message TEXT;

CREATE INDEX IF NOT EXISTS idx_activities_user_date ON activities (user_id, activity_date);
CREATE INDEX IF NOT EXISTS idx_donations_date ON donations (donation_date);
CREATE INDEX IF NOT EXISTS idx_coupons_user ON coupons (user_id) WHERE redeemed_at IS NULL;

-- Community impact view
CREATE OR REPLACE VIEW community_impact AS
SELECT
  COUNT(*)                            AS total_meals,
  COUNT(DISTINCT user_id)             AS total_donors,
  COUNT(*) FILTER (WHERE donation_date = CURRENT_DATE) AS meals_today
FROM donations;

-- Aggregate demographic view for reporting (grouped/anonymous only — never individual)
CREATE OR REPLACE VIEW demographics_by_age AS
SELECT age_group, COUNT(*) AS users, COALESCE(SUM(meals),0) AS meals
FROM (
  SELECT u.id,
    CASE
      WHEN u.birth_year IS NULL THEN 'desconocido'
      WHEN EXTRACT(YEAR FROM now())-u.birth_year < 18 THEN '<18'
      WHEN EXTRACT(YEAR FROM now())-u.birth_year < 25 THEN '18-24'
      WHEN EXTRACT(YEAR FROM now())-u.birth_year < 35 THEN '25-34'
      WHEN EXTRACT(YEAR FROM now())-u.birth_year < 45 THEN '35-44'
      WHEN EXTRACT(YEAR FROM now())-u.birth_year < 60 THEN '45-59'
      ELSE '60+' END AS age_group,
    (SELECT COUNT(*) FROM donations d WHERE d.user_id = u.id) AS meals
  FROM users u
) t GROUP BY age_group;
`;

export const SEED_SQL = `-- Camina y Alimenta — development seed data
INSERT INTO reward_categories (id, label) VALUES
  ('comida_saludable','Comida saludable'),
  ('conciertos','Conciertos y eventos'),
  ('deporte','Deporte'),
  ('mercado','Mercado'),
  ('bienestar','Bienestar')
ON CONFLICT DO NOTHING;

INSERT INTO food_banks (id, name, city) VALUES
  ('11111111-1111-1111-1111-111111111111','Banco de Alimentos de Bogotá','Bogotá')
ON CONFLICT DO NOTHING;

-- demo sponsor login → email: demo@lacticol.co · password: demo1234
-- (hash is bcrypt of 'demo1234'; change before production)
INSERT INTO sponsors (id, name, contact_email, password_hash) VALUES
  ('22222222-2222-2222-2222-222222222222','LactiCol (demo)','demo@lacticol.co',
   '$2a$10$/avpVcBEvwkc5DPj7cgIz./.0WryFbclHzXaDb.FOnB7suSYWRsqG')
ON CONFLICT DO NOTHING;

INSERT INTO campaigns (id, sponsor_id, food_bank_id, meals_budget, cost_per_meal_cop, starts_at, ends_at) VALUES
  ('33333333-3333-3333-3333-333333333333',
   '22222222-2222-2222-2222-222222222222',
   '11111111-1111-1111-1111-111111111111',
   50000, 3000, CURRENT_DATE - 1, CURRENT_DATE + 90)
ON CONFLICT DO NOTHING;

INSERT INTO offers (id, campaign_id, title, retailer, category_id, valid_days) VALUES
  ('44444444-4444-4444-4444-444444444444','33333333-3333-3333-3333-333333333333','30% dcto. leche deslactosada 1L','Éxito','comida_saludable',7)
ON CONFLICT (id) DO NOTHING;
`;
