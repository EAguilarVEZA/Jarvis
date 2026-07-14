/**
 * Anti-fraud validation for activity syncs.
 * Production: replace pedometer source with HealthKit / Health Connect
 * attested data and add device-attestation (Play Integrity / App Attest).
 */
const MAX_DAILY_STEPS = 60_000;       // beyond this, reject
const MAX_STEPS_PER_MINUTE = 220;     // elite runner cadence ceiling

export interface ActivitySync {
  steps: number;
  windowMinutes: number; // minutes since last sync
  source: 'pedometer' | 'healthkit' | 'health_connect';
}

export function validateActivity(sync: ActivitySync): { ok: boolean; reason?: string } {
  if (sync.steps < 0) return { ok: false, reason: 'negative_steps' };
  if (sync.steps > MAX_DAILY_STEPS) return { ok: false, reason: 'daily_ceiling' };
  if (sync.windowMinutes > 0) {
    const cadence = sync.steps / sync.windowMinutes;
    if (cadence > MAX_STEPS_PER_MINUTE) return { ok: false, reason: 'implausible_cadence' };
  }
  return { ok: true };
}
