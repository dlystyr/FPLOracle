from __future__ import annotations

from pydantic import BaseModel, ConfigDict

# Position map: element_type int → short string
POS_MAP = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
POS_REVERSE = {v: k for k, v in POS_MAP.items()}


class PlayerRef(BaseModel):
    """Compact player reference used across all tool responses."""

    model_config = ConfigDict(extra="forbid")

    id: int
    name: str  # web_name
    team: str  # short_name e.g. "ARS"
    pos: str  # GK/DEF/MID/FWD
    price: float  # £m


class PlayerDetail(PlayerRef):
    """Full player info returned by get_players."""

    form: float
    points: int
    minutes: int
    goals: int
    assists: int
    cs: int
    xg: float
    xa: float
    ict: float
    ownership: float
    status: str
    news: str | None = None


class ScoredPlayer(PlayerRef):
    """Player with expected-points scoring."""

    xpts: float
    form: float
    minutes: int
    xg: float | None = None
    xa: float | None = None
    fixture_run: str | None = None  # e.g. "2.4 avg (excellent)"
    score: float | None = None  # composite score for rankings


class CompareRow(PlayerDetail):
    """Player row for side-by-side comparison."""

    xpts: float | None = None
    ppg: float | None = None


class FixtureInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gw: int
    opponent: str  # short_name
    home: bool
    difficulty: int
    kickoff: str | None = None


class TeamOutlook(BaseModel):
    model_config = ConfigDict(extra="forbid")

    team: str
    avg_diff: float
    run_quality: str  # excellent / good / mixed / tough
    fixtures: list[FixtureInfo]
    is_dgw: list[int] | None = None  # GWs with double
    is_bgw: list[int] | None = None  # GWs blanking


class ManagerOverview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manager_id: int
    name: str
    team_name: str
    overall_rank: int | None = None
    overall_points: int | None = None
    bank: float | None = None  # £m remaining
    team_value: float | None = None


class SquadPlayer(ScoredPlayer):
    """Player in a manager's squad with role info."""

    starting: bool = True
    captain: bool = False
    vice_captain: bool = False
    multiplier: int = 1


class TransferSuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    out: PlayerRef
    in_: ScoredPlayer  # using in_ to avoid keyword clash
    net_cost: float  # price difference
    xpts_gain: float
    reason: str


class CaptainPick(BaseModel):
    model_config = ConfigDict(extra="forbid")

    player: PlayerRef
    captain_score: float
    next_fixture: str  # "bur (H)"
    xpts: float
    form: float


class LivePlayerGW(BaseModel):
    model_config = ConfigDict(extra="forbid")

    player: PlayerRef
    points: int
    bps: int
    minutes: int
    goals: int = 0
    assists: int = 0
    bonus: int = 0


class EnrichedStats(BaseModel):
    model_config = ConfigDict(extra="forbid")

    player: PlayerRef
    understat: dict | None = None
    fbref: dict | None = None
    summary: str | None = None
