-- 001_initial — the whole schema.
--
-- STRICT tables throughout: SQLite's default type affinity will silently store
-- a string in an INTEGER column, which is exactly the class of quiet corruption
-- this project exists to stop.
--
-- Timestamps are ISO8601 UTC TEXT. Dates are ISO8601 date TEXT. JSON is TEXT.
--
-- Founder-owned data lives in its own tables (founder_mark, person_founder) so
-- that "no worker may write it" is a structural fact, not a convention that a
-- later refactor can quietly bypass.

-- ---------------------------------------------------------------- networks --

CREATE TABLE network (
    network_id       TEXT PRIMARY KEY,          -- matches config/networks.yaml id
    name             TEXT NOT NULL,
    directory_url    TEXT,
    discovery_method TEXT,
    sectors          TEXT NOT NULL DEFAULT '[]',
    tier             TEXT NOT NULL CHECK (tier IN ('A','B','C')),
    node_count_actual INTEGER,                  -- established by W1; never the estimate
    last_refreshed   TEXT
) STRICT;

-- ----------------------------------------------------------- organizations --

CREATE TABLE organization (
    org_id            TEXT PRIMARY KEY,
    canonical_domain  TEXT NOT NULL,
    name              TEXT NOT NULL,
    name_normalized   TEXT NOT NULL,
    aliases           TEXT NOT NULL DEFAULT '[]',
    org_type          TEXT,
    network_id        TEXT REFERENCES network(network_id),
    member_unit       TEXT CHECK (member_unit IN ('company','individual','mixed','not_stated')),
    employer_reach_est INTEGER,
    sectors           TEXT NOT NULL DEFAULT '[]',
    geo_city          TEXT,
    geo_state         TEXT,
    geo_scope         TEXT CHECK (geo_scope IN ('local','state','regional','national')),
    tier              TEXT NOT NULL DEFAULT 'C' CHECK (tier IN ('A','B','C')),
    first_seen        TEXT NOT NULL,
    last_mapped       TEXT,
    discovered_from   TEXT,                     -- provenance: org_id or route_id of the hop
    UNIQUE (canonical_domain)
) STRICT;

CREATE INDEX ix_org_name_norm ON organization(name_normalized);
CREATE INDEX ix_org_network   ON organization(network_id);
CREATE INDEX ix_org_tier      ON organization(tier, last_mapped);

-- --------------------------------------------------------------- employers --

CREATE TABLE employer (
    employer_id     TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    name_normalized TEXT NOT NULL,
    domain          TEXT,
    naics           TEXT,
    site_city       TEXT,
    site_state      TEXT,
    employee_count  INTEGER,
    sectors         TEXT NOT NULL DEFAULT '[]',
    -- cross-family link: this employer is reachable through a CHANNEL route
    reached_via_route_id TEXT,
    first_seen      TEXT NOT NULL,
    UNIQUE (domain)
) STRICT;

CREATE INDEX ix_employer_name_norm ON employer(name_normalized);
CREATE INDEX ix_employer_via       ON employer(reached_via_route_id);

-- ------------------------------------------------------------------ people --

CREATE TABLE person (
    person_id    TEXT PRIMARY KEY,
    org_id       TEXT REFERENCES organization(org_id),
    employer_id  TEXT REFERENCES employer(employer_id),
    name         TEXT NOT NULL,
    title        TEXT,
    email        TEXT,
    phone        TEXT,
    role         TEXT CHECK (role IN (
                    'program_owner','chair','staff','exec',
                    'partnership_owner','problem_owner','practitioner')),
    controls     TEXT,
    source_url   TEXT,
    verified_at  TEXT,
    -- role-change tracking (PERSON family, route_type ROLE_CHANGE)
    previous_title      TEXT,
    leverage_change     TEXT CHECK (leverage_change IN ('up','flat','down')),
    change_detected_at  TEXT
) STRICT;

CREATE INDEX ix_person_org      ON person(org_id);
CREATE INDEX ix_person_employer ON person(employer_id);

-- FOUNDER-OWNED. Separate table so "no worker writes this" is structural.
-- access_warmth is the one dimension the system cannot research; it is entered
-- by the founder and must survive every rebuild.
CREATE TABLE person_founder (
    person_id            TEXT PRIMARY KEY REFERENCES person(person_id) ON DELETE CASCADE,
    known_to_art         TEXT NOT NULL DEFAULT 'unknown'
                           CHECK (known_to_art IN ('yes','no','unknown')),
    how_known            TEXT,
    last_contact         TEXT,
    connector_person_id  TEXT REFERENCES person(person_id),
    notes                TEXT,
    updated_at           TEXT NOT NULL
) STRICT;

-- ------------------------------------------------------------------ routes --
-- The unit of work. One row per (target, mechanism, how you get in).

CREATE TABLE route (
    route_id       TEXT PRIMARY KEY,
    family         TEXT NOT NULL CHECK (family IN ('ROOM','CHANNEL','EMPLOYER','PERSON')),
    -- exactly one target is set, determined by family
    org_id         TEXT REFERENCES organization(org_id),
    employer_id    TEXT REFERENCES employer(employer_id),
    person_id      TEXT REFERENCES person(person_id),

    mechanism_name TEXT NOT NULL,
    route_type     TEXT NOT NULL,

    -- route_url is the page you ACT on. evidence_url is the page that PROVES.
    -- They are never conflated. A null route_url cannot enter the BEST list.
    route_url      TEXT,
    route_url_is_offdomain INTEGER NOT NULL DEFAULT 0 CHECK (route_url_is_offdomain IN (0,1)),
    evidence_url   TEXT,

    eligibility    TEXT,
    owner_person_id TEXT REFERENCES person(person_id),

    series_key     TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'active'
                     CHECK (status IN ('active','watch','closed','excluded','quarantined')),
    surface        TEXT CHECK (surface IN ('BEST','WORTH_A_LOOK','LIBRARY')),
    excluded_by_rule_id TEXT,

    unresolved     TEXT NOT NULL DEFAULT '[]',  -- the blocking question(s)
    created_at     TEXT NOT NULL,
    last_verified  TEXT,

    UNIQUE (series_key),
    CHECK (
        (family = 'ROOM'     AND org_id IS NOT NULL) OR
        (family = 'CHANNEL'  AND org_id IS NOT NULL) OR
        (family = 'EMPLOYER' AND employer_id IS NOT NULL) OR
        (family = 'PERSON'   AND person_id IS NOT NULL)
    )
) STRICT;

CREATE INDEX ix_route_family_status ON route(family, status);
CREATE INDEX ix_route_surface       ON route(surface);
CREATE INDEX ix_route_org           ON route(org_id);
CREATE INDEX ix_route_employer      ON route(employer_id);
CREATE INDEX ix_route_last_verified ON route(last_verified);

-- 1:1 extension, family = ROOM
CREATE TABLE route_room (
    route_id        TEXT PRIMARY KEY REFERENCES route(route_id) ON DELETE CASCADE,
    deadline        TEXT,
    next_occurrence TEXT,
    cadence         TEXT,
    formats         TEXT NOT NULL DEFAULT '[]',
    session_length  TEXT,
    cost            TEXT,
    precedent       TEXT,
    member_unit     TEXT CHECK (member_unit IN ('company','individual','mixed','not_stated')),
    stated_roles    TEXT NOT NULL DEFAULT '[]',
    named_employers TEXT NOT NULL DEFAULT '[]',
    expected_size   INTEGER
) STRICT;

CREATE INDEX ix_room_deadline ON route_room(deadline);
CREATE INDEX ix_room_next     ON route_room(next_occurrence);

-- 1:1 extension, family = CHANNEL — the family with no event in it
CREATE TABLE route_channel (
    route_id            TEXT PRIMARY KEY REFERENCES route(route_id) ON DELETE CASCADE,
    relationship_nature TEXT CHECK (relationship_nature IN
                          ('members','clients','contracted','funded','convened','not_stated')),
    employer_count      INTEGER,
    named_employers     TEXT NOT NULL DEFAULT '[]',
    delivery_model      TEXT CHECK (delivery_model IN
                          ('staff','instructors','partners','providers','mixed','not_stated')),
    intake_url          TEXT,
    intake_criteria     TEXT,
    approver            TEXT,
    scope_contracted    TEXT,
    existing_providers  TEXT NOT NULL DEFAULT '[]',
    network_id          TEXT REFERENCES network(network_id),
    peer_node_count     INTEGER
) STRICT;

-- dated instances of a ROOM route
CREATE TABLE occurrence (
    occ_id           TEXT PRIMARY KEY,
    route_id         TEXT NOT NULL REFERENCES route(route_id) ON DELETE CASCADE,
    occurs_on        TEXT,
    city             TEXT,
    state            TEXT,
    venue            TEXT,
    registration_url TEXT,
    occurrence_key   TEXT NOT NULL,
    UNIQUE (occurrence_key)
) STRICT;

CREATE INDEX ix_occ_route ON occurrence(route_id);
CREATE INDEX ix_occ_date  ON occurrence(occurs_on);

-- ---------------------------------------------------------------- triggers --
-- An EMPLOYER route requires at least one. Strength decays; a trigger below
-- 1.0 drops its route to LIBRARY without human action.

CREATE TABLE trigger (
    trigger_id  TEXT PRIMARY KEY,
    employer_id TEXT NOT NULL REFERENCES employer(employer_id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    what        TEXT NOT NULL,
    occurred_on TEXT NOT NULL,
    source_url  TEXT NOT NULL,
    span_text   TEXT NOT NULL,
    capability_implication TEXT,
    detected_at       TEXT NOT NULL,
    decayed_strength  REAL,
    decay_computed_at TEXT
) STRICT;

CREATE INDEX ix_trigger_employer ON trigger(employer_id, occurred_on);
CREATE INDEX ix_trigger_strength ON trigger(decayed_strength);

-- ---------------------------------------------------------------- evidence --
-- One row per claim. This is what makes every score auditable and every
-- fabrication detectable: a field with no supporting span cannot be written.

CREATE TABLE evidence (
    ev_id        TEXT PRIMARY KEY,
    route_id     TEXT REFERENCES route(route_id) ON DELETE CASCADE,
    org_id       TEXT REFERENCES organization(org_id),
    field_name   TEXT NOT NULL,
    value        TEXT,
    span_text    TEXT,
    span_match   TEXT CHECK (span_match IN ('exact','normalized','approximate','absent')),
    source_url   TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    snapshot_uri TEXT,
    extractor    TEXT NOT NULL,
    prompt_version TEXT,
    fetched_at   TEXT NOT NULL
) STRICT;

CREATE INDEX ix_evidence_route ON evidence(route_id);
CREATE INDEX ix_evidence_hash  ON evidence(content_hash);
CREATE INDEX ix_evidence_span  ON evidence(span_match);

-- ------------------------------------------------------------------ scores --

CREATE TABLE score (
    score_id    TEXT PRIMARY KEY,
    route_id    TEXT NOT NULL REFERENCES route(route_id) ON DELETE CASCADE,
    scored_at   TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    fit         INTEGER NOT NULL CHECK (fit BETWEEN 0 AND 100),
    route_score INTEGER NOT NULL CHECK (route_score BETWEEN 0 AND 100),
    confidence  INTEGER NOT NULL CHECK (confidence BETWEEN 0 AND 100),
    components  TEXT NOT NULL           -- {"employer_presence": 4, "reach": 5, ...}
) STRICT;

CREATE INDEX ix_score_route ON score(route_id, scored_at);
CREATE INDEX ix_score_rank  ON score(fit, route_score);

-- ----------------------------------------------------------------- signals --

CREATE TABLE signal (
    signal_id   TEXT PRIMARY KEY,
    route_id    TEXT REFERENCES route(route_id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN
                  ('new','opened','closed','date_moved','owner_changed','new_content')),
    detected_at TEXT NOT NULL,
    previous    TEXT,
    current     TEXT,
    source_url  TEXT,
    run_id      TEXT
) STRICT;

CREATE INDEX ix_signal_route ON signal(route_id, detected_at);
CREATE INDEX ix_signal_run   ON signal(run_id);

-- ---------------------------------------------------------- founder-owned --
-- No worker writes these. Enforced structurally by table separation and by the
-- repository guard (E1.S3). A mark is never overwritten, only added to.

CREATE TABLE founder_mark (
    mark_id        TEXT PRIMARY KEY,
    route_id       TEXT NOT NULL REFERENCES route(route_id),
    marked_at      TEXT NOT NULL,
    verdict        TEXT,
    target_verdict TEXT,
    note_freetext  TEXT,
    outcome        TEXT,
    knows_someone  TEXT CHECK (knows_someone IN ('yes','no','unknown')),
    UNIQUE (route_id, marked_at)
) STRICT;

CREATE INDEX ix_mark_route   ON founder_mark(route_id);
CREATE INDEX ix_mark_verdict ON founder_mark(verdict);

-- Standing rejections persist OUTSIDE the row, keyed by normalized name AND
-- registrable domain. Matching on both is what stops a rejected organization
-- reappearing under a name variant.
CREATE TABLE rejection (
    rejection_id  TEXT PRIMARY KEY,
    match_name    TEXT,
    match_domain  TEXT,
    family_scope  TEXT CHECK (family_scope IN ('ROOM','CHANNEL','EMPLOYER','PERSON','ALL')),
    scope         TEXT NOT NULL DEFAULT 'organization'
                    CHECK (scope IN ('organization','mechanism','archetype')),
    pattern_tag   TEXT,
    reason        TEXT,
    created_from_mark_id TEXT REFERENCES founder_mark(mark_id),
    created_at    TEXT NOT NULL,
    CHECK (match_name IS NOT NULL OR match_domain IS NOT NULL OR pattern_tag IS NOT NULL)
) STRICT;

CREATE INDEX ix_rejection_name   ON rejection(match_name);
CREATE INDEX ix_rejection_domain ON rejection(match_domain);

-- -------------------------------------------------------------- run state --

CREATE TABLE run (
    run_id         TEXT PRIMARY KEY,
    workflow       TEXT NOT NULL,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    status         TEXT NOT NULL DEFAULT 'running'
                     CHECK (status IN ('running','ok','failed','budget_stopped')),
    config_hash    TEXT,
    orgs_mapped    INTEGER NOT NULL DEFAULT 0,
    pages_fetched  INTEGER NOT NULL DEFAULT 0,
    candidates     INTEGER NOT NULL DEFAULT 0,
    survived_gate  INTEGER NOT NULL DEFAULT 0,
    survived_rerank INTEGER NOT NULL DEFAULT 0,
    routes_written INTEGER NOT NULL DEFAULT 0,
    quarantined    INTEGER NOT NULL DEFAULT 0,
    cost_usd       REAL NOT NULL DEFAULT 0.0,
    not_reached    TEXT NOT NULL DEFAULT '[]',   -- mandatory output, never empty on truncation
    error          TEXT
) STRICT;

CREATE INDEX ix_run_started ON run(started_at);

CREATE TABLE config_version (
    config_hash TEXT PRIMARY KEY,
    captured_at TEXT NOT NULL,
    payload     TEXT NOT NULL,     -- full canonical JSON, so a rollback is a lookup
    note        TEXT
) STRICT;
