"""Smoke tests: the package imports and the repo's own invariants hold.

These are cheap and they catch the two failure modes that waste the most time —
a broken install, and a config file that drifted from what the code expects.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]

CONFIG_FILES = [
    "families.yaml",
    "weights.yaml",
    "lexicon.yaml",
    "paths.yaml",
    "hosts.yaml",
    "networks.yaml",
    "thesis.yaml",
    "sources.yaml",
]


def test_package_imports() -> None:
    import finder

    assert finder is not None


def test_expected_layout_exists() -> None:
    for rel in ("config", "docs", "plan", "scripts", "src/finder", "tests"):
        assert (ROOT / rel).is_dir(), f"missing directory: {rel}"


@pytest.mark.parametrize("name", CONFIG_FILES)
def test_config_file_parses(name: str) -> None:
    path = ROOT / "config" / name
    assert path.is_file(), f"missing config file: {name}"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{name} did not parse to a mapping"
    assert data.get("version") == 1, f"{name} is missing a version key"


def test_fit_weights_sum_to_100_per_family() -> None:
    """The single invariant most likely to be broken by hand-editing weights."""
    weights = yaml.safe_load((ROOT / "config" / "weights.yaml").read_text(encoding="utf-8"))
    for family, dims in weights["fit_weights"].items():
        total = sum(dims.values())
        assert total == 100, f"{family} weights sum to {total}, expected 100"


def test_geography_is_not_a_scored_dimension() -> None:
    """ADR: geography is a display facet and a sort key, never an input to quality.

    A national event with employers in the room and a workshop slot is a top
    opportunity wherever it is. This test exists so that rule cannot be quietly
    reversed by adding a weight.
    """
    weights = yaml.safe_load((ROOT / "config" / "weights.yaml").read_text(encoding="utf-8"))
    banned = {"geo_rank", "geography", "geo", "distance", "proximity", "travel"}
    for family, dims in weights["fit_weights"].items():
        offending = banned & set(dims)
        assert not offending, f"{family} scores geography via {offending}"


def test_backlog_stories_are_self_contained() -> None:
    """Every story must be executable by a stranger.

    If a story has no files, no steps or no acceptance criterion, it cannot be
    picked up cold — which is the whole point of the backlog.
    """
    backlog = yaml.safe_load((ROOT / "plan" / "backlog.yaml").read_text(encoding="utf-8"))
    incomplete: list[str] = []
    for epic in backlog["epics"]:
        for story in epic.get("stories", []):
            if not (story.get("files") and story.get("steps") and story.get("acceptance")):
                incomplete.append(story["id"])
    assert not incomplete, f"stories missing files/steps/acceptance: {incomplete}"


def test_backlog_dependencies_resolve() -> None:
    """No story may depend on an id that does not exist."""
    backlog = yaml.safe_load((ROOT / "plan" / "backlog.yaml").read_text(encoding="utf-8"))
    known = {s["id"] for e in backlog["epics"] for s in e.get("stories", [])}
    dangling: list[tuple[str, str]] = []
    for epic in backlog["epics"]:
        for story in epic.get("stories", []):
            for dep in story.get("depends") or []:
                if dep not in known:
                    dangling.append((story["id"], dep))
    assert not dangling, f"dangling dependencies: {dangling}"
