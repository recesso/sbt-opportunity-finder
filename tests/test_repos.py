"""E1.S2 — repository layer.

Each test here targets a specific behaviour that, if the code were wrong, would
cause a specific real failure. The mutation audit
(``scripts/audit_tests.py``) proves that claim by breaking the code on purpose
and checking these tests notice.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from finder.store import ids
from finder.store.db import open_db, utcnow
from finder.store.models import (
    Employer,
    Evidence,
    FounderMark,
    Organization,
    Person,
    Rejection,
    Route,
    Score,
    Trigger,
)
from finder.store.repos import RUN_COUNTERS, RepoError, Store

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def store() -> Store:
    return Store(open_db(":memory:"))


def make_org(domain: str = "gamep.org", name: str = "GaMEP", **kw) -> Organization:
    return Organization(
        org_id=ids.org_id(domain),
        canonical_domain=domain,
        name=name,
        name_normalized=name.lower(),
        first_seen=kw.pop("first_seen", utcnow()),
        **kw,
    )


def make_route(org_id: str, series_key: str = "gamep|lunch-and-learn", **kw) -> Route:
    return Route(
        route_id=ids.route_id(series_key),
        family=kw.pop("family", "ROOM"),
        mechanism_name=kw.pop("mechanism_name", "Lunch and Learn circuit"),
        route_type=kw.pop("route_type", "PARTNER_DELIVERY"),
        series_key=series_key,
        created_at=kw.pop("created_at", utcnow()),
        org_id=org_id,
        **kw,
    )


# --- identifiers -----------------------------------------------------------


def test_ids_are_deterministic() -> None:
    """A replayed run must produce the same ids, or replay is not idempotent."""
    assert ids.org_id("gamep.org") == ids.org_id("gamep.org")
    assert ids.route_id("a|b") == ids.route_id("a|b")


def test_ids_normalise_case_and_whitespace() -> None:
    assert ids.org_id("GaMEP.org") == ids.org_id("  gamep.org ")


def test_different_inputs_give_different_ids() -> None:
    assert ids.org_id("gamep.org") != ids.org_id("gsae.org")


@given(st.text(min_size=1).filter(lambda s: s.strip()))
def test_org_id_is_stable_and_well_formed(domain: str) -> None:
    first = ids.org_id(domain)
    assert first == ids.org_id(domain)
    assert first.startswith("org-")
    assert len(first) == len("org-") + 12


@given(
    st.text(min_size=1).filter(lambda s: s.strip()),
    st.text(min_size=1).filter(lambda s: s.strip()),
)
def test_distinct_series_keys_do_not_collide(a: str, b: str) -> None:
    """Not a proof of collision resistance — a guard against a truncation bug
    that would make unrelated routes share an id."""
    if a.strip().lower() != b.strip().lower():
        assert ids.route_id(a) != ids.route_id(b)


def test_empty_natural_key_is_rejected() -> None:
    for fn, arg in ((ids.org_id, "   "), (ids.route_id, ""), (ids.occurrence_id, " ")):
        with pytest.raises(ValueError):
            fn(arg)


def test_employer_id_prefers_domain_but_falls_back_to_name() -> None:
    assert ids.employer_id("acme.com", "Acme") == ids.employer_id("acme.com", "Different Name")
    assert ids.employer_id(None, "Acme") == ids.employer_id("", "Acme")
    with pytest.raises(ValueError):
        ids.employer_id(None, "  ")


# --- organizations ---------------------------------------------------------


def test_organization_round_trips(store: Store) -> None:
    org = make_org(sectors=["manufacturing"], aliases=["Georgia MEP"], tier="A")
    saved = store.organizations.upsert(org)
    assert saved == store.organizations.get(org.org_id)
    assert saved.sectors == ["manufacturing"]
    assert saved.aliases == ["Georgia MEP"]


def test_upsert_is_idempotent(store: Store) -> None:
    org = make_org()
    store.organizations.upsert(org)
    store.organizations.upsert(org)
    assert store.organizations.count() == 1


def test_upsert_preserves_first_seen(store: Store) -> None:
    """When an organization was first discovered is a fact about history."""
    original = make_org(first_seen="2026-01-01T00:00:00+00:00")
    store.organizations.upsert(original)

    later = make_org(first_seen="2026-09-01T00:00:00+00:00", name="GaMEP renamed")
    saved = store.organizations.upsert(later)

    assert saved.first_seen == "2026-01-01T00:00:00+00:00"
    assert saved.name == "GaMEP renamed"


def test_upsert_does_not_erase_known_values_with_nulls(store: Store) -> None:
    """A later, thinner extraction must not blank out what an earlier one knew."""
    store.organizations.upsert(make_org(org_type="MEP center", geo_state="GA"))
    saved = store.organizations.upsert(make_org(org_type=None, geo_state=None))
    assert saved.org_type == "MEP center"
    assert saved.geo_state == "GA"


def test_same_domain_different_name_is_one_organization(store: Store) -> None:
    """Names drift; domains do not. This is the dedupe backbone."""
    store.organizations.upsert(make_org("scmep.org", "SC Manufacturers & Commerce"))
    store.organizations.upsert(make_org("scmep.org", "South Carolina Manufacturers Council"))
    assert store.organizations.count() == 1


def test_due_for_mapping_respects_tier_and_cutoff(store: Store) -> None:
    store.organizations.upsert(make_org("a.org", "A", tier="A", last_mapped="2026-01-01"))
    store.organizations.upsert(make_org("b.org", "B", tier="A", last_mapped="2026-09-01"))
    store.organizations.upsert(make_org("c.org", "C", tier="B", last_mapped="2026-01-01"))
    store.organizations.upsert(make_org("d.org", "D", tier="A"))  # never mapped

    due = store.organizations.due_for_mapping("A", "2026-06-01")
    domains = {o.canonical_domain for o in due}
    assert domains == {"a.org", "d.org"}, "only tier A, only stale or never-mapped"


def test_mark_mapped_moves_it_out_of_the_due_list(store: Store) -> None:
    org = store.organizations.upsert(make_org(tier="A"))
    assert store.organizations.due_for_mapping("A", "2026-06-01")
    store.organizations.mark_mapped(org.org_id, "2026-06-02")
    assert store.organizations.due_for_mapping("A", "2026-06-01") == []


# --- routes ----------------------------------------------------------------


def test_route_round_trips_with_both_urls(store: Store) -> None:
    """route_url is the page you act on; evidence_url proves the claim.

    Conflating them is how a past event page gets presented as a way in.
    """
    org = store.organizations.upsert(make_org())
    route = make_route(
        org.org_id,
        route_url="https://www.surveymonkey.com/r/NKSQCY6",
        route_url_is_offdomain=True,
        evidence_url="https://www.gsae.org/speaker-interest-form",
    )
    saved = store.routes.upsert(route)
    assert saved.route_url == "https://www.surveymonkey.com/r/NKSQCY6"
    assert saved.route_url_is_offdomain is True
    assert saved.evidence_url == "https://www.gsae.org/speaker-interest-form"


def test_same_series_key_is_one_route(store: Store) -> None:
    """880 of the predecessor's 976 keyed rows were duplicates because this
    check never ran before a write."""
    org = store.organizations.upsert(make_org())
    store.routes.upsert(make_route(org.org_id, "gamep|circuit"))
    store.routes.upsert(make_route(org.org_id, "gamep|circuit", mechanism_name="renamed"))
    assert store.routes.count() == 1
    assert store.routes.get_by_series_key("gamep|circuit").mechanism_name == "renamed"


def test_two_families_at_one_organization_are_two_routes(store: Store) -> None:
    """GaMEP is a ROOM (the lunch-and-learn) and a CHANNEL (the instructor
    path). A system that produces only the ROOM route has failed."""
    org = store.organizations.upsert(make_org())
    store.routes.upsert(make_route(org.org_id, "gamep|lunch-and-learn", family="ROOM"))
    store.routes.upsert(
        make_route(
            org.org_id,
            "gamep|instructor-partner-route",
            family="CHANNEL",
            route_type="UNKNOWN",
            unresolved=["Who selects lunch-and-learn presenters?"],
        )
    )
    assert store.routes.count() == 2
    channel = store.routes.get_by_series_key("gamep|instructor-partner-route")
    assert channel.family == "CHANNEL"
    assert channel.route_url is None
    assert channel.unresolved == ["Who selects lunch-and-learn presenters?"]


def test_route_url_survives_a_later_extraction_that_missed_it(store: Store) -> None:
    org = store.organizations.upsert(make_org())
    store.routes.upsert(make_route(org.org_id, "k", route_url="https://apply.example/form"))
    saved = store.routes.upsert(make_route(org.org_id, "k", route_url=None))
    assert saved.route_url == "https://apply.example/form"


def test_route_with_a_bad_family_is_refused(store: Store) -> None:
    org = store.organizations.upsert(make_org())
    with pytest.raises(RepoError):
        store.routes.upsert(make_route(org.org_id, "k", family="WAREHOUSE"))


def test_room_route_without_an_organization_is_refused(store: Store) -> None:
    bad = Route(
        route_id="rt-x",
        family="ROOM",
        mechanism_name="orphan",
        route_type="OPEN_CALL",
        series_key="orphan",
        created_at=utcnow(),
    )
    with pytest.raises(RepoError):
        store.routes.upsert(bad)


def test_exclude_retains_the_route_with_its_reason(store: Store) -> None:
    """Nothing is ever deleted. Rejected routes move to LIBRARY with the rule."""
    org = store.organizations.upsert(make_org())
    route = store.routes.upsert(make_route(org.org_id))
    store.routes.exclude(route.route_id, "rj-abc")

    after = store.routes.get(route.route_id)
    assert after is not None, "an excluded route must still exist"
    assert after.status == "excluded"
    assert after.surface == "LIBRARY"
    assert after.excluded_by_rule_id == "rj-abc"


def test_by_surface_filters_by_family(store: Store) -> None:
    org = store.organizations.upsert(make_org())
    r1 = store.routes.upsert(make_route(org.org_id, "a", family="ROOM"))
    r2 = store.routes.upsert(make_route(org.org_id, "b", family="CHANNEL"))
    store.routes.set_surface(r1.route_id, "BEST")
    store.routes.set_surface(r2.route_id, "BEST")

    assert len(store.routes.by_surface("BEST")) == 2
    assert len(store.routes.by_surface("BEST", family="CHANNEL")) == 1


# --- evidence --------------------------------------------------------------


def _ev(route_id: str, field_name: str, **kw) -> Evidence:
    content_hash = kw.pop("content_hash", "hash-1")
    return Evidence(
        ev_id=ids.evidence_id(route_id, field_name, content_hash),
        field_name=field_name,
        source_url=kw.pop("source_url", "https://example.org/page"),
        content_hash=content_hash,
        extractor=kw.pop("extractor", "w3@v1"),
        fetched_at=kw.pop("fetched_at", utcnow()),
        route_id=route_id,
        **kw,
    )


def test_evidence_round_trips(store: Store) -> None:
    org = store.organizations.upsert(make_org())
    route = store.routes.upsert(make_route(org.org_id))
    ev = store.evidence.add(
        _ev(route.route_id, "deadline", value="2026-10-01", span_text="due October 1")
    )
    assert ev.span_text == "due October 1"
    assert store.evidence.for_route(route.route_id) == [ev]


def test_re_extracting_the_same_field_updates_in_place(store: Store) -> None:
    """Provenance must not pile up duplicates when a snapshot is re-processed."""
    org = store.organizations.upsert(make_org())
    route = store.routes.upsert(make_route(org.org_id))
    store.evidence.add(_ev(route.route_id, "deadline", value="wrong"))
    store.evidence.add(_ev(route.route_id, "deadline", value="2026-10-01"))
    rows = store.evidence.for_route(route.route_id)
    assert len(rows) == 1
    assert rows[0].value == "2026-10-01"


def test_a_new_snapshot_creates_new_evidence(store: Store) -> None:
    org = store.organizations.upsert(make_org())
    route = store.routes.upsert(make_route(org.org_id))
    store.evidence.add(_ev(route.route_id, "deadline", content_hash="h1"))
    store.evidence.add(_ev(route.route_id, "deadline", content_hash="h2"))
    assert len(store.evidence.for_route(route.route_id)) == 2


def test_fields_without_span_are_findable(store: Store) -> None:
    """A field with no supporting span cannot be trusted; this is the audit query."""
    org = store.organizations.upsert(make_org())
    route = store.routes.upsert(make_route(org.org_id))
    store.evidence.add(_ev(route.route_id, "owner", span_text="Alfred Gardner", span_match="exact"))
    store.evidence.add(_ev(route.route_id, "cost", span_text=None))
    store.evidence.add(_ev(route.route_id, "venue", span_text="invented", span_match="absent"))

    assert set(store.evidence.fields_without_span(route.route_id)) == {"cost", "venue"}


# --- scores ----------------------------------------------------------------


def test_score_history_is_kept_and_latest_wins(store: Store) -> None:
    """Every score carries its config_hash so a ranking change is attributable
    and a bad weight fit is a one-line rollback."""
    org = store.organizations.upsert(make_org())
    route = store.routes.upsert(make_route(org.org_id))

    for stamp, cfg, fit in (
        ("2026-08-01T00:00:00+00:00", "cfg-a", 60),
        ("2026-09-01T00:00:00+00:00", "cfg-b", 72),
    ):
        store.scores.add(
            Score(
                score_id=ids.score_id(route.route_id, cfg, stamp),
                route_id=route.route_id,
                scored_at=stamp,
                config_hash=cfg,
                fit=fit,
                route_score=80,
                confidence=75,
                components={"reach": 5},
            )
        )

    latest = store.scores.latest_for_route(route.route_id)
    assert latest.fit == 72
    assert latest.config_hash == "cfg-b"
    assert latest.components == {"reach": 5}
    assert len(store.scores.history(route.route_id)) == 2


def test_out_of_range_score_is_refused(store: Store) -> None:
    org = store.organizations.upsert(make_org())
    route = store.routes.upsert(make_route(org.org_id))
    with pytest.raises(RepoError):
        store.scores.add(
            Score(
                score_id="sc-bad",
                route_id=route.route_id,
                scored_at=utcnow(),
                config_hash="c",
                fit=101,
                route_score=50,
                confidence=50,
            )
        )


# --- founder-owned ---------------------------------------------------------


def test_a_mark_is_never_overwritten(store: Store) -> None:
    """The predecessor destroyed the founder's work by rewriting these; he had
    to redo dispositions more than once. Ingest is insert-or-leave-alone."""
    org = store.organizations.upsert(make_org())
    route = store.routes.upsert(make_route(org.org_id))
    stamp = "2026-09-01T12:00:00+00:00"

    first = store.marks.ingest(
        FounderMark(
            mark_id=ids.mark_id(route.route_id, stamp),
            route_id=route.route_id,
            marked_at=stamp,
            verdict="PURSUE",
            note_freetext="this is the one",
        )
    )
    second = store.marks.ingest(
        FounderMark(
            mark_id=ids.mark_id(route.route_id, stamp),
            route_id=route.route_id,
            marked_at=stamp,
            verdict="DROP_TARGET",
            note_freetext="",
        )
    )

    assert first.verdict == "PURSUE"
    assert second.verdict == "PURSUE", "an existing mark must survive re-ingestion"
    assert second.note_freetext == "this is the one"
    assert store.marks.count() == 1


def test_marks_at_different_times_are_both_kept(store: Store) -> None:
    org = store.organizations.upsert(make_org())
    route = store.routes.upsert(make_route(org.org_id))
    for stamp, verdict in (
        ("2026-09-01T00:00:00+00:00", "MONITOR"),
        ("2026-09-08T00:00:00+00:00", "PURSUE"),
    ):
        store.marks.ingest(
            FounderMark(
                mark_id=ids.mark_id(route.route_id, stamp),
                route_id=route.route_id,
                marked_at=stamp,
                verdict=verdict,
            )
        )
    assert [m.verdict for m in store.marks.for_route(route.route_id)] == ["MONITOR", "PURSUE"]


def test_mark_repo_has_no_update_or_delete() -> None:
    """Structural, not conventional: there is no path to change a mark."""
    from finder.store.repos import MarkRepo

    forbidden = {"update", "delete", "set", "remove", "overwrite"}
    exposed = {n for n in dir(MarkRepo) if not n.startswith("_")}
    assert not (exposed & forbidden), f"MarkRepo exposes mutation methods: {exposed & forbidden}"


# --- rejections ------------------------------------------------------------


def _rej(name: str | None, domain: str | None, family: str = "ALL", **kw) -> Rejection:
    return Rejection(
        rejection_id=ids.rejection_id(name or "", domain or "", family),
        created_at=utcnow(),
        match_name=name,
        match_domain=domain,
        family_scope=family,
        **kw,
    )


def test_rejection_matches_on_domain_when_the_name_changed(store: Store) -> None:
    """The real case: 'South Carolina Manufacturers Council' is the same
    organization as 'South Carolina Manufacturers & Commerce'. Twelve rows of it
    survived a permanent rejection in the predecessor because the check was by
    name only."""
    store.rejections.add(
        _rej("south carolina manufacturers commerce", "scmc.org", reason="do not surface again")
    )
    assert store.rejections.blocks(
        name_normalized="south carolina manufacturers council",
        domain="scmc.org",
        family="ROOM",
    )


def test_rejection_matches_on_name_when_the_domain_is_unknown(store: Store) -> None:
    store.rejections.add(_rej("pmi atlanta chapter", None))
    assert store.rejections.blocks(
        name_normalized="pmi atlanta chapter", domain=None, family="ROOM"
    )


def test_rejection_scoped_to_one_family_does_not_block_another(store: Store) -> None:
    """Rejecting a room says nothing about a channel at the same organization."""
    store.rejections.add(_rej("gamep", "gamep.org", family="ROOM"))
    assert store.rejections.blocks(name_normalized="gamep", domain="gamep.org", family="ROOM")
    assert not store.rejections.blocks(
        name_normalized="gamep", domain="gamep.org", family="CHANNEL"
    )


def test_unrelated_candidate_is_not_blocked(store: Store) -> None:
    store.rejections.add(_rej("pmi atlanta chapter", "pmiatlanta.org"))
    assert not store.rejections.blocks(
        name_normalized="georgia manufacturing alliance", domain="gma.org", family="ROOM"
    )


def test_a_rejection_that_matches_nothing_is_refused(store: Store) -> None:
    with pytest.raises(RepoError):
        store.rejections.add(Rejection(rejection_id="rj-empty", created_at=utcnow()))


# --- cross-family linking --------------------------------------------------


def test_employer_can_be_linked_to_the_channel_that_reaches_it(store: Store) -> None:
    """A company on a channel's client roster becomes reachable through it.

    When a trigger later fires there, the EMPLOYER route is CHANNEL_INTRO —
    because a way in already exists. This link is what one-to-many means.
    """
    org = store.organizations.upsert(make_org())
    channel = store.routes.upsert(
        make_route(org.org_id, "gamep|provider-network", family="CHANNEL")
    )
    emp = store.employers.upsert(
        Employer(
            employer_id=ids.employer_id("acme-mfg.com", "Acme Manufacturing"),
            name="Acme Manufacturing",
            name_normalized="acme manufacturing",
            first_seen=utcnow(),
            domain="acme-mfg.com",
        )
    )
    store.employers.link_to_channel(emp.employer_id, channel.route_id)

    assert store.employers.get(emp.employer_id).reached_via_route_id == channel.route_id


def test_strongest_trigger_drives_employer_relevance(store: Store) -> None:
    """A trigger decayed below 1.0 must not keep its employer on the list."""
    emp = store.employers.upsert(
        Employer(
            employer_id=ids.employer_id("acme.com", "Acme"),
            name="Acme",
            name_normalized="acme",
            first_seen=utcnow(),
            domain="acme.com",
        )
    )
    for kind, strength in (("contract_award", 0.4), ("expansion", 3.1)):
        store.triggers.add(
            Trigger(
                trigger_id=ids.trigger_id(emp.employer_id, kind, "2026-01-01", "https://x"),
                employer_id=emp.employer_id,
                kind=kind,
                what="something happened",
                occurred_on="2026-01-01",
                source_url="https://x",
                span_text="quoted from the page",
                detected_at=utcnow(),
                decayed_strength=strength,
            )
        )
    assert store.triggers.strongest_for_employer(emp.employer_id) == pytest.approx(3.1)


def test_employer_with_no_triggers_has_zero_strength(store: Store) -> None:
    emp = store.employers.upsert(
        Employer(
            employer_id=ids.employer_id("none.com", "None"),
            name="None",
            name_normalized="none",
            first_seen=utcnow(),
            domain="none.com",
        )
    )
    assert store.triggers.strongest_for_employer(emp.employer_id) == 0.0


# --- transactions ----------------------------------------------------------


def test_unit_of_work_rolls_back_everything_on_failure(store: Store) -> None:
    org = store.organizations.upsert(make_org("first.org", "First"))
    with pytest.raises(RepoError), store.unit_of_work() as uow:
        uow.organizations.upsert(make_org("second.org", "Second"))
        uow.routes.upsert(make_route("org-does-not-exist", "orphan"))

    assert store.organizations.count() == 1
    assert store.organizations.get(org.org_id) is not None
    assert store.organizations.get_by_domain("second.org") is None


def test_unit_of_work_commits_together(store: Store) -> None:
    with store.unit_of_work() as uow:
        org = uow.organizations.upsert(make_org())
        uow.routes.upsert(make_route(org.org_id))
        uow.people.upsert(
            Person(
                person_id=ids.person_id("gamep.org", "Alfred Gardner"),
                name="Alfred Gardner",
                org_id=org.org_id,
                title="Project Manager, HR, Strategy and Leadership Development",
                role="program_owner",
            )
        )
    assert (store.organizations.count(), store.routes.count(), store.people.count()) == (1, 1, 1)


# --- the architectural boundary --------------------------------------------


RAW_SQL = re.compile(r"import sqlite3|\bconn\.execute(many)?\(")


def raw_sql_offenders(package: Path) -> list[str]:
    """Modules outside ``package/store`` that touch SQL directly.

    Walks the working tree rather than shelling out to `git grep`, which only
    sees tracked files — a brand-new module, the moment this rule is easiest to
    break, was invisible to the tracked-only version until it was committed.
    One was.
    """
    store = package / "store"
    return sorted(
        path.relative_to(package).as_posix()
        for path in package.rglob("*.py")
        if store not in path.parents and RAW_SQL.search(path.read_text(encoding="utf-8"))
    )


def test_no_raw_sql_outside_the_store_package() -> None:
    """The guarantee that makes the founder-owned guard enforceable.

    If any worker can open its own cursor, the repository layer is decoration.
    """
    offenders = raw_sql_offenders(ROOT / "src" / "finder")
    assert not offenders, f"raw SQL outside src/finder/store/: {offenders}"


def test_the_raw_sql_guard_can_actually_fail(tmp_path: Path) -> None:
    """A guard that cannot fail is decoration, so run the real scanner on a
    tree that breaks the rule and confirm it names the offender."""
    (tmp_path / "store").mkdir()
    (tmp_path / "store" / "repos.py").write_text("import sqlite3\n", encoding="utf-8")
    (tmp_path / "clean.py").write_text("from finder.store.repos import Store\n", encoding="utf-8")
    assert raw_sql_offenders(tmp_path) == []

    (tmp_path / "harvest").mkdir()
    (tmp_path / "harvest" / "worker.py").write_text(
        "rows = self.conn.execute('SELECT 1')\n", encoding="utf-8"
    )
    assert raw_sql_offenders(tmp_path) == ["harvest/worker.py"]


def test_founder_owned_tables_are_declared() -> None:
    from finder.store.repos import FOUNDER_OWNED_TABLES

    assert {"founder_mark", "person_founder"} == FOUNDER_OWNED_TABLES


def test_repo_errors_are_typed_not_raw_sqlite(store: Store) -> None:
    """Callers must never have to catch sqlite3 exceptions."""
    org = store.organizations.upsert(make_org())
    with pytest.raises(RepoError) as exc:
        store.routes.upsert(make_route(org.org_id, "k", family="NOPE"))
    assert not isinstance(exc.value, sqlite3.Error)


# --- accessors used by later workers ---------------------------------------
# These are covered here rather than left as dead paths: an accessor with no
# test is a promise nobody has checked.


def test_get_returns_none_for_unknown_ids(store: Store) -> None:
    assert store.organizations.get("org-nope") is None
    assert store.organizations.get_by_domain("nope.org") is None
    assert store.employers.get("emp-nope") is None
    assert store.people.get("per-nope") is None
    assert store.routes.get("rt-nope") is None
    assert store.routes.get_by_series_key("nope") is None
    assert store.evidence.get("ev-nope") is None
    assert store.scores.latest_for_route("rt-nope") is None
    assert store.triggers.get("tg-nope") is None
    assert store.rejections.get("rj-nope") is None


def test_counts_start_at_zero_and_track_writes(store: Store) -> None:
    repos = (
        store.organizations,
        store.employers,
        store.people,
        store.routes,
        store.evidence,
        store.scores,
        store.triggers,
        store.marks,
        store.rejections,
    )
    assert [r.count() for r in repos] == [0] * len(repos)

    org = store.organizations.upsert(make_org())
    store.routes.upsert(make_route(org.org_id))
    assert store.organizations.count() == 1
    assert store.routes.count() == 1


def test_find_by_normalized_name_groups_variants(store: Store) -> None:
    """Two domains, one normalised name — the shape a rejection has to catch."""
    store.organizations.upsert(make_org("scmc.org", "SC Manufacturers"))
    store.organizations.upsert(make_org("scmanufacturers.org", "SC Manufacturers"))
    found = store.organizations.find_by_normalized_name("sc manufacturers")
    assert {o.canonical_domain for o in found} == {"scmc.org", "scmanufacturers.org"}
    assert store.organizations.find_by_normalized_name("nobody") == []


def test_people_for_organization(store: Store) -> None:
    org = store.organizations.upsert(make_org())
    other = store.organizations.upsert(make_org("gsae.org", "GSAE"))
    store.people.upsert(
        Person(
            person_id=ids.person_id("gamep.org", "Alfred Gardner"),
            name="Alfred Gardner",
            org_id=org.org_id,
            role="program_owner",
        )
    )
    store.people.upsert(
        Person(
            person_id=ids.person_id("gsae.org", "Jewel Hazelton"),
            name="Jewel Hazelton",
            org_id=other.org_id,
            role="staff",
        )
    )
    assert [p.name for p in store.people.for_organization(org.org_id)] == ["Alfred Gardner"]


def test_triggers_for_employer_are_newest_first(store: Store) -> None:
    emp = store.employers.upsert(
        Employer(
            employer_id=ids.employer_id("acme.com", "Acme"),
            name="Acme",
            name_normalized="acme",
            first_seen=utcnow(),
            domain="acme.com",
        )
    )
    for day in ("2026-03-01", "2026-07-01"):
        store.triggers.add(
            Trigger(
                trigger_id=ids.trigger_id(emp.employer_id, "expansion", day, "https://x"),
                employer_id=emp.employer_id,
                kind="expansion",
                what="new site",
                occurred_on=day,
                source_url="https://x",
                span_text="opening a new plant",
                detected_at=utcnow(),
                decayed_strength=2.0,
            )
        )
    assert [t.occurred_on for t in store.triggers.for_employer(emp.employer_id)] == [
        "2026-07-01",
        "2026-03-01",
    ]


def test_set_decay_updates_strength_in_place(store: Store) -> None:
    """Nightly decay must not create a second trigger row."""
    emp = store.employers.upsert(
        Employer(
            employer_id=ids.employer_id("acme.com", "Acme"),
            name="Acme",
            name_normalized="acme",
            first_seen=utcnow(),
            domain="acme.com",
        )
    )
    tid = ids.trigger_id(emp.employer_id, "grant_award", "2026-01-01", "https://x")
    store.triggers.add(
        Trigger(
            trigger_id=tid,
            employer_id=emp.employer_id,
            kind="grant_award",
            what="awarded",
            occurred_on="2026-01-01",
            source_url="https://x",
            span_text="received an award",
            detected_at=utcnow(),
            decayed_strength=5.0,
        )
    )
    store.triggers.set_decay(tid, 0.4, utcnow())
    assert store.triggers.count() == 1
    assert store.triggers.get(tid).decayed_strength == pytest.approx(0.4)
    assert store.triggers.strongest_for_employer(emp.employer_id) == pytest.approx(0.4)


def test_all_marks_is_the_whole_training_set(store: Store) -> None:
    """Every learning mechanism starts from this list."""
    org = store.organizations.upsert(make_org())
    assert store.marks.all_marks() == []
    for i, key in enumerate(("a", "b")):
        route = store.routes.upsert(make_route(org.org_id, key))
        store.marks.ingest(
            FounderMark(
                mark_id=ids.mark_id(route.route_id, f"2026-09-0{i + 1}T00:00:00+00:00"),
                route_id=route.route_id,
                marked_at=f"2026-09-0{i + 1}T00:00:00+00:00",
                verdict="PURSUE" if i else "DROP_TARGET",
            )
        )
    assert [m.verdict for m in store.marks.all_marks()] == ["DROP_TARGET", "PURSUE"]


def test_rejection_reason_can_be_amended_without_duplicating(store: Store) -> None:
    rej = _rej("gta", "gatrucking.org", reason="")
    store.rejections.add(rej)
    updated = store.rejections.add(replace(rej, reason="individual-practitioner society"))
    assert store.rejections.count() == 1
    assert updated.reason == "individual-practitioner society"


def test_empty_json_columns_round_trip_as_empty_lists(store: Store) -> None:
    org = store.organizations.upsert(make_org())
    assert org.sectors == [] and org.aliases == []
    route = store.routes.upsert(make_route(org.org_id))
    assert route.unresolved == []


# --- the run ledger --------------------------------------------------------


def test_run_repo_round_trips_a_run(store: Store) -> None:
    run = store.runs.start("r-1", "weekly", config_hash="cfg-1")
    assert run.status == "running"
    assert run.counters == dict.fromkeys(RUN_COUNTERS, 0)
    assert run.not_reached == []
    assert store.runs.count() == 1

    store.runs.bump("r-1", "pages_fetched", 3)
    store.runs.bump("r-1", "pages_fetched")
    store.runs.append_not_reached("r-1", {"reason": "budget", "detail": "300 of 800", "count": 500})
    store.runs.finish("r-1", status="budget_stopped", error=None, cost_usd=1.25)

    done = store.runs.get("r-1")
    assert done.status == "budget_stopped"
    assert done.counters["pages_fetched"] == 4
    assert done.cost_usd == 1.25
    assert done.not_reached == [{"reason": "budget", "detail": "300 of 800", "count": 500}]
    assert done.finished_at is not None


def test_run_repo_rejects_an_unknown_counter(store: Store) -> None:
    """The counter name is interpolated into SQL, so the whitelist is load-bearing."""
    store.runs.start("r-1", "weekly")
    with pytest.raises(RepoError, match="unknown run counter"):
        store.runs.bump("r-1", "cost_usd", 1)


def test_appending_truncation_to_a_missing_run_is_an_error(store: Store) -> None:
    with pytest.raises(RepoError, match="no such run"):
        store.runs.append_not_reached("nope", {"reason": "x", "detail": "y", "count": 1})


def test_a_corrupt_not_reached_column_reads_as_empty_not_as_a_crash(store: Store) -> None:
    """Defensive: a hand-edited row must not take down the report."""
    store.runs.start("r-1", "weekly")
    store.conn.execute("UPDATE run SET not_reached = '{\"oops\": 1}' WHERE run_id = 'r-1'")
    assert store.runs.get("r-1").not_reached == []


def test_reopen_clears_the_finish_stamp(store: Store) -> None:
    store.runs.start("r-1", "weekly")
    store.runs.finish("r-1", status="ok", error=None, cost_usd=0.0)
    store.runs.reopen("r-1")
    run = store.runs.get("r-1")
    assert (run.status, run.finished_at) == ("running", None)


def test_unknown_run_reads_as_none(store: Store) -> None:
    assert store.runs.get("nope") is None
    assert store.runs.last() is None
    assert store.runs.unfinished() == []


# --- checkpoints -----------------------------------------------------------


def test_stage_run_repo_tracks_item_state(store: Store) -> None:
    store.runs.start("r-1", "weekly")
    assert store.stage_runs.status("r-1", "map", "org-1") is None

    store.stage_runs.start_item("r-1", "map", "org-1")
    assert store.stage_runs.status("r-1", "map", "org-1") == "running"
    assert store.stage_runs.finished_keys("r-1", "map") == set()

    assert store.stage_runs.finish_item("r-1", "map", "org-1", "done", None) is True
    assert store.stage_runs.finished_keys("r-1", "map") == {"org-1"}
    assert store.stage_runs.summary("r-1", "map") == {"done": 1}
    assert store.stage_runs.count() == 1


def test_finishing_an_unclaimed_item_reports_false(store: Store) -> None:
    """The caller turns this into an error; the repo must not pretend it worked."""
    assert store.stage_runs.finish_item("r-1", "map", "ghost", "done", None) is False


def test_reclaiming_an_item_clears_the_previous_outcome(store: Store) -> None:
    """A retried item must not carry last attempt's error into this one."""
    store.runs.start("r-1", "weekly")
    store.stage_runs.start_item("r-1", "map", "org-1")
    store.stage_runs.finish_item("r-1", "map", "org-1", "failed", "timeout")
    store.stage_runs.start_item("r-1", "map", "org-1")

    row = store.conn.execute(
        "SELECT status, error, finished_at FROM stage_run WHERE item_key = 'org-1'"
    ).fetchone()
    assert (row["status"], row["error"], row["finished_at"]) == ("running", None, None)


def test_finished_keys_counts_every_terminal_state(store: Store) -> None:
    store.runs.start("r-1", "weekly")
    for key, status in (("a", "done"), ("b", "failed"), ("c", "skipped"), ("d", "running")):
        store.stage_runs.start_item("r-1", "map", key)
        if status != "running":
            store.stage_runs.finish_item("r-1", "map", key, status, None)
    assert store.stage_runs.finished_keys("r-1", "map") == {"a", "b", "c"}


# --- spend -----------------------------------------------------------------


def test_cost_repo_totals_and_splits_by_provider(store: Store) -> None:
    store.costs.record("c-1", "r-1", "firecrawl", "scrape", units=10, usd=0.01)
    store.costs.record("c-2", "r-1", "firecrawl", "map", usd=0.002)
    store.costs.record("c-3", "r-1", "llm", "extract", usd=0.15)
    store.costs.record("c-4", "r-2", "llm", "extract", usd=99.0)

    assert store.costs.total("r-1") == pytest.approx(0.162)
    assert store.costs.by_provider("r-1") == {
        "firecrawl": pytest.approx(0.012),
        "llm": pytest.approx(0.15),
    }
    assert store.costs.count() == 4


def test_a_run_with_no_cost_events_totals_zero(store: Store) -> None:
    assert store.costs.total("r-1") == 0.0
    assert store.costs.by_provider("r-1") == {}
