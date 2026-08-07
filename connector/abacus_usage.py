"""Pure, dependency-free helpers for safe Abacus credit reporting."""
import datetime as _dt
import math
import re
from urllib.parse import urlparse


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# The Abacus org compute-point log is a ROLLING ~30-DAY window. It is never
# calendar-month semantics: name it explicitly everywhere.
PERIOD_ROLLING_30D = "rolling_30d"
MAX_SNAPSHOT_ROWS = 500
MAX_SNAPSHOT_CREDITS = 1_000_000.0


def _finite_float(value: object, default: float = 0.0) -> float | None:
    """Round a present float value, or return ``default`` when absent/junk.

    Returns ``None`` when a *present* value is non-finite (NaN/Inf) so callers
    can skip poison rows instead of silently coercing them to a number.
    """
    if value is None:
        return default
    try:
        numeric = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if not math.isfinite(numeric):
        return None
    return round(numeric, 2)


def normalize_email(value: object) -> str:
    """Lowercase+trim an email; return '' when it is not a plausible address."""
    if not isinstance(value, str):
        return ""
    normalized = value.strip().lower()
    if len(normalized) > 254 or not EMAIL_RE.fullmatch(normalized):
        return ""
    return normalized


def _valid_dates(daily_rows: object) -> list[str]:
    if not isinstance(daily_rows, list):
        return []
    dates: list[str] = []
    for row in daily_rows:
        if not isinstance(row, dict):
            continue
        candidate = row.get("date")
        if not isinstance(candidate, str) or not DATE_RE.fullmatch(candidate):
            continue
        try:
            _dt.date.fromisoformat(candidate)
        except ValueError:
            continue
        dates.append(candidate)
    return sorted(dates)


def classify_credit_log_period(daily_rows: object, today: str) -> dict[str, object]:
    """Classify the covered window of the byUser:true credit log.

    The byUser:true log itself carries no date or period metadata; the Abacus
    org compute-point log is a rolling ~30-day window by contract. The dated
    byUser:false daily log, when present, supplies observed window bounds.
    The period is ALWAYS ``rolling_30d`` — never calendar-month. Bounds are
    null when no usable dates were observed.
    """
    dates = _valid_dates(daily_rows)
    if not dates:
        return {"type": PERIOD_ROLLING_30D, "key": PERIOD_ROLLING_30D, "window_start": None, "window_end": None}
    return {"type": PERIOD_ROLLING_30D, "key": PERIOD_ROLLING_30D, "window_start": dates[0], "window_end": dates[-1]}


def _snapshot_log_rows(payload: object) -> object:
    """Boundedly extract the compute-point log list from a sanitized Abacus payload.

    Canonical live responses nest the log under ``result.log``. When ``result``
    is a dict that explicitly carries a ``log`` key, that value is
    authoritative (the caller validates it is a list), so malformed canonical
    data fails closed rather than downgrading to a top-level fallback. When
    ``result`` carries no ``log`` key, fall back to a top-level ``log`` for
    legacy compatibility. Never coerces arbitrary iterables/objects and never
    copies raw data.
    """
    if not isinstance(payload, dict):
        return None
    result = payload.get("result")
    if isinstance(result, dict) and "log" in result:
        return result["log"]
    return payload.get("log")


def parse_by_user_log(payload: object, max_rows: int = MAX_SNAPSHOT_ROWS, max_credits: float = MAX_SNAPSHOT_CREDITS) -> dict[str, object]:
    """Sanitize a byUser:true compute-point log into {email, routeLlmCredits, totalCredits}.

    Drops the username and every non-RouteLLM bucket (privacy), validates and
    bounds every value, rejects duplicate normalized emails (never summed),
    and skips (with counting) any row that fails. Raises ValueError when the
    response envelope itself is unusable.
    """
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise ValueError("Abacus by-user log response was invalid")
    rows = _snapshot_log_rows(payload)
    if not isinstance(rows, list):
        raise ValueError("Abacus by-user log response was incomplete")
    users: list[dict[str, object]] = []
    seen_emails: set[str] = set()
    skipped = 0
    duplicates = 0
    for row in rows[:max_rows]:
        if not isinstance(row, dict):
            skipped += 1
            continue
        email = normalize_email(row.get("email"))
        total = _finite_float(row.get("total"))
        route_llm = _finite_float(row.get("RouteLLM API"))
        # NaN/Inf (None poison), negative, or over-cap values are skipped, not coerced.
        if not email or total is None or route_llm is None or total < 0 or route_llm < 0 or total > max_credits or route_llm > max_credits:
            skipped += 1
            continue
        if email in seen_emails:
            duplicates += 1
            continue
        seen_emails.add(email)
        users.append({"email": email, "routeLlmCredits": route_llm, "totalCredits": total})
    overflow = max(0, len(rows) - max_rows)
    return {"users": users, "stats": {"rows": len(users), "skipped": skipped + overflow, "duplicates": duplicates}}


def build_by_user_snapshot(by_user_payload: object, daily_payload: object, generated_at_ms: int, today: str) -> dict[str, object]:
    """Assemble the bounded, sanitized snapshot returned to the portal.

    Source metadata is explicit and bounded: a single source name, the
    ``rolling_30d`` period type (never calendar-month), and the fetch
    timestamp so the scheduler can judge freshness.
    """
    parsed = parse_by_user_log(by_user_payload)
    daily_rows = _snapshot_log_rows(daily_payload)
    period = classify_credit_log_period(daily_rows, today)
    return {
        "source": "abacus_credits_by_user",
        "periodType": PERIOD_ROLLING_30D,
        "period": period,
        "fetchedAt": generated_at_ms,
        "users": parsed["users"],
        "stats": parsed["stats"],
    }


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
