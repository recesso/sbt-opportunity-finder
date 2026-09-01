#!/usr/bin/env python3
"""Build the labelled dedupe evaluation set from the predecessor export.

The labels must not come from the code under test, or the evaluation is
circular and proves nothing. Every label here is derived from signals that
``finder.store.keys`` does not use, or is hand-checked:

  DUPLICATE      identical Organization string AND identical Official source URL
                 AND mechanism similarity >= 90. Two rows citing the same page,
                 for the same organization, describing the same thing.

  DISTINCT       different registrable domain AND different first word of the
                 organization name. Unrelated by construction.

This script generates ONLY those two easy classes. The hard cases — a name
variant versus a sibling sub-body, a shared platform host versus a shared
organization — live in tests/fixtures/dedupe_hard_cases.json and are curated by
hand, because an automated labeller cannot make the judgement that is being
tested. A first version of this script tried, and mislabelled JAX Chamber Health
Council and JAX Chamber IT Council as the same organization.

    python scripts/build_dedupe_fixture.py --review   # inspect candidate pairs
    python scripts/build_dedupe_fixture.py            # write the fixture
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
from collections import defaultdict
from pathlib import Path

from rapidfuzz import fuzz

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "tests" / "fixtures" / "dedupe_labelled.json"

SOURCE = Path(
    r"C:\Users\artre\Documents\Claude\Projects\GTM-Event-Tracker"
    r"\tracker-export\All Opportunities.csv"
)

SEED = 20260901
TARGET_TOTAL = 500

_HOST = re.compile(r"^(?:https?://)?([^/?#]+)")
_HOSTING = {"www", "members", "member", "business", "my", "portal", "events", "web"}


def domain_of(url: str) -> str:
    """Deliberately re-implemented here, not imported from the code under test."""
    m = _HOST.match((url or "").strip())
    if not m:
        return ""
    labels = m.group(1).lower().split(":")[0].strip(".").split(".")
    while len(labels) > 2 and labels[0] in _HOSTING:
        labels = labels[1:]
    return ".".join(labels[-2:]) if len(labels) >= 2 else ".".join(labels)


PLACE_MARKERS = {"post", "chapter", "roundtable", "section", "branch"}


def place_of(name: str) -> str:
    """Crude place extraction, independent of the code under test."""
    tokens = re.sub(r"[^\w\s]", " ", (name or "").lower()).split()
    out: list[str] = []
    for i, t in enumerate(tokens):
        if t in PLACE_MARKERS and i > 0:
            out.extend(tokens[max(0, i - 2) : i])
    return " ".join(sorted(set(out)))


def load_rows() -> list[dict[str, str]]:
    if not SOURCE.is_file():
        raise SystemExit(
            f"predecessor export not found at:\n  {SOURCE}\n"
            "This fixture is built once and committed; you only need the export "
            "to regenerate it."
        )
    with SOURCE.open(encoding="utf-8-sig", newline="") as fh:
        for _ in range(4):
            next(fh)
        rows = list(csv.DictReader(fh))
    return [
        {
            "id": r.get("ID", "").strip(),
            "org": (r.get("Organization") or "").strip(),
            "mech": (r.get("Opportunity / mechanism") or "").strip(),
            "url": (r.get("Official source") or "").strip(),
        }
        for r in rows
        if r.get("ID", "").strip()
    ]


def build(rows: list[dict[str, str]]) -> dict[str, list[dict]]:
    rng = random.Random(SEED)
    pairs: dict[str, list[dict]] = defaultdict(list)

    # --- DUPLICATE: same org string, same URL, similar mechanism -----------
    by_org_url: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        if r["org"] and r["url"]:
            by_org_url[(r["org"], r["url"])].append(r)
    for group in by_org_url.values():
        if len(group) < 2:
            continue
        a, b = group[0], group[1]
        if fuzz.token_set_ratio(a["mech"].lower(), b["mech"].lower()) >= 90:
            pairs["DUPLICATE"].append({"a": a, "b": b, "label": True})

    # --- DISTINCT: different domain and different leading word ------------
    shuffled = rows[:]
    rng.shuffle(shuffled)
    for a, b in zip(shuffled[::2], shuffled[1::2], strict=False):
        da, db = domain_of(a["url"]), domain_of(b["url"])
        if not da or not db or da == db:
            continue
        wa = a["org"].split()[:1]
        wb = b["org"].split()[:1]
        if wa and wb and wa[0].lower() == wb[0].lower():
            continue
        pairs["DISTINCT"].append({"a": a, "b": b, "label": False})

    for cls in ("DUPLICATE", "DISTINCT"):
        rng.shuffle(pairs[cls])
        pairs[cls] = pairs[cls][: TARGET_TOTAL // 2]

    return pairs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--review", action="store_true", help="print hard pairs for hand-checking")
    args = ap.parse_args()

    rows = load_rows()
    pairs = build(rows)

    if args.review:
        for cls in ("DUPLICATE", "DISTINCT"):
            print(f"\n{'=' * 78}\n{cls}  ({len(pairs[cls])} pairs)\n{'=' * 78}")
            for p in pairs[cls][:15]:
                print(f"    A  {p['a']['org'][:70]}")
                print(f"    B  {p['b']['org'][:70]}")
        return 0

    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "_method": (
            "Labels derived from signals independent of finder.store.keys, or "
            "hand-checked. See scripts/build_dedupe_fixture.py for the rules. "
            "Regenerate with that script against the predecessor export."
        ),
        "_source": "GTM-Event-Tracker/tracker-export/All Opportunities.csv (1,689 rows)",
        "_seed": SEED,
        "counts": {k: len(v) for k, v in pairs.items()},
        "pairs": [dict(p, klass=k) for k, v in pairs.items() for p in v],
    }
    OUT.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")
    total = sum(len(v) for v in pairs.values())
    print(f"wrote {OUT.relative_to(ROOT)}  ({total} pairs)")
    for k in payload["counts"]:
        print(f"  {k:16} {len(pairs[k]):4}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
