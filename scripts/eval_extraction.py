#!/usr/bin/env python3
"""Measure the extractor against the labelled pages, with a real model.

This is the E5.S2 acceptance gate: **≥90% field accuracy and ZERO fabricated
spans**. It cannot be answered by the offline suite, because a scripted fake
returns whatever the test wrote and would only measure the test. So it lives
here, is run deliberately, and costs one model call per page.

    make eval-extraction                 # the ten labelled pages
    python scripts/eval_extraction.py --page thin_page

The two numbers are not equally weighted, and the report says so. Field accuracy
is a quality bar that can be argued about. Fabricated spans is a correctness
bar: a single one means the system asserted something the page does not say,
which is the failure everything here was built to prevent. One is enough to
fail the gate.

Exit code 0 only if both bars are met.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from finder.acquire.providers.base import Snapshot  # noqa: E402
from finder.acquire.providers.llm import AnthropicLLM  # noqa: E402
from finder.acquire.snapshot import content_hash  # noqa: E402
from finder.extract.schemas import NOT_STATED  # noqa: E402
from finder.extract.w3_mechanism import MechanismExtractor  # noqa: E402
from finder.secrets import load_secrets, require  # noqa: E402
from finder.store.db import utcnow  # noqa: E402

LABELLED = ROOT / "tests" / "fixtures" / "extraction_labelled" / "pages.json"
ACCURACY_BAR = 0.90
FABRICATION_BAR = 0


def load_pages() -> list[dict[str, Any]]:
    return json.loads(LABELLED.read_text(encoding="utf-8"))["pages"]


def field_value(record: Any, name: str) -> Any:
    """The extracted value for a labelled field, looking one level in.

    Labels name fields the way a person would (`member_unit`), not the way the
    schema nests them (`audience.member_unit`).
    """
    for holder in (record, getattr(record, "audience", None), getattr(record, "intake", None)):
        if holder is None:
            continue
        found = getattr(holder, name, None)
        if found is not None:
            return getattr(found, "value", found)
    return None


def compare(expected: Any, actual: Any) -> bool:
    """Loose on formatting, strict on substance.

    not_stated must match exactly — that is the whole discipline. Everything
    else is compared case-insensitively on stripped text, because "Zack Huhn"
    and "zack huhn " are the same answer and marking them different would make
    the number meaningless.
    """
    if expected == NOT_STATED or actual == NOT_STATED:
        return expected == actual
    if isinstance(expected, list) and isinstance(actual, list):
        return {str(x).strip().lower() for x in expected} == {
            str(x).strip().lower() for x in actual
        }
    return str(expected).strip().lower() == str(actual).strip().lower()


def evaluate(extractor: MechanismExtractor, spec: dict[str, Any]) -> dict[str, Any]:
    snapshot = Snapshot(
        content_hash=content_hash(spec["markdown"]),
        url=spec["url"],
        canonical_url=spec["url"],
        markdown=spec["markdown"],
        fetched_at=utcnow(),
        provider="fixture",
    )
    result = extractor.extract(spec["family"], snapshot)

    checked: list[tuple[str, Any, Any, bool]] = []
    if result.record is not None:
        for name, expected in spec["expect"].items():
            if name.startswith("_"):
                continue
            actual = field_value(result.record, name)
            if actual is None:
                continue  # a label for a field this family does not carry
            checked.append((name, expected, actual, compare(expected, actual)))

    return {
        "id": spec["id"],
        "family": spec["family"],
        "quarantined": result.quarantined is not None,
        "fabricated": list(result.dropped),
        "checked": checked,
        "correct": sum(1 for *_, ok in checked if ok),
        "total": len(checked),
    }


def report(results: list[dict[str, Any]]) -> int:
    correct = sum(r["correct"] for r in results)
    total = sum(r["total"] for r in results)
    fabricated = sum(len(r["fabricated"]) for r in results)
    quarantined = sum(1 for r in results if r["quarantined"])
    accuracy = correct / total if total else 0.0

    print("\n" + "=" * 72)
    for r in results:
        flag = "FABRICATED" if r["fabricated"] else ("QUARANTINED" if r["quarantined"] else "")
        score = f"{r['correct']}/{r['total']}" if r["total"] else "-"
        print(f"  {r['id']:24} {r['family']:9} {score:>7}  {flag}")
        for name, expected, actual, ok in r["checked"]:
            if not ok:
                print(f"      MISS {name}: expected {expected!r}, got {actual!r}")
        for name in r["fabricated"]:
            print(f"      FABRICATED SPAN {name} — stated with no support in the page")
    print("=" * 72)

    print(f"\nField accuracy      {accuracy:.1%}  ({correct}/{total})   bar {ACCURACY_BAR:.0%}")
    print(f"Fabricated spans    {fabricated}                bar {FABRICATION_BAR}")
    print(f"Quarantined pages   {quarantined}")

    failures = []
    if accuracy < ACCURACY_BAR:
        failures.append(f"field accuracy {accuracy:.1%} is below {ACCURACY_BAR:.0%}")
    if fabricated > FABRICATION_BAR:
        failures.append(
            f"{fabricated} fabricated span(s). This is the bar that matters most: the "
            "system asserted something the page does not say."
        )
    if failures:
        print("\nGATE NOT MET:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("\nGate met. Both bars.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--page", action="append", default=[], help="run one labelled page by id")
    ap.add_argument("--model", default=None)
    ap.add_argument("--today", default=utcnow()[:10])
    args = ap.parse_args()

    pages = load_pages()
    if args.page:
        wanted = set(args.page)
        pages = [p for p in pages if p["id"] in wanted]
        if not pages:
            sys.stderr.write(f"no labelled page matching {sorted(wanted)}\n")
            return 2

    secrets = load_secrets()
    require(secrets, "LLM_API_KEY")
    llm = AnthropicLLM(secrets.LLM_API_KEY or "", **({"model": args.model} if args.model else {}))
    extractor = MechanismExtractor(llm, today=args.today)

    print(f"Evaluating {len(pages)} labelled page(s) with {llm.model}...")
    results = [evaluate(extractor, spec) for spec in pages]
    return report(results)


if __name__ == "__main__":
    raise SystemExit(main())
