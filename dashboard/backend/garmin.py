"""Garmin Connect sync — unofficial garminconnect lib (no public OAuth2 API).

Each metric is fetched in an independent try/except so one failure never
prevents the rest from being stored. All exceptions are caught and logged;
callers receive a partial snapshot with null fields for anything that failed.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from .config import (
    GARMIN_EMAIL,
    GARMIN_MFA_SECRET,
    GARMIN_PASSWORD,
    GARMIN_TOKEN_STORE,
)
from . import userdb

log = logging.getLogger(__name__)


def is_configured() -> bool:
    return bool(GARMIN_EMAIL and GARMIN_PASSWORD)


def _login():
    """Return an authenticated Garmin client, or None if credentials are missing / auth fails."""
    if not is_configured():
        return None

    try:
        from garminconnect import (
            Garmin,
            GarminConnectAuthenticationError,
            GarminConnectConnectionError,
            GarminConnectTooManyRequestsError,
        )
    except ImportError:
        log.error("garminconnect package not installed")
        return None

    try:
        GARMIN_TOKEN_STORE.mkdir(parents=True, exist_ok=True)
        token_dir = str(GARMIN_TOKEN_STORE)

        if GARMIN_MFA_SECRET:
            import pyotp
            def _mfa() -> str:
                return pyotp.TOTP(GARMIN_MFA_SECRET).now()
            client = Garmin(GARMIN_EMAIL, GARMIN_PASSWORD, prompt_mfa=_mfa)
        else:
            client = Garmin(GARMIN_EMAIL, GARMIN_PASSWORD)

        client.login(tokenstore=token_dir)
        return client

    except Exception as exc:
        log.error("Garmin login failed: %s", exc)
        return None


def _safe(fn, *args, **kwargs) -> tuple[Any, Optional[str]]:
    """Run fn(*args, **kwargs) and return (result, error_str). Never raises."""
    try:
        return fn(*args, **kwargs), None
    except Exception as exc:
        return None, str(exc)


class GarminLoginError(RuntimeError):
    """Garmin login failed or credentials are missing. Nothing is written when this is raised."""


def _local_ts(ms: Any) -> Optional[str]:
    """Garmin's sleep *Local timestamps are epoch milliseconds that already hold the local clock time."""
    if not isinstance(ms, (int, float)):
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M")


def _primary_device(d: Any) -> tuple[Optional[str], dict]:
    """Garmin keys some answers by device id: return (id, entry) of the primary training device.

    (None, {}) when there is none: the caller records that, nothing is guessed.
    """
    if isinstance(d, dict):
        for key, v in d.items():
            if isinstance(v, dict) and v.get("primaryTrainingDevice"):
                return key, v
    return None, {}


_READINESS_FACTORS = (
    ("sleep", "sleepScoreFactor"),
    ("recovery time", "recoveryTimeFactor"),
    ("load", "acwrFactor"),
    ("HRV", "hrvFactor"),
    ("stress history", "stressHistoryFactor"),
)

_LOAD_FOCUS = (
    ("Low aerobic", "monthlyLoadAerobicLow"),
    ("High aerobic", "monthlyLoadAerobicHigh"),
    ("Anaerobic", "monthlyLoadAnaerobic"),
)

_CLASS_PREFIX = "classificationLowerLimit"


def _round_or_none(v: Any) -> Optional[int]:
    return round(v) if isinstance(v, (int, float)) else None


def paris_today() -> date:
    """Today's date in Paris. Garmin days follow the watch owner's clock, which the page also uses, never UTC."""
    return datetime.now(ZoneInfo("Europe/Paris")).date()


def fetch_and_store(target_date: str | None = None, client=None) -> dict:
    """Fetch all Garmin metrics for target_date and persist to dashboard.db.

    Logs in only when no client is given; raises GarminLoginError when that login fails (nothing is
    written then). A call that fails names itself in fetch_error and leaves the stored value as it
    was: a key with no value is never written, so it can never replace a stored value with NULL.
    """
    if target_date is None:
        target_date = paris_today().strftime("%Y-%m-%d")
    if client is None:
        client = _login()
        if client is None:
            raise GarminLoginError("Garmin login failed or credentials are not configured")

    is_today = target_date == paris_today().strftime("%Y-%m-%d")
    snapshot: dict[str, Any] = {"date": target_date}
    raw: dict[str, Any] = {}
    errors: list[str] = []

    # ── Sleep ──────────────────────────────────────────────────────────────────
    sleep_data, err = _safe(client.get_sleep_data, target_date)
    if err:
        errors.append(f"sleep: {err}")
    else:
        raw["sleep"] = sleep_data
        dto = (sleep_data or {}).get("dailySleepDTO") or {}
        scores = dto.get("sleepScores") or {}
        overall = scores.get("overall") or {}
        snapshot["sleep_score"] = overall.get("value") if isinstance(overall, dict) else overall
        snapshot["sleep_score_word"] = overall.get("qualifierKey") if isinstance(overall, dict) else None
        snapshot["sleep_duration_sec"] = dto.get("sleepTimeSeconds")
        snapshot["sleep_deep_sec"]     = dto.get("deepSleepSeconds")
        snapshot["sleep_light_sec"]    = dto.get("lightSleepSeconds")
        snapshot["sleep_rem_sec"]      = dto.get("remSleepSeconds")
        snapshot["sleep_awake_sec"]    = dto.get("awakeSleepSeconds")
        snapshot["sleep_start_local"]  = _local_ts(dto.get("sleepStartTimestampLocal"))
        snapshot["sleep_end_local"]    = _local_ts(dto.get("sleepEndTimestampLocal"))
        need = dto.get("sleepNeed")
        snapshot["sleep_need_min"] = need.get("actual") if isinstance(need, dict) else None
        # The true per-segment hypnogram (Garmin's sleepLevels timeline), stored as a JSON document.
        # activityLevel maps {0:deep, 1:light, 2:rem, 3:awake} (verified: the per-segment
        # second-sums equal the DTO deep/light/rem/awake totals).
        levels = (sleep_data or {}).get("sleepLevels")
        snapshot["sleep_levels_json"] = json.dumps(levels) if isinstance(levels, list) and levels else None

    # ── Body battery: the series, then the day's figures from the stats call below ──
    bb_data, err = _safe(client.get_body_battery, target_date, target_date)
    if err:
        errors.append(f"body_battery: {err}")
    else:
        raw["body_battery"] = bb_data
        series: list = []
        if isinstance(bb_data, list):
            for item in bb_data:
                arr = item.get("bodyBatteryValuesArray") if isinstance(item, dict) else None
                if isinstance(arr, list):
                    series.extend([p[0], p[1]] for p in arr if isinstance(p, (list, tuple)) and len(p) >= 2 and p[1] is not None)
        snapshot["body_battery_series_json"] = json.dumps(series) if series else None

    # ── Resting HR ─────────────────────────────────────────────────────────────
    hr_data, err = _safe(client.get_heart_rates, target_date)
    if err:
        errors.append(f"heart_rate: {err}")
    else:
        raw["heart_rate"] = hr_data
        snapshot["resting_hr"] = (hr_data or {}).get("restingHeartRate")

    # ── Steps ──────────────────────────────────────────────────────────────────
    steps_data, err = _safe(client.get_steps_data, target_date)
    if err:
        errors.append(f"steps: {err}")
    else:
        raw["steps"] = steps_data
        if isinstance(steps_data, list) and steps_data:
            snapshot["steps"] = sum(s.get("steps", 0) for s in steps_data if s.get("steps"))

    # ── Training readiness: the morning reading decides the day's score ─────────
    rd, err = _safe(client.get_morning_training_readiness, target_date)
    if err:
        errors.append(f"readiness: {err}")
    else:
        raw["readiness"] = rd
        if not isinstance(rd, dict):
            rd = {}
        snapshot["readiness"] = rd.get("score")
        snapshot["readiness_level"] = rd.get("level")
        snapshot["acute_load"] = rd.get("acuteLoad")
        # The daily recovery-time advisor (minutes until fully recovered), stored raw in minutes.
        snapshot["recovery_minutes"] = rd.get("recoveryTime")
        factors = []
        for name, prefix in _READINESS_FACTORS:
            pct = rd.get(f"{prefix}Percent")
            if pct is not None:
                factors.append({"name": name, "percent": pct, "feedback": rd.get(f"{prefix}Feedback")})
        snapshot["readiness_factors_json"] = json.dumps(factors) if factors else None

    # ── Stress ─────────────────────────────────────────────────────────────────
    stress_data, err = _safe(client.get_stress_data, target_date)
    if err:
        errors.append(f"stress: {err}")
    else:
        raw["stress"] = stress_data
        sd = stress_data
        if isinstance(sd, list):
            sd = sd[0] if sd else {}
        if not isinstance(sd, dict):
            sd = {}
        snapshot["stress"] = sd.get("overallStressLevel")

    # ── Day summary: calories, intensity minutes, body battery, step goal ───────
    stats_data, err = _safe(client.get_stats, target_date)
    if err:
        errors.append(f"stats: {err}")
    else:
        raw["stats"] = stats_data
        sd2 = stats_data if isinstance(stats_data, dict) else {}
        for col, key in (("total_calories", "totalKilocalories"), ("active_calories", "activeKilocalories"),
                         ("resting_calories", "bmrKilocalories")):
            v = sd2.get(key)
            snapshot[col] = int(v) if v is not None else None
        snapshot["intensity_moderate_min"] = sd2.get("moderateIntensityMinutes")
        snapshot["intensity_vigorous_min"] = sd2.get("vigorousIntensityMinutes")
        snapshot["body_battery_pct"]      = sd2.get("bodyBatteryMostRecentValue")
        snapshot["body_battery_high"]     = sd2.get("bodyBatteryHighestValue")
        snapshot["body_battery_low"]      = sd2.get("bodyBatteryLowestValue")
        snapshot["body_battery_at_wake"]  = sd2.get("bodyBatteryAtWakeTime")
        snapshot["steps_goal"]            = sd2.get("dailyStepGoal")

    # ── Overnight HRV ──────────────────────────────────────────────────────────
    # Garmin returns { hrvSummary: { lastNightAvg, status, baseline: { balancedLow, balancedUpper } }, ... }.
    hrv_data, err = _safe(client.get_hrv_data, target_date)
    if err:
        errors.append(f"hrv: {err}")
    else:
        raw["hrv"] = hrv_data
        hd = hrv_data if isinstance(hrv_data, dict) else {}
        summary = hd.get("hrvSummary") if isinstance(hd.get("hrvSummary"), dict) else {}
        hrv_val = summary.get("lastNightAvg") or summary.get("lastNight5MinHigh") or summary.get("weeklyAvg")
        snapshot["hrv_overnight"] = int(hrv_val) if hrv_val is not None else None
        snapshot["hrv_status"] = summary.get("status")
        baseline = summary.get("baseline") if isinstance(summary.get("baseline"), dict) else {}
        snapshot["hrv_balanced_low"]  = baseline.get("balancedLow")
        snapshot["hrv_balanced_high"] = baseline.get("balancedUpper")

    # ── Training status and the 4-week load focus ──────────────────────────────
    ts, err = _safe(client.get_training_status, target_date)
    if err:
        errors.append(f"training_status: {err}")
    else:
        raw["training_status"] = ts
        ts = ts if isinstance(ts, dict) else {}
        dev_id, latest = _primary_device((ts.get("mostRecentTrainingStatus") or {}).get("latestTrainingStatusData"))
        if dev_id is None:
            errors.append("training_status: no primary training device in the answer")
        phrase = latest.get("trainingStatusFeedbackPhrase")
        snapshot["training_status"] = re.sub(r"_\d$", "", phrase) if isinstance(phrase, str) else None
        bal_map = (ts.get("mostRecentTrainingLoadBalance") or {}).get("metricsTrainingLoadBalanceDTOMap")
        bal = bal_map.get(dev_id) if dev_id is not None and isinstance(bal_map, dict) else None
        bal = bal if isinstance(bal, dict) else {}
        focus = []
        for name, key in _LOAD_FOCUS:
            if bal.get(key) is not None:
                focus.append({
                    "name": name,
                    "value": _round_or_none(bal.get(key)),
                    "target_min": bal.get(f"{key}TargetMin"),
                    "target_max": bal.get(f"{key}TargetMax"),
                })
        snapshot["load_focus_json"] = json.dumps(focus) if focus else None

    # ── Endurance score with Garmin's class limits ─────────────────────────────
    es, err = _safe(client.get_endurance_score, target_date)
    if err:
        errors.append(f"endurance: {err}")
    else:
        raw["endurance"] = es
        es = es if isinstance(es, dict) else {}
        snapshot["endurance_score"] = es.get("overallScore")
        classes = sorted(
            ({"name": k[len(_CLASS_PREFIX):], "lower": v} for k, v in es.items()
             if k.startswith(_CLASS_PREFIX) and isinstance(v, (int, float))),
            key=lambda c: c["lower"],
        )
        if classes:
            snapshot["endurance_limits_json"] = json.dumps({
                "gauge_lower": es.get("gaugeLowerLimit"),
                "gauge_upper": es.get("gaugeUpperLimit"),
                "classes": classes,
            })

    # ── Race predictions and lactate threshold: Garmin answers "latest" only, so a past day is left empty
    if is_today:
        rp, err = _safe(client.get_race_predictions)
        if err:
            errors.append(f"race_predictions: {err}")
        else:
            raw["race_predictions"] = rp
            rp = rp if isinstance(rp, dict) else {}
            snapshot["race_5k_sec"]       = _round_or_none(rp.get("time5K"))
            snapshot["race_10k_sec"]      = _round_or_none(rp.get("time10K"))
            snapshot["race_half_sec"]     = _round_or_none(rp.get("timeHalfMarathon"))
            snapshot["race_marathon_sec"] = _round_or_none(rp.get("timeMarathon"))

        lt, err = _safe(client.get_lactate_threshold)
        if err:
            errors.append(f"lactate_threshold: {err}")
        else:
            raw["lactate_threshold"] = lt
            lt = lt if isinstance(lt, dict) else {}
            shr = lt.get("speed_and_heart_rate") if isinstance(lt.get("speed_and_heart_rate"), dict) else {}
            pw = lt.get("power") if isinstance(lt.get("power"), dict) else {}
            snapshot["lactate_threshold_hr"] = shr.get("heartRate")
            snapshot["threshold_power_w"]    = _round_or_none(pw.get("functionalThresholdPower"))

    # ── VO2 max (Garmin's precise value), fitness age, hill score, weekly intensity goal ──
    mm, err = _safe(client.get_max_metrics, target_date)
    if err:
        errors.append(f"max_metrics: {err}")
    else:
        raw["max_metrics"] = mm
        first = mm[0] if isinstance(mm, list) and mm else mm
        generic = first.get("generic") if isinstance(first, dict) and isinstance(first.get("generic"), dict) else {}
        snapshot["vo2max_garmin"] = generic.get("vo2MaxPreciseValue")

    fa, err = _safe(client.get_fitnessage_data, target_date)
    if err:
        errors.append(f"fitness_age: {err}")
    else:
        raw["fitness_age"] = fa
        snapshot["fitness_age"] = fa.get("fitnessAge") if isinstance(fa, dict) else None

    hs, err = _safe(client.get_hill_score, target_date)
    if err:
        errors.append(f"hill_score: {err}")
    else:
        raw["hill_score"] = hs
        snapshot["hill_score"] = _round_or_none(hs.get("overallScore")) if isinstance(hs, dict) else None

    wi, err = _safe(client.get_weekly_intensity_minutes, target_date, target_date)
    if err:
        errors.append(f"weekly_intensity: {err}")
    else:
        raw["weekly_intensity"] = wi
        goals = [w.get("weeklyGoal") for w in (wi if isinstance(wi, list) else []) if isinstance(w, dict) and w.get("weeklyGoal") is not None]
        snapshot["intensity_goal"] = goals[-1] if goals else None

    # The as-of VO2max from activities on or before this day (a slowly-changing estimate).
    vo2max, vo2max_as_of = _latest_vo2max(target_date)
    snapshot["vo2max"]       = vo2max
    snapshot["vo2max_as_of"] = vo2max_as_of

    # A failed call leaves its old raw answer in place: merge into what is stored.
    stored = get_snapshot(target_date)
    merged_raw: dict[str, Any] = {}
    if stored and stored.get("raw_json"):
        try:
            loaded = json.loads(stored["raw_json"])
        except ValueError:
            loaded = {}
        if isinstance(loaded, dict):
            merged_raw = loaded
    merged_raw.update(raw)
    snapshot["raw_json"]    = json.dumps(merged_raw, default=str)
    snapshot["fetch_error"] = "; ".join(errors) if errors else None
    snapshot["fetched_at"]  = datetime.utcnow().isoformat()

    _upsert(snapshot)
    log.info("Garmin sync complete for %s%s", target_date,
             f" (partial errors: {snapshot['fetch_error']})" if errors else "")
    return snapshot


def _latest_vo2max(on_or_before: str) -> tuple[Optional[float], Optional[str]]:
    """The latest activity on or before `on_or_before` (YYYY-MM-DD) that carries a VO2max, as (value, date).

    Returns (None, None) when none has: honest omission, never a faked value.
    """
    conn = userdb.open_rw()
    try:
        row = conn.execute(
            "SELECT vo2max, start_time_local FROM garmin_activities "
            "WHERE vo2max IS NOT NULL AND substr(start_time_local, 1, 10) <= ? "
            "ORDER BY start_time_local DESC LIMIT 1",
            (on_or_before,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None, None
    return row["vo2max"], (row["start_time_local"] or "")[:10] or None


_ALWAYS_WRITTEN = ("fetch_error", "fetched_at")


def _upsert(snap: dict) -> None:
    """Insert or update one day. A key with no value (None) is left out, so a stored value is never
    replaced by NULL; fetch_error and fetched_at always describe the latest run."""
    values = {k: v for k, v in snap.items() if v is not None or k in _ALWAYS_WRITTEN}
    cols = list(values)
    update_set = ", ".join(f"{c} = excluded.{c}" for c in cols if c != "date")
    conn = userdb.open_rw()
    try:
        conn.execute(
            f"INSERT INTO garmin_snapshots ({', '.join(cols)}) "
            f"VALUES ({', '.join(':' + c for c in cols)}) "
            f"ON CONFLICT (date) DO UPDATE SET {update_set}",
            values,
        )
        conn.commit()
    finally:
        conn.close()


# ── Activities ───────────────────────────────────────────────────────────────

# Numeric activity-detail metric keys we parse out of the wire format. The
# Garmin payload puts samples in a flat `activityDetailMetrics[i].metrics`
# array and a separate `metricDescriptors` list says which index of that array
# holds which metric. We dereference once here and yield one row per sample.
_TS_METRIC_KEYS = (
    "directTimestamp",
    "directHeartRate",
    "directSpeed",
    "directElevation",
    "directRunCadence",
    "directBikeCadence",
    "directPower",
    "directTemperature",
    "directRespirationRate",
    "directStrideLength",
    "directGroundContactTime",
    "directVerticalOscillation",
    "directVerticalRatio",
    "directLatitude",
    "directLongitude",
)


def _summary_row(act: dict) -> dict:
    """Map a Garmin activity-list entry to our DB row shape."""
    def _f(*keys, default=None):
        for k in keys:
            v = act.get(k)
            if v is not None:
                return v
        return default

    type_dto = act.get("activityType") or {}

    dist_m = _f("distance")
    dur_s = _f("duration")
    avg_speed = _f("averageSpeed")
    # Pace (min/km) is more useful for foot sports than raw m/s.
    pace = None
    if dist_m and dur_s and dist_m > 0:
        pace = (dur_s / 60.0) / (dist_m / 1000.0)

    return {
        "activity_id":         str(act.get("activityId")),
        "start_time_local":    _f("startTimeLocal"),
        "start_time_gmt":      _f("startTimeGMT"),
        "type_key":            type_dto.get("typeKey"),
        "parent_type_key":     type_dto.get("parentTypeKey"),
        "name":                _f("activityName"),
        "duration_sec":        dur_s,
        "elapsed_sec":         _f("elapsedDuration"),
        "moving_sec":          _f("movingDuration"),
        "distance_m":          dist_m,
        "avg_hr":              _safe_int(_f("averageHR")),
        "max_hr":              _safe_int(_f("maxHR")),
        "total_calories":      _safe_int(_f("calories")),
        "active_calories":     _safe_int(_f("activeCalories", "bmrCalories")),
        "avg_speed_mps":       avg_speed,
        "max_speed_mps":       _f("maxSpeed"),
        "avg_pace_min_per_km": pace,
        "elevation_gain_m":    _f("elevationGain"),
        "elevation_loss_m":    _f("elevationLoss"),
        "min_elevation_m":     _f("minElevation"),
        "max_elevation_m":     _f("maxElevation"),
        "avg_cadence":         _f("averageRunningCadenceInStepsPerMinute",
                                  "averageBikingCadenceInRevPerMinute",
                                  "averageCadence"),
        "max_cadence":         _f("maxRunningCadenceInStepsPerMinute",
                                  "maxBikingCadenceInRevPerMinute",
                                  "maxCadence"),
        "avg_power":           _f("avgPower", "averagePower"),
        "max_power":           _f("maxPower"),
        "aerobic_te":          _f("aerobicTrainingEffect"),
        "anaerobic_te":        _f("anaerobicTrainingEffect"),
        "training_load":       _f("activityTrainingLoad"),
        "vo2max":              _f("vO2MaxValue"),
        "avg_stride_m":        _f("avgStrideLength"),
        "avg_ground_contact_ms": _f("avgGroundContactTime"),
        "avg_vertical_osc_cm": _f("avgVerticalOscillation"),
        "avg_vertical_ratio":  _f("avgVerticalRatio"),
        "avg_respiration_rpm": _f("avgRespirationRate"),
        "avg_temp_c":          _f("averageTemperature"),
        "summary_json":        json.dumps(act, default=str),
    }


def _safe_int(v):
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _f_to_c(f):
    """Convert Fahrenheit → Celsius. Pass-through None."""
    if f is None:
        return None
    try:
        return round((float(f) - 32) * 5 / 9, 1)
    except (TypeError, ValueError):
        return None


def fetch_recent_activities(limit: int = 10, client=None) -> list[dict]:
    """Fetch the `limit` most-recent activities and upsert into DB. Returns rows.

    Logs in only when no client is given.
    """
    if client is None:
        client = _login()
    if client is None:
        return []

    raw, err = _safe(client.get_activities, 0, limit)
    if err:
        log.error("Garmin activity list fetch failed: %s", err)
        return []
    if not isinstance(raw, list):
        return []

    conn = userdb.open_rw()
    rows: list[dict] = []
    try:
        for act in raw:
            if not isinstance(act, dict) or not act.get("activityId"):
                continue
            row = _summary_row(act)
            _upsert_activity(conn, row)
            rows.append(row)
        conn.commit()
    finally:
        conn.close()
    return rows


def _upsert_activity(conn, row: dict) -> None:
    cols = list(row.keys())
    placeholders = ", ".join(f":{c}" for c in cols)
    col_list = ", ".join(cols)
    update_set = ", ".join(f"{c} = excluded.{c}" for c in cols if c != "activity_id")
    conn.execute(
        f"""INSERT INTO garmin_activities ({col_list}, fetched_at)
            VALUES ({placeholders}, datetime('now'))
            ON CONFLICT (activity_id) DO UPDATE SET
                {update_set},
                fetched_at = datetime('now')""",
        row,
    )


def get_recent_activities(limit: int = 3) -> list[dict]:
    """Read the cached recent activities. If table is empty, returns []."""
    conn = userdb.open_rw()
    try:
        rows = conn.execute(
            """SELECT * FROM garmin_activities
               ORDER BY datetime(start_time_local) DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def fetch_activity_detail(activity_id: str) -> dict:
    """Fetch all per-activity detail blobs and cache them. Returns the parsed bundle."""
    client = _login()
    if client is None:
        return {"error": "Garmin login failed"}

    bundle: dict[str, Any] = {"activity_id": activity_id}
    blobs: dict[str, Any] = {}

    blobs["summary"], _   = _safe(client.get_activity, activity_id)
    blobs["details"], _   = _safe(client.get_activity_details, activity_id)
    blobs["splits"], _    = _safe(client.get_activity_splits, activity_id)
    blobs["hr_zones"], _  = _safe(client.get_activity_hr_in_timezones, activity_id)
    blobs["weather"], _   = _safe(client.get_activity_weather, activity_id)
    blobs["exercise_sets"], _ = _safe(client.get_activity_exercise_sets, activity_id)
    blobs["gear"], _      = _safe(client.get_activity_gear, activity_id)

    conn = userdb.open_rw()
    try:
        conn.execute(
            """INSERT INTO garmin_activity_details
               (activity_id, detail_json, splits_json, hr_zones_json,
                weather_json, exercise_sets_json, gear_json, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT (activity_id) DO UPDATE SET
                   detail_json        = excluded.detail_json,
                   splits_json        = excluded.splits_json,
                   hr_zones_json      = excluded.hr_zones_json,
                   weather_json       = excluded.weather_json,
                   exercise_sets_json = excluded.exercise_sets_json,
                   gear_json          = excluded.gear_json,
                   fetched_at         = excluded.fetched_at""",
            (
                activity_id,
                json.dumps(blobs["details"], default=str),
                json.dumps(blobs["splits"], default=str),
                json.dumps(blobs["hr_zones"], default=str),
                json.dumps(blobs["weather"], default=str),
                json.dumps(blobs["exercise_sets"], default=str),
                json.dumps(blobs["gear"], default=str),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    bundle.update(_parse_detail_bundle(blobs))
    return bundle


def _parse_detail_bundle(blobs: dict) -> dict:
    """Turn raw Garmin blobs into a clean shape for the frontend."""
    out: dict[str, Any] = {}

    # ── Time-series (HR / pace / elevation / cadence / power) ───────────────────
    details = blobs.get("details") or {}
    descriptors = details.get("metricDescriptors") or []
    metrics_list = details.get("activityDetailMetrics") or []

    # Map metric key → array index.
    idx_by_key: dict[str, int] = {}
    for d in descriptors:
        key = d.get("key")
        i = d.get("metricsIndex")
        if key and i is not None:
            idx_by_key[key] = i

    samples: list[dict] = []
    for entry in metrics_list:
        m = entry.get("metrics") or []
        sample: dict[str, Any] = {}
        for k in _TS_METRIC_KEYS:
            i = idx_by_key.get(k)
            if i is not None and i < len(m):
                sample[k] = m[i]
        if sample:
            samples.append(sample)

    out["samples"] = samples

    # GPS polyline can be either inside details.geoPolylineDTO.polyline
    # or derivable from the directLatitude/directLongitude samples.
    geo = (details.get("geoPolylineDTO") or {}).get("polyline") or []
    if geo:
        out["polyline"] = [
            {"lat": p.get("lat"), "lon": p.get("lon")}
            for p in geo
            if isinstance(p, dict) and p.get("lat") is not None
        ]
    else:
        out["polyline"] = [
            {"lat": s["directLatitude"], "lon": s["directLongitude"]}
            for s in samples
            if s.get("directLatitude") is not None and s.get("directLongitude") is not None
        ]

    # ── Splits ──────────────────────────────────────────────────────────────────
    splits = blobs.get("splits") or {}
    laps = splits.get("lapDTOs") or splits.get("splits") or []
    parsed_splits = []
    for lap in laps:
        if not isinstance(lap, dict):
            continue
        dist = lap.get("distance")
        dur = lap.get("duration") or lap.get("movingDuration")
        pace = None
        if dist and dur and dist > 0:
            pace = (dur / 60.0) / (dist / 1000.0)
        parsed_splits.append({
            "lap": lap.get("lapIndex") or lap.get("messageIndex"),
            "distance_m": dist,
            "duration_sec": dur,
            "avg_hr": lap.get("averageHR"),
            "max_hr": lap.get("maxHR"),
            "avg_speed_mps": lap.get("averageSpeed"),
            "pace_min_per_km": pace,
            "elevation_gain_m": lap.get("elevationGain"),
            "calories": lap.get("calories"),
        })
    out["splits"] = parsed_splits

    # ── HR zones ────────────────────────────────────────────────────────────────
    hr_zones = blobs.get("hr_zones") or []
    parsed_zones = []
    if isinstance(hr_zones, list):
        for z in hr_zones:
            if not isinstance(z, dict):
                continue
            parsed_zones.append({
                "zone": z.get("zoneNumber"),
                "low_bpm": z.get("zoneLowBoundary"),
                "high_bpm": z.get("zoneHighBoundary"),
                "seconds": z.get("secsInZone"),
            })
    out["hr_zones"] = parsed_zones

    # ── Weather ─────────────────────────────────────────────────────────────────
    # Garmin's activity-weather endpoint returns temperatures in *Fahrenheit*
    # regardless of the user's display unit preference — their mobile app
    # converts client-side. We normalise to °C here so the UI doesn't have to.
    w = blobs.get("weather") or {}
    if isinstance(w, dict) and w:
        out["weather"] = {
            "temp_c":       _f_to_c(w.get("temp")),
            "feels_like_c": _f_to_c(w.get("apparentTemp")),
            "humidity":     w.get("relativeHumidity"),
            "wind_kph":     w.get("windSpeed"),
            "wind_dir":     w.get("windDirectionCompassPoint")
                             or w.get("windDirection"),
            "conditions":   (w.get("weatherTypeDTO") or {}).get("desc")
                             or w.get("weatherType"),
            "dew_point_c":  _f_to_c(w.get("dewPoint")),
        }

    # ── Exercise sets (strength) ───────────────────────────────────────────────
    ex = blobs.get("exercise_sets") or {}
    sets = []
    if isinstance(ex, dict):
        for s in ex.get("exerciseSets") or []:
            if not isinstance(s, dict):
                continue
            exercises = s.get("exercises") or []
            name = None
            if exercises and isinstance(exercises[0], dict):
                name = exercises[0].get("name") or exercises[0].get("category")
            sets.append({
                "name": name,
                "set_type": s.get("setType"),
                "reps":     s.get("repetitionCount"),
                "weight_kg": s.get("weight"),
                "duration_sec": s.get("duration"),
                "rest_sec":  s.get("restDuration"),
            })
    out["exercise_sets"] = sets

    # ── Summary highlights from get_activity() (richer than the list view) ──────
    summary = blobs.get("summary") or {}
    out["summary_extras"] = {
        "recovery_hours":   _nested_get(summary, "summaryDTO.recoveryTimeInHours"),
        "training_effect_msg": _nested_get(summary, "summaryDTO.trainingEffectMessage"),
        "device_name":      _nested_get(summary, "metadataDTO.manufacturer"),
    }

    # ── Gear ─────────────────────────────────────────────────────────────────────
    gear = blobs.get("gear") or []
    parsed_gear = []
    if isinstance(gear, list):
        for g in gear:
            if not isinstance(g, dict):
                continue
            parsed_gear.append({
                "uuid": g.get("uuid"),
                "name": g.get("displayName") or g.get("customMakeModel"),
                "type": g.get("gearTypeName"),
            })
    out["gear"] = parsed_gear

    return out


def _nested_get(d: dict, dotted: str):
    cur: Any = d
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def get_activity_detail_cached(activity_id: str) -> Optional[dict]:
    """Return the cached detail row for an activity_id (raw JSON blobs)."""
    conn = userdb.open_rw()
    try:
        row = conn.execute(
            "SELECT * FROM garmin_activity_details WHERE activity_id = ?",
            (activity_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_activity_summary(activity_id: str) -> Optional[dict]:
    conn = userdb.open_rw()
    try:
        row = conn.execute(
            "SELECT * FROM garmin_activities WHERE activity_id = ?",
            (activity_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_activity_full(activity_id: str, force_refresh: bool = False) -> dict:
    """Return the full activity bundle (summary + parsed detail).
    Triggers a Garmin fetch if not cached."""
    summary = get_activity_summary(activity_id)
    cached = get_activity_detail_cached(activity_id)
    if cached is None or force_refresh:
        parsed = fetch_activity_detail(activity_id)
        # After fetching, also refresh the summary in case the list was stale.
        if summary is None:
            summary = get_activity_summary(activity_id)
    else:
        blobs = {
            "details":       json.loads(cached.get("detail_json") or "null"),
            "splits":        json.loads(cached.get("splits_json") or "null"),
            "hr_zones":      json.loads(cached.get("hr_zones_json") or "null"),
            "weather":       json.loads(cached.get("weather_json") or "null"),
            "exercise_sets": json.loads(cached.get("exercise_sets_json") or "null"),
            "gear":          json.loads(cached.get("gear_json") or "null"),
            "summary":       json.loads(summary.get("summary_json") or "null") if summary else None,
        }
        parsed = {"activity_id": activity_id, **_parse_detail_bundle(blobs)}

    return {"summary": summary, "detail": parsed}


def get_snapshot(target_date: str) -> Optional[dict]:
    conn = userdb.open_rw()
    try:
        row = conn.execute(
            "SELECT * FROM garmin_snapshots WHERE date = ?", (target_date,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def sync_state() -> dict:
    """What the hourly sync last did, read from garmin_snapshots (UTC, "Z" appended).

    last_sync_at is the newest fetched_at; last_sync_error is the fetch_error of that same row (a tie
    goes to the newest date); watch_data_until is the newest day that holds steps or sleep. A database
    with no garmin_snapshots table yet answers configured with the three fields null."""
    state = {
        "configured": is_configured(),
        "last_sync_at": None,
        "last_sync_error": None,
        "watch_data_until": None,
    }
    conn = userdb.open_rw()
    try:
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'garmin_snapshots'"
        ).fetchone()
        if has_table is None:
            return state
        last = conn.execute(
            "SELECT fetched_at, fetch_error FROM garmin_snapshots "
            "ORDER BY fetched_at DESC, date DESC LIMIT 1"
        ).fetchone()
        if last is not None:
            state["last_sync_at"] = last["fetched_at"].replace(" ", "T") + "Z"
            state["last_sync_error"] = last["fetch_error"]
        watch = conn.execute(
            "SELECT MAX(date) AS d FROM garmin_snapshots "
            "WHERE steps IS NOT NULL OR sleep_duration_sec IS NOT NULL"
        ).fetchone()
        state["watch_data_until"] = watch["d"]
        return state
    finally:
        conn.close()


def get_week_snapshots(end_date: str | None = None) -> list[dict]:
    if end_date is None:
        end_date = paris_today().strftime("%Y-%m-%d")
    conn = userdb.open_rw()
    try:
        rows = conn.execute(
            "SELECT * FROM garmin_snapshots WHERE date <= ? ORDER BY date DESC LIMIT 7",
            (end_date,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_snapshots_since(start_date: str, end_date: str) -> list[dict]:
    """Cached snapshots with start_date <= date <= end_date (YYYY-MM-DD), oldest first. No row is invented for a missing day."""
    conn = userdb.open_rw()
    try:
        rows = conn.execute(
            "SELECT * FROM garmin_snapshots WHERE date BETWEEN ? AND ? ORDER BY date ASC",
            (start_date, end_date),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
