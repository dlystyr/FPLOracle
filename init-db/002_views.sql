-- Analytics views and functions

-- Player form trend: current vs recent snapshots
CREATE OR REPLACE VIEW v_player_form_trend AS
SELECT
    p.id AS player_id,
    p.web_name,
    p.form AS current_form,
    s1.form AS form_1gw_ago,
    s3.form AS form_3gw_ago,
    COALESCE(p.form - s1.form, 0) AS form_change_1gw,
    COALESCE(p.form - s3.form, 0) AS form_change_3gw
FROM players p
LEFT JOIN LATERAL (
    SELECT form FROM player_snapshots
    WHERE player_id = p.id ORDER BY gameweek DESC LIMIT 1
) s1 ON TRUE
LEFT JOIN LATERAL (
    SELECT form FROM player_snapshots
    WHERE player_id = p.id ORDER BY gameweek DESC OFFSET 2 LIMIT 1
) s3 ON TRUE;

-- Team fixture difficulty (upcoming)
CREATE OR REPLACE VIEW v_team_fixture_difficulty AS
SELECT
    t.id AS team_id,
    t.short_name,
    AVG(CASE WHEN f.team_h = t.id THEN f.team_h_difficulty ELSE f.team_a_difficulty END) AS avg_difficulty
FROM teams t
JOIN fixtures f ON (f.team_h = t.id OR f.team_a = t.id) AND NOT f.finished
GROUP BY t.id, t.short_name;

-- Price change candidates
CREATE OR REPLACE VIEW v_price_change_candidates AS
SELECT
    p.id AS player_id,
    p.web_name,
    t.short_name AS team,
    p.now_cost,
    p.transfers_in_event,
    p.transfers_out_event,
    (p.transfers_in_event - p.transfers_out_event) AS net_transfers,
    CASE
        WHEN (p.transfers_in_event - p.transfers_out_event) > 50000 THEN 'likely_rise'
        WHEN (p.transfers_in_event - p.transfers_out_event) < -50000 THEN 'likely_fall'
        ELSE 'stable'
    END AS prediction
FROM players p
JOIN teams t ON p.team_id = t.id
WHERE ABS(p.transfers_in_event - p.transfers_out_event) > 20000;

-- xG overperformers
CREATE OR REPLACE VIEW v_xg_overperformers AS
SELECT
    p.id AS player_id,
    p.web_name,
    t.short_name AS team,
    p.goals_scored,
    p.expected_goals AS xg,
    p.assists,
    p.expected_assists AS xa,
    (p.goals_scored - p.expected_goals) + (p.assists - p.expected_assists) AS total_overperformance,
    p.minutes
FROM players p
JOIN teams t ON p.team_id = t.id
WHERE p.minutes >= 450;

-- Populate team results from finished fixtures
CREATE OR REPLACE FUNCTION populate_team_results() RETURNS void AS $$
BEGIN
    INSERT INTO team_results (team_id, fixture_id, event, opponent_id, was_home, goals_for, goals_against, result, points, clean_sheet)
    SELECT
        t.id,
        f.id,
        f.event,
        CASE WHEN f.team_h = t.id THEN f.team_a ELSE f.team_h END,
        f.team_h = t.id,
        CASE WHEN f.team_h = t.id THEN f.team_h_score ELSE f.team_a_score END,
        CASE WHEN f.team_h = t.id THEN f.team_a_score ELSE f.team_h_score END,
        CASE
            WHEN (f.team_h = t.id AND f.team_h_score > f.team_a_score) OR
                 (f.team_a = t.id AND f.team_a_score > f.team_h_score) THEN 'W'
            WHEN f.team_h_score = f.team_a_score THEN 'D'
            ELSE 'L'
        END,
        CASE
            WHEN (f.team_h = t.id AND f.team_h_score > f.team_a_score) OR
                 (f.team_a = t.id AND f.team_a_score > f.team_h_score) THEN 3
            WHEN f.team_h_score = f.team_a_score THEN 1
            ELSE 0
        END,
        CASE WHEN f.team_h = t.id THEN f.team_a_score = 0 ELSE f.team_h_score = 0 END
    FROM fixtures f
    CROSS JOIN teams t
    WHERE f.finished = TRUE
      AND f.team_h_score IS NOT NULL
      AND (f.team_h = t.id OR f.team_a = t.id)
    ON CONFLICT (team_id, fixture_id) DO UPDATE SET
        goals_for = EXCLUDED.goals_for,
        goals_against = EXCLUDED.goals_against,
        result = EXCLUDED.result,
        points = EXCLUDED.points,
        clean_sheet = EXCLUDED.clean_sheet;
END;
$$ LANGUAGE plpgsql;

-- Populate player opponent history
CREATE OR REPLACE FUNCTION update_player_opponent_history() RETURNS void AS $$
BEGIN
    INSERT INTO player_opponent_history (player_id, opponent_id, games_played, total_points, goals, assists, avg_points)
    SELECT
        ph.player_id,
        ph.opponent_team,
        COUNT(*),
        SUM(ph.total_points),
        SUM(ph.goals_scored),
        SUM(ph.assists),
        ROUND(AVG(ph.total_points)::numeric, 2)
    FROM player_history ph
    GROUP BY ph.player_id, ph.opponent_team
    ON CONFLICT (player_id, opponent_id) DO UPDATE SET
        games_played = EXCLUDED.games_played,
        total_points = EXCLUDED.total_points,
        goals = EXCLUDED.goals,
        assists = EXCLUDED.assists,
        avg_points = EXCLUDED.avg_points;

    -- Mark bogey teams (<70% of player avg) and favourites (>130%)
    UPDATE player_opponent_history poh SET
        is_bogey_team = poh.avg_points < (
            SELECT COALESCE(AVG(ph2.total_points) * 0.7, 0)
            FROM player_history ph2 WHERE ph2.player_id = poh.player_id
        ),
        is_favourite = poh.avg_points > (
            SELECT COALESCE(AVG(ph2.total_points) * 1.3, 0)
            FROM player_history ph2 WHERE ph2.player_id = poh.player_id
        )
    WHERE poh.games_played >= 2;
END;
$$ LANGUAGE plpgsql;
