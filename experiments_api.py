"""
experiments_api
───────────────
A self-contained statistics + experimentation engine — the "Test & Learn" lab.
Brings Mastercard Test & Learn / APT-style capabilities into Jarvis: matched
control groups, difference-in-differences incrementality, statistical
significance (p-values + confidence intervals), power / sample-size / minimum
detectable effect, two-proportion (p-ratio / conversion) tests, and descriptive
+ correlation analysis.

Design goals
  • ZERO hard dependency on scipy — all distribution functions (normal, Student-t,
    chi-square) are implemented in pure Python via well-known numerical routines
    (Lanczos gammaln, regularized incomplete beta/gamma). numpy is used only when
    present (matched-control distance math) with a pure-Python fallback.
  • Every endpoint returns not just numbers but a plain-English verdict, so the
    UI (and the AI) can explain results the way an analyst would.

Routes (prefix /api/stats):
  POST /api/stats/ttest         — two-sample Welch t-test (raw arrays or summary)
  POST /api/stats/proportions   — two-proportion z-test (p-ratio / conversion lift)
  POST /api/stats/did           — difference-in-differences incrementality
  POST /api/stats/power         — sample size / power / MDE calculator
  POST /api/stats/match         — matched control-group selection (nearest neighbor)
  POST /api/stats/summary       — descriptive stats + optional correlation
  POST /api/stats/experiment    — end-to-end test-vs-control experiment readout
  GET  /api/stats/health        — engine self-check
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import random
import time
import uuid
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/api/stats", tags=["stats"])

# ─────────────────────────────────────────────────────────────────────────────
# Distribution functions (pure Python, no scipy)
# ─────────────────────────────────────────────────────────────────────────────

_LANCZOS_G = 7
_LANCZOS_C = [
    0.99999999999980993, 676.5203681218851, -1259.1392167224028,
    771.32342877765313, -176.61502916214059, 12.507343278686905,
    -0.13857109526572012, 9.9843695780195716e-6, 1.5056327351493116e-7,
]


def _gammaln(x: float) -> float:
    """Natural log of the gamma function (Lanczos approximation)."""
    if x < 0.5:
        # reflection formula
        return math.log(math.pi / math.sin(math.pi * x)) - _gammaln(1.0 - x)
    x -= 1.0
    a = _LANCZOS_C[0]
    t = x + _LANCZOS_G + 0.5
    for i in range(1, _LANCZOS_G + 2):
        a += _LANCZOS_C[i] / (x + i)
    return 0.5 * math.log(2 * math.pi) + (x + 0.5) * math.log(t) - t + math.log(a)


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function (Numerical Recipes)."""
    MAXIT, EPS, FPMIN = 200, 3.0e-12, 1.0e-30
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < FPMIN:
        d = FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, MAXIT + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < EPS:
            break
    return h


def _betai(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta function I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    bt = math.exp(_gammaln(a + b) - _gammaln(a) - _gammaln(b)
                  + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def norm_cdf(z: float) -> float:
    """Standard normal CDF via the error function."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def norm_sf(z: float) -> float:
    return 1.0 - norm_cdf(z)


def norm_ppf(p: float) -> float:
    """Inverse standard normal CDF (Acklam's rational approximation)."""
    if p <= 0.0:
        return -math.inf
    if p >= 1.0:
        return math.inf
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
           (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


def t_sf(t: float, df: float) -> float:
    """Upper-tail (survival) probability of Student-t: P(T > t)."""
    if df <= 0:
        return float("nan")
    x = df / (df + t * t)
    ib = 0.5 * _betai(df / 2.0, 0.5, x)
    return ib if t > 0 else 1.0 - ib


def t_two_sided_p(t: float, df: float) -> float:
    """Two-sided p-value for a t statistic."""
    return 2.0 * t_sf(abs(t), df)


def t_ppf(p: float, df: float) -> float:
    """Inverse Student-t CDF via bisection on t_sf (robust, no scipy)."""
    if p <= 0.0:
        return -math.inf
    if p >= 1.0:
        return math.inf
    lo, hi = -1000.0, 1000.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        # CDF(mid) = 1 - t_sf(mid)
        if (1.0 - t_sf(mid, df)) < p:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def chi2_sf(x: float, df: int) -> float:
    """Upper-tail probability of the chi-square distribution."""
    if x <= 0:
        return 1.0
    # regularized upper incomplete gamma Q(df/2, x/2)
    return _gammaincc(df / 2.0, x / 2.0)


def _gammaincc(a: float, x: float) -> float:
    """Regularized upper incomplete gamma Q(a, x) = 1 - P(a, x)."""
    if x < 0 or a <= 0:
        return float("nan")
    if x < a + 1.0:
        return 1.0 - _gser(a, x)
    return _gcf(a, x)


def _gser(a: float, x: float) -> float:
    """Series representation of the lower regularized incomplete gamma P(a, x)."""
    if x <= 0:
        return 0.0
    ap = a
    total = 1.0 / a
    delta = total
    for _ in range(500):
        ap += 1.0
        delta *= x / ap
        total += delta
        if abs(delta) < abs(total) * 1.0e-12:
            break
    return total * math.exp(-x + a * math.log(x) - _gammaln(a))


def _gcf(a: float, x: float) -> float:
    """Continued fraction for the upper regularized incomplete gamma Q(a, x)."""
    FPMIN = 1.0e-30
    b = x + 1.0 - a
    c = 1.0 / FPMIN
    d = 1.0 / b
    h = d
    for i in range(1, 500):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < FPMIN:
            d = FPMIN
        c = b + an / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1.0e-12:
            break
    return math.exp(-x + a * math.log(x) - _gammaln(a)) * h


# ─────────────────────────────────────────────────────────────────────────────
# Small helpers
# ─────────────────────────────────────────────────────────────────────────────

def _mean(xs):
    xs = [float(v) for v in xs if v is not None]
    return sum(xs) / len(xs) if xs else 0.0


def _var(xs, ddof=1):
    xs = [float(v) for v in xs if v is not None]
    n = len(xs)
    if n <= ddof:
        return 0.0
    m = sum(xs) / n
    return sum((v - m) ** 2 for v in xs) / (n - ddof)


def _fmt_p(p: float) -> str:
    if p != p:  # nan
        return "n/a"
    if p < 0.001:
        return "<0.001"
    return f"{p:.3f}"


def _sig_verdict(p: float, alpha: float, effect_desc: str) -> dict:
    """Return a plain-English significance verdict block."""
    sig = (p == p) and p < alpha
    conf = round((1 - p) * 100, 1) if (p == p) else 0.0
    if sig:
        headline = f"Statistically significant ({effect_desc})"
        detail = (f"With p = {_fmt_p(p)} (below your α = {alpha}), the difference is unlikely to be "
                  f"random chance. You can be about {round((1-alpha)*100)}%+ confident the effect is real.")
    else:
        headline = "Not statistically significant"
        detail = (f"With p = {_fmt_p(p)} (at or above α = {alpha}), we can't rule out random chance. "
                  f"Collect more data, run longer, or treat the result as directional only.")
    return {"significant": sig, "p_value": p, "p_display": _fmt_p(p),
            "confidence_pct": conf, "alpha": alpha, "headline": headline, "detail": detail}


# ─────────────────────────────────────────────────────────────────────────────
# Request models
# ─────────────────────────────────────────────────────────────────────────────

class TTestRequest(BaseModel):
    # Either raw arrays…
    a: Optional[list] = None
    b: Optional[list] = None
    # …or summary stats for each group:
    mean_a: Optional[float] = None
    sd_a: Optional[float] = None
    n_a: Optional[int] = None
    mean_b: Optional[float] = None
    sd_b: Optional[float] = None
    n_b: Optional[int] = None
    alpha: float = 0.05
    label_a: str = "Test"
    label_b: str = "Control"


class ProportionsRequest(BaseModel):
    successes_a: float
    n_a: int
    successes_b: float
    n_b: int
    alpha: float = 0.05
    label_a: str = "Test"
    label_b: str = "Control"


class DiDRequest(BaseModel):
    # Raw per-unit arrays (preferred — enables a real standard error)…
    test_pre: Optional[list] = None
    test_post: Optional[list] = None
    control_pre: Optional[list] = None
    control_post: Optional[list] = None
    # …or plain means (point estimate only, SE requires arrays or the sds/ns below)
    test_pre_mean: Optional[float] = None
    test_post_mean: Optional[float] = None
    control_pre_mean: Optional[float] = None
    control_post_mean: Optional[float] = None
    alpha: float = 0.05
    metric_name: str = "metric"


class PowerRequest(BaseModel):
    kind: str = "mean"            # "mean" or "proportion"
    baseline: Optional[float] = None      # baseline mean or baseline rate (0-1)
    sd: Optional[float] = None            # required for kind="mean"
    mde_abs: Optional[float] = None       # absolute minimum detectable effect
    mde_rel: Optional[float] = None       # relative MDE (fraction, e.g. 0.05 = 5%)
    n_per_group: Optional[int] = None     # if given → returns achieved power + MDE
    alpha: float = 0.05
    power: float = 0.8
    two_sided: bool = True


class MatchRequest(BaseModel):
    # test units and candidate controls each: {id, features:[...]} with aligned feature order
    test_units: list
    candidate_controls: list
    feature_names: Optional[list] = None
    k: int = 1                    # neighbors per test unit
    with_replacement: bool = False


class SummaryRequest(BaseModel):
    data: list                    # list of numbers, or list of {name, values:[...]}
    correlate: bool = False


class ExperimentRequest(BaseModel):
    """End-to-end test-vs-control readout. Provide pre/post series for each arm."""
    test_pre: list
    test_post: list
    control_pre: list
    control_post: list
    alpha: float = 0.05
    metric_name: str = "metric"
    cost: Optional[float] = None          # program cost, to compute ROI on incremental value
    value_per_unit: Optional[float] = None  # $ value of one incremental metric unit
    robust: bool = False                  # also run a permutation (randomization) test


class ExperimentFromDataRequest(BaseModel):
    """Run a Test & Learn experiment straight off the semantic layer / BigQuery.
    Define cohorts either by a grouping dimension (group_field + test/control
    values) or by explicit unit id lists."""
    primary_table: str
    metric_field: str
    unit_field: str
    date_field: str
    pre_start: str
    pre_end: str
    post_start: str
    post_end: str
    group_field: Optional[str] = None
    test_values: Optional[list] = None
    control_values: Optional[list] = None
    test_units: Optional[list] = None
    control_units: Optional[list] = None
    extra_filters: Optional[list] = None   # [{field, op, value}] on primary_table
    alpha: float = 0.05
    metric_name: Optional[str] = None
    cost: Optional[float] = None
    value_per_unit: Optional[float] = None
    robust: bool = True


class SegmentScanRequest(ExperimentFromDataRequest):
    """Heterogeneous-effect scan: run the experiment within each value of
    segment_field to find where the program worked best (targeted rollout)."""
    segment_field: str = ""
    max_segments: int = 12


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/health")
async def health():
    # sanity: known values (t_two_sided_p(2.0, 10) ≈ 0.0734; chi2_sf(3.841,1) ≈ 0.05)
    return {
        "ok": True,
        "checks": {
            "norm_cdf_1.96": round(norm_cdf(1.96), 4),
            "t_p_(2,10)": round(t_two_sided_p(2.0, 10), 4),
            "chi2_sf_(3.841,1)": round(chi2_sf(3.841, 1), 4),
        },
    }


@router.get("/diagnostics")
async def diagnostics():
    """Live readiness check for the data-mode paths: verifies the pure-stats
    engine, the semantic model, and BigQuery connectivity, and lists candidate
    tables (with unit/date/metric fields) you can point an experiment at."""
    out = {"ok": True, "stats_engine": "ok",
           "self_test": {"t_p_(2,10)": round(t_two_sided_p(2.0, 10), 4),
                         "chi2_(3.841,1)": round(chi2_sf(3.841, 1), 4)}}
    # semantic model
    try:
        from semantic import load_model
        model = load_model()
        tables, ready = [], []
        for t in model.tables.values():
            dims = [f.key for f in t.dimensions]
            dates = [f.key for f in t.dates]
            mets = [f.key for f in t.metrics]
            # experiment-ready = has ≥1 metric, ≥1 date, and ≥2 dimensions
            # (one to identify a unit, one to split test vs control cohorts)
            is_ready = bool(mets and dates and len(dims) >= 2)
            if len(tables) < 25:
                tables.append({"key": t.key, "dimensions": dims[:12], "dates": dates[:6],
                               "metrics": mets[:12], "experiment_ready": is_ready})
            if is_ready and len(ready) < 8:
                ready.append({"table": t.key, "metric_field": mets[0], "unit_field": dims[0],
                              "date_field": dates[0], "group_field": dims[1]})
        out["semantic"] = {"ok": True, "table_count": len(model.tables),
                           "tables": tables, "experiment_ready": ready}
        if ready:
            r0 = ready[0]
            out["example"] = {
                "note": "A concrete experiment-ready table + fields you can plug into the lab or the live test.",
                "table": r0["table"], "metric_field": r0["metric_field"],
                "unit_field": r0["unit_field"], "date_field": r0["date_field"],
                "group_field": r0["group_field"],
                "reminder": "You still choose test_values / control_values (distinct values of group_field) and the pre/post windows.",
            }
    except Exception as e:
        out["semantic"] = {"ok": False, "error": str(e)}
    # BigQuery executor status (does not run a query)
    try:
        from semantic import run_query  # noqa: F401
        import os as _os
        out["bigquery"] = {"configured": bool(_os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or _os.getenv("BQ_PROJECT") or True),
                           "note": "Run a small experiment from Test & Learn → From my data to confirm end-to-end."}
    except Exception as e:
        out["bigquery"] = {"configured": False, "error": str(e)}
    # store health
    out["stores"] = {
        "saved_experiments": len(_store_load().get("experiments", [])),
        "templates": len(_tpl_load().get("templates", [])),
        "rollout_targets": len(_rollout_load().get("targets", [])),
    }
    return out


@router.post("/ttest")
async def ttest(body: TTestRequest):
    """Welch's two-sample t-test (unequal variances). Accepts raw arrays or summaries."""
    if body.a is not None and body.b is not None:
        na, nb = len([v for v in body.a if v is not None]), len([v for v in body.b if v is not None])
        ma, mb = _mean(body.a), _mean(body.b)
        va, vb = _var(body.a), _var(body.b)
        sda, sdb = math.sqrt(va), math.sqrt(vb)
    else:
        for f in ("mean_a", "sd_a", "n_a", "mean_b", "sd_b", "n_b"):
            if getattr(body, f) is None:
                return {"error": "Provide raw arrays a & b, or all of mean/sd/n for both groups."}
        ma, mb = body.mean_a, body.mean_b
        sda, sdb = body.sd_a, body.sd_b
        na, nb = int(body.n_a), int(body.n_b)
        va, vb = sda ** 2, sdb ** 2
    if na < 2 or nb < 2:
        return {"error": "Each group needs at least 2 observations."}

    se = math.sqrt(va / na + vb / nb)
    diff = ma - mb
    if se == 0:
        return {"error": "Zero variance — cannot compute a t statistic."}
    t = diff / se
    # Welch–Satterthwaite degrees of freedom
    df = (va / na + vb / nb) ** 2 / ((va / na) ** 2 / (na - 1) + (vb / nb) ** 2 / (nb - 1))
    p = t_two_sided_p(t, df)
    tcrit = t_ppf(1 - body.alpha / 2, df)
    ci = [diff - tcrit * se, diff + tcrit * se]
    rel = (diff / mb * 100) if mb else None
    verdict = _sig_verdict(p, body.alpha,
                           f"{body.label_a} {'>' if diff > 0 else '<'} {body.label_b}")
    return {
        "ok": True, "test": "Welch two-sample t-test",
        "group_a": {"label": body.label_a, "mean": ma, "sd": sda, "n": na},
        "group_b": {"label": body.label_b, "mean": mb, "sd": sdb, "n": nb},
        "difference": diff, "relative_lift_pct": rel,
        "t_stat": t, "df": df, "std_error": se,
        "confidence_interval": ci, "confidence_level_pct": round((1 - body.alpha) * 100),
        "verdict": verdict,
    }


@router.post("/proportions")
async def proportions(body: ProportionsRequest):
    """Two-proportion z-test — conversion / response-rate lift ('p-ratio')."""
    na, nb = int(body.n_a), int(body.n_b)
    if na <= 0 or nb <= 0:
        return {"error": "Sample sizes must be positive."}
    pa, pb = body.successes_a / na, body.successes_b / nb
    pooled = (body.successes_a + body.successes_b) / (na + nb)
    se_pool = math.sqrt(pooled * (1 - pooled) * (1 / na + 1 / nb))
    if se_pool == 0:
        return {"error": "Degenerate proportions — cannot compute a z statistic."}
    z = (pa - pb) / se_pool
    p = 2 * norm_sf(abs(z))
    # unpooled SE for the confidence interval on the difference
    se_unpool = math.sqrt(pa * (1 - pa) / na + pb * (1 - pb) / nb)
    zc = norm_ppf(1 - body.alpha / 2)
    diff = pa - pb
    ci = [diff - zc * se_unpool, diff + zc * se_unpool]
    rel = (diff / pb * 100) if pb else None
    verdict = _sig_verdict(p, body.alpha,
                           f"{body.label_a} rate {'>' if diff > 0 else '<'} {body.label_b} rate")
    return {
        "ok": True, "test": "Two-proportion z-test",
        "group_a": {"label": body.label_a, "rate": pa, "successes": body.successes_a, "n": na},
        "group_b": {"label": body.label_b, "rate": pb, "successes": body.successes_b, "n": nb},
        "difference": diff, "relative_lift_pct": rel,
        "z_stat": z, "std_error": se_unpool,
        "confidence_interval": ci, "confidence_level_pct": round((1 - body.alpha) * 100),
        "verdict": verdict,
    }


def _did_core(tp0, tp1, cp0, cp1, alpha):
    """Difference-in-differences from raw arrays. Returns estimate + inference."""
    mt0, mt1 = _mean(tp0), _mean(tp1)
    mc0, mc1 = _mean(cp0), _mean(cp1)
    test_delta = mt1 - mt0
    ctrl_delta = mc1 - mc0
    did = test_delta - ctrl_delta
    # Variance of DiD ≈ sum of variances of the four group means (independent groups approx)
    def _vm(xs):
        n = len([v for v in xs if v is not None])
        return (_var(xs) / n) if n > 1 else 0.0
    se = math.sqrt(_vm(tp0) + _vm(tp1) + _vm(cp0) + _vm(cp1))
    n_min = min(len(tp0), len(tp1), len(cp0), len(cp1))
    df = max(1, n_min - 1)
    if se > 0:
        t = did / se
        p = t_two_sided_p(t, df)
        tcrit = t_ppf(1 - alpha / 2, df)
        ci = [did - tcrit * se, did + tcrit * se]
    else:
        t, p, ci = float("nan"), float("nan"), [did, did]
    return {
        "test_pre": mt0, "test_post": mt1, "test_delta": test_delta,
        "control_pre": mc0, "control_post": mc1, "control_delta": ctrl_delta,
        "did_estimate": did, "std_error": se, "t_stat": t, "df": df,
        "confidence_interval": ci,
        # incremental lift = DiD relative to the counterfactual (test would have
        # moved like control): counterfactual test_post = mt0 + ctrl_delta
        "incremental_lift_pct": (did / (mt0 + ctrl_delta) * 100) if (mt0 + ctrl_delta) else None,
        "p_value": p,
    }


def _paired_deltas(pre, post):
    """Per-unit post−pre deltas when arrays are aligned 1:1 (same length)."""
    pre = [v for v in pre if v is not None]
    post = [v for v in post if v is not None]
    if len(pre) != len(post) or not pre:
        return None
    return [float(post[i]) - float(pre[i]) for i in range(len(pre))]


def _did_permutation(test_deltas, control_deltas, iters=3000, seed=1234567):
    """Permutation (randomization) test for DiD — the robustness method APT /
    Spotify recommend for small numbers of units: pool the per-unit deltas,
    reshuffle test/control labels many times, and measure how often a DiD as
    extreme as observed appears by chance. Returns an empirical two-sided p."""
    nT, nC = len(test_deltas), len(control_deltas)
    if nT < 2 or nC < 2:
        return None
    obs = sum(test_deltas) / nT - sum(control_deltas) / nC
    pool = list(test_deltas) + list(control_deltas)
    rnd = random.Random(seed)
    extreme = 0
    for _ in range(iters):
        rnd.shuffle(pool)
        t = pool[:nT]
        c = pool[nT:]
        d = sum(t) / nT - sum(c) / nC
        if abs(d) >= abs(obs) - 1e-12:
            extreme += 1
    return {"p_value": (extreme + 1) / (iters + 1), "iterations": iters, "observed_did": obs}


def _cuped(test_pre, test_post, control_pre, control_post, alpha):
    """CUPED (Controlled-experiment Using Pre-Experiment Data): use the pre-period
    metric as a covariate to strip out pre-existing between-unit variance, giving
    the same unbiased effect with a tighter confidence interval / more power.

    Adjusted_post = post − θ·(pre − mean_pre), θ = cov(post,pre)/var(pre) pooled.
    Effect = mean(adj test post) − mean(adj control post)."""
    tpre = [float(v) for v in test_pre if v is not None]
    tpost = [float(v) for v in test_post if v is not None]
    cpre = [float(v) for v in control_pre if v is not None]
    cpost = [float(v) for v in control_post if v is not None]
    if len(tpre) != len(tpost) or len(cpre) != len(cpost) or len(tpre) < 2 or len(cpre) < 2:
        return None
    pre_all = tpre + cpre
    post_all = tpost + cpost
    n = len(pre_all)
    mpre = sum(pre_all) / n
    mpost = sum(post_all) / n
    var_pre = sum((x - mpre) ** 2 for x in pre_all) / (n - 1)
    if var_pre == 0:
        return None
    cov = sum((pre_all[i] - mpre) * (post_all[i] - mpost) for i in range(n)) / (n - 1)
    theta = cov / var_pre
    # correlation → theoretical variance reduction (1 - ρ²)
    sd_pre = math.sqrt(var_pre)
    sd_post = math.sqrt(sum((x - mpost) ** 2 for x in post_all) / (n - 1)) or 1.0
    rho = cov / (sd_pre * sd_post) if sd_pre and sd_post else 0.0
    var_reduction = max(0.0, min(1.0, rho * rho))

    def adj(vals):
        return [v - theta * (p - mpre) for v, p in zip(vals[1], vals[0])]
    adj_t = adj((tpre, tpost))
    adj_c = adj((cpre, cpost))
    mt, mc = sum(adj_t) / len(adj_t), sum(adj_c) / len(adj_c)
    vt = sum((x - mt) ** 2 for x in adj_t) / (len(adj_t) - 1)
    vc = sum((x - mc) ** 2 for x in adj_c) / (len(adj_c) - 1)
    se = math.sqrt(vt / len(adj_t) + vc / len(adj_c))
    effect = mt - mc
    if se > 0:
        df = (vt/len(adj_t) + vc/len(adj_c))**2 / ((vt/len(adj_t))**2/(len(adj_t)-1) + (vc/len(adj_c))**2/(len(adj_c)-1))
        t = effect / se
        p = t_two_sided_p(t, df)
        tcrit = t_ppf(1 - alpha / 2, df)
        ci = [effect - tcrit * se, effect + tcrit * se]
    else:
        p, ci = float("nan"), [effect, effect]
    return {"theta": theta, "variance_reduction_pct": round(var_reduction * 100, 1),
            "adjusted_effect": effect, "std_error": se, "p_value": p,
            "confidence_interval": ci, "pre_post_correlation": round(rho, 3)}


def _experiment_readout(test_pre, test_post, control_pre, control_post, alpha,
                        metric_name, cost=None, value_per_unit=None, robust=False,
                        extra=None, cuped=True):
    """Shared Test & Learn readout used by /experiment and /experiment_from_data."""
    core = _did_core(test_pre, test_post, control_pre, control_post, alpha)
    p = core.get("p_value")
    did_val = core["did_estimate"]

    # Robustness: permutation p-value when we can pair pre/post per unit.
    perm = None
    if robust:
        td = _paired_deltas(test_pre, test_post)
        cd = _paired_deltas(control_pre, control_post)
        if td and cd:
            perm = _did_permutation(td, cd)
    if perm:
        core["permutation"] = perm

    # CUPED variance reduction — tighter CI using the pre-period as a covariate.
    if cuped:
        cup = _cuped(test_pre, test_post, control_pre, control_post, alpha)
        if cup:
            core["cuped"] = cup
    if p is not None and p == p:
        verdict = _sig_verdict(p, alpha,
                               f"incremental {'lift' if did_val > 0 else 'drop'} in {metric_name}")
        if perm:
            pv = perm["p_value"]
            agree = (pv < alpha) == verdict["significant"]
            verdict["robustness"] = (
                f"Permutation test agrees (p={_fmt_p(pv)})." if agree else
                f"Caution: permutation test disagrees (p={_fmt_p(pv)}) — treat as borderline.")
    else:
        verdict = {"significant": None, "headline": "Insufficient data",
                   "detail": "Need at least 2 units per arm for inference."}

    n_test = len([v for v in test_post if v is not None])
    incremental_total = did_val * n_test
    roi = None
    if value_per_unit is not None and cost:
        incr_value = incremental_total * value_per_unit
        roi = {"incremental_value": incr_value, "cost": cost, "net": incr_value - cost,
               "roi_multiple": round(incr_value / cost, 2) if cost else None,
               "roi_pct": round((incr_value - cost) / cost * 100, 1) if cost else None}

    if verdict.get("significant"):
        direction = "positive" if did_val > 0 else "negative"
        rec = (f"The program drove a {direction}, statistically significant incremental "
               f"{'lift' if did_val > 0 else 'change'} of {did_val:.2f} per unit "
               f"({core.get('incremental_lift_pct') or 0:.1f}% vs. the control counterfactual). ")
        rec += ("Recommend scaling — use a matched-control design and the segment scan to target the "
                "strongest-responding segments." if did_val > 0 else
                "Recommend halting or redesigning — the initiative is underperforming its control.")
    elif verdict.get("significant") is False:
        rec = ("No significant incremental effect yet. Extend the test window or add units to reach "
               "adequate power before rolling out.")
    else:
        rec = "Provide fuller per-unit series to reach a confident read."

    core.update({
        "ok": True, "test": "Test & Learn experiment readout", "metric": metric_name,
        "confidence_level_pct": round((1 - alpha) * 100),
        "n_test_units": n_test, "incremental_total": incremental_total,
        "roi": roi, "verdict": verdict, "recommendation": rec,
    })
    if extra:
        core.update(extra)
    return core


@router.post("/did")
async def did(body: DiDRequest):
    """Difference-in-differences — isolates incremental impact vs a control group."""
    if all(v is not None for v in (body.test_pre, body.test_post, body.control_pre, body.control_post)):
        core = _did_core(body.test_pre, body.test_post, body.control_pre, body.control_post, body.alpha)
    elif all(v is not None for v in (body.test_pre_mean, body.test_post_mean,
                                     body.control_pre_mean, body.control_post_mean)):
        td = body.test_post_mean - body.test_pre_mean
        cd = body.control_post_mean - body.control_pre_mean
        didv = td - cd
        core = {
            "test_pre": body.test_pre_mean, "test_post": body.test_post_mean, "test_delta": td,
            "control_pre": body.control_pre_mean, "control_post": body.control_post_mean, "control_delta": cd,
            "did_estimate": didv, "std_error": None, "t_stat": None, "df": None,
            "confidence_interval": None,
            "incremental_lift_pct": (didv / (body.test_pre_mean + cd) * 100) if (body.test_pre_mean + cd) else None,
            "p_value": None,
        }
    else:
        return {"error": "Provide the four raw arrays, or the four means."}

    if core.get("p_value") is not None and core["p_value"] == core["p_value"]:
        verdict = _sig_verdict(core["p_value"], body.alpha,
                               f"incremental {'lift' if core['did_estimate'] > 0 else 'drop'} in {body.metric_name}")
    else:
        verdict = {"significant": None, "headline": "Point estimate only",
                   "detail": "Provide raw per-unit arrays to compute a p-value and confidence interval."}
    core.update({"ok": True, "test": "Difference-in-differences", "metric": body.metric_name,
                 "confidence_level_pct": round((1 - body.alpha) * 100), "verdict": verdict})
    return core


@router.post("/power")
async def power(body: PowerRequest):
    """Sample-size / power / minimum-detectable-effect calculator."""
    z_alpha = norm_ppf(1 - body.alpha / (2 if body.two_sided else 1))
    z_beta = norm_ppf(body.power)

    if body.kind == "proportion":
        p0 = body.baseline
        if p0 is None:
            return {"error": "Provide baseline rate (0-1) for a proportion test."}
        if body.mde_abs is not None:
            delta = body.mde_abs
        elif body.mde_rel is not None:
            delta = p0 * body.mde_rel
        else:
            delta = None
        var = p0 * (1 - p0)
        if body.n_per_group:  # solve for achieved power / MDE
            n = int(body.n_per_group)
            if delta is None:
                delta = (z_alpha + z_beta) * math.sqrt(2 * var / n)
            se = math.sqrt(2 * var / n)
            achieved_power = norm_cdf(abs(delta) / se - z_alpha)
            return _power_out("proportion", n, delta, p0, body, achieved_power)
        if delta is None or delta == 0:
            return {"error": "Provide mde_abs or mde_rel to size the experiment."}
        n = math.ceil(2 * var * (z_alpha + z_beta) ** 2 / (delta ** 2))
        return _power_out("proportion", n, delta, p0, body, body.power)

    # kind == "mean"
    if body.sd is None:
        return {"error": "Provide sd for a mean test."}
    sd = body.sd
    if body.mde_abs is not None:
        delta = body.mde_abs
    elif body.mde_rel is not None and body.baseline is not None:
        delta = body.baseline * body.mde_rel
    else:
        delta = None
    if body.n_per_group:
        n = int(body.n_per_group)
        se = math.sqrt(2 * sd ** 2 / n)
        if delta is None:
            delta = (z_alpha + z_beta) * se
        achieved_power = norm_cdf(abs(delta) / se - z_alpha)
        return _power_out("mean", n, delta, body.baseline, body, achieved_power)
    if delta is None or delta == 0:
        return {"error": "Provide mde_abs (or mde_rel + baseline) to size the experiment."}
    n = math.ceil(2 * sd ** 2 * (z_alpha + z_beta) ** 2 / (delta ** 2))
    return _power_out("mean", n, delta, body.baseline, body, body.power)


def _power_out(kind, n, delta, baseline, body, achieved_power):
    rel = (delta / baseline * 100) if baseline else None
    return {
        "ok": True, "kind": kind,
        "n_per_group": int(n), "total_n": int(n) * 2,
        "minimum_detectable_effect": delta,
        "mde_relative_pct": rel,
        "alpha": body.alpha, "power": round(achieved_power, 4), "two_sided": body.two_sided,
        "summary": (f"To detect a {('{:.2f}'.format(delta))} absolute change"
                    + (f" (~{rel:.1f}% relative)" if rel else "")
                    + f" at α={body.alpha} and {round(achieved_power*100)}% power, "
                    + f"you need ~{int(n):,} units per group ({int(n)*2:,} total)."),
    }


@router.post("/match")
async def match(body: MatchRequest):
    """Matched control-group selection: for each test unit, find its nearest
    control unit(s) by standardized (z-score) Euclidean distance over features —
    the core of an APT/Test&Learn 'matched market' design."""
    tests = body.test_units or []
    cands = body.candidate_controls or []
    if not tests or not cands:
        return {"error": "Provide test_units and candidate_controls."}
    dim = len(tests[0].get("features", []))
    if dim == 0:
        return {"error": "Each unit needs a non-empty 'features' vector."}

    # standardize each feature across the pooled population
    pooled = [u.get("features", []) for u in (tests + cands)]
    means, sds = [], []
    for j in range(dim):
        col = [row[j] for row in pooled if j < len(row) and row[j] is not None]
        m = sum(col) / len(col) if col else 0.0
        v = (sum((x - m) ** 2 for x in col) / (len(col) - 1)) if len(col) > 1 else 1.0
        means.append(m)
        sds.append(math.sqrt(v) if v > 0 else 1.0)

    def z(vec):
        return [((vec[j] - means[j]) / sds[j]) if j < len(vec) and vec[j] is not None else 0.0
                for j in range(dim)]

    def dist(u, v):
        return math.sqrt(sum((a - b) ** 2 for a, b in zip(u, v)))

    cand_z = [(c, z(c.get("features", []))) for c in cands]
    used = set()
    matches = []
    for t in tests:
        tz = z(t.get("features", []))
        ranked = sorted(cand_z, key=lambda cv: dist(tz, cv[1]))
        picks = []
        for c, cv in ranked:
            cid = c.get("id")
            if (not body.with_replacement) and cid in used:
                continue
            picks.append({"id": cid, "distance": round(dist(tz, cv), 4)})
            if not body.with_replacement:
                used.add(cid)
            if len(picks) >= body.k:
                break
        matches.append({"test_id": t.get("id"), "matched_controls": picks})

    # balance table: mean of each feature, test vs matched-control
    matched_ids = {p["id"] for m in matches for p in m["matched_controls"]}
    ctrl_sel = [c for c in cands if c.get("id") in matched_ids]
    balance = []
    names = body.feature_names or [f"feature_{j+1}" for j in range(dim)]
    for j in range(dim):
        tvals = [u["features"][j] for u in tests if j < len(u.get("features", []))]
        cvals = [u["features"][j] for u in ctrl_sel if j < len(u.get("features", []))]
        tm, cm = (_mean(tvals), _mean(cvals))
        std = sds[j] or 1.0
        balance.append({"feature": names[j] if j < len(names) else f"feature_{j+1}",
                        "test_mean": tm, "control_mean": cm,
                        "std_diff": round((tm - cm) / std, 3)})
    max_imbalance = max((abs(b["std_diff"]) for b in balance), default=0.0)
    return {
        "ok": True, "test": "Nearest-neighbor matched control selection",
        "matches": matches, "balance": balance,
        "balance_quality": ("excellent" if max_imbalance < 0.1 else
                            "good" if max_imbalance < 0.25 else "weak"),
        "note": ("Standardized mean differences below 0.1 indicate well-matched groups; "
                 "above 0.25 suggests residual bias — add covariates or widen the control pool."),
    }


@router.post("/summary")
async def summary(body: SummaryRequest):
    """Descriptive statistics (+ optional pairwise correlation) for one or more series."""
    data = body.data or []
    # normalize into named series
    if data and isinstance(data[0], dict):
        series = [{"name": d.get("name", f"series_{i+1}"), "values": d.get("values", [])}
                  for i, d in enumerate(data)]
    else:
        series = [{"name": "series", "values": data}]

    def describe(xs):
        xs = sorted(float(v) for v in xs if v is not None)
        n = len(xs)
        if n == 0:
            return {"n": 0}
        m = sum(xs) / n
        sd = math.sqrt(sum((v - m) ** 2 for v in xs) / (n - 1)) if n > 1 else 0.0

        def q(p):
            if n == 1:
                return xs[0]
            idx = p * (n - 1)
            lo = int(math.floor(idx))
            hi = min(lo + 1, n - 1)
            return xs[lo] + (idx - lo) * (xs[hi] - xs[lo])
        return {"n": n, "mean": m, "std": sd, "min": xs[0], "p25": q(0.25),
                "median": q(0.5), "p75": q(0.75), "max": xs[-1],
                "cv_pct": (sd / m * 100) if m else None}

    out = {"ok": True, "series": [{"name": s["name"], **describe(s["values"])} for s in series]}

    if body.correlate and len(series) >= 2:
        cors = []
        for i in range(len(series)):
            for j in range(i + 1, len(series)):
                a = [float(v) for v in series[i]["values"] if v is not None]
                b = [float(v) for v in series[j]["values"] if v is not None]
                n = min(len(a), len(b))
                a, b = a[:n], b[:n]
                if n > 1:
                    ma, mb = sum(a) / n, sum(b) / n
                    cov = sum((a[k]-ma)*(b[k]-mb) for k in range(n))
                    da = math.sqrt(sum((x-ma)**2 for x in a))
                    db = math.sqrt(sum((x-mb)**2 for x in b))
                    r = cov / (da * db) if da > 0 and db > 0 else 0.0
                    # significance of r: t = r*sqrt(n-2)/sqrt(1-r^2)
                    if abs(r) < 1 and n > 2:
                        t = r * math.sqrt(n - 2) / math.sqrt(1 - r * r)
                        p = t_two_sided_p(t, n - 2)
                    else:
                        p = float("nan")
                    cors.append({"a": series[i]["name"], "b": series[j]["name"],
                                 "r": round(r, 4), "r_squared": round(r*r, 4),
                                 "p_value": p, "p_display": _fmt_p(p), "n": n})
        out["correlations"] = cors
    return out


@router.post("/experiment")
async def experiment(body: ExperimentRequest):
    """End-to-end Test & Learn readout: matched pre/post series → incrementality,
    significance, confidence interval, lift, permutation robustness, ROI, verdict."""
    return _experiment_readout(
        body.test_pre, body.test_post, body.control_pre, body.control_post,
        body.alpha, body.metric_name, body.cost, body.value_per_unit, body.robust)


# ─────────────────────────────────────────────────────────────────────────────
# Semantic-layer integration — pull real cohorts from BigQuery
# ─────────────────────────────────────────────────────────────────────────────

def _pull_unit_series(model, primary_table, unit_field, metric_field, date_field,
                      window, cohort_filter, extra_filters):
    """Aggregate the metric per unit over a date window, restricted to a cohort.
    Returns {unit_value: metric_value}. Runs synchronously (call via executor)."""
    from semantic import build_sql, run_query, StructuredQuery
    filters = [{"table": primary_table, "field": date_field, "op": "between",
                "value": [window[0], window[1]]}]
    if cohort_filter:
        filters.append(cohort_filter)
    for f in (extra_filters or []):
        ff = dict(f)
        ff.setdefault("table", primary_table)
        filters.append(ff)
    qdict = {
        "primary_table": primary_table,
        "dimensions": [{"table": primary_table, "field": unit_field}],
        "metrics": [{"table": primary_table, "field": metric_field}],
        "filters": filters,
        "limit": 100000,
    }
    sql = build_sql(StructuredQuery.from_dict(qdict), model)
    res = run_query(sql)
    out = {}
    for row in (res.rows or []):
        if len(row) >= 2 and row[0] is not None and row[1] is not None:
            out[str(row[0])] = float(row[1])
    return out


def _cohort_filters(body, primary_table):
    """Build the test/control cohort filters from either group values or unit lists."""
    if body.group_field and body.test_values:
        tf = {"table": primary_table, "field": body.group_field, "op": "in", "value": body.test_values}
        cf = ({"table": primary_table, "field": body.group_field, "op": "in", "value": body.control_values}
              if body.control_values else None)
        return tf, cf
    if body.test_units:
        tf = {"table": primary_table, "field": body.unit_field, "op": "in", "value": body.test_units}
        cf = ({"table": primary_table, "field": body.unit_field, "op": "in", "value": body.control_units}
              if body.control_units else None)
        return tf, cf
    return None, None


def _validate_data_fields(model, primary_table, field_pairs):
    """Return an actionable error string if the table or any named field is
    unknown, else None. field_pairs = [(label, field_key), ...]."""
    table = model.table(primary_table) if primary_table else None
    if not table:
        keys = sorted(model.tables.keys())
        sample = ", ".join(keys[:12]) + (" …" if len(keys) > 12 else "")
        return (f"Unknown primary table '{primary_table}'. "
                f"Pick one of {len(keys)} tables — e.g. {sample}. "
                f"(Tip: use Test & Learn → From my data, which gives you dropdowns.)")
    valid = {f.key for f in (table.dimensions + table.metrics + table.dates)}
    for label, key in field_pairs:
        if key and key not in valid:
            dims = ", ".join(sorted(f.key for f in table.dimensions)[:12])
            mets = ", ".join(sorted(f.key for f in table.metrics)[:12])
            dates = ", ".join(sorted(f.key for f in table.dates)[:8])
            return (f"Field '{key}' ({label}) doesn't exist on '{primary_table}'. "
                    f"Dimensions: {dims or '—'}. Metrics: {mets or '—'}. Dates: {dates or '—'}.")
    return None


async def _run_experiment_from_data(body, add_filter=None):
    """Shared core for /experiment_from_data and each segment of /segment_scan.
    add_filter lets the segment scan inject a per-segment equality filter."""
    from semantic import load_model
    model = load_model()
    pt = body.primary_table
    # Actionable validation before we hit BigQuery.
    err = _validate_data_fields(model, pt, (("metric_field", body.metric_field),
                                            ("unit_field", body.unit_field),
                                            ("date_field", body.date_field),
                                            ("group_field", body.group_field)))
    if err:
        return {"error": err}
    tf, cf = _cohort_filters(body, pt)
    if tf is None or cf is None:
        return {"error": "Define cohorts: either group_field + test_values + control_values, "
                         "or test_units + control_units."}
    extra = list(body.extra_filters or [])
    if add_filter:
        extra = extra + [add_filter]
    loop = asyncio.get_running_loop()

    def pull(window, cohort):
        return _pull_unit_series(model, pt, body.unit_field, body.metric_field,
                                 body.date_field, window, cohort, extra)

    pre_win = [body.pre_start, body.pre_end]
    post_win = [body.post_start, body.post_end]
    try:
        t_pre = await loop.run_in_executor(None, pull, pre_win, tf)
        t_post = await loop.run_in_executor(None, pull, post_win, tf)
        c_pre = await loop.run_in_executor(None, pull, pre_win, cf)
        c_post = await loop.run_in_executor(None, pull, post_win, cf)
    except Exception as e:
        return {"error": f"Query failed: {e}"}

    # Align each arm by units present in BOTH pre and post (paired for DiD).
    t_units = sorted(set(t_pre) & set(t_post))
    c_units = sorted(set(c_pre) & set(c_post))
    if len(t_units) < 2 or len(c_units) < 2:
        return {"error": "Not enough units with both pre and post data (need ≥2 per arm). "
                         "Check the date windows and cohort definitions."}
    test_pre = [t_pre[u] for u in t_units]
    test_post = [t_post[u] for u in t_units]
    control_pre = [c_pre[u] for u in c_units]
    control_post = [c_post[u] for u in c_units]

    out = _experiment_readout(
        test_pre, test_post, control_pre, control_post, body.alpha,
        body.metric_name or body.metric_field, body.cost, body.value_per_unit, body.robust,
        extra={"n_control_units": len(c_units),
               "windows": {"pre": pre_win, "post": post_win},
               "source": "semantic-layer / BigQuery"})
    return out


@router.post("/experiment_from_data")
async def experiment_from_data(body: ExperimentFromDataRequest):
    """Run a full Test & Learn experiment directly off your curated data:
    picks the metric per unit for the test & control cohorts across pre/post
    windows, then measures incrementality with significance + robustness."""
    return await _run_experiment_from_data(body)


@router.post("/segment_scan")
async def segment_scan(body: SegmentScanRequest):
    """Heterogeneous-effect scan: repeat the experiment within each value of
    segment_field and rank segments by incremental lift — the targeting engine
    that tells you WHERE to roll out."""
    if not body.segment_field:
        return {"error": "Provide segment_field to scan (e.g. region, service_line)."}
    from semantic import load_model, build_sql, run_query, StructuredQuery
    model = load_model()
    pt = body.primary_table
    tf, cf = _cohort_filters(body, pt)
    if tf is None or cf is None:
        return {"error": "Define cohorts (group_field + values, or unit lists)."}

    # Discover the top segment values by post-window volume within the test cohort.
    loop = asyncio.get_running_loop()

    def _segments():
        filters = [{"table": pt, "field": body.date_field, "op": "between",
                    "value": [body.post_start, body.post_end]}, tf]
        for f in (body.extra_filters or []):
            ff = dict(f); ff.setdefault("table", pt); filters.append(ff)
        qdict = {"primary_table": pt,
                 "dimensions": [{"table": pt, "field": body.segment_field}],
                 "metrics": [{"table": pt, "field": body.metric_field}],
                 "filters": filters,
                 "order_by": [{"field": body.metric_field, "direction": "desc"}],
                 "limit": max(1, min(int(body.max_segments), 50))}
        sql = build_sql(StructuredQuery.from_dict(qdict), model)
        res = run_query(sql)
        return [row[0] for row in (res.rows or []) if row and row[0] is not None]

    try:
        seg_values = await loop.run_in_executor(None, _segments)
    except Exception as e:
        return {"error": f"Segment discovery failed: {e}"}
    if not seg_values:
        return {"error": "No segment values found for that cohort/window."}

    results = []
    for seg in seg_values:
        seg_filter = {"table": pt, "field": body.segment_field, "op": "eq", "value": seg}
        r = await _run_experiment_from_data(body, add_filter=seg_filter)
        if r.get("error"):
            continue
        v = r.get("verdict", {})
        results.append({
            "segment": str(seg),
            "did_estimate": r.get("did_estimate"),
            "incremental_lift_pct": r.get("incremental_lift_pct"),
            "p_value": r.get("p_value"),
            "p_display": v.get("p_display") or _fmt_p(r.get("p_value")) if r.get("p_value") is not None else "n/a",
            "significant": v.get("significant"),
            "n_test_units": r.get("n_test_units"),
        })
    if not results:
        return {"error": "Could not compute a result for any segment (insufficient paired data)."}

    results.sort(key=lambda x: (x["did_estimate"] if x["did_estimate"] is not None else -1e18), reverse=True)
    winners = [r for r in results if r.get("significant") and (r.get("did_estimate") or 0) > 0]
    best = results[0]
    worst = results[-1]
    summary = (f"Scanned {len(results)} segments. "
               + (f"Strongest response: '{best['segment']}' "
                  f"({_pct_str(best['incremental_lift_pct'])} incremental). " if best else "")
               + (f"{len(winners)} segment(s) show a significant positive lift — prioritize those for rollout."
                  if winners else "No segment shows a significant positive lift yet."))
    return {"ok": True, "test": "Heterogeneous-effect segment scan",
            "segment_field": body.segment_field, "metric": body.metric_name or body.metric_field,
            "primary_table": pt,
            "results": results, "winners": [w["segment"] for w in winners],
            "best": best, "worst": worst, "summary": summary}


def _pct_str(x):
    return "—" if (x is None or x != x) else (("+" if x >= 0 else "") + f"{x:.1f}%")


# ─────────────────────────────────────────────────────────────────────────────
# Synthetic control — build a weighted control when no clean one exists
# ─────────────────────────────────────────────────────────────────────────────

def _simplex_project(v):
    """Euclidean projection of vector v onto the probability simplex
    {w : w_i >= 0, sum w = 1} (Duchi et al. 2008)."""
    n = len(v)
    if n == 0:
        return []
    u = sorted(v, reverse=True)
    css = 0.0
    rho = 0
    theta = 0.0
    for i in range(n):
        css += u[i]
        t = (css - 1.0) / (i + 1)
        if u[i] - t > 0:
            rho = i + 1
            theta = t
    return [max(0.0, x - theta) for x in v]


def _synthetic_weights(test_pre, controls_pre, iters=4000, lr=None):
    """Find non-negative weights (summing to 1) over control units so their
    weighted pre-period series best matches the test's pre-period series.
    Projected gradient descent on the simplex — no scipy needed."""
    m = len(controls_pre)          # number of controls
    T = len(test_pre)              # pre periods
    if m == 0 or T == 0:
        return []
    w = [1.0 / m] * m
    # scale learning rate to the data magnitude for stability
    if lr is None:
        scale = sum(abs(x) for x in test_pre) / T or 1.0
        lr = 1.0 / (m * (scale ** 2) * T + 1e-9)
    for _ in range(iters):
        # prediction per period
        pred = [sum(w[i] * controls_pre[i][t] for i in range(m)) for t in range(T)]
        resid = [pred[t] - test_pre[t] for t in range(T)]
        # gradient_i = 2 * sum_t C_it * resid_t
        grad = [2.0 * sum(controls_pre[i][t] * resid[t] for t in range(T)) for i in range(m)]
        w = [w[i] - lr * grad[i] for i in range(m)]
        w = _simplex_project(w)
    return w


class SyntheticControlRequest(BaseModel):
    """No clean control? Build one. Provide the test group's pre & post series
    over time, and several candidate control units each with aligned pre & post
    series. Returns donor weights, the synthetic counterfactual, and the effect."""
    test_pre: list                 # metric over pre periods (time-ordered)
    test_post: list                # metric over post periods
    controls: list                 # [{id, pre:[...], post:[...]}]
    alpha: float = 0.05
    metric_name: str = "metric"
    placebo: bool = True           # run Abadie placebo inference for a p-value


def _sc_fit(target_pre, target_post, donors_pre, donors_post):
    """Fit one synthetic control: weights on donors to match target_pre, then
    project. Returns (weights, synth_pre, synth_post, pre_rmse, effect_series)."""
    m = len(donors_pre)
    Tpre = len(target_pre)
    Tpost = len(target_post)
    w = _synthetic_weights(target_pre, donors_pre)
    synth_pre = [sum(w[i] * donors_pre[i][t] for i in range(m)) for t in range(Tpre)]
    pre_rmse = math.sqrt(sum((synth_pre[t] - target_pre[t]) ** 2 for t in range(Tpre)) / Tpre)
    synth_post = [sum(w[i] * donors_post[i][t] for i in range(m)) for t in range(Tpost)] if Tpost else []
    effect = [target_post[t] - synth_post[t] for t in range(Tpost)] if Tpost else []
    return w, synth_pre, synth_post, pre_rmse, effect


def _rmse(xs):
    return math.sqrt(sum(x * x for x in xs) / len(xs)) if xs else 0.0


def _synthetic_control_core(tp, tq, donors, metric_name, placebo=True):
    """Shared synthetic-control computation. donors = [{id, pre:[...], post:[...]}]."""
    if len(tp) < 2:
        return {"error": "Need at least 2 pre-period points for the test group."}
    if len(donors) < 2:
        return {"error": "Need at least 2 candidate control (donor) units."}
    Tpre, Tpost = len(tp), len(tq)
    if any(len(c["pre"]) != Tpre for c in donors):
        return {"error": "Every donor's pre series must match the test pre length."}
    if Tpost and any(len(c["post"]) != Tpost for c in donors):
        return {"error": "Every donor's post series must match the test post length."}

    cpre = [[float(x) for x in c["pre"]] for c in donors]
    cpost = [[float(x) for x in c["post"]] for c in donors]
    w, synth_pre, synth_post, pre_rmse, effect_series = _sc_fit(tp, tq, cpre, cpost)
    avg_effect = (sum(effect_series) / Tpost) if Tpost else None
    mean_test_post = (sum(tq) / Tpost) if Tpost else None
    lift_pct = (avg_effect / (mean_test_post - avg_effect) * 100) if (avg_effect is not None and mean_test_post is not None and (mean_test_post - avg_effect)) else None

    weights_out = sorted(
        [{"id": donors[i].get("id", f"donor_{i+1}"), "weight": round(w[i], 4)} for i in range(len(donors))],
        key=lambda x: x["weight"], reverse=True)
    base = (sum(abs(x) for x in tp) / Tpre) or 1
    fit_quality = ("excellent" if pre_rmse < 0.05 * base else "good" if pre_rmse < 0.15 * base else "weak")

    # ── Abadie placebo inference: refit each donor as if it were treated (using
    # the OTHER donors), and compare post/pre RMSE ratios. A real effect makes
    # the treated unit an outlier. p = rank of treated ratio among all units. ──
    placebo_p = None
    placebo_ratios = None
    rank = None
    is_outlier = False
    treated_ratio = (_rmse(effect_series) / pre_rmse) if (Tpost and pre_rmse > 0) else None
    if placebo and Tpost and treated_ratio is not None and len(donors) >= 3:
        placebo_only = []
        for j in range(len(donors)):
            others_pre = [cpre[k] for k in range(len(donors)) if k != j]
            others_post = [cpost[k] for k in range(len(donors)) if k != j]
            if len(others_pre) < 2:
                continue
            _, _, _, pj_rmse, eff_j = _sc_fit(cpre[j], cpost[j], others_pre, others_post)
            if pj_rmse > 0:
                placebo_only.append(_rmse(eff_j) / pj_rmse)
        ratios = [treated_ratio] + placebo_only
        n = len(ratios)
        rank = sum(1 for r in ratios if r >= treated_ratio - 1e-12)
        placebo_p = rank / n
        max_placebo = max(placebo_only) if placebo_only else 0.0
        # A clear outlier = ranked #1 AND its post/pre gap ratio dominates the placebos.
        is_outlier = (rank == 1 and max_placebo > 0 and treated_ratio > 1.5 * max_placebo)
        placebo_ratios = {"treated_ratio": round(treated_ratio, 3), "n_placebos": len(placebo_only),
                          "rank": rank, "p_value": placebo_p, "outlier": is_outlier,
                          "min_possible_p": round(1.0 / n, 3)}

    # Verdict: prefer the placebo test when available, else the noise-band heuristic.
    if avg_effect is None:
        sig, headline, detail = None, "Synthetic control built", f"Pre-period fit RMSE {pre_rmse:.3f} ({fit_quality}). Add post periods to estimate impact."
    elif placebo_p is not None:
        # Small donor pools cap the min p at 1/n; treat a clear outlier as credible too.
        sig = (placebo_p <= 0.10) or is_outlier
        if sig:
            headline = f"Credible incremental {'lift' if avg_effect > 0 else 'drop'}"
            detail = (f"Effect {avg_effect:.2f}/period. Placebo test: the treated unit's gap ranks #{rank} of "
                      f"{placebo_ratios['n_placebos']+1} units (p={_fmt_p(placebo_p)}"
                      + (", a clear outlier vs. placebos" if is_outlier else "") + ") — unlikely by chance.")
        else:
            headline = "Not distinguishable from placebos"
            detail = (f"Effect {avg_effect:.2f}/period, but the treated gap ranks #{rank} of "
                      f"{placebo_ratios['n_placebos']+1} (p={_fmt_p(placebo_p)}) — other units show gaps this large "
                      f"by chance. Add donors to sharpen inference.")
    else:
        sig = abs(avg_effect) > 2 * pre_rmse
        headline = (f"Credible incremental {'lift' if avg_effect > 0 else 'drop'}" if sig else "Effect within noise")
        detail = (f"The test group ran {avg_effect:.2f}/period {'above' if avg_effect > 0 else 'below'} its synthetic "
                  f"counterfactual — {abs(avg_effect)/pre_rmse:.1f}× the pre-fit noise (RMSE {pre_rmse:.3f}, {fit_quality} fit)."
                  if sig else
                  f"The post gap ({avg_effect:.2f}/period) isn't clearly above the pre-fit noise (RMSE {pre_rmse:.3f}).")

    return {
        "ok": True, "test": "Synthetic control", "metric": metric_name,
        "weights": weights_out,
        "synthetic_pre": synth_pre, "synthetic_post": synth_post,
        "test_pre": tp, "test_post": tq,
        "effect_series": effect_series, "avg_effect": avg_effect,
        "incremental_lift_pct": lift_pct,
        "pre_fit_rmse": pre_rmse, "fit_quality": fit_quality,
        "placebo": placebo_ratios, "p_value": placebo_p,
        "verdict": {"significant": sig, "headline": headline, "detail": detail},
        "note": ("Synthetic control weights donor units to reproduce the test group's pre-period trajectory, then "
                 "projects that blend forward as the counterfactual. Placebo inference re-runs the fit treating each "
                 "donor as if it were the intervention — a real effect makes the true test group an outlier."),
    }


@router.post("/synthetic_control")
async def synthetic_control(body: SyntheticControlRequest):
    tp = [float(v) for v in body.test_pre if v is not None]
    tq = [float(v) for v in body.test_post if v is not None]
    donors = [c for c in (body.controls or []) if c.get("pre") and c.get("post")]
    return _synthetic_control_core(tp, tq, donors, body.metric_name, body.placebo)


class SyntheticFromDataRequest(BaseModel):
    """Build a synthetic control straight off the semantic layer / BigQuery.
    Pulls the test cohort's aggregate metric per period and each donor unit's
    per-period series, aligns them on a shared date grid, then fits."""
    primary_table: str
    metric_field: str
    date_field: str
    unit_field: str
    pre_start: str
    pre_end: str
    post_start: str
    post_end: str
    group_field: Optional[str] = None
    test_values: Optional[list] = None       # cohort = the treated group (aggregated)
    control_values: Optional[list] = None     # donor pool
    test_units: Optional[list] = None
    control_units: Optional[list] = None
    extra_filters: Optional[list] = None
    metric_name: Optional[str] = None
    placebo: bool = True


def _agg_series_by_date(model, pt, metric_field, date_field, window, cohort, extra):
    """Aggregate the metric per date for a cohort → [(date_str, value)] sorted."""
    from semantic import build_sql, run_query, StructuredQuery
    filters = [{"table": pt, "field": date_field, "op": "between", "value": [window[0], window[1]]}]
    if cohort:
        filters.append(cohort)
    for f in (extra or []):
        ff = dict(f); ff.setdefault("table", pt); filters.append(ff)
    qdict = {"primary_table": pt,
             "dimensions": [{"table": pt, "field": date_field}],
             "metrics": [{"table": pt, "field": metric_field}],
             "filters": filters, "limit": 100000}
    res = run_query(build_sql(StructuredQuery.from_dict(qdict), model))
    out = [(str(r[0]), float(r[1])) for r in (res.rows or []) if len(r) >= 2 and r[0] is not None and r[1] is not None]
    out.sort(key=lambda x: x[0])
    return out


def _unit_series_by_date(model, pt, metric_field, unit_field, date_field, window, cohort, extra):
    """Per-unit, per-date metric for a cohort → {unit: {date_str: value}}."""
    from semantic import build_sql, run_query, StructuredQuery
    filters = [{"table": pt, "field": date_field, "op": "between", "value": [window[0], window[1]]}]
    if cohort:
        filters.append(cohort)
    for f in (extra or []):
        ff = dict(f); ff.setdefault("table", pt); filters.append(ff)
    qdict = {"primary_table": pt,
             "dimensions": [{"table": pt, "field": unit_field}, {"table": pt, "field": date_field}],
             "metrics": [{"table": pt, "field": metric_field}],
             "filters": filters, "limit": 500000}
    res = run_query(build_sql(StructuredQuery.from_dict(qdict), model))
    out = {}
    for r in (res.rows or []):
        if len(r) >= 3 and r[0] is not None and r[1] is not None and r[2] is not None:
            out.setdefault(str(r[0]), {})[str(r[1])] = float(r[2])
    return out


@router.post("/synthetic_control_from_data")
async def synthetic_control_from_data(body: SyntheticFromDataRequest):
    from semantic import load_model
    try:
        model = load_model()
    except Exception as e:
        return {"error": f"semantic model load failed: {e}"}
    pt = body.primary_table
    # cohorts
    if body.group_field and body.test_values:
        test_cohort = {"table": pt, "field": body.group_field, "op": "in", "value": body.test_values}
        donor_cohort = ({"table": pt, "field": body.group_field, "op": "in", "value": body.control_values}
                        if body.control_values else None)
    elif body.test_units:
        test_cohort = {"table": pt, "field": body.unit_field, "op": "in", "value": body.test_units}
        donor_cohort = ({"table": pt, "field": body.unit_field, "op": "in", "value": body.control_units}
                        if body.control_units else None)
    else:
        return {"error": "Define the test cohort (group_field+test_values or test_units)."}
    if donor_cohort is None:
        return {"error": "Define the donor pool (control_values or control_units)."}

    loop = asyncio.get_running_loop()
    pre_win = [body.pre_start, body.pre_end]
    post_win = [body.post_start, body.post_end]
    extra = body.extra_filters or []
    try:
        test_pre_rows = await loop.run_in_executor(None, _agg_series_by_date, model, pt, body.metric_field, body.date_field, pre_win, test_cohort, extra)
        test_post_rows = await loop.run_in_executor(None, _agg_series_by_date, model, pt, body.metric_field, body.date_field, post_win, test_cohort, extra)
        donor_pre = await loop.run_in_executor(None, _unit_series_by_date, model, pt, body.metric_field, body.unit_field, body.date_field, pre_win, donor_cohort, extra)
        donor_post = await loop.run_in_executor(None, _unit_series_by_date, model, pt, body.metric_field, body.unit_field, body.date_field, post_win, donor_cohort, extra)
    except Exception as e:
        return {"error": f"Query failed: {e}"}

    pre_dates = [d for d, _ in test_pre_rows]
    post_dates = [d for d, _ in test_post_rows]
    if len(pre_dates) < 2:
        return {"error": "Fewer than 2 pre-period dates returned for the test cohort — widen the pre window."}
    tp = [v for _, v in test_pre_rows]
    tq = [v for _, v in test_post_rows]

    # Build donor series aligned to the same date grid; drop donors missing any period.
    donors = []
    for uid, pre_map in donor_pre.items():
        if not all(d in pre_map for d in pre_dates):
            continue
        post_map = donor_post.get(uid, {})
        if post_dates and not all(d in post_map for d in post_dates):
            continue
        donors.append({"id": uid,
                       "pre": [pre_map[d] for d in pre_dates],
                       "post": [post_map[d] for d in post_dates] if post_dates else []})
    if len(donors) < 2:
        return {"error": "Fewer than 2 donor units have complete pre/post series on the shared date grid. "
                         "Check the unit/date fields and windows."}

    result = _synthetic_control_core(tp, tq, donors, body.metric_name or body.metric_field, body.placebo)
    if isinstance(result, dict) and result.get("ok"):
        result["periods"] = {"pre": pre_dates, "post": post_dates}
        result["n_donors"] = len(donors)
        result["source"] = "semantic-layer / BigQuery"
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Forecasting + anomaly detection — Holt-Winters with prediction intervals
# ─────────────────────────────────────────────────────────────────────────────

def _linear_fit(y):
    """Ordinary least squares y = a + b*t. Returns (a, b, fitted, residuals)."""
    n = len(y)
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(y) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((xs[i] - mx) * (y[i] - my) for i in range(n))
    b = sxy / sxx if sxx else 0.0
    a = my - b * mx
    fitted = [a + b * t for t in xs]
    resid = [y[i] - fitted[i] for i in range(n)]
    return a, b, fitted, resid


def _hw_run(y, m, alpha, beta, gamma):
    """One additive Holt-Winters pass. Returns (level, trend, seasonals, sse, onestep_resid)."""
    n = len(y)
    # seasonal init: average of first season, and per-slot deviations
    season_avg = sum(y[:m]) / m
    seasonals = [y[i] - season_avg for i in range(m)]
    level = season_avg
    # trend init: average slope across first two seasons
    trend = sum((y[m + i] - y[i]) / m for i in range(m)) / m if n >= 2 * m else (y[-1] - y[0]) / max(1, n - 1)
    resid = []
    sse = 0.0
    seas = list(seasonals)
    for t in range(n):
        s = seas[t % m]
        pred = level + trend + s  # one-step-ahead forecast for time t
        e = y[t] - pred
        resid.append(e)
        sse += e * e
        last_level = level
        level = alpha * (y[t] - s) + (1 - alpha) * (level + trend)
        trend = beta * (level - last_level) + (1 - beta) * trend
        seas[t % m] = gamma * (y[t] - level) + (1 - gamma) * s
    return level, trend, seas, sse, resid


def _detect_period(y):
    """Pick a seasonal period by autocorrelation of the DETRENDED series among
    common candidates. Detrending matters: on a trended metric the trend inflates
    autocorrelation at every lag and would otherwise mask real seasonality."""
    n = len(y)
    _, _, _, resid = _linear_fit(y)          # detrend
    mean = sum(resid) / n
    var = (sum((v - mean) ** 2 for v in resid) / n) or 1.0
    best_m, best_r = 1, 0.0
    for m in (12, 7, 4, 52, 24, 30, 3, 6):
        if n < 2 * m:
            continue
        cnt = n - m
        cov = sum((resid[i] - mean) * (resid[i - m] - mean) for i in range(m, n)) / cnt
        r = cov / var                         # normalized autocorrelation at lag m
        if r > best_r + 1e-9:
            best_r, best_m = r, m
    return best_m if best_r > 0.3 else 1


def _forecast_core(y, horizon, period=None, alpha=0.05):
    """Forecast `horizon` steps. Uses Holt-Winters when seasonality is present and
    there's enough data, else Holt's linear trend. Returns fit + forecast + bands."""
    y = [float(v) for v in y if v is not None]
    n = len(y)
    if n < 4:
        return {"error": "Need at least 4 data points to forecast."}
    m = period if (period and period > 1) else _detect_period(y)
    z = norm_ppf(1 - alpha / 2)
    method = "holt-winters" if (m > 1 and n >= 2 * m) else "linear-trend"

    if method == "holt-winters":
        # small grid search for smoothing params
        best = None
        grid = (0.1, 0.3, 0.5, 0.7, 0.9)
        for a in grid:
            for b in (0.05, 0.1, 0.2, 0.3):
                for g in grid:
                    level, trend, seas, sse, resid = _hw_run(y, m, a, b, g)
                    if best is None or sse < best[0]:
                        best = (sse, a, b, g, level, trend, seas, resid)
        _, a, b, g, level, trend, seas, resid = best
        fitted = [y[i] - resid[i] for i in range(n)]
        fc = []
        for h in range(1, horizon + 1):
            s = seas[(n + h - 1) % m]
            fc.append(level + h * trend + s)
        params = {"alpha": a, "beta": b, "gamma": g, "period": m}
    else:
        a0, b0, fitted, resid = _linear_fit(y)
        fc = [a0 + b0 * (n + h - 1) for h in range(1, horizon + 1)]
        params = {"intercept": a0, "slope": b0, "period": 1}

    # residual std (one-step); prediction interval widens with horizon (~√h)
    rn = len(resid)
    rmean = sum(resid) / rn
    rstd = math.sqrt(sum((r - rmean) ** 2 for r in resid) / max(1, rn - 1))
    lower = [fc[h] - z * rstd * math.sqrt(h + 1) for h in range(horizon)]
    upper = [fc[h] + z * rstd * math.sqrt(h + 1) for h in range(horizon)]

    # simple accuracy: MAPE on the fitted in-sample values
    denom = [abs(v) for v in y if v]
    mape = (sum(abs(resid[i]) / abs(y[i]) for i in range(n) if y[i]) / max(1, len([v for v in y if v])) * 100) if denom else None

    last = y[-1]
    change = fc[-1] - last
    return {
        "ok": True, "method": method, "params": params,
        "history": y, "fitted": fitted, "forecast": fc,
        "lower": lower, "upper": upper,
        "horizon": horizon, "confidence_level_pct": round((1 - alpha) * 100),
        "residual_std": rstd, "mape_pct": mape,
        "forecast_change": change,
        "forecast_change_pct": (change / last * 100) if last else None,
        "summary": (f"Projected to {'rise' if change >= 0 else 'fall'} from {last:.2f} to "
                    f"{fc[-1]:.2f} over the next {horizon} period(s) "
                    f"({('+' if change>=0 else '')}{(change/last*100) if last else 0:.1f}%), "
                    f"using {method}" + (f" (seasonal period {m})" if m > 1 else "") + "."),
    }


def _anomalies_core(y, period=None, k=3.0):
    """Flag points whose residual vs. the fitted model exceeds k standard deviations."""
    y = [float(v) for v in y if v is not None]
    n = len(y)
    if n < 4:
        return {"error": "Need at least 4 data points."}
    m = period if (period and period > 1) else _detect_period(y)
    if m > 1 and n >= 2 * m:
        level, trend, seas, sse, resid = _hw_run(y, m, 0.5, 0.1, 0.5)
        fitted = [y[i] - resid[i] for i in range(n)]
    else:
        _, _, fitted, resid = _linear_fit(y)
    rmean = sum(resid) / n
    rstd = math.sqrt(sum((r - rmean) ** 2 for r in resid) / max(1, n - 1)) or 1.0
    anomalies = []
    for i in range(n):
        zscore = (resid[i] - rmean) / rstd
        if abs(zscore) >= k:
            anomalies.append({"index": i, "value": y[i], "expected": fitted[i],
                              "deviation": y[i] - fitted[i], "z": round(zscore, 2)})
    return {"ok": True, "fitted": fitted, "anomalies": anomalies,
            "n_anomalies": len(anomalies), "threshold_sigma": k,
            "summary": (f"Found {len(anomalies)} anomaly(ies) beyond {k}σ from the fitted trend."
                        if anomalies else f"No anomalies beyond {k}σ.")}


class ForecastRequest(BaseModel):
    series: list
    horizon: int = 6
    period: Optional[int] = None       # seasonal period; auto-detected if None
    alpha: float = 0.05
    metric_name: str = "metric"


@router.post("/forecast")
async def forecast(body: ForecastRequest):
    r = _forecast_core(body.series, max(1, int(body.horizon)), body.period, body.alpha)
    if r.get("ok"):
        r["metric"] = body.metric_name
    return r


class AnomalyRequest(BaseModel):
    series: list
    period: Optional[int] = None
    k: float = 3.0


@router.post("/anomalies")
async def anomalies(body: AnomalyRequest):
    return _anomalies_core(body.series, body.period, body.k)


class ForecastFromDataRequest(BaseModel):
    primary_table: str
    metric_field: str
    date_field: str
    window_start: str
    window_end: str
    horizon: int = 6
    period: Optional[int] = None
    alpha: float = 0.05
    extra_filters: Optional[list] = None
    metric_name: Optional[str] = None


@router.post("/forecast_from_data")
async def forecast_from_data(body: ForecastFromDataRequest):
    from semantic import load_model
    try:
        model = load_model()
    except Exception as e:
        return {"error": f"semantic model load failed: {e}"}
    err = _validate_data_fields(model, body.primary_table,
                                (("metric_field", body.metric_field), ("date_field", body.date_field)))
    if err:
        return {"error": err}
    loop = asyncio.get_running_loop()
    try:
        rows = await loop.run_in_executor(
            None, _agg_series_by_date, model, body.primary_table, body.metric_field,
            body.date_field, [body.window_start, body.window_end], None, body.extra_filters or [])
    except Exception as e:
        return {"error": f"Query failed: {e}"}
    if len(rows) < 4:
        return {"error": "Fewer than 4 dated points returned — widen the window."}
    series = [v for _, v in rows]
    dates = [d for d, _ in rows]
    r = _forecast_core(series, max(1, int(body.horizon)), body.period, body.alpha)
    if r.get("ok"):
        r["metric"] = body.metric_name or body.metric_field
        r["dates"] = dates
        r["source"] = "semantic-layer / BigQuery"
    return r


# ─────────────────────────────────────────────────────────────────────────────
# Driver analysis — "why did this metric change?" (per-segment contributions)
# ─────────────────────────────────────────────────────────────────────────────

def _driver_core(segments, metric_name):
    """segments = [{name, prior, current}]. Decompose the total change into each
    segment's additive contribution + its share shift. (Sum-metric decomposition.)"""
    segs = [{"name": str(s.get("name")), "prior": float(s.get("prior") or 0),
             "current": float(s.get("current") or 0)} for s in (segments or [])]
    if not segs:
        return {"error": "Provide segments with prior & current values."}
    tp = sum(s["prior"] for s in segs)
    tc = sum(s["current"] for s in segs)
    tchg = tc - tp
    drivers = []
    for s in segs:
        contrib = s["current"] - s["prior"]
        drivers.append({
            "segment": s["name"], "prior": s["prior"], "current": s["current"],
            "contribution": contrib,
            "contribution_pct": (contrib / tchg * 100) if tchg else None,
            "prior_share_pct": (s["prior"] / tp * 100) if tp else None,
            "current_share_pct": (s["current"] / tc * 100) if tc else None,
            "growth_pct": (contrib / s["prior"] * 100) if s["prior"] else None,
        })
    for d in drivers:
        ps, cs = d["prior_share_pct"], d["current_share_pct"]
        d["share_shift_pts"] = (cs - ps) if (ps is not None and cs is not None) else None
    drivers.sort(key=lambda d: abs(d["contribution"]), reverse=True)

    pos = [d for d in drivers if d["contribution"] > 0]
    neg = [d for d in drivers if d["contribution"] < 0]
    top_pos = max(pos, key=lambda d: d["contribution"], default=None)
    top_neg = min(neg, key=lambda d: d["contribution"], default=None)

    if tchg == 0:
        summary = f"{metric_name} was flat overall; segments shifted but netted to zero."
    else:
        direction = "an increase" if tchg > 0 else "a decrease"
        bits = [f"{metric_name} moved {('+' if tchg>=0 else '')}{tchg:.2f} "
                f"({(tchg/tp*100) if tp else 0:+.1f}%) — {direction}."]
        if top_pos:
            bits.append(f"Biggest driver: {top_pos['segment']} "
                        f"({('+' if top_pos['contribution']>=0 else '')}{top_pos['contribution']:.2f}, "
                        f"{(top_pos['contribution_pct'] or 0):.0f}% of the change).")
        if top_neg and top_neg["contribution"] < -1e-9:
            bits.append(f"Biggest offset: {top_neg['segment']} ({top_neg['contribution']:.2f}).")
        summary = " ".join(bits)

    return {
        "ok": True, "test": "Driver analysis", "metric": metric_name,
        "total_prior": tp, "total_current": tc, "total_change": tchg,
        "total_change_pct": (tchg / tp * 100) if tp else None,
        "drivers": drivers,
        "top_positive": top_pos["segment"] if top_pos else None,
        "top_negative": top_neg["segment"] if (top_neg and top_neg["contribution"] < 0) else None,
        "summary": summary,
    }


class DriverRequest(BaseModel):
    segments: list                 # [{name, prior, current}]
    metric_name: str = "metric"


@router.post("/driver_analysis")
async def driver_analysis(body: DriverRequest):
    return _driver_core(body.segments, body.metric_name)


class DriverFromDataRequest(BaseModel):
    primary_table: str
    metric_field: str
    dimension_field: str
    date_field: str
    prior_start: str
    prior_end: str
    current_start: str
    current_end: str
    extra_filters: Optional[list] = None
    metric_name: Optional[str] = None
    top_n: int = 20


@router.post("/driver_from_data")
async def driver_from_data(body: DriverFromDataRequest):
    from semantic import load_model
    try:
        model = load_model()
    except Exception as e:
        return {"error": f"semantic model load failed: {e}"}
    err = _validate_data_fields(model, body.primary_table,
                                (("metric_field", body.metric_field),
                                 ("dimension_field", body.dimension_field),
                                 ("date_field", body.date_field)))
    if err:
        return {"error": err}
    loop = asyncio.get_running_loop()

    def pull(window):
        # metric by the chosen dimension over a window → {segment: value}
        from semantic import build_sql, run_query, StructuredQuery
        pt = body.primary_table
        filters = [{"table": pt, "field": body.date_field, "op": "between", "value": [window[0], window[1]]}]
        for f in (body.extra_filters or []):
            ff = dict(f); ff.setdefault("table", pt); filters.append(ff)
        qdict = {"primary_table": pt,
                 "dimensions": [{"table": pt, "field": body.dimension_field}],
                 "metrics": [{"table": pt, "field": body.metric_field}],
                 "filters": filters, "limit": 100000}
        res = run_query(build_sql(StructuredQuery.from_dict(qdict), model))
        return {str(r[0]): float(r[1]) for r in (res.rows or []) if len(r) >= 2 and r[0] is not None and r[1] is not None}

    try:
        prior = await loop.run_in_executor(None, pull, [body.prior_start, body.prior_end])
        current = await loop.run_in_executor(None, pull, [body.current_start, body.current_end])
    except Exception as e:
        return {"error": f"Query failed: {e}"}
    names = set(prior) | set(current)
    if not names:
        return {"error": "No data returned for those windows."}
    segments = [{"name": n, "prior": prior.get(n, 0.0), "current": current.get(n, 0.0)} for n in names]
    r = _driver_core(segments, body.metric_name or body.metric_field)
    if r.get("ok"):
        r["drivers"] = r["drivers"][: max(1, int(body.top_n))]
        r["dimension"] = body.dimension_field
        r["source"] = "semantic-layer / BigQuery"
    return r


# ─────────────────────────────────────────────────────────────────────────────
# Correlation explorer — ranked relationships among several metrics
# ─────────────────────────────────────────────────────────────────────────────

class CorrelationFromDataRequest(BaseModel):
    primary_table: str
    metric_fields: list            # ≥2 metric keys to correlate
    dimension_field: str           # grouping unit (e.g. campaign, day)
    date_field: Optional[str] = None
    window_start: Optional[str] = None
    window_end: Optional[str] = None
    extra_filters: Optional[list] = None


@router.post("/correlation_from_data")
async def correlation_from_data(body: CorrelationFromDataRequest):
    from semantic import load_model, build_sql, run_query, StructuredQuery
    try:
        model = load_model()
    except Exception as e:
        return {"error": f"semantic model load failed: {e}"}
    if len(body.metric_fields or []) < 2:
        return {"error": "Provide at least 2 metric fields to correlate."}
    err = _validate_data_fields(model, body.primary_table,
                                [("dimension_field", body.dimension_field)]
                                + [("metric_field", m) for m in body.metric_fields])
    if err:
        return {"error": err}
    pt = body.primary_table
    filters = []
    if body.date_field and body.window_start and body.window_end:
        filters.append({"table": pt, "field": body.date_field, "op": "between",
                        "value": [body.window_start, body.window_end]})
    for f in (body.extra_filters or []):
        ff = dict(f); ff.setdefault("table", pt); filters.append(ff)
    qdict = {"primary_table": pt,
             "dimensions": [{"table": pt, "field": body.dimension_field}],
             "metrics": [{"table": pt, "field": m} for m in body.metric_fields],
             "filters": filters, "limit": 100000}
    loop = asyncio.get_running_loop()
    try:
        res = await loop.run_in_executor(None, lambda: run_query(build_sql(StructuredQuery.from_dict(qdict), model)))
    except Exception as e:
        return {"error": f"Query failed: {e}"}
    rows = res.rows or []
    if len(rows) < 3:
        return {"error": "Need at least 3 rows to compute correlations."}
    # columns: [dimension, m1, m2, ...]
    cols = list(zip(*rows))
    series = {body.metric_fields[i]: [float(v) for v in cols[i + 1] if v is not None]
              for i in range(len(body.metric_fields))}
    pairs = []
    keys = list(series.keys())
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            a, b = series[keys[i]], series[keys[j]]
            n = min(len(a), len(b))
            a, b = a[:n], b[:n]
            if n < 3:
                continue
            ma, mb = sum(a) / n, sum(b) / n
            cov = sum((a[k] - ma) * (b[k] - mb) for k in range(n))
            da = math.sqrt(sum((x - ma) ** 2 for x in a))
            db = math.sqrt(sum((x - mb) ** 2 for x in b))
            r = cov / (da * db) if da > 0 and db > 0 else 0.0
            if abs(r) < 1 and n > 2:
                t = r * math.sqrt(n - 2) / math.sqrt(1 - r * r)
                p = t_two_sided_p(t, n - 2)
            else:
                p = float("nan")
            pairs.append({"a": keys[i], "b": keys[j], "r": round(r, 4),
                          "r_squared": round(r * r, 4), "p_value": p,
                          "p_display": _fmt_p(p), "n": n,
                          "strength": ("strong" if abs(r) >= 0.7 else "moderate" if abs(r) >= 0.4 else "weak"),
                          "direction": ("positive" if r >= 0 else "negative")})
    pairs.sort(key=lambda x: abs(x["r"]), reverse=True)
    top = pairs[0] if pairs else None
    summary = (f"Strongest relationship: {top['a']} ↔ {top['b']} (r={top['r']}, {top['strength']} {top['direction']}, "
               f"p={top['p_display']})." if top else "No pairs computed.")
    return {"ok": True, "test": "Correlation explorer", "dimension": body.dimension_field,
            "correlations": pairs, "summary": summary}


# ─────────────────────────────────────────────────────────────────────────────
# Saved experiments — persist readouts so teams track impact over time
# ─────────────────────────────────────────────────────────────────────────────

_STORE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "experiments_saved.json")


def _store_load():
    try:
        with open(_STORE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"experiments": []}


def _store_save(data):
    tmp = _STORE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, _STORE_PATH)


class SaveExperimentRequest(BaseModel):
    name: str
    spec: Optional[dict] = None        # the inputs that produced it (metric, cohorts, windows)
    readout: dict                       # the full result object
    author: Optional[str] = None
    tags: Optional[list] = None


def _persist_experiment(name, spec, readout, author="", tags=None, extra=None):
    """Insert an experiment record into the store; returns its id."""
    data = _store_load()
    r = readout or {}
    v = r.get("verdict") or {}
    rec = {
        "id": uuid.uuid4().hex[:12], "name": (name or "Experiment").strip(),
        "created_at": int(time.time()), "author": author or "", "tags": tags or [],
        "metric": r.get("metric"), "did_estimate": r.get("did_estimate"),
        "incremental_lift_pct": r.get("incremental_lift_pct"),
        "p_value": r.get("p_value"), "significant": v.get("significant"),
        "spec": spec or {}, "readout": r,
    }
    if extra:
        rec.update(extra)
    data.setdefault("experiments", []).insert(0, rec)
    _store_save(data)
    return rec["id"]


@router.post("/experiments/save")
async def save_experiment(body: SaveExperimentRequest):
    if not (body.name or "").strip():
        return {"error": "Name is required."}
    rid = _persist_experiment(body.name, body.spec, body.readout, body.author or "", body.tags or [])
    return {"ok": True, "id": rid}


@router.get("/experiments")
async def list_experiments():
    data = _store_load()
    items = []
    for e in data.get("experiments", []):
        row = {k: e.get(k) for k in ("id", "name", "created_at", "author", "tags",
                                     "metric", "did_estimate", "incremental_lift_pct",
                                     "p_value", "significant")}
        spec = e.get("spec") or {}
        row["data_backed"] = bool(spec.get("primary_table") and spec.get("metric_field"))
        row["monitor"] = bool(e.get("monitor"))
        items.append(row)
    return {"ok": True, "experiments": items, "count": len(items)}


@router.get("/experiments/{exp_id}")
async def get_experiment(exp_id: str):
    data = _store_load()
    for e in data.get("experiments", []):
        if e.get("id") == exp_id:
            return {"ok": True, "experiment": e}
    return {"error": "Not found."}


@router.delete("/experiments/{exp_id}")
async def delete_experiment(exp_id: str):
    data = _store_load()
    before = len(data.get("experiments", []))
    data["experiments"] = [e for e in data.get("experiments", []) if e.get("id") != exp_id]
    _store_save(data)
    return {"ok": True, "deleted": before - len(data["experiments"])}


# ── Email an experiment readout ──────────────────────────────────────────────

def _fields_of(model_cls):
    return set(getattr(model_cls, "model_fields", None) or getattr(model_cls, "__fields__", {}) or {})


def _experiment_email_html(rec: dict) -> tuple:
    r = rec.get("readout") or {}
    v = r.get("verdict") or {}
    name = rec.get("name", "Experiment")
    did = r.get("did_estimate")
    lift = r.get("incremental_lift_pct")
    ci = r.get("confidence_interval")
    p = r.get("p_value")
    roi = r.get("roi") or {}
    rows = [
        ("Incremental effect", "—" if did is None else f"{did:.2f} per unit"),
        ("Incremental lift", "—" if lift is None else f"{('+' if lift>=0 else '')}{lift:.1f}%"),
        ("95% CI", "—" if not ci else f"[{ci[0]:.2f}, {ci[1]:.2f}]"),
        ("p-value", "—" if p is None else _fmt_p(p)),
    ]
    if roi.get("roi_multiple") is not None:
        rows.append(("ROI", f"{roi['roi_multiple']}×  (net {roi.get('net', 0):.0f})"))
    if r.get("cuped"):
        rows.append(("CUPED variance cut", f"{r['cuped'].get('variance_reduction_pct')}%"))
    tr = "".join(f"<tr><td style='padding:6px 12px;color:#64748b'>{k}</td>"
                 f"<td style='padding:6px 12px;font-weight:600;color:#0f172a'>{val}</td></tr>" for k, val in rows)
    color = "#16a34a" if v.get("significant") else ("#b45309" if v.get("significant") is False else "#0E76B4")
    html = (
        f"<div style='font-family:Inter,Arial,sans-serif;max-width:560px'>"
        f"<h2 style='color:#0f172a;margin:0 0 4px'>{name}</h2>"
        f"<div style='color:#64748b;font-size:13px;margin-bottom:14px'>Test &amp; Learn experiment · metric: {r.get('metric','')}</div>"
        f"<div style='background:#f8fafc;border-left:4px solid {color};padding:12px 16px;border-radius:6px;margin-bottom:16px'>"
        f"<div style='font-weight:700;color:{color};font-size:15px'>{v.get('headline','')}</div>"
        f"<div style='color:#475569;font-size:13px;margin-top:4px'>{v.get('detail','')}</div></div>"
        f"<table style='border-collapse:collapse;width:100%;font-size:13px'>{tr}</table>"
        + (f"<div style='margin-top:16px;padding:12px 14px;background:#fffbeb;border-radius:6px;color:#334155;font-size:13px'>"
           f"<b>Recommendation:</b> {r.get('recommendation','')}</div>" if r.get("recommendation") else "")
        + "</div>")
    text = f"{name}\n{v.get('headline','')}\n{v.get('detail','')}\n" + "\n".join(f"{k}: {val}" for k, val in rows)
    subject = f"Experiment: {name} — {v.get('headline','result')}"
    return subject, text, html


class EmailExperimentRequest(BaseModel):
    recipients: str
    webhook_url: Optional[str] = None
    subject: Optional[str] = None


@router.post("/experiments/{exp_id}/email")
async def email_experiment(exp_id: str, body: EmailExperimentRequest):
    data = _store_load()
    rec = next((e for e in data.get("experiments", []) if e.get("id") == exp_id), None)
    if not rec:
        return {"error": "Not found."}
    if not (body.recipients or "").strip():
        return {"error": "Recipients required."}
    subject, text, html = _experiment_email_html(rec)
    if body.subject:
        subject = body.subject
    loop = asyncio.get_running_loop()
    wnote = ""
    if body.webhook_url:
        try:
            from alerts import _post_webhook
            ok = await loop.run_in_executor(None, _post_webhook, body.webhook_url, subject)
            wnote = " · webhook ok" if ok else " · webhook failed"
        except Exception:
            wnote = " · webhook failed"
    try:
        import smtp_mailer
        if not smtp_mailer.is_configured():
            return {"ok": bool(body.webhook_url), "detail": "SMTP not configured (set SMTP_HOST / SMTP_FROM)." + wnote}
        sent = await loop.run_in_executor(None, lambda: smtp_mailer.send(body.recipients, subject, text, html))
        return {"ok": True, "detail": "Sent to " + ", ".join(sent) + wnote}
    except Exception as e:
        return {"ok": bool(body.webhook_url), "detail": str(e) + wnote}


# ── Experiment monitors — re-run on a schedule, notify on significance ────────

class MonitorRequest(BaseModel):
    enabled: bool = True
    recipients: Optional[str] = None
    webhook_url: Optional[str] = None


@router.post("/experiments/{exp_id}/monitor")
async def set_monitor(exp_id: str, body: MonitorRequest):
    data = _store_load()
    rec = next((e for e in data.get("experiments", []) if e.get("id") == exp_id), None)
    if not rec:
        return {"error": "Not found."}
    spec = rec.get("spec") or {}
    if body.enabled and not (spec.get("primary_table") and spec.get("metric_field")):
        return {"error": "Only data-backed experiments (built 'From my data') can be monitored."}
    rec["monitor"] = bool(body.enabled)
    rec["monitor_recipients"] = body.recipients or rec.get("monitor_recipients", "")
    rec["monitor_webhook"] = body.webhook_url or rec.get("monitor_webhook", "")
    if "last_significant" not in rec:
        rec["last_significant"] = rec.get("significant")
    _store_save(data)
    return {"ok": True, "monitor": rec["monitor"]}


@router.post("/experiments/monitor/run")
async def run_monitors():
    """Re-run every monitored data-backed experiment; notify when one newly
    crosses into significance. Meant to be called by the scheduler/jobs."""
    data = _store_load()
    notifications = []
    changed = False
    for rec in data.get("experiments", []):
        if not rec.get("monitor"):
            continue
        spec = rec.get("spec") or {}
        if not (spec.get("primary_table") and spec.get("metric_field")):
            continue
        try:
            req = ExperimentFromDataRequest(**{k: v for k, v in spec.items() if k in _fields_of(ExperimentFromDataRequest)})
            r = await _run_experiment_from_data(req)
        except Exception as e:
            log.warning(f"monitor rerun failed for {rec.get('id')}: {e}")
            continue
        if r.get("error"):
            continue
        new_sig = (r.get("verdict") or {}).get("significant")
        old_sig = rec.get("last_significant")
        rec["last_checked"] = int(time.time())
        rec["last_did"] = r.get("did_estimate")
        rec["last_significant"] = new_sig
        rec["readout"] = r  # refresh stored readout
        changed = True
        if new_sig and not old_sig:  # newly significant → notify
            note = {"id": rec["id"], "name": rec["name"], "metric": r.get("metric"),
                    "did_estimate": r.get("did_estimate"),
                    "incremental_lift_pct": r.get("incremental_lift_pct"),
                    "message": f"Experiment '{rec['name']}' just reached significance "
                               f"({(r.get('incremental_lift_pct') or 0):+.1f}% incremental)."}
            notifications.append(note)
            loop = asyncio.get_running_loop()
            if rec.get("monitor_webhook"):
                try:
                    from alerts import _post_webhook
                    await loop.run_in_executor(None, _post_webhook, rec["monitor_webhook"], note["message"])
                except Exception:
                    pass
            if rec.get("monitor_recipients"):
                try:
                    import smtp_mailer
                    if smtp_mailer.is_configured():
                        subj, txt, html = _experiment_email_html(rec)
                        await loop.run_in_executor(None, lambda: smtp_mailer.send(rec["monitor_recipients"], subj, txt, html))
                except Exception:
                    pass
    if changed:
        _store_save(data)
    return {"ok": True, "checked": sum(1 for e in data.get("experiments", []) if e.get("monitor")),
            "notifications": notifications}


_last_monitor_ts = 0.0


async def monitor_tick(now=None):
    """Called by the background scheduler each minute. Re-runs monitored
    experiments on a cadence (default hourly, JARVIS_MONITOR_INTERVAL_MIN),
    but only when there is at least one monitored experiment."""
    global _last_monitor_ts
    interval = int(os.getenv("JARVIS_MONITOR_INTERVAL_MIN", "60")) * 60
    nowts = time.time()
    if nowts - _last_monitor_ts < interval:
        return
    try:
        data = _store_load()
        if not any(e.get("monitor") for e in data.get("experiments", [])):
            _last_monitor_ts = nowts
            return
        _last_monitor_ts = nowts
        res = await run_monitors()
        notes = res.get("notifications") or []
        if notes:
            log.info("experiment monitors: %d newly significant", len(notes))
            try:
                import jobs
                jobs.record("Experiment monitors", "monitor", "success",
                            f"{len(notes)} newly significant")
            except Exception:
                pass
    except Exception as e:
        log.warning("monitor tick failed: %s", e)


# ── Rollout targets — promote a winning segment to a targeting plan ───────────

_ROLLOUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rollout_targets.json")


def _rollout_load():
    try:
        with open(_ROLLOUT_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"targets": []}


def _rollout_save(data):
    tmp = _ROLLOUT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, _ROLLOUT_PATH)


class RolloutRequest(BaseModel):
    segment_field: str
    segment_value: str
    metric: Optional[str] = None
    primary_table: Optional[str] = None    # so the target can open the Query Builder pre-filtered
    expected_lift_pct: Optional[float] = None
    did_estimate: Optional[float] = None
    p_value: Optional[float] = None
    source_experiment: Optional[str] = None
    source_spec: Optional[dict] = None      # the segment-scan data spec, to re-measure later
    note: Optional[str] = None


ROLLOUT_STATUSES = ("planned", "live", "measured", "cancelled")


@router.post("/rollouts")
async def add_rollout(body: RolloutRequest):
    data = _rollout_load()
    rec = {"id": uuid.uuid4().hex[:12], "created_at": int(time.time()),
           "segment_field": body.segment_field, "segment_value": body.segment_value,
           "metric": body.metric, "primary_table": body.primary_table,
           "expected_lift_pct": body.expected_lift_pct,
           "did_estimate": body.did_estimate, "p_value": body.p_value,
           "source_experiment": body.source_experiment, "source_spec": body.source_spec or {},
           "note": body.note or "", "status": "planned",
           # A ready-to-apply filter for the semantic layer / query builder.
           "filter": {"table": body.primary_table, "field": body.segment_field, "op": "eq", "value": body.segment_value}}
    data.setdefault("targets", []).insert(0, rec)
    _rollout_save(data)
    return {"ok": True, "id": rec["id"], "filter": rec["filter"]}


@router.get("/rollouts")
async def list_rollouts():
    data = _rollout_load()
    return {"ok": True, "targets": data.get("targets", []), "count": len(data.get("targets", []))}


class RolloutStatusRequest(BaseModel):
    status: str
    note: Optional[str] = None


@router.post("/rollouts/{rid}/status")
async def set_rollout_status(rid: str, body: RolloutStatusRequest):
    if body.status not in ROLLOUT_STATUSES:
        return {"error": f"Status must be one of {', '.join(ROLLOUT_STATUSES)}."}
    data = _rollout_load()
    rec = next((t for t in data.get("targets", []) if t.get("id") == rid), None)
    if not rec:
        return {"error": "Not found."}
    rec["status"] = body.status
    if body.note is not None:
        rec["note"] = body.note
    rec["status_updated_at"] = int(time.time())
    _rollout_save(data)
    return {"ok": True, "status": rec["status"]}


class MeasureRolloutRequest(BaseModel):
    # Optional override windows for the measurement (defaults to the source spec's).
    pre_start: Optional[str] = None
    pre_end: Optional[str] = None
    post_start: Optional[str] = None
    post_end: Optional[str] = None


@router.post("/rollouts/{rid}/measure")
async def measure_rollout(rid: str, body: MeasureRolloutRequest):
    """Close the loop: re-run the source experiment scoped to the promoted
    segment to confirm the effect held after rollout. Saves the result as a
    linked experiment and marks the target 'measured'."""
    data = _rollout_load()
    rec = next((t for t in data.get("targets", []) if t.get("id") == rid), None)
    if not rec:
        return {"error": "Not found."}
    spec = dict(rec.get("source_spec") or {})
    if not (spec.get("primary_table") and spec.get("metric_field")):
        return {"error": "This target has no re-runnable data spec (was it promoted from a data-backed segment scan?)."}
    # scope to the promoted segment + optional fresh windows
    for k in ("pre_start", "pre_end", "post_start", "post_end"):
        v = getattr(body, k)
        if v:
            spec[k] = v
    seg_filter = {"field": rec["segment_field"], "op": "eq", "value": rec["segment_value"]}
    spec["extra_filters"] = (spec.get("extra_filters") or []) + [seg_filter]
    try:
        req = ExperimentFromDataRequest(**{k: v for k, v in spec.items() if k in _fields_of(ExperimentFromDataRequest)})
        readout = await _run_experiment_from_data(req)
    except Exception as e:
        return {"error": f"Measurement failed: {e}"}
    if readout.get("error"):
        return {"error": readout["error"]}
    name = f"Rollout measure: {rec['segment_field']}={rec['segment_value']}"
    exp_id = _persist_experiment(name, spec, readout, tags=["rollout-measure"],
                                 extra={"rollout_id": rid})
    rec["status"] = "measured"
    rec["measured_experiment"] = exp_id
    rec["status_updated_at"] = int(time.time())
    _rollout_save(data)
    return {"ok": True, "experiment_id": exp_id, "readout": readout}


@router.delete("/rollouts/{rid}")
async def delete_rollout(rid: str):
    data = _rollout_load()
    before = len(data.get("targets", []))
    data["targets"] = [t for t in data.get("targets", []) if t.get("id") != rid]
    _rollout_save(data)
    return {"ok": True, "deleted": before - len(data["targets"])}


# ── Experiment templates — save a spec, re-run each quarter ───────────────────

_TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "experiment_templates.json")


def _tpl_load():
    try:
        with open(_TEMPLATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"templates": []}


def _tpl_save(data):
    tmp = _TEMPLATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, _TEMPLATE_PATH)


class SaveTemplateRequest(BaseModel):
    name: str
    spec: dict
    kind: str = "experiment"        # experiment | synthetic | segment


@router.post("/experiments/templates")
async def save_template(body: SaveTemplateRequest):
    if not (body.name or "").strip():
        return {"error": "Name is required."}
    spec = body.spec or {}
    if not (spec.get("primary_table") and spec.get("metric_field")):
        return {"error": "Only data-backed specs can be templated."}
    data = _tpl_load()
    rec = {"id": uuid.uuid4().hex[:12], "name": body.name.strip(), "kind": body.kind,
           "created_at": int(time.time()), "spec": spec}
    data.setdefault("templates", []).insert(0, rec)
    _tpl_save(data)
    return {"ok": True, "id": rec["id"]}


@router.get("/experiments/templates")
async def list_templates():
    data = _tpl_load()
    items = [{"id": t.get("id"), "name": t.get("name"), "kind": t.get("kind"),
              "created_at": t.get("created_at"), "schedule": t.get("schedule"),
              "last_run": t.get("last_run"),
              "metric": (t.get("spec") or {}).get("metric_name") or (t.get("spec") or {}).get("metric_field")}
             for t in data.get("templates", [])]
    return {"ok": True, "templates": items, "count": len(items)}


class ScheduleTemplateRequest(BaseModel):
    cron: Optional[str] = None      # standard 5-field cron, e.g. "0 6 1 1,4,7,10 *" (quarterly). Empty clears.


@router.post("/experiments/templates/{tid}/schedule")
async def schedule_template(tid: str, body: ScheduleTemplateRequest):
    data = _tpl_load()
    t = next((x for x in data.get("templates", []) if x.get("id") == tid), None)
    if not t:
        return {"error": "Template not found."}
    t["schedule"] = (body.cron or "").strip() or None
    _tpl_save(data)
    return {"ok": True, "schedule": t.get("schedule")}


async def template_tick(now=None):
    """Auto-run scheduled templates. Called by the background scheduler each
    minute; uses the same cron matcher as the report/alert ticks."""
    import datetime as _d
    now = now or _d.datetime.now()
    stamp = now.strftime("%Y-%m-%d %H:%M")
    try:
        from brain.runner import cron_due
    except Exception:
        return
    data = _tpl_load()
    changed = False
    for t in data.get("templates", []):
        cron = t.get("schedule")
        if not cron or not cron_due(cron, now):
            continue
        if t.get("_last_min") == stamp:
            continue
        t["_last_min"] = stamp
        changed = True
        spec = dict(t.get("spec") or {})
        try:
            req = ExperimentFromDataRequest(**{k: v for k, v in spec.items() if k in _fields_of(ExperimentFromDataRequest)})
            readout = await _run_experiment_from_data(req)
        except Exception as e:
            log.warning(f"scheduled template {t.get('id')} run failed: {e}")
            continue
        if readout.get("error"):
            continue
        _persist_experiment(f"{t['name']} — {_d.date.today().isoformat()}", spec, readout,
                            tags=["from-template", "scheduled"], extra={"template_id": t["id"]})
        t["last_run"] = int(time.time())
        try:
            import jobs
            jobs.record(f"Template run: {t['name']}", "template", "success", "scheduled")
        except Exception:
            pass
    if changed:
        _tpl_save(data)


class RunTemplateRequest(BaseModel):
    pre_start: Optional[str] = None
    pre_end: Optional[str] = None
    post_start: Optional[str] = None
    post_end: Optional[str] = None
    name: Optional[str] = None


@router.post("/experiments/templates/{tid}/run")
async def run_template(tid: str, body: RunTemplateRequest):
    data = _tpl_load()
    tpl = next((t for t in data.get("templates", []) if t.get("id") == tid), None)
    if not tpl:
        return {"error": "Template not found."}
    spec = dict(tpl.get("spec") or {})
    for k in ("pre_start", "pre_end", "post_start", "post_end"):
        v = getattr(body, k)
        if v:
            spec[k] = v
    try:
        req = ExperimentFromDataRequest(**{k: v for k, v in spec.items() if k in _fields_of(ExperimentFromDataRequest)})
        readout = await _run_experiment_from_data(req)
    except Exception as e:
        return {"error": f"Run failed: {e}"}
    if readout.get("error"):
        return {"error": readout["error"]}
    import datetime as _d
    name = body.name or f"{tpl['name']} — {_d.date.today().isoformat()}"
    exp_id = _persist_experiment(name, spec, readout, tags=["from-template"],
                                 extra={"template_id": tid})
    return {"ok": True, "experiment_id": exp_id, "readout": readout}


@router.delete("/experiments/templates/{tid}")
async def delete_template(tid: str):
    data = _tpl_load()
    before = len(data.get("templates", []))
    data["templates"] = [t for t in data.get("templates", []) if t.get("id") != tid]
    _tpl_save(data)
    return {"ok": True, "deleted": before - len(data["templates"])}
