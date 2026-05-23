"""Parsing helpers for InPlayGuru Telegram betting signals."""

from dataclasses import dataclass
from decimal import Decimal
import re


@dataclass(frozen=True)
class BettingSignal:
    raw_text: str
    odds: Decimal
    selection: str
    line: Decimal
    market: str
    expiry: str
    league: str | None = None
    country: str | None = None
    league_line: str | None = None
    teams: str | None = None
    timer_minute: int | None = None
    score: str | None = None

    @property
    def selection_label(self) -> str:
        line_text = str(self.line.normalize())
        return f"{self.selection} {line_text}"

    @property
    def home_team(self) -> str | None:
        if not self.teams:
            return None
        parts = re.split(r"\s+vs\s+", self.teams, maxsplit=1, flags=re.IGNORECASE)
        if not parts or not parts[0].strip():
            return None
        return re.sub(r"[`*_~]+", "", parts[0]).strip() or None

    @property
    def away_team(self) -> str | None:
        if not self.teams:
            return None
        parts = re.split(r"\s+vs\s+", self.teams, maxsplit=1, flags=re.IGNORECASE)
        if len(parts) <= 1 or not parts[1].strip():
            return None
        return re.sub(r"[`*_~]+", "", parts[1]).strip() or None


ODDS_RE = re.compile(
    r"Odds:\s*(?P<odds>\d+(?:[.,]\d+)?)\s+"
    r"(?P<selection>Over|Under)\s+"
    r"(?P<line>\d+(?:[.,]\d+)?)(?P<title>[^\r\n]*)",
    re.IGNORECASE,
)
TIMER_RE = re.compile(r"Timer:\s*(?P<minute>\d+)'")
GOALS_RE = re.compile(r"Goals:\s*(?P<home>\d+)\s*-\s*(?P<away>\d+)")
RANKING_RE = re.compile(r"\s*\([^)]*\bvs\b[^)]*\)", re.IGNORECASE)

KNOWN_COUNTRIES = [
    "Saudi Arabia",
    "South Korea",
    "Hong Kong",
    "United Arab Emirates",
    "United States",
    "United Kingdom",
    "Czech Republic",
    "Costa Rica",
    "Japan",
    "China",
    "India",
    "Mongolia",
    "Portugal",
    "Vietnam",
]


def _clean_league_line(line: str) -> str:
    without_ranking = RANKING_RE.sub("", line).strip()
    return re.sub(r"^[^A-Za-z]+", "", without_ranking).strip()


def _split_country_league(league_line: str | None) -> tuple[str | None, str | None]:
    if not league_line:
        return None, None

    for country in sorted(KNOWN_COUNTRIES, key=len, reverse=True):
        if league_line.lower().startswith(country.lower() + " "):
            return country, league_line[len(country):].strip()

    parts = league_line.split(maxsplit=1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return None, league_line


def _detect_market(title_tail: str) -> str:
    normalized = re.sub(r"[`*_~]+", "", title_tail or "").lower()
    normalized = re.sub(r"\s+", " ", normalized)
    if "sh goals" in normalized or "second half" in normalized or re.search(r"\b2h\b", normalized):
        return "SECOND HALF GOALS"
    if "next goal" in normalized:
        return "NEXT GOAL BEFORE"
    return "FULL TIME OVER/UNDER"


def parse_betting_signal(text: str) -> BettingSignal | None:
    """Return a parsed betting signal, or None when a message is not actionable."""
    odds_match = ODDS_RE.search(text)
    if not odds_match:
        return None

    league_line = None
    teams = None
    for line in text.splitlines():
        normalized = line.strip()
        is_ranking_line = re.search(r"\(\d+(?:st|nd|rd|th)?\s+vs\s+\d+", normalized, re.IGNORECASE)
        if " vs " in normalized.lower() and not is_ranking_line:
            # Strip Markdown formatting (backticks / asterisks / underscores) that the
            # InPlayGuru bot wraps around team names — otherwise they leak into the
            # Black search query and break match lookup.
            cleaned = re.sub(r"[`*_~]+", "", normalized)
            cleaned = re.sub(r"\s+", " ", cleaned).strip()
            teams = cleaned
            break
        if is_ranking_line and league_line is None:
            league_line = _clean_league_line(normalized)

    country, league = _split_country_league(league_line)

    timer_match = TIMER_RE.search(text)
    goals_match = GOALS_RE.search(text)

    market = _detect_market(odds_match.group("title") or "")

    return BettingSignal(
        raw_text=text,
        odds=Decimal(odds_match.group("odds").replace(",", ".")),
        selection=odds_match.group("selection").title(),
        line=Decimal(odds_match.group("line").replace(",", ".")),
        market=market,
        expiry="30 min",
        league=league,
        country=country,
        league_line=league_line,
        teams=teams,
        timer_minute=int(timer_match.group("minute")) if timer_match else None,
        score=f"{goals_match.group('home')}-{goals_match.group('away')}" if goals_match else None,
    )