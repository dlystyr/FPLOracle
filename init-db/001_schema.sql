-- FPLOracle core schema

CREATE TABLE IF NOT EXISTS teams (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    short_name      TEXT NOT NULL,
    code            INTEGER,
    strength        INTEGER,
    strength_overall_home   INTEGER,
    strength_overall_away   INTEGER,
    strength_attack_home    INTEGER,
    strength_attack_away    INTEGER,
    strength_defence_home   INTEGER,
    strength_defence_away   INTEGER
);

CREATE TABLE IF NOT EXISTS players (
    id                      INTEGER PRIMARY KEY,
    code                    INTEGER,
    first_name              TEXT,
    second_name             TEXT,
    web_name                TEXT NOT NULL,
    team_id                 INTEGER REFERENCES teams(id),
    element_type            INTEGER NOT NULL,  -- 1=GK, 2=DEF, 3=MID, 4=FWD
    now_cost                INTEGER NOT NULL,  -- price * 10
    selected_by_percent     NUMERIC(5,2),
    transfers_in_event      INTEGER DEFAULT 0,
    transfers_out_event     INTEGER DEFAULT 0,
    form                    NUMERIC(5,2),
    points_per_game         NUMERIC(5,2),
    total_points            INTEGER DEFAULT 0,
    minutes                 INTEGER DEFAULT 0,
    goals_scored            INTEGER DEFAULT 0,
    assists                 INTEGER DEFAULT 0,
    clean_sheets            INTEGER DEFAULT 0,
    goals_conceded          INTEGER DEFAULT 0,
    own_goals               INTEGER DEFAULT 0,
    penalties_saved         INTEGER DEFAULT 0,
    penalties_missed        INTEGER DEFAULT 0,
    yellow_cards            INTEGER DEFAULT 0,
    red_cards               INTEGER DEFAULT 0,
    saves                   INTEGER DEFAULT 0,
    bonus                   INTEGER DEFAULT 0,
    bps                     INTEGER DEFAULT 0,
    expected_goals          NUMERIC(6,2) DEFAULT 0,
    expected_assists        NUMERIC(6,2) DEFAULT 0,
    expected_goal_involvements  NUMERIC(6,2) DEFAULT 0,
    expected_goals_conceded     NUMERIC(6,2) DEFAULT 0,
    influence               NUMERIC(8,2) DEFAULT 0,
    creativity              NUMERIC(8,2) DEFAULT 0,
    threat                  NUMERIC(8,2) DEFAULT 0,
    ict_index               NUMERIC(8,2) DEFAULT 0,
    starts                  INTEGER DEFAULT 0,
    status                  TEXT DEFAULT 'a',
    chance_of_playing_next_round  INTEGER,
    chance_of_playing_this_round  INTEGER,
    news                    TEXT,
    news_added              TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS events (
    id                  INTEGER PRIMARY KEY,
    name                TEXT NOT NULL,
    deadline_time       TIMESTAMPTZ,
    finished            BOOLEAN DEFAULT FALSE,
    is_current          BOOLEAN DEFAULT FALSE,
    is_next             BOOLEAN DEFAULT FALSE,
    is_previous         BOOLEAN DEFAULT FALSE,
    average_entry_score INTEGER,
    highest_score       INTEGER,
    most_selected       INTEGER,
    most_captained      INTEGER,
    most_vice_captained INTEGER
);

CREATE TABLE IF NOT EXISTS fixtures (
    id                  INTEGER PRIMARY KEY,
    code                INTEGER,
    event               INTEGER REFERENCES events(id),
    team_h              INTEGER REFERENCES teams(id),
    team_a              INTEGER REFERENCES teams(id),
    team_h_score        INTEGER,
    team_a_score        INTEGER,
    finished            BOOLEAN DEFAULT FALSE,
    kickoff_time        TIMESTAMPTZ,
    team_h_difficulty   INTEGER,
    team_a_difficulty   INTEGER
);

CREATE TABLE IF NOT EXISTS player_history (
    id              SERIAL PRIMARY KEY,
    player_id       INTEGER REFERENCES players(id),
    fixture_id      INTEGER,
    event           INTEGER,
    opponent_team   INTEGER REFERENCES teams(id),
    was_home        BOOLEAN,
    total_points    INTEGER,
    goals_scored    INTEGER DEFAULT 0,
    assists         INTEGER DEFAULT 0,
    clean_sheets    INTEGER DEFAULT 0,
    bonus           INTEGER DEFAULT 0,
    bps             INTEGER DEFAULT 0,
    expected_goals  NUMERIC(6,2) DEFAULT 0,
    expected_assists        NUMERIC(6,2) DEFAULT 0,
    expected_goal_involvements  NUMERIC(6,2) DEFAULT 0,
    expected_goals_conceded     NUMERIC(6,2) DEFAULT 0,
    influence       NUMERIC(8,2) DEFAULT 0,
    creativity      NUMERIC(8,2) DEFAULT 0,
    threat          NUMERIC(8,2) DEFAULT 0,
    ict_index       NUMERIC(8,2) DEFAULT 0,
    value           INTEGER,
    selected        INTEGER,
    minutes         INTEGER DEFAULT 0,
    UNIQUE(player_id, event)
);

CREATE TABLE IF NOT EXISTS player_snapshots (
    id                  SERIAL PRIMARY KEY,
    player_id           INTEGER REFERENCES players(id),
    gameweek            INTEGER NOT NULL,
    now_cost            INTEGER,
    selected_by_percent NUMERIC(5,2),
    form                NUMERIC(5,2),
    points_per_game     NUMERIC(5,2),
    total_points        INTEGER,
    minutes             INTEGER,
    goals_scored        INTEGER DEFAULT 0,
    assists             INTEGER DEFAULT 0,
    clean_sheets        INTEGER DEFAULT 0,
    goals_conceded      INTEGER DEFAULT 0,
    bonus               INTEGER DEFAULT 0,
    bps                 INTEGER DEFAULT 0,
    expected_goals      NUMERIC(6,2) DEFAULT 0,
    expected_assists    NUMERIC(6,2) DEFAULT 0,
    expected_goal_involvements  NUMERIC(6,2) DEFAULT 0,
    ict_index           NUMERIC(8,2) DEFAULT 0,
    influence           NUMERIC(8,2) DEFAULT 0,
    creativity          NUMERIC(8,2) DEFAULT 0,
    threat              NUMERIC(8,2) DEFAULT 0,
    transfers_in_event  INTEGER DEFAULT 0,
    transfers_out_event INTEGER DEFAULT 0,
    UNIQUE(player_id, gameweek)
);

CREATE TABLE IF NOT EXISTS sync_log (
    id              SERIAL PRIMARY KEY,
    sync_type       TEXT NOT NULL,
    started_at      TIMESTAMPTZ DEFAULT NOW(),
    completed_at    TIMESTAMPTZ,
    status          TEXT DEFAULT 'running',
    records_updated INTEGER DEFAULT 0,
    error_message   TEXT
);

CREATE TABLE IF NOT EXISTS team_results (
    id              SERIAL PRIMARY KEY,
    team_id         INTEGER REFERENCES teams(id),
    fixture_id      INTEGER REFERENCES fixtures(id),
    event           INTEGER,
    opponent_id     INTEGER REFERENCES teams(id),
    was_home        BOOLEAN,
    goals_for       INTEGER,
    goals_against   INTEGER,
    result          TEXT,  -- W/D/L
    points          INTEGER,
    clean_sheet     BOOLEAN,
    UNIQUE(team_id, fixture_id)
);

CREATE TABLE IF NOT EXISTS player_opponent_history (
    id              SERIAL PRIMARY KEY,
    player_id       INTEGER REFERENCES players(id),
    opponent_id     INTEGER REFERENCES teams(id),
    games_played    INTEGER DEFAULT 0,
    total_points    INTEGER DEFAULT 0,
    goals           INTEGER DEFAULT 0,
    assists         INTEGER DEFAULT 0,
    avg_points      NUMERIC(5,2) DEFAULT 0,
    is_bogey_team   BOOLEAN DEFAULT FALSE,
    is_favourite    BOOLEAN DEFAULT FALSE,
    UNIQUE(player_id, opponent_id)
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_players_team ON players(team_id);
CREATE INDEX IF NOT EXISTS idx_players_element_type ON players(element_type);
CREATE INDEX IF NOT EXISTS idx_players_form ON players(form DESC);
CREATE INDEX IF NOT EXISTS idx_player_history_player ON player_history(player_id);
CREATE INDEX IF NOT EXISTS idx_player_history_event ON player_history(event);
CREATE INDEX IF NOT EXISTS idx_player_snapshots_player_gw ON player_snapshots(player_id, gameweek);
CREATE INDEX IF NOT EXISTS idx_fixtures_event ON fixtures(event);
CREATE INDEX IF NOT EXISTS idx_team_results_team ON team_results(team_id);
