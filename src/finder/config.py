"""Config-as-data loading and validation.

ADR-008: all tuning lives in config/*.yaml and nowhere else. Changing a weight
must never require a code change, and every score row records the config hash
that produced it — so any ranking change is attributable and rollback is one
line.

Validation is fail-fast and loud. A typo in a config file must stop the run at
startup naming the file and field, not produce a subtly wrong ranking three
weeks later.

    from finder.config import load_config
    cfg = load_config()
    cfg.hash                      # sha256 over canonical JSON
    cfg.weights.fit_weights["ROOM"]["employer_presence"]
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

# ---------------------------------------------------------------------------

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"

FAMILIES: tuple[str, ...] = ("ROOM", "CHANNEL", "EMPLOYER", "PERSON")

CONFIG_FILES: dict[str, str] = {
    "families": "families.yaml",
    "weights": "weights.yaml",
    "lexicon": "lexicon.yaml",
    "paths": "paths.yaml",
    "hosts": "hosts.yaml",
    "networks": "networks.yaml",
    "thesis": "thesis.yaml",
    "sources": "sources.yaml",
}


class ConfigError(Exception):
    """Raised when configuration is missing, unparseable or internally inconsistent.

    The message always names the file and, where possible, the field.
    """


class _Base(BaseModel):
    """Forbid unknown keys so a typo fails loudly instead of being ignored."""

    model_config = ConfigDict(extra="forbid", frozen=True)


# --- families.yaml ---------------------------------------------------------


class RouteType(_Base):
    base: int = Field(ge=0, le=5)
    requires: list[str] = Field(default_factory=list)


class FamilyDef(_Base):
    description: str
    route_types: dict[str, RouteType]
    rule: str | None = None


class TriggerDef(_Base):
    base: int = Field(ge=0, le=5)
    half_life_days: int | None = None


class FamiliesConfig(_Base):
    version: int
    families: dict[str, FamilyDef]
    openness: dict[str, float]
    triggers: dict[str, TriggerDef]

    @model_validator(mode="after")
    def _check(self) -> FamiliesConfig:
        missing = set(FAMILIES) - set(self.families)
        if missing:
            raise ValueError(f"families.yaml is missing families: {sorted(missing)}")
        unknown = set(self.families) - set(FAMILIES)
        if unknown:
            raise ValueError(f"families.yaml defines unknown families: {sorted(unknown)}")
        for fam, defn in self.families.items():
            if "UNKNOWN" not in defn.route_types and fam != "PERSON":
                raise ValueError(
                    f"families.yaml: family {fam} has no UNKNOWN route_type. "
                    "Every family except PERSON needs one, because a target whose "
                    "route is not yet established must still be representable."
                )
        for name, mult in self.openness.items():
            if not 0.0 <= mult <= 1.0:
                raise ValueError(f"families.yaml: openness.{name}={mult} outside 0..1")
        return self


# --- weights.yaml ----------------------------------------------------------

# ADR: geography is a display facet and a sort key, never an input to quality.
# A national event with employers in the room and a workshop slot is a top
# opportunity wherever it is. This tuple exists so the rule cannot be quietly
# reversed by adding a weight.
BANNED_SCORE_DIMENSIONS: frozenset[str] = frozenset(
    {"geo_rank", "geography", "geo", "distance", "proximity", "travel", "drive_time"}
)


class BestThresholds(_Base):
    fit_min: int = Field(ge=0, le=100)
    route_min: int = Field(ge=0, le=100)
    confidence_min: int = Field(ge=0, le=100)
    require_route_url: bool


class WorthALookThresholds(_Base):
    fit_min: int = Field(ge=0, le=100)


class Thresholds(_Base):
    best: BestThresholds
    worth_a_look: WorthALookThresholds
    pin_if_door_closes_within_days: int = Field(ge=0)


class ConfidenceConfig(_Base):
    component_weights: dict[str, float]
    recency_factor: dict[str, float]
    evidence_level: dict[str, int]

    @model_validator(mode="after")
    def _check(self) -> ConfidenceConfig:
        total = sum(self.component_weights.values())
        if abs(total - 1.0) > 1e-9:
            raise ValueError(
                f"weights.yaml: confidence.component_weights sum to {total}, expected 1.0"
            )
        return self


class WeightFitting(_Base):
    min_marks_per_family: int = Field(ge=1)
    max_delta_pct: int = Field(ge=0, le=100)
    weight_bounds: list[int]
    stability_min_jaccard: float = Field(ge=0.0, le=1.0)


class SourceAllocation(_Base):
    floor_pct_per_class: int = Field(ge=0, le=100)
    demote_if_good_rate_below: float = Field(ge=0.0, le=1.0)
    demote_after_consecutive_runs: int = Field(ge=1)


class GeographyFacet(_Base):
    """Display and sorting only. Deliberately not part of any score."""

    home_base: str
    drivable_max_minutes: int = Field(ge=0)
    display_order: list[str]


class WeightsConfig(_Base):
    version: int
    fit_weights: dict[str, dict[str, int]]
    thresholds: Thresholds
    confidence: ConfidenceConfig
    weight_fitting: WeightFitting
    source_allocation: SourceAllocation
    geography_facet: GeographyFacet

    @model_validator(mode="after")
    def _check(self) -> WeightsConfig:
        missing = set(FAMILIES) - set(self.fit_weights)
        if missing:
            raise ValueError(f"weights.yaml is missing fit_weights for: {sorted(missing)}")

        for family, dims in self.fit_weights.items():
            if family not in FAMILIES:
                raise ValueError(f"weights.yaml: unknown family {family!r} in fit_weights")

            total = sum(dims.values())
            if total != 100:
                raise ValueError(
                    f"weights.yaml: fit_weights.{family} sums to {total}, expected 100"
                )

            banned = BANNED_SCORE_DIMENSIONS & set(dims)
            if banned:
                raise ValueError(
                    f"weights.yaml: fit_weights.{family} scores geography via {sorted(banned)}. "
                    "Geography ranks nothing — it is a facet you sort and filter by. "
                    "Use repeatability if you meant 'can I be here more than once'."
                )

            for dim, value in dims.items():
                if not 0 <= value <= 100:
                    raise ValueError(
                        f"weights.yaml: fit_weights.{family}.{dim}={value} outside 0..100"
                    )

        lo, hi = self.weight_fitting.weight_bounds
        if lo >= hi:
            raise ValueError(f"weights.yaml: weight_bounds {[lo, hi]} is not an increasing range")

        t = self.thresholds
        if t.worth_a_look.fit_min > t.best.fit_min:
            raise ValueError(
                "weights.yaml: worth_a_look.fit_min exceeds best.fit_min, which would make "
                "WORTH A LOOK unreachable"
            )
        return self


# --- lexicon.yaml ----------------------------------------------------------


class LexiconConfig(_Base):
    version: int
    min_classes: int = Field(ge=1)
    require_classes: list[str] = []
    classes: dict[str, list[str]]
    strong_combinations: list[str]
    trace_phrasings: list[str]

    @model_validator(mode="after")
    def _check(self) -> LexiconConfig:
        letters = {name[0].upper() for name in self.classes}
        if self.min_classes > len(self.classes):
            raise ValueError(
                f"lexicon.yaml: min_classes={self.min_classes} exceeds the "
                f"{len(self.classes)} classes defined"
            )
        unknown_required = {r.upper() for r in self.require_classes} - letters
        if unknown_required:
            raise ValueError(
                f"lexicon.yaml: require_classes references unknown class letters "
                f"{sorted(unknown_required)}"
            )
        for combo in self.strong_combinations:
            unknown = {c for c in combo.upper()} - letters
            if unknown:
                raise ValueError(
                    f"lexicon.yaml: strong_combination {combo!r} references "
                    f"unknown class letters {sorted(unknown)}"
                )
        for name, terms in self.classes.items():
            if not terms:
                raise ValueError(f"lexicon.yaml: class {name} is empty")
            lowered = [t.lower() for t in terms]
            dupes = {t for t in lowered if lowered.count(t) > 1}
            if dupes:
                raise ValueError(f"lexicon.yaml: class {name} has duplicate terms {sorted(dupes)}")
        return self

    @property
    def positive_classes(self) -> dict[str, list[str]]:
        """Marker classes A-E. Excludes the negative class."""
        return {k: v for k, v in self.classes.items() if not k.startswith("N_")}

    @property
    def negative_terms(self) -> list[str]:
        out: list[str] = []
        for k, v in self.classes.items():
            if k.startswith("N_"):
                out.extend(v)
        return out


# --- paths.yaml ------------------------------------------------------------


class PathsConfig(_Base):
    version: int
    PROGRAMMING_PATHS: list[str]
    PARTNER_PATHS: list[str]

    @model_validator(mode="after")
    def _check(self) -> PathsConfig:
        for name in ("PROGRAMMING_PATHS", "PARTNER_PATHS"):
            terms = getattr(self, name)
            if not terms:
                raise ValueError(f"paths.yaml: {name} is empty")
        return self


# --- hosts.yaml ------------------------------------------------------------


class AmsPattern(_Base):
    hosts: list[str]
    paths: list[str] = Field(default_factory=list)


class HostsConfig(_Base):
    version: int
    submission_hosts: list[str]
    ams_patterns: dict[str, AmsPattern]

    @model_validator(mode="after")
    def _check(self) -> HostsConfig:
        if not self.submission_hosts:
            raise ValueError(
                "hosts.yaml: submission_hosts is empty. Off-domain form resolution "
                "depends on it — the GSAE speaker form is a SurveyMonkey link."
            )
        return self


# --- networks.yaml ---------------------------------------------------------


class SeedMember(_Base):
    name: str
    domain: str | None = None
    state: str | None = None


class NetworkDef(_Base):
    id: str
    name: str
    directory_url: str | None = None
    discovery_method: Literal["ams_host_patterns", "graph_expansion"] | None = None
    sectors: list[str]
    node_count_est: int | None = None
    tier: Literal["A", "B", "C"]
    notes: str | None = None
    seed_members: list[SeedMember] | None = None

    @model_validator(mode="after")
    def _check(self) -> NetworkDef:
        if not (self.directory_url or self.seed_members or self.discovery_method):
            raise ValueError(
                f"networks.yaml: network {self.id!r} gives W1 nothing to enumerate from. "
                "Set one of: directory_url, seed_members, or discovery_method."
            )
        return self


class NetworksConfig(_Base):
    version: int
    networks: list[NetworkDef]

    @model_validator(mode="after")
    def _check(self) -> NetworksConfig:
        ids = [n.id for n in self.networks]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"networks.yaml: duplicate network ids {sorted(dupes)}")
        return self


# --- thesis.yaml -----------------------------------------------------------


class PractitionerRoster(_Base):
    seed_query_shapes: list[str]
    people: list[Any] = Field(default_factory=list)
    firms: list[Any] = Field(default_factory=list)


class ThesisConfig(_Base):
    version: int
    sbt_context: str
    thesis: dict[str, str]
    practitioner_roster: PractitionerRoster

    @model_validator(mode="after")
    def _check(self) -> ThesisConfig:
        missing = set(FAMILIES) - set(self.thesis)
        if missing:
            raise ValueError(f"thesis.yaml is missing a thesis for: {sorted(missing)}")
        for family, text in self.thesis.items():
            if len(text.split()) < 25:
                raise ValueError(
                    f"thesis.yaml: thesis.{family} is too short to be a useful "
                    "embedding target (under 25 words)"
                )
        return self


# --- sources.yaml ----------------------------------------------------------


class SourceDef(_Base):
    id: int
    name: str
    kind: Literal["direct", "indirect"]
    worker: str
    cadence: str
    method: str
    note: str | None = None


class TierRule(_Base):
    cadence: str
    rule: str


class SourcesConfig(_Base):
    version: int
    sources: list[SourceDef]
    tiering: dict[str, TierRule]

    @model_validator(mode="after")
    def _check(self) -> SourcesConfig:
        ids = [s.id for s in self.sources]
        if sorted(ids) != list(range(1, len(ids) + 1)):
            raise ValueError(f"sources.yaml: source ids must be 1..N with no gaps, got {ids}")
        missing = {"A", "B", "C"} - set(self.tiering)
        if missing:
            raise ValueError(f"sources.yaml: tiering is missing tiers {sorted(missing)}")
        return self


# --- the aggregate ---------------------------------------------------------


class Config(_Base):
    """The whole configuration, validated and frozen.

    ``hash`` is a sha256 over the canonical JSON of every value. It is recorded
    on every score row so a ranking is always attributable to a specific
    configuration, and rolling back a bad weight fit is a one-line change.
    """

    families: FamiliesConfig
    weights: WeightsConfig
    lexicon: LexiconConfig
    paths: PathsConfig
    hosts: HostsConfig
    networks: NetworksConfig
    thesis: ThesisConfig
    sources: SourcesConfig
    hash: str

    def family(self, name: str) -> FamilyDef:
        try:
            return self.families.families[name]
        except KeyError:
            raise ConfigError(f"unknown family {name!r}; expected one of {FAMILIES}") from None

    def route_base(self, family: str, route_type: str) -> int:
        """Base route strength 0-5 for a (family, route_type) pair."""
        types = self.family(family).route_types
        try:
            return types[route_type].base
        except KeyError:
            raise ConfigError(
                f"unknown route_type {route_type!r} for family {family!r}; "
                f"expected one of {sorted(types)}"
            ) from None

    def fit_weights(self, family: str) -> dict[str, int]:
        try:
            return self.weights.fit_weights[family]
        except KeyError:
            raise ConfigError(f"no fit_weights for family {family!r}") from None


# ---------------------------------------------------------------------------


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"missing config file: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path.name} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name} did not parse to a mapping")
    if data.get("version") != 1:
        raise ConfigError(f"{path.name}: expected 'version: 1', got {data.get('version')!r}")
    return data


def _canonical_json(cfg_dicts: dict[str, dict[str, Any]]) -> str:
    return json.dumps(cfg_dicts, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _format_validation_error(filename: str, exc: ValidationError) -> str:
    """Flatten pydantic's multi-line output into one line per problem.

    The first line must carry file, field and reason — someone editing YAML at
    2am should not have to read a stack trace to find the typo.
    """
    lines: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "(root)"
        msg = err["msg"].removeprefix("Value error, ").strip()
        # Our own validators already name the file; don't say it twice.
        msg = msg.removeprefix(f"{filename}: ")
        if err["type"] == "extra_forbidden":
            msg = f"unknown key {loc!r} — check for a typo"
            lines.append(f"{filename}: {msg}")
        else:
            lines.append(f"{filename}: {loc}: {msg}")
    return "\n".join(lines)


def load_config(config_dir: Path | str | None = None) -> Config:
    """Read, validate and freeze the whole configuration.

    Raises ``ConfigError`` naming the file and field on any problem. Never
    returns a partially valid configuration.
    """
    directory = Path(config_dir) if config_dir else DEFAULT_CONFIG_DIR

    raw: dict[str, dict[str, Any]] = {}
    for key, filename in CONFIG_FILES.items():
        raw[key] = _read_yaml(directory / filename)

    models: dict[str, type[BaseModel]] = {
        "families": FamiliesConfig,
        "weights": WeightsConfig,
        "lexicon": LexiconConfig,
        "paths": PathsConfig,
        "hosts": HostsConfig,
        "networks": NetworksConfig,
        "thesis": ThesisConfig,
        "sources": SourcesConfig,
    }

    parsed: dict[str, BaseModel] = {}
    for key, model in models.items():
        try:
            parsed[key] = model.model_validate(raw[key])
        except ValidationError as exc:
            raise ConfigError(_format_validation_error(CONFIG_FILES[key], exc)) from exc

    _validate_cross_file(parsed)

    return Config(**parsed, hash=hashlib.sha256(_canonical_json(raw).encode()).hexdigest())


def _validate_cross_file(parsed: dict[str, BaseModel]) -> None:
    """Invariants that span more than one file."""
    families: FamiliesConfig = parsed["families"]  # type: ignore[assignment]
    weights: WeightsConfig = parsed["weights"]  # type: ignore[assignment]
    thesis: ThesisConfig = parsed["thesis"]  # type: ignore[assignment]

    for family in weights.fit_weights:
        if family not in families.families:
            raise ConfigError(
                f"weights.yaml defines fit_weights for {family!r}, which families.yaml "
                "does not define"
            )

    for family in thesis.thesis:
        if family not in families.families:
            raise ConfigError(
                f"thesis.yaml defines a thesis for {family!r}, which families.yaml does not define"
            )

    # trigger_strength is only meaningful for EMPLOYER, and EMPLOYER is only
    # meaningful with triggers defined.
    if "trigger_strength" in weights.fit_weights.get("EMPLOYER", {}) and not families.triggers:
        raise ConfigError(
            "weights.yaml scores EMPLOYER on trigger_strength but families.yaml defines no triggers"
        )
