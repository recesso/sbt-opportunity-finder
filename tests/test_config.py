"""E0.S2 — config loading and validation.

Every negative test here corresponds to a way a config file can be broken by
hand-editing. The requirement is that each one fails at startup naming the file
and field, rather than producing a subtly wrong ranking three weeks later.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from finder.config import (
    DEFAULT_CONFIG_DIR,
    FAMILIES,
    Config,
    ConfigError,
    load_config,
)


@pytest.fixture
def cfg_dir(tmp_path: Path) -> Path:
    """A writable copy of the real config, so tests can corrupt one field."""
    dest = tmp_path / "config"
    shutil.copytree(DEFAULT_CONFIG_DIR, dest)
    return dest


def _edit(cfg_dir: Path, filename: str, mutate) -> None:
    path = cfg_dir / filename
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


# --- happy path ------------------------------------------------------------


def test_real_config_loads() -> None:
    cfg = load_config()
    assert isinstance(cfg, Config)
    assert set(cfg.weights.fit_weights) == set(FAMILIES)
    assert len(cfg.hash) == 64


def test_hash_is_stable_across_loads() -> None:
    assert load_config().hash == load_config().hash


def test_hash_changes_when_a_value_changes(cfg_dir: Path) -> None:
    before = load_config(cfg_dir).hash

    def bump(d):
        d["fit_weights"]["ROOM"]["reach"] += 1
        d["fit_weights"]["ROOM"]["precedent"] -= 1

    _edit(cfg_dir, "weights.yaml", bump)
    after = load_config(cfg_dir).hash
    assert before != after, "config hash must change when any value changes"


def test_accessors() -> None:
    cfg = load_config()
    assert cfg.route_base("ROOM", "OPEN_CALL") == 5
    assert cfg.route_base("ROOM", "UNKNOWN") == 0
    assert cfg.route_base("CHANNEL", "PROVIDER_NETWORK") == 5
    assert sum(cfg.fit_weights("CHANNEL").values()) == 100


def test_config_is_frozen() -> None:
    cfg = load_config()
    with pytest.raises(ValidationError):
        cfg.hash = "tampered"  # type: ignore[misc]


def test_lexicon_class_helpers() -> None:
    lex = load_config().lexicon
    assert "N_negative" not in lex.positive_classes
    assert len(lex.positive_classes) == 5
    assert lex.negative_terms, "negative terms drive the negative-density rule"


# --- missing and malformed -------------------------------------------------


def test_missing_file_names_the_file(cfg_dir: Path) -> None:
    (cfg_dir / "lexicon.yaml").unlink()
    with pytest.raises(ConfigError, match=r"lexicon\.yaml"):
        load_config(cfg_dir)


def test_bad_yaml_names_the_file(cfg_dir: Path) -> None:
    (cfg_dir / "paths.yaml").write_text("this: [is: not: valid", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"paths\.yaml"):
        load_config(cfg_dir)


def test_wrong_version_is_rejected(cfg_dir: Path) -> None:
    _edit(cfg_dir, "hosts.yaml", lambda d: d.update(version=2))
    with pytest.raises(ConfigError, match=r"hosts\.yaml"):
        load_config(cfg_dir)


def test_unknown_key_is_rejected(cfg_dir: Path) -> None:
    """extra='forbid' — a typo must fail rather than be silently ignored."""
    _edit(cfg_dir, "weights.yaml", lambda d: d.update(fit_weightz={}))
    with pytest.raises(ConfigError, match=r"weights\.yaml"):
        load_config(cfg_dir)


# --- the invariants that matter --------------------------------------------


def test_weights_not_summing_to_100_is_rejected(cfg_dir: Path) -> None:
    _edit(cfg_dir, "weights.yaml", lambda d: d["fit_weights"]["ROOM"].update(reach=17))
    with pytest.raises(ConfigError, match="sums to 99"):
        load_config(cfg_dir)


def test_geography_as_a_scored_dimension_is_rejected(cfg_dir: Path) -> None:
    """ADR: geography is a facet you sort by, never an input to quality.

    A national event with employers in the room and a workshop slot is a top
    opportunity wherever it is. This test is the guard rail on that decision.
    """

    def add_geo(d):
        d["fit_weights"]["ROOM"]["repeatability"] -= 5
        d["fit_weights"]["ROOM"]["geo_rank"] = 5

    _edit(cfg_dir, "weights.yaml", add_geo)
    with pytest.raises(ConfigError, match="geography"):
        load_config(cfg_dir)


def test_out_of_range_route_base_is_rejected(cfg_dir: Path) -> None:
    _edit(
        cfg_dir,
        "families.yaml",
        lambda d: d["families"]["ROOM"]["route_types"]["OPEN_CALL"].update(base=9),
    )
    with pytest.raises(ConfigError, match=r"families\.yaml"):
        load_config(cfg_dir)


def test_missing_family_is_rejected(cfg_dir: Path) -> None:
    _edit(cfg_dir, "families.yaml", lambda d: d["families"].pop("CHANNEL"))
    with pytest.raises(ConfigError, match="CHANNEL"):
        load_config(cfg_dir)


def test_family_without_unknown_route_type_is_rejected(cfg_dir: Path) -> None:
    """A target whose route is not yet established must remain representable.

    That is the whole WORTH A LOOK surface — GaMEP's provider path has no
    published intake and must still reach the founder with a question.
    """
    _edit(
        cfg_dir,
        "families.yaml",
        lambda d: d["families"]["CHANNEL"]["route_types"].pop("UNKNOWN"),
    )
    with pytest.raises(ConfigError, match="UNKNOWN"):
        load_config(cfg_dir)


def test_confidence_components_must_sum_to_one(cfg_dir: Path) -> None:
    _edit(
        cfg_dir,
        "weights.yaml",
        lambda d: d["confidence"]["component_weights"].update(recency=0.5),
    )
    with pytest.raises(ConfigError, match="component_weights"):
        load_config(cfg_dir)


def test_unreachable_worth_a_look_is_rejected(cfg_dir: Path) -> None:
    _edit(cfg_dir, "weights.yaml", lambda d: d["thresholds"]["worth_a_look"].update(fit_min=90))
    with pytest.raises(ConfigError, match="unreachable"):
        load_config(cfg_dir)


def test_unknown_lexicon_class_in_combination_is_rejected(cfg_dir: Path) -> None:
    _edit(cfg_dir, "lexicon.yaml", lambda d: d["strong_combinations"].append("XZ"))
    with pytest.raises(ConfigError, match="strong_combination"):
        load_config(cfg_dir)


def test_duplicate_lexicon_terms_are_rejected(cfg_dir: Path) -> None:
    _edit(cfg_dir, "lexicon.yaml", lambda d: d["classes"]["D_format"].append("workshop"))
    with pytest.raises(ConfigError, match="duplicate"):
        load_config(cfg_dir)


def test_empty_submission_hosts_is_rejected(cfg_dir: Path) -> None:
    """Off-domain form resolution depends on this list.

    GSAE's speaker form is a SurveyMonkey URL embedded in body text — not on
    gsae.org at all — which is why it could not be found by hand.
    """
    _edit(cfg_dir, "hosts.yaml", lambda d: d.update(submission_hosts=[]))
    with pytest.raises(ConfigError, match="submission_hosts"):
        load_config(cfg_dir)


def test_network_with_no_way_to_enumerate_is_rejected(cfg_dir: Path) -> None:
    """W1 must be able to reach the real node list somehow.

    node_count_est is a planning figure, never a source of truth — a network
    with no directory, no seeds and no discovery method is unbuildable.
    """

    def blank_it(d):
        d["networks"][0]["directory_url"] = None
        d["networks"][0].pop("seed_members", None)
        d["networks"][0].pop("discovery_method", None)

    _edit(cfg_dir, "networks.yaml", blank_it)
    with pytest.raises(ConfigError, match="nothing to enumerate from"):
        load_config(cfg_dir)


def test_every_network_can_be_enumerated() -> None:
    """The real config, not a mutated copy: no network is a dead end."""
    for net in load_config().networks.networks:
        assert net.directory_url or net.seed_members or net.discovery_method, (
            f"network {net.id} has no enumeration path"
        )


def test_duplicate_network_ids_are_rejected(cfg_dir: Path) -> None:
    def dupe(d):
        d["networks"].append(dict(d["networks"][0]))

    _edit(cfg_dir, "networks.yaml", dupe)
    with pytest.raises(ConfigError, match="duplicate network ids"):
        load_config(cfg_dir)


def test_thesis_missing_a_family_is_rejected(cfg_dir: Path) -> None:
    _edit(cfg_dir, "thesis.yaml", lambda d: d["thesis"].pop("EMPLOYER"))
    with pytest.raises(ConfigError, match="EMPLOYER"):
        load_config(cfg_dir)


def test_source_ids_must_be_contiguous(cfg_dir: Path) -> None:
    _edit(cfg_dir, "sources.yaml", lambda d: d["sources"][3].update(id=99))
    with pytest.raises(ConfigError, match=r"1\.\.N"):
        load_config(cfg_dir)


# --- accessor error paths --------------------------------------------------


def test_unknown_family_accessor_raises() -> None:
    with pytest.raises(ConfigError, match="unknown family"):
        load_config().family("WAREHOUSE")


def test_unknown_route_type_accessor_raises() -> None:
    with pytest.raises(ConfigError, match="unknown route_type"):
        load_config().route_base("ROOM", "TELEPATHY")
