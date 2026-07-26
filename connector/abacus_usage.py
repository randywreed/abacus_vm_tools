"""Pure, dependency-free helpers for safe Abacus credit reporting."""
import re
import math
from urllib.parse import urlparse


def yaml_scalar(block: str, key: str) -> str:
    match = re.search(rf"(?m)^\s*{re.escape(key)}:\s*['\"]?([^\s'#\"]+)", block)
    return match.group(1).strip() if match else ""


def is_routellm_base_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        return parsed.scheme == "https" and parsed.hostname == "routellm.abacus.ai"
    except ValueError:
        return False


def key_if_abacus_target(block: str) -> str:
    base_url = yaml_scalar(block, "base_url")
    api_key = yaml_scalar(block, "api_key")
    return api_key if api_key and is_routellm_base_url(base_url) else ""


def abacus_api_key_from_config(text: str) -> str:
    top_model = re.search(r"(?ms)^model:\s*$([\s\S]*?)(?=^[^ \t#][^:]*:\s*|\Z)", text)
    if top_model:
        selected = key_if_abacus_target(top_model.group(1))
        if selected:
            return selected
    providers = re.search(r"(?ms)^custom_providers:\s*$([\s\S]*?)(?=^[^ \t#][^:]*:\s*|\Z)", text)
    if providers:
        for stanza in re.split(r"(?m)^\s*-\s+", providers.group(1)):
            selected = key_if_abacus_target(stanza)
            if selected:
                return selected
    return ""


def credit(value: object) -> float:
    try:
        numeric = float(value)
        return round(numeric / 100.0, 2) if math.isfinite(numeric) else 0.0
    except (TypeError, ValueError):
        return 0.0


def balance_credit_report(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise ValueError("Abacus balance response was invalid")
    points = ((payload.get("result") or {}).get("organization") or {}).get("computePointInfo")
    if not isinstance(points, dict):
        raise ValueError("Abacus balance response was incomplete")
    def required_credit(key: str) -> float:
        try:
            numeric = float(points[key])
        except (KeyError, TypeError, ValueError):
            raise ValueError("Abacus balance response was incomplete") from None
        if not math.isfinite(numeric):
            raise ValueError("Abacus balance response was incomplete")
        return round(numeric / 100.0, 2)
    total = required_credit("currMonthAvailPoints"); used = required_credit("currMonthUsage"); remaining = max(0.0, round(total - used, 2))
    updated = points.get("updatedAt")
    return {"cycleTotalCredits": total, "cycleUsedCredits": used, "cycleRemainingCredits": remaining, "percentRemaining": round((remaining / total) * 100, 1) if total else None, "last24hCredits": credit(points.get("last24HoursUsage")), "last7dCredits": credit(points.get("last7DaysUsage")), "updatedAt": updated if isinstance(updated, str) else None, "daily": {"available": False}}


def daily_credit_report(payload: object, today: str) -> dict[str, object]:
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise ValueError("Abacus daily log response was invalid")
    rows = ((payload.get("result") or {}).get("log") or [])
    if not isinstance(rows, list):
        raise ValueError("Abacus daily log response was incomplete")
    row = next((item for item in rows if isinstance(item, dict) and item.get("date") == today), None)
    if row is None: return {"available": True, "date": today, "totalCredits": 0.0, "sourceBuckets": {}}
    buckets = {str(key): round(float(value), 2) for key, value in row.items() if key not in {"date", "total"} and isinstance(value, (int, float))}
    return {"available": True, "date": today, "totalCredits": round(float(row.get("total") or 0), 2), "sourceBuckets": buckets}
