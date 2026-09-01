"""Deterministic identifiers.

Ids are derived from the natural key, not from a counter or a UUID. Two
consequences that matter:

* A replayed run produces the same ids, so re-running is genuinely idempotent
  rather than idempotent-if-you-squint.
* An id collision means a real duplicate, which the UNIQUE constraints then
  catch — instead of two rows quietly coexisting under different surrogate keys.
  That is precisely how the predecessor accumulated 880 duplicate rows.
"""

from __future__ import annotations

import hashlib

_HASH_LEN = 12


def _digest(*parts: str) -> str:
    joined = "\x1f".join(p.strip().lower() for p in parts)
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:_HASH_LEN]


def org_id(canonical_domain: str) -> str:
    """Organizations are identified by their registrable domain.

    Names drift ("SC Manufacturers & Commerce" vs "South Carolina Manufacturers
    Council"); domains do not.
    """
    if not canonical_domain.strip():
        raise ValueError("canonical_domain is required to derive an org_id")
    return f"org-{_digest(canonical_domain)}"


def employer_id(domain: str | None, name: str) -> str:
    if domain and domain.strip():
        return f"emp-{_digest(domain)}"
    if not name.strip():
        raise ValueError("employer needs a domain or a name")
    return f"emp-{_digest(name)}"


def route_id(series_key: str) -> str:
    """One route per series_key. The UNIQUE constraint and the id agree."""
    if not series_key.strip():
        raise ValueError("series_key is required to derive a route_id")
    return f"rt-{_digest(series_key)}"


def occurrence_id(occurrence_key: str) -> str:
    if not occurrence_key.strip():
        raise ValueError("occurrence_key is required to derive an occurrence_id")
    return f"occ-{_digest(occurrence_key)}"


def person_id(org_key: str, name: str) -> str:
    if not name.strip():
        raise ValueError("person needs a name")
    return f"per-{_digest(org_key, name)}"


def evidence_id(route_key: str, field_name: str, content_hash: str) -> str:
    """One evidence row per (route, field, source snapshot).

    Re-extracting the same field from the same snapshot must update in place
    rather than pile up duplicate provenance.
    """
    return f"ev-{_digest(route_key, field_name, content_hash)}"


def score_id(route_key: str, config_hash: str, scored_at: str) -> str:
    return f"sc-{_digest(route_key, config_hash, scored_at)}"


def trigger_id(employer_key: str, kind: str, occurred_on: str, source_url: str) -> str:
    return f"tg-{_digest(employer_key, kind, occurred_on, source_url)}"


def signal_id(route_key: str, kind: str, detected_at: str) -> str:
    return f"sg-{_digest(route_key, kind, detected_at)}"


def rejection_id(match_name: str, match_domain: str, family_scope: str) -> str:
    return f"rj-{_digest(match_name or '', match_domain or '', family_scope)}"


def mark_id(route_key: str, marked_at: str) -> str:
    return f"mk-{_digest(route_key, marked_at)}"
