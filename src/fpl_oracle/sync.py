"""Data sync: FPL API → PostgreSQL + Redis cache."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from dateutil.parser import isoparse

from fpl_oracle import db, cache, fpl_api
from fpl_oracle.log import get_logger

log = get_logger(__name__)


def _parse_dt(val: Any) -> datetime | None:
    """Parse a datetime string from the FPL API into a datetime object."""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    try:
        return isoparse(str(val))
    except (ValueError, TypeError):
        return None


async def run_sync() -> dict[str, Any]:
    """Full data sync from FPL API into Postgres and Redis.

    Returns summary with record counts.
    """
    started = datetime.now(timezone.utc)
    log.info("sync_started")

    # Log start
    await db.execute(
        "INSERT INTO sync_log (sync_type, started_at, status) VALUES ($1, $2, $3)",
        "full", started, "running",
    )

    try:
        bootstrap = await fpl_api.bootstrap()

        teams_data = bootstrap.get("teams", [])
        players_data = bootstrap.get("elements", [])
        events_data = bootstrap.get("events", [])
        fixtures_data = await fpl_api.fixtures()

        # Sync teams
        teams_count = await _sync_teams(teams_data)
        log.info("synced_teams", count=teams_count)

        # Sync events
        events_count = await _sync_events(events_data)
        log.info("synced_events", count=events_count)

        # Sync players
        players_count = await _sync_players(players_data)
        log.info("synced_players", count=players_count)

        # Sync fixtures
        fixtures_count = await _sync_fixtures(fixtures_data)
        log.info("synced_fixtures", count=fixtures_count)

        # Sync player history (per-GW stats)
        current_gw = next(
            (e["id"] for e in events_data if e.get("is_current")), None
        )
        if current_gw:
            history_count = await _sync_player_history(players_data, current_gw)
            log.info("synced_player_history", count=history_count)

            # Player snapshots for current GW
            snap_count = await _sync_snapshots(players_data, current_gw)
            log.info("synced_snapshots", count=snap_count)
        else:
            history_count = 0
            snap_count = 0

        # Run stored procs
        await db.execute("SELECT populate_team_results()")
        await db.execute("SELECT update_player_opponent_history()")
        log.info("stored_procs_executed")

        # Populate Redis cache
        await _populate_cache(teams_data, players_data, current_gw)
        log.info("cache_populated")

        total = teams_count + events_count + players_count + fixtures_count
        completed = datetime.now(timezone.utc)

        await db.execute(
            "UPDATE sync_log SET completed_at = $1, status = $2, records_updated = $3 "
            "WHERE sync_type = 'full' AND started_at = $4",
            completed, "success", total, started,
        )

        duration = (completed - started).total_seconds()
        log.info("sync_completed", duration_s=round(duration, 1), records=total)

        return {
            "status": "success",
            "teams": teams_count,
            "events": events_count,
            "players": players_count,
            "fixtures": fixtures_count,
            "history": history_count,
            "snapshots": snap_count,
            "duration_s": round(duration, 1),
        }

    except Exception as e:
        log.error("sync_failed", error=str(e), exc_info=True)
        await db.execute(
            "UPDATE sync_log SET completed_at = $1, status = $2, error_message = $3 "
            "WHERE sync_type = 'full' AND started_at = $4",
            datetime.now(timezone.utc), "failed", str(e), started,
        )
        return {"status": "failed", "error": str(e)}


async def _sync_teams(teams: list[dict]) -> int:
    count = 0
    for t in teams:
        await db.execute(
            "INSERT INTO teams (id, name, short_name, code, strength, "
            "strength_overall_home, strength_overall_away, "
            "strength_attack_home, strength_attack_away, "
            "strength_defence_home, strength_defence_away) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) "
            "ON CONFLICT (id) DO UPDATE SET "
            "name=EXCLUDED.name, short_name=EXCLUDED.short_name, "
            "strength=EXCLUDED.strength, "
            "strength_overall_home=EXCLUDED.strength_overall_home, "
            "strength_overall_away=EXCLUDED.strength_overall_away, "
            "strength_attack_home=EXCLUDED.strength_attack_home, "
            "strength_attack_away=EXCLUDED.strength_attack_away, "
            "strength_defence_home=EXCLUDED.strength_defence_home, "
            "strength_defence_away=EXCLUDED.strength_defence_away",
            t["id"], t["name"], t["short_name"], t.get("code"),
            t.get("strength"),
            t.get("strength_overall_home"), t.get("strength_overall_away"),
            t.get("strength_attack_home"), t.get("strength_attack_away"),
            t.get("strength_defence_home"), t.get("strength_defence_away"),
        )
        count += 1
    return count


async def _sync_events(events: list[dict]) -> int:
    count = 0
    for e in events:
        dl = _parse_dt(e.get("deadline_time"))
        await db.execute(
            "INSERT INTO events (id, name, deadline_time, finished, "
            "is_current, is_next, is_previous, "
            "average_entry_score, highest_score, "
            "most_selected, most_captained, most_vice_captained) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12) "
            "ON CONFLICT (id) DO UPDATE SET "
            "name=EXCLUDED.name, deadline_time=EXCLUDED.deadline_time, "
            "finished=EXCLUDED.finished, is_current=EXCLUDED.is_current, "
            "is_next=EXCLUDED.is_next, is_previous=EXCLUDED.is_previous, "
            "average_entry_score=EXCLUDED.average_entry_score, "
            "highest_score=EXCLUDED.highest_score, "
            "most_selected=EXCLUDED.most_selected, "
            "most_captained=EXCLUDED.most_captained, "
            "most_vice_captained=EXCLUDED.most_vice_captained",
            e["id"], e["name"], dl,
            e.get("finished", False), e.get("is_current", False),
            e.get("is_next", False), e.get("is_previous", False),
            e.get("average_entry_score"), e.get("highest_score"),
            e.get("most_selected"), e.get("most_captained"),
            e.get("most_vice_captained"),
        )
        count += 1
    return count


async def _sync_players(players: list[dict]) -> int:
    count = 0
    for p in players:
        await db.execute(
            "INSERT INTO players (id, code, first_name, second_name, web_name, "
            "team_id, element_type, now_cost, selected_by_percent, "
            "transfers_in_event, transfers_out_event, form, points_per_game, "
            "total_points, minutes, goals_scored, assists, clean_sheets, "
            "goals_conceded, own_goals, penalties_saved, penalties_missed, "
            "yellow_cards, red_cards, saves, bonus, bps, "
            "expected_goals, expected_assists, expected_goal_involvements, "
            "expected_goals_conceded, influence, creativity, threat, ict_index, "
            "starts, status, chance_of_playing_next_round, "
            "chance_of_playing_this_round, news, news_added) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,"
            "$18,$19,$20,$21,$22,$23,$24,$25,$26,$27,$28,$29,$30,$31,$32,$33,$34,"
            "$35,$36,$37,$38,$39,$40,$41) "
            "ON CONFLICT (id) DO UPDATE SET "
            "code=EXCLUDED.code, first_name=EXCLUDED.first_name, "
            "second_name=EXCLUDED.second_name, web_name=EXCLUDED.web_name, "
            "team_id=EXCLUDED.team_id, element_type=EXCLUDED.element_type, "
            "now_cost=EXCLUDED.now_cost, selected_by_percent=EXCLUDED.selected_by_percent, "
            "transfers_in_event=EXCLUDED.transfers_in_event, "
            "transfers_out_event=EXCLUDED.transfers_out_event, "
            "form=EXCLUDED.form, points_per_game=EXCLUDED.points_per_game, "
            "total_points=EXCLUDED.total_points, minutes=EXCLUDED.minutes, "
            "goals_scored=EXCLUDED.goals_scored, assists=EXCLUDED.assists, "
            "clean_sheets=EXCLUDED.clean_sheets, goals_conceded=EXCLUDED.goals_conceded, "
            "own_goals=EXCLUDED.own_goals, penalties_saved=EXCLUDED.penalties_saved, "
            "penalties_missed=EXCLUDED.penalties_missed, "
            "yellow_cards=EXCLUDED.yellow_cards, red_cards=EXCLUDED.red_cards, "
            "saves=EXCLUDED.saves, bonus=EXCLUDED.bonus, bps=EXCLUDED.bps, "
            "expected_goals=EXCLUDED.expected_goals, "
            "expected_assists=EXCLUDED.expected_assists, "
            "expected_goal_involvements=EXCLUDED.expected_goal_involvements, "
            "expected_goals_conceded=EXCLUDED.expected_goals_conceded, "
            "influence=EXCLUDED.influence, creativity=EXCLUDED.creativity, "
            "threat=EXCLUDED.threat, ict_index=EXCLUDED.ict_index, "
            "starts=EXCLUDED.starts, status=EXCLUDED.status, "
            "chance_of_playing_next_round=EXCLUDED.chance_of_playing_next_round, "
            "chance_of_playing_this_round=EXCLUDED.chance_of_playing_this_round, "
            "news=EXCLUDED.news, news_added=EXCLUDED.news_added",
            p["id"], p.get("code"), p.get("first_name"), p.get("second_name"),
            p.get("web_name"), p.get("team"), p.get("element_type"),
            p.get("now_cost"), p.get("selected_by_percent"),
            p.get("transfers_in_event", 0), p.get("transfers_out_event", 0),
            p.get("form"), p.get("points_per_game"),
            p.get("total_points", 0), p.get("minutes", 0),
            p.get("goals_scored", 0), p.get("assists", 0),
            p.get("clean_sheets", 0), p.get("goals_conceded", 0),
            p.get("own_goals", 0), p.get("penalties_saved", 0),
            p.get("penalties_missed", 0), p.get("yellow_cards", 0),
            p.get("red_cards", 0), p.get("saves", 0),
            p.get("bonus", 0), p.get("bps", 0),
            p.get("expected_goals"), p.get("expected_assists"),
            p.get("expected_goal_involvements"), p.get("expected_goals_conceded"),
            p.get("influence"), p.get("creativity"),
            p.get("threat"), p.get("ict_index"),
            p.get("starts", 0), p.get("status", "a"),
            p.get("chance_of_playing_next_round"),
            p.get("chance_of_playing_this_round"),
            p.get("news"), _parse_dt(p.get("news_added")),
        )
        count += 1
    return count


async def _sync_fixtures(fixtures: list[dict]) -> int:
    count = 0
    for f in fixtures:
        await db.execute(
            "INSERT INTO fixtures (id, code, event, team_h, team_a, "
            "team_h_score, team_a_score, finished, kickoff_time, "
            "team_h_difficulty, team_a_difficulty) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) "
            "ON CONFLICT (id) DO UPDATE SET "
            "code=EXCLUDED.code, event=EXCLUDED.event, "
            "team_h=EXCLUDED.team_h, team_a=EXCLUDED.team_a, "
            "team_h_score=EXCLUDED.team_h_score, team_a_score=EXCLUDED.team_a_score, "
            "finished=EXCLUDED.finished, kickoff_time=EXCLUDED.kickoff_time, "
            "team_h_difficulty=EXCLUDED.team_h_difficulty, "
            "team_a_difficulty=EXCLUDED.team_a_difficulty",
            f["id"], f.get("code"), f.get("event"),
            f.get("team_h"), f.get("team_a"),
            f.get("team_h_score"), f.get("team_a_score"),
            f.get("finished", False), _parse_dt(f.get("kickoff_time")),
            f.get("team_h_difficulty"), f.get("team_a_difficulty"),
        )
        count += 1
    return count


async def _sync_player_history(players: list[dict], current_gw: int) -> int:
    """Sync player history from element-summary endpoints."""
    count = 0
    # Only sync players with significant minutes to avoid excessive API calls
    active = [p for p in players if (p.get("minutes", 0) or 0) > 0]

    for p in active:
        try:
            summary = await fpl_api.element_summary(p["id"])
            history = summary.get("history", [])
            for h in history:
                await db.execute(
                    "INSERT INTO player_history "
                    "(player_id, fixture_id, event, opponent_team, was_home, "
                    "total_points, goals_scored, assists, clean_sheets, "
                    "bonus, bps, expected_goals, expected_assists, "
                    "expected_goal_involvements, expected_goals_conceded, "
                    "influence, creativity, threat, ict_index, "
                    "value, selected, minutes) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,"
                    "$16,$17,$18,$19,$20,$21,$22) "
                    "ON CONFLICT (player_id, event) DO UPDATE SET "
                    "total_points=EXCLUDED.total_points, "
                    "goals_scored=EXCLUDED.goals_scored, "
                    "assists=EXCLUDED.assists, clean_sheets=EXCLUDED.clean_sheets, "
                    "bonus=EXCLUDED.bonus, bps=EXCLUDED.bps, "
                    "expected_goals=EXCLUDED.expected_goals, "
                    "expected_assists=EXCLUDED.expected_assists, "
                    "expected_goal_involvements=EXCLUDED.expected_goal_involvements, "
                    "expected_goals_conceded=EXCLUDED.expected_goals_conceded, "
                    "influence=EXCLUDED.influence, creativity=EXCLUDED.creativity, "
                    "threat=EXCLUDED.threat, ict_index=EXCLUDED.ict_index, "
                    "value=EXCLUDED.value, selected=EXCLUDED.selected, "
                    "minutes=EXCLUDED.minutes",
                    p["id"], h.get("fixture"), h.get("round"),
                    h.get("opponent_team"), h.get("was_home", False),
                    h.get("total_points", 0), h.get("goals_scored", 0),
                    h.get("assists", 0), h.get("clean_sheets", 0),
                    h.get("bonus", 0), h.get("bps", 0),
                    h.get("expected_goals"), h.get("expected_assists"),
                    h.get("expected_goal_involvements"),
                    h.get("expected_goals_conceded"),
                    h.get("influence"), h.get("creativity"),
                    h.get("threat"), h.get("ict_index"),
                    h.get("value"), h.get("selected"),
                    h.get("minutes", 0),
                )
                count += 1
            # Small delay to be respectful to FPL API
            await asyncio.sleep(0.1)
        except Exception:
            log.warning("player_history_sync_failed", player_id=p["id"], exc_info=True)

    return count


async def _sync_snapshots(players: list[dict], current_gw: int) -> int:
    count = 0
    for p in players:
        await db.execute(
            "INSERT INTO player_snapshots "
            "(player_id, gameweek, now_cost, selected_by_percent, form, "
            "points_per_game, total_points, minutes, goals_scored, assists, "
            "clean_sheets, goals_conceded, bonus, bps, "
            "expected_goals, expected_assists, expected_goal_involvements, "
            "ict_index, influence, creativity, threat, "
            "transfers_in_event, transfers_out_event) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,"
            "$18,$19,$20,$21,$22,$23) "
            "ON CONFLICT (player_id, gameweek) DO UPDATE SET "
            "now_cost=EXCLUDED.now_cost, selected_by_percent=EXCLUDED.selected_by_percent, "
            "form=EXCLUDED.form, points_per_game=EXCLUDED.points_per_game, "
            "total_points=EXCLUDED.total_points, minutes=EXCLUDED.minutes, "
            "goals_scored=EXCLUDED.goals_scored, assists=EXCLUDED.assists, "
            "clean_sheets=EXCLUDED.clean_sheets, goals_conceded=EXCLUDED.goals_conceded, "
            "bonus=EXCLUDED.bonus, bps=EXCLUDED.bps, "
            "expected_goals=EXCLUDED.expected_goals, "
            "expected_assists=EXCLUDED.expected_assists, "
            "expected_goal_involvements=EXCLUDED.expected_goal_involvements, "
            "ict_index=EXCLUDED.ict_index, influence=EXCLUDED.influence, "
            "creativity=EXCLUDED.creativity, threat=EXCLUDED.threat, "
            "transfers_in_event=EXCLUDED.transfers_in_event, "
            "transfers_out_event=EXCLUDED.transfers_out_event",
            p["id"], current_gw, p.get("now_cost"),
            p.get("selected_by_percent"), p.get("form"),
            p.get("points_per_game"), p.get("total_points", 0),
            p.get("minutes", 0), p.get("goals_scored", 0),
            p.get("assists", 0), p.get("clean_sheets", 0),
            p.get("goals_conceded", 0), p.get("bonus", 0),
            p.get("bps", 0), p.get("expected_goals"),
            p.get("expected_assists"), p.get("expected_goal_involvements"),
            p.get("ict_index"), p.get("influence"),
            p.get("creativity"), p.get("threat"),
            p.get("transfers_in_event", 0), p.get("transfers_out_event", 0),
        )
        count += 1
    return count


async def _populate_cache(
    teams: list[dict], players: list[dict], current_gw: int | None
) -> None:
    """Populate Redis with key data for fast tool access."""
    # Teams
    team_map = {t["id"]: t for t in teams}
    await cache.set("fpl:teams:all", teams, ttl=7200)
    for t in teams:
        await cache.set(f"fpl:team:{t['id']}", t, ttl=7200)

    # Players with team info
    for p in players:
        team = team_map.get(p.get("team", 0), {})
        p["team_short_name"] = team.get("short_name", "???")
        await cache.set(f"fpl:player:{p['id']}", p, ttl=3600)

    await cache.set("fpl:players:all", players, ttl=3600)

    if current_gw:
        await cache.set("fpl:current_gw", current_gw, ttl=3600)

    from datetime import datetime, timezone
    await cache.set(
        "fpl:last_updated",
        datetime.now(timezone.utc).isoformat(),
        ttl=86400,
    )
