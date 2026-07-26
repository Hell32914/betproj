"""OpenAI vision assist for match/market picking and a hard Place gate."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from dotenv import dotenv_values, load_dotenv

_ENV_PATH = Path(__file__).resolve().with_name(".env")
load_dotenv(dotenv_path=_ENV_PATH, override=False)

OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
OPENAI_MODEL = (os.getenv("OPENAI_MODEL") or "gpt-4o").strip()
OPENAI_ASSIST_ENABLED = (os.getenv("OPENAI_ASSIST_ENABLED") or "1").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

# Soft assist: ignore model output below this confidence.
SOFT_MIN_CONFIDENCE = 0.55
# Hard Place gate: reject when confidence is below this, even if ok=true.
GATE_MIN_CONFIDENCE = 0.70
_STATUS_REPORTED = False


def _refresh_runtime_config() -> None:
    """Reload local configuration so launch cwd/import order cannot hide the key."""
    global OPENAI_API_KEY, OPENAI_MODEL, OPENAI_ASSIST_ENABLED
    load_dotenv(dotenv_path=_ENV_PATH, override=False)
    file_values = dotenv_values(_ENV_PATH) if _ENV_PATH.is_file() else {}
    OPENAI_API_KEY = (
        os.getenv("OPENAI_API_KEY") or file_values.get("OPENAI_API_KEY") or ""
    ).strip()
    OPENAI_MODEL = (
        os.getenv("OPENAI_MODEL") or file_values.get("OPENAI_MODEL") or "gpt-4o"
    ).strip()
    enabled_value = (
        os.getenv("OPENAI_ASSIST_ENABLED")
        or file_values.get("OPENAI_ASSIST_ENABLED")
        or "1"
    )
    OPENAI_ASSIST_ENABLED = str(enabled_value).strip().lower() in {
        "1", "true", "yes", "on",
    }


def is_openai_assist_enabled() -> bool:
    _refresh_runtime_config()
    return bool(OPENAI_ASSIST_ENABLED and OPENAI_API_KEY)


def _report_status_once(profile_label: str) -> None:
    global _STATUS_REPORTED
    _refresh_runtime_config()
    if _STATUS_REPORTED:
        return
    _STATUS_REPORTED = True
    if not OPENAI_ASSIST_ENABLED:
        detail = "disabled by OPENAI_ASSIST_ENABLED"
    elif not OPENAI_API_KEY:
        env_detail = (
            f"key absent in {_ENV_PATH}"
            if _ENV_PATH.is_file()
            else f".env not found at {_ENV_PATH}"
        )
        detail = f"unavailable: OPENAI_API_KEY missing ({env_detail})"
    else:
        detail = f"enabled, model={OPENAI_MODEL}"
    print(f"[{profile_label}] OpenAI assist: {detail}.", flush=True)


def signal_brief(signal: Any) -> dict[str, Any]:
    """Compact signal payload for vision prompts (no full raw Telegram text)."""
    market = getattr(signal, "market", None) or ""
    raw_text = (getattr(signal, "raw_text", None) or "").lower()
    is_sh_alert = "second half" in market.lower() or "sh goals" in raw_text
    return {
        "home_team": getattr(signal, "home_team", None),
        "away_team": getattr(signal, "away_team", None),
        "teams": getattr(signal, "teams", None),
        "league": getattr(signal, "league", None) or getattr(signal, "league_line", None),
        "country": getattr(signal, "country", None),
        "market": market,
        "placement_intent": (
            "full_time_absolute_line"
            if is_sh_alert
            else "ordinary_over_under_line"
        ),
        "selection": getattr(signal, "selection", None),
        "line": str(getattr(signal, "line", "")),
        "odds": str(getattr(signal, "odds", "")),
        "timer_minute": getattr(signal, "timer_minute", None),
        "score": getattr(signal, "score", None),
        "selection_label": getattr(signal, "selection_label", None),
    }


def screenshot_b64(driver: Any) -> str:
    """Return a PNG screenshot as base64 (no data-URL prefix)."""
    raw = driver.get_screenshot_as_base64()
    if isinstance(raw, bytes):
        raw = raw.decode("ascii", errors="ignore")
    return str(raw or "").strip()


def _extract_json_object(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        raise ValueError("Empty model response")
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not match:
            raise
        data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object, got {type(data).__name__}")
    return data


def ask_vision(
    task: str,
    brief: dict[str, Any],
    image_b64: str,
    *,
    candidates: list[dict[str, Any]] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Call OpenAI vision and parse a JSON object response.

    ``extra`` is optional context (target stake/odds, exchange name, etc.).
    """
    if not is_openai_assist_enabled():
        raise RuntimeError("OpenAI assist disabled or OPENAI_API_KEY missing")
    if not image_b64:
        raise RuntimeError("Screenshot is empty")

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("openai package is not installed; pip install openai") from exc

    payload: dict[str, Any] = {
        "task": task,
        "signal": brief,
    }
    if candidates is not None:
        payload["candidates"] = candidates
    if extra:
        payload["context"] = extra

    system = (
        "You assist a sports betting automation bot. "
        "Use the screenshot as the primary source of UI truth. "
        "Reply with a single JSON object only — no markdown, no prose. "
        "Never invent markets or matches that are not visible."
    )
    user_text = (
        "Analyze the screenshot with this JSON context and answer the task.\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )

    client = OpenAI(api_key=OPENAI_API_KEY)
    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        temperature=0,
        max_tokens=400,
        messages=[
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{image_b64}",
                            "detail": "high",
                        },
                    },
                ],
            },
        ],
    )
    content = ""
    try:
        content = response.choices[0].message.content or ""
    except Exception as exc:
        raise RuntimeError(f"OpenAI response missing content: {exc}") from exc
    return _extract_json_object(content)


def match_pick_is_ambiguous(candidates: list[dict[str, Any]]) -> bool:
    """Soft assist only when the heuristic ranking is unclear."""
    if len(candidates) < 2:
        return False
    try:
        top = float(candidates[0].get("score") or 0)
        second = float(candidates[1].get("score") or 0)
    except (TypeError, ValueError):
        return True
    if top < 90:
        return True
    return (top - second) < 25


def assist_pick_match(
    driver: Any,
    signal: Any,
    candidates: list[dict[str, Any]],
    *,
    profile_label: str = "Profile",
    exchange: str = "unknown",
) -> int | None:
    """Soft: return candidate index, or None to keep the heuristic pick."""
    _report_status_once(profile_label)
    if not is_openai_assist_enabled() or not candidates:
        return None
    if not match_pick_is_ambiguous(candidates):
        return None
    try:
        image = screenshot_b64(driver)
        result = ask_vision(
            task=(
                "Pick the search-result row that matches the signal match. "
                "Return JSON: "
                '{"index": <0-based index from candidates or null>, '
                '"confidence": <0..1>, "reason": "<short>"}'
            ),
            brief=signal_brief(signal),
            image_b64=image,
            candidates=[
                {
                    "index": i,
                    "text": str(item.get("text") or "")[:220],
                    "score": item.get("score"),
                }
                for i, item in enumerate(candidates)
            ],
            extra={"exchange": exchange, "profile": profile_label},
        )
        confidence = float(result.get("confidence") or 0)
        index = result.get("index")
        if index is None or confidence < SOFT_MIN_CONFIDENCE:
            print(
                f"[{profile_label}] OpenAI match assist: no pick "
                f"(confidence={confidence:.2f}, reason={result.get('reason')!r})",
                flush=True,
            )
            return None
        index_int = int(index)
        if index_int < 0 or index_int >= len(candidates):
            return None
        print(
            f"[{profile_label}] OpenAI match assist: picked={index_int} "
            f"confidence={confidence:.2f} reason={result.get('reason')!r}",
            flush=True,
        )
        return index_int
    except Exception as exc:
        print(f"[{profile_label}] OpenAI match assist failed (soft fallback): {exc}", flush=True)
        return None


def assist_pick_market(
    driver: Any,
    signal: Any,
    *,
    profile_label: str = "Profile",
    exchange: str = "unknown",
    candidates: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Soft: confirm market/line visibility; return model dict or None on soft failure."""
    _report_status_once(profile_label)
    if not is_openai_assist_enabled():
        return None
    brief = signal_brief(signal)
    try:
        image = screenshot_b64(driver)
        result = ask_vision(
            task=(
                "Confirm whether the target Over/Under market and line from the signal "
                "are visible and selectable on this page. IMPORTANT: InPlayGuru text "
                "'SH Goals' is only an alert strategy label; its numeric line is the "
                "absolute FULL-TIME match total shown by the exchange. Therefore use "
                "ordinary Asian Total Goals / Over-Under X Goals and do not require a "
                "Second Half tab. "
                "Return JSON: "
                '{"ok": true|false, "index": <candidate index or null>, '
                '"header": "<visible market header or null>", '
                '"confidence": <0..1>, "reason": "<short Russian or English>"}'
            ),
            brief=brief,
            image_b64=image,
            candidates=candidates,
            extra={"exchange": exchange, "profile": profile_label},
        )
        confidence = float(result.get("confidence") or 0)
        if confidence < SOFT_MIN_CONFIDENCE:
            print(
                f"[{profile_label}] OpenAI market assist: low confidence "
                f"({confidence:.2f}); ignoring. reason={result.get('reason')!r}",
                flush=True,
            )
            return None
        print(
            f"[{profile_label}] OpenAI market assist: ok={result.get('ok')} "
            f"header={result.get('header')!r} confidence={confidence:.2f} "
            f"reason={result.get('reason')!r}",
            flush=True,
        )
        return result
    except Exception as exc:
        print(f"[{profile_label}] OpenAI market assist failed (soft fallback): {exc}", flush=True)
        return None


def assist_pick_ui_action(
    driver: Any,
    signal: Any,
    candidates: list[dict[str, Any]],
    *,
    profile_label: str = "Profile",
    exchange: str = "unknown",
    goal: str = "focus the target market",
) -> int | None:
    """Choose one whitelisted DOM candidate to click/scroll into view."""
    _report_status_once(profile_label)
    if not is_openai_assist_enabled() or not candidates:
        return None
    try:
        result = ask_vision(
            task=(
                f"Choose the safest UI element to {goal}. Candidates are a whitelist "
                "collected from the current DOM; return only one candidate index. "
                "For an InPlayGuru 'SH Goals' alert, choose the ordinary absolute "
                "FULL-TIME Asian Total Goals / Over-Under X Goals element. Never "
                "choose Asian Handicap, team totals, correct score, or another line. "
                "Return JSON: "
                '{"index": <0-based index or null>, "confidence": <0..1>, '
                '"reason": "<short>"}'
            ),
            brief=signal_brief(signal),
            image_b64=screenshot_b64(driver),
            candidates=[
                {
                    "index": index,
                    "text": str(item.get("text") or "")[:180],
                    "context": str(item.get("context") or "")[:260],
                    "clickable": bool(item.get("clickable")),
                    "in_viewport": bool(item.get("in_viewport")),
                    "x": item.get("x"),
                    "y": item.get("y"),
                }
                for index, item in enumerate(candidates)
            ],
            extra={"exchange": exchange, "profile": profile_label, "goal": goal},
        )
        confidence = float(result.get("confidence") or 0)
        index = result.get("index")
        if index is None or confidence < SOFT_MIN_CONFIDENCE:
            print(
                f"[{profile_label}] OpenAI UI action: no pick "
                f"(confidence={confidence:.2f}, reason={result.get('reason')!r})",
                flush=True,
            )
            return None
        index_int = int(index)
        if not 0 <= index_int < len(candidates):
            return None
        print(
            f"[{profile_label}] OpenAI UI action: picked={index_int} "
            f"text={str(candidates[index_int].get('text') or '')[:80]!r} "
            f"confidence={confidence:.2f} reason={result.get('reason')!r}",
            flush=True,
        )
        return index_int
    except Exception as exc:
        print(f"[{profile_label}] OpenAI UI action failed (soft fallback): {exc}", flush=True)
        return None


def gate_place_bet(
    driver: Any,
    signal: Any,
    *,
    profile_label: str = "Profile",
    exchange: str = "unknown",
    stake: str | None = None,
    target_odds: str | None = None,
    filled_odds: str | None = None,
) -> dict[str, Any]:
    """Hard gate before Place. Fail-closed on API errors / low confidence / reject."""
    _report_status_once(profile_label)
    if not OPENAI_ASSIST_ENABLED:
        return {"ok": True, "reason": "openai-assist-disabled", "confidence": 1.0}
    if not OPENAI_API_KEY:
        reason = "OPENAI_API_KEY missing while OPENAI_ASSIST_ENABLED=1"
        print(f"[{profile_label}] OpenAI place gate: REJECT ({reason})", flush=True)
        return {"ok": False, "reason": reason, "confidence": 0.0}

    brief = signal_brief(signal)
    try:
        image = screenshot_b64(driver)
        result = ask_vision(
            task=(
                "Hard verification before clicking Place. Approve only if the betslip "
                "clearly matches the signal: correct match/teams, selection Over/Under, line, "
                "and odds at least the target when visible. Stake must not be empty. "
                "IMPORTANT: InPlayGuru 'SH Goals' is a strategy label and maps to the "
                "ordinary absolute FULL-TIME Over/Under line on the exchange; do not "
                "require or expect a Second Half market. "
                "Reject To Score / team totals / wrong period. "
                "Return JSON: "
                '{"ok": true|false, "confidence": <0..1>, "reason": "<short Russian>"}'
            ),
            brief=brief,
            image_b64=image,
            candidates=None,
            extra={
                "exchange": exchange,
                "profile": profile_label,
                "stake": stake,
                "target_odds": target_odds or str(brief.get("odds") or ""),
                "filled_odds": filled_odds,
            },
        )
    except Exception as exc:
        reason = f"OpenAI place gate недоступен: {exc}"
        print(f"[{profile_label}] OpenAI place gate: REJECT ({reason})", flush=True)
        return {"ok": False, "reason": reason, "confidence": 0.0}

    confidence = 0.0
    try:
        confidence = float(result.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    approved = bool(result.get("ok")) and confidence >= GATE_MIN_CONFIDENCE
    reason = str(result.get("reason") or ("ok" if approved else "отклонено"))
    if approved:
        print(
            f"[{profile_label}] OpenAI place gate: APPROVE confidence={confidence:.2f} "
            f"reason={reason!r}",
            flush=True,
        )
        return {"ok": True, "reason": reason, "confidence": confidence}

    print(
        f"[{profile_label}] OpenAI place gate: REJECT confidence={confidence:.2f} "
        f"reason={reason!r}",
        flush=True,
    )
    return {"ok": False, "reason": reason, "confidence": confidence}
