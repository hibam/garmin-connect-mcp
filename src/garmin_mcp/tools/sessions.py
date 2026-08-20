"""All-sport session tools (any Garmin activity type)."""

from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from mcp.server.fastmcp import FastMCP

from garmin_mcp.client import today_str
from garmin_mcp.tools.activities import RUNNING_TYPE_KEYS, _format_pace, _is_running

# Garmin list API parent types (activityType query param)
_PARENT_ACTIVITY_TYPES = {
    "cycling",
    "running",
    "swimming",
    "multi_sport",
    "fitness_equipment",
    "hiking",
    "walking",
    "other",
}


def _activity_type(activity: dict[str, Any]) -> dict[str, str]:
    activity_type = activity.get("activityType") or activity.get("activityTypeDTO") or {}
    return {
        "type_key": activity_type.get("typeKey") or "",
        "parent_type_key": activity_type.get("parentTypeKey") or "",
    }


def _normalize_sport(sport_type: str) -> str:
    return sport_type.strip().lower().replace(" ", "_").replace("-", "_")


def _matches_sport(activity: dict[str, Any], sport_type: str) -> bool:
    if not sport_type:
        return True
    needle = _normalize_sport(sport_type)
    keys = _activity_type(activity)
    if needle in {keys["type_key"].lower(), keys["parent_type_key"].lower()}:
        return True
    if needle in {"run", "runs"} and _is_running(activity):
        return True
    aliases = {
        "pingpong": "table_tennis",
        "ping_pong": "table_tennis",
        "tabletennis": "table_tennis",
        "탁구": "table_tennis",
        "배드민턴": "badminton",
    }
    mapped = aliases.get(needle, needle)
    return mapped in {keys["type_key"].lower(), keys["parent_type_key"].lower()}


def _api_activity_type(sport_type: str) -> str | None:
    """Map filter to Garmin list API parent type, or None to fetch all."""
    if not sport_type:
        return None
    needle = _normalize_sport(sport_type)
    if needle in _PARENT_ACTIVITY_TYPES:
        return needle
    if needle in {"run", "runs"} or needle in RUNNING_TYPE_KEYS:
        return "running"
    return None


def _round_or_none(value: Any, digits: int = 1) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _summarize_session(activity: dict[str, Any]) -> dict[str, Any]:
    """Sport-agnostic metrics: load, HR, calories. Pace only for running."""
    keys = _activity_type(activity)
    type_key = keys["type_key"]
    duration_s = activity.get("duration") or 0
    distance_m = activity.get("distance") or 0
    is_run = _is_running(activity)
    avg_pace_s = (duration_s / (distance_m / 1000)) if is_run and distance_m > 0 else None

    return {
        "activity_id": activity.get("activityId"),
        "name": activity.get("activityName"),
        "date": activity.get("startTimeLocal"),
        "type": type_key or None,
        "parent_type": keys["parent_type_key"] or None,
        "distance_km": round(distance_m / 1000, 2) if distance_m else 0,
        "duration_seconds": round(duration_s, 1) if duration_s else 0,
        "elapsed_duration_seconds": _round_or_none(activity.get("elapsedDuration")),
        "moving_duration_seconds": _round_or_none(activity.get("movingDuration")),
        "avg_pace": _format_pace(avg_pace_s) if is_run else None,
        "calories": activity.get("calories"),
        "bmr_calories": activity.get("bmrCalories"),
        "avg_heart_rate": activity.get("averageHR"),
        "max_heart_rate": activity.get("maxHR"),
        "hr_zone_1_seconds": activity.get("hrTimeInZone_1"),
        "hr_zone_2_seconds": activity.get("hrTimeInZone_2"),
        "hr_zone_3_seconds": activity.get("hrTimeInZone_3"),
        "hr_zone_4_seconds": activity.get("hrTimeInZone_4"),
        "hr_zone_5_seconds": activity.get("hrTimeInZone_5"),
        "training_effect_aerobic": activity.get("aerobicTrainingEffect"),
        "training_effect_anaerobic": activity.get("anaerobicTrainingEffect"),
        "training_load": _round_or_none(activity.get("activityTrainingLoad"), 1),
        "training_effect_label": activity.get("trainingEffectLabel"),
        "moderate_intensity_minutes": activity.get("moderateIntensityMinutes"),
        "vigorous_intensity_minutes": activity.get("vigorousIntensityMinutes"),
        "steps": activity.get("steps"),
    }


def _sport_bucket(activity: dict[str, Any]) -> str:
    keys = _activity_type(activity)
    if _is_running(activity):
        return "running"
    return keys["type_key"] or keys["parent_type_key"] or "other"


def _week_summary(activities: list[dict[str, Any]]) -> dict[str, Any]:
    by_sport: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "count": 0,
            "duration_seconds": 0.0,
            "calories": 0.0,
            "training_load": 0.0,
            "hr_sum": 0.0,
            "hr_n": 0,
        }
    )
    for a in activities:
        bucket = _sport_bucket(a)
        row = by_sport[bucket]
        row["count"] += 1
        row["duration_seconds"] += a.get("duration") or 0
        row["calories"] += a.get("calories") or 0
        row["training_load"] += a.get("activityTrainingLoad") or 0
        if a.get("averageHR"):
            row["hr_sum"] += a["averageHR"]
            row["hr_n"] += 1

    sports = []
    for sport, row in sorted(by_sport.items(), key=lambda x: -x[1]["training_load"]):
        sports.append(
            {
                "type": sport,
                "count": row["count"],
                "duration_seconds": round(row["duration_seconds"], 1),
                "calories": round(row["calories"]),
                "training_load": round(row["training_load"], 1),
                "avg_heart_rate": round(row["hr_sum"] / row["hr_n"], 1) if row["hr_n"] else None,
            }
        )

    return {
        "total_sessions": len(activities),
        "total_duration_seconds": round(sum(a.get("duration") or 0 for a in activities), 1),
        "total_calories": round(sum(a.get("calories") or 0 for a in activities)),
        "total_training_load": round(
            sum(a.get("activityTrainingLoad") or 0 for a in activities), 1
        ),
        "by_sport": sports,
    }


def register(mcp: FastMCP):
    @mcp.tool()
    def get_recent_sessions(count: int = 20, sport_type: str = "") -> list[dict[str, Any]]:
        """Get recent Garmin activities of any sport (cycling, swimming,
        strength training, team sports, etc.). Use for recovery / load
        decisions when non-running sessions matter. For running-only pace
        analysis use get_recent_activities instead.

        Args:
            count: Number of sessions to return (default: 20, max: 100)
            sport_type: Optional filter. typeKey (e.g. strength_training,
                indoor_cycling, yoga) or parent type (running, cycling,
                swimming, hiking, fitness_equipment, other). Empty = all.
        """
        from garmin_mcp import get_client

        client = get_client()
        count = min(max(count, 1), 100)
        fetch_limit = count if not sport_type else min(count * 4, 200)
        activities = client.get_activities(start=0, limit=fetch_limit)

        sessions = []
        for activity in activities:
            if _matches_sport(activity, sport_type):
                sessions.append(_summarize_session(activity))
                if len(sessions) >= count:
                    break
        return sessions

    @mcp.tool()
    def get_sessions_by_date(
        start_date: str,
        end_date: str,
        sport_type: str = "",
    ) -> list[dict[str, Any]]:
        """Get activities of any sport in a date range.

        Args:
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (YYYY-MM-DD)
            sport_type: Optional filter (see get_recent_sessions). Empty = all.
        """
        from garmin_mcp import get_client

        client = get_client()
        activities = client.get_activities_by_date(
            start_date,
            end_date,
            _api_activity_type(sport_type),
        )
        return [
            _summarize_session(a) for a in activities if _matches_sport(a, sport_type)
        ]

    @mcp.tool()
    def get_weekly_session_summary(
        end_date: str = "",
        weeks: int = 1,
    ) -> list[dict[str, Any]]:
        """Weekly all-sport summary: session count, duration, calories,
        training load, split by sport. Use instead of weekly running summary
        when any non-running activity affects recovery or total load.

        Args:
            end_date: End date (YYYY-MM-DD), defaults to today
            weeks: Number of weeks (default: 1, max: 12)
        """
        from garmin_mcp import get_client

        client = get_client()
        end = date.fromisoformat(end_date) if end_date else date.fromisoformat(today_str())
        weeks = min(max(weeks, 1), 12)

        results = []
        for w in range(weeks):
            week_end = end - timedelta(weeks=w)
            week_start = week_end - timedelta(days=week_end.weekday())
            week_end_date = week_start + timedelta(days=6)
            activities = client.get_activities_by_date(
                week_start.isoformat(),
                week_end_date.isoformat(),
            )
            summary = _week_summary(activities)
            summary["week_start"] = week_start.isoformat()
            summary["week_end"] = week_end_date.isoformat()
            results.append(summary)
        return results
