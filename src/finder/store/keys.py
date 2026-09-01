"""Normalisation and content dedupe keys.

Dedupe by content, never by ID. In the predecessor database 880 of the 976 rows
that carried a key were inside duplicate clusters — the keys existed on the
schema and were never checked before a write.

The hard part is not hashing. It is deciding what counts as the same thing, and
the answer is asymmetric in a way the real data makes obvious:

* ``same.org`` hosts nine SAME posts, ``cscmp.org`` hosts the Atlanta and
  Charlotte roundtables, ``hfma.org`` hosts twelve state chapters. These are
  **different organizations** that happen to share a domain.
* ``South Carolina Manufacturers & Commerce`` and ``South Carolina Manufacturers
  Council`` share a domain and are the **same organization** under a name
  variant — and treating them as different is why twelve rows of a permanently
  rejected organization survived in the predecessor.

The discriminator between those two cases is the *place*: two names on one
domain with different place qualifiers are different chapters; with the same or
no place qualifier they are the same body under a different name.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

from rapidfuzz import fuzz

# --- domains ---------------------------------------------------------------

# Subdomains that are hosting artefacts, not identity. Association management
# systems put member portals on these (business.athensga.com,
# members.tagonline.org, dekalb.chambermaster.com).
_HOSTING_SUBDOMAINS = {"www", "members", "member", "business", "my", "portal", "events", "web"}

# Enough multi-part suffixes for the geographies in scope. This is deliberately
# not a full Public Suffix List: bundling one means bundling its staleness, and
# every organization in this system is US-based. Revisit if that changes.
_MULTIPART_SUFFIXES = {
    "co.uk",
    "org.uk",
    "ac.uk",
    "gov.uk",
    "com.au",
    "org.au",
    "net.au",
    "co.nz",
    "org.nz",
    "co.za",
}

# Hosts that belong to a platform, not to the organization using it. Two
# unrelated bodies routinely share one: in the predecessor data the Maritime
# Association of South Carolina and the South Carolina Manufacturers Council
# both cite glueup.com. Keying identity on these merges strangers.
PLATFORM_HOSTS = {
    # event and registration platforms
    "glueup.com",
    "eventbrite.com",
    "cvent.com",
    "whova.com",
    "bizzabo.com",
    "swoogo.com",
    "birdease.com",
    "regfox.com",
    "ticketleap.com",
    # association management systems
    "chambermaster.com",
    "memberclicks.net",
    "site-ym.com",
    "yourmembership.com",
    "novi.io",
    "novisolutions.com",
    "weblinkconnect.com",
    "personifycloud.com",
    "impexium.com",
    "growthzone.com",
    "wildapricot.org",
    # form and submission hosts
    "surveymonkey.com",
    "google.com",
    "forms.gle",
    "jotform.com",
    "typeform.com",
    "wufoo.com",
    "formstack.com",
    "sessionize.com",
    "papercall.io",
    "proposalspace.com",
    "submittable.com",
    "airtable.com",
    "smartsheet.com",
    # social and generic publishing
    "linkedin.com",
    "facebook.com",
    "x.com",
    "twitter.com",
    "instagram.com",
    "youtube.com",
    "eventful.com",
    "meetup.com",
    "mailchi.mp",
    "constantcontact.com",
}

_URL_HOST = re.compile(r"^(?:https?://)?([^/?#]+)", re.IGNORECASE)


def registrable_domain(url_or_host: str) -> str:
    """The domain that identifies an organization, stripped of hosting noise.

    ``https://members.tagonline.org/calendar`` -> ``tagonline.org``
    """
    if not url_or_host or not url_or_host.strip():
        return ""
    match = _URL_HOST.match(url_or_host.strip())
    if not match:
        return ""
    host = match.group(1).lower().split(":")[0].strip(".")
    if not host:
        return ""

    labels = host.split(".")
    while len(labels) > 2 and labels[0] in _HOSTING_SUBDOMAINS:
        labels = labels[1:]

    if len(labels) <= 2:
        return ".".join(labels)

    if ".".join(labels[-2:]) in _MULTIPART_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


# --- organization names ----------------------------------------------------

_LEGAL_SUFFIXES = {
    "inc",
    "llc",
    "llp",
    "ltd",
    "corp",
    "corporation",
    "co",
    "company",
    "incorporated",
    "limited",
    "plc",
    "lp",
    "pc",
}

# Body-type words that do not distinguish one organization from another.
# "Georgia Association of Manufacturers" and "Georgia Manufacturers Association"
# are one body; the word "association" carries no identity.
_BODY_WORDS = {
    "the",
    "of",
    "and",
    "for",
    "a",
    "an",
    "at",
    "in",
    "on",
    "association",
    "assn",
    "alliance",
    "society",
    "federation",
    "organization",
    "organisation",
    "group",
    "network",
    "coalition",
    "partnership",
    "consortium",
    "center",
    "centre",
}

# Markers that introduce a chapter's place name. Deliberately narrow.
# "Council" is excluded: it is used as a body-type word at least as often as a
# chapter marker, and including it splits "SC Manufacturers Council" away from
# "SC Manufacturers & Commerce" — which is the exact failure this must prevent.
_CHAPTER_MARKERS = {"post", "chapter", "roundtable", "section", "branch", "affiliate"}

_US_STATES = {
    "alabama",
    "alaska",
    "arizona",
    "arkansas",
    "california",
    "colorado",
    "connecticut",
    "delaware",
    "florida",
    "georgia",
    "hawaii",
    "idaho",
    "illinois",
    "indiana",
    "iowa",
    "kansas",
    "kentucky",
    "louisiana",
    "maine",
    "maryland",
    "massachusetts",
    "michigan",
    "minnesota",
    "mississippi",
    "missouri",
    "montana",
    "nebraska",
    "nevada",
    "ohio",
    "oklahoma",
    "oregon",
    "pennsylvania",
    "tennessee",
    "texas",
    "utah",
    "vermont",
    "virginia",
    "washington",
    "wisconsin",
    "wyoming",
}

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES = re.compile(r"\s+")


def _fold(text: str) -> str:
    """Casefold, strip accents, drop punctuation, collapse whitespace."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return _SPACES.sub(" ", _PUNCT.sub(" ", stripped.casefold())).strip()


def normalize_org(name: str) -> str:
    """Normalised organization name for matching and for rejection lookups.

    Drops legal suffixes and body-type words, so "Georgia Association of
    Manufacturers" and "Georgia Manufacturers Association" agree.
    """
    tokens = [t for t in _fold(name).split() if t not in _LEGAL_SUFFIXES]
    meaningful = [t for t in tokens if t not in _BODY_WORDS]
    # Never normalise a name out of existence: if every token was a body word,
    # keep the original tokens rather than returning "".
    return " ".join(sorted(meaningful) if meaningful else sorted(tokens))


def chapter_qualifier(name: str) -> str:
    """The place that distinguishes one chapter of a national body from another.

    ``SAME Charleston Post``  -> ``charleston``
    ``HFMA Alabama Chapter``  -> ``alabama``
    ``Georgia HFMA``          -> ``georgia``
    ``SC Manufacturers Council`` and ``SC Manufacturers & Commerce`` both ->
    ``south carolina``, so they resolve to one organization.
    """
    tokens = _fold(name).split()
    found: set[str] = set()

    for i, token in enumerate(tokens):
        if token in _CHAPTER_MARKERS and i > 0:
            # Exactly the word before the marker. Taking two swept up the
            # national body's own name ("same charleston" instead of
            # "charleston"), which made every chapter look distinct from itself.
            preceding = tokens[i - 1]
            if preceding not in _BODY_WORDS:
                found.add(preceding)
        if token in _US_STATES:
            found.add(token)

    for first, second in (
        ("south", "carolina"),
        ("north", "carolina"),
        ("north", "dakota"),
        ("south", "dakota"),
        ("new", "york"),
        ("west", "virginia"),
        ("new", "jersey"),
        ("new", "mexico"),
        ("rhode", "island"),
    ):
        if first in tokens and second in tokens:
            found.update({first, second})

    return " ".join(sorted(found))


# Words that mark a legally distinct entity sharing its parent's domain and
# place. "Georgia Chamber of Commerce" and "Georgia Chamber of Commerce
# Foundation" are two bodies with two budgets and two programmes.
_DISTINCT_ENTITY_MARKERS = {"foundation", "fund", "pac", "institute", "trust"}


def entity_marker(name: str) -> str:
    """A distinct-entity word, when one name carries it and a sibling does not."""
    tokens = set(_fold(name).split())
    return " ".join(sorted(tokens & _DISTINCT_ENTITY_MARKERS))


def is_platform_host(url_or_host: str) -> bool:
    """True when the URL belongs to a platform rather than an organization."""
    return registrable_domain(url_or_host) in PLATFORM_HOSTS


def org_identity(name: str, source_url: str) -> str:
    """The natural key for an organization.

    Domain alone is wrong — nine SAME posts share ``same.org``, and unrelated
    bodies share ``glueup.com``. Name alone is wrong — names drift. Identity is
    the organization's own domain plus the chapter's place, falling back to the
    normalised name when the only URL is a platform's.
    """
    domain = registrable_domain(source_url)
    if domain in PLATFORM_HOSTS:
        domain = ""  # a platform URL says nothing about who the organization is
    base = domain or normalize_org(name)
    if not base:
        raise ValueError("cannot derive an organization identity from an empty name and url")
    discriminators = " ".join(x for x in (chapter_qualifier(name), entity_marker(name)) if x)
    return f"{base}|{discriminators}" if discriminators else base


# --- mechanisms ------------------------------------------------------------

# Named sub-bodies with their own governance keep their own record.
_NAMED_SUBBODY = re.compile(
    r"\b(council|committee|chapter|society|post|board|roundtable|task\s*force|"
    r"working\s*group|cohort|academy)\b",
    re.IGNORECASE,
)

# Generic program pages fold into the parent organization.
_GENERIC_PROGRAM = re.compile(
    r"^\s*(forums?|speakers?\s*bank|speakers?\s*bureau|events?|calendar|programs?|"
    r"membership|about|home\s*page|homepage|contact)\s*$",
    re.IGNORECASE,
)

_YEARS = re.compile(r"\b(19|20)\d{2}\b")
_ORDINALS = re.compile(r"\b\d+(st|nd|rd|th)\b", re.IGNORECASE)


def normalize_mechanism(text: str) -> str:
    """Normalised mechanism name.

    Years and ordinals are removed: "2nd Annual DeKalb Manufacturers Summit" and
    "3rd Annual DeKalb Manufacturers Summit" are the same recurring mechanism,
    and the occurrence key carries the date that separates instances.
    """
    without_years = _ORDINALS.sub(" ", _YEARS.sub(" ", text or ""))
    tokens = [t for t in _fold(without_years).split() if t not in _BODY_WORDS]
    return " ".join(tokens) if tokens else _fold(without_years)


def is_named_subbody(mechanism: str) -> bool:
    """True for Council/Committee/Chapter/Society/Post/Board — its own record."""
    return bool(_NAMED_SUBBODY.search(mechanism or ""))


def is_generic_program_page(mechanism: str) -> bool:
    """True for 'Forums', 'Speaker Bank' and similar — folds into the parent."""
    return bool(_GENERIC_PROGRAM.match(mechanism or ""))


# --- keys ------------------------------------------------------------------


def _hash(*parts: str) -> str:
    return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


def series_key(org_key: str, mechanism: str) -> str:
    """Identifies a recurring mechanism at an organization.

    A generic program page collapses onto the organization itself, so a
    "Forums" page and an "Events" page at one body do not become two routes.
    """
    if not org_key.strip():
        raise ValueError("series_key requires an organization identity")
    if is_generic_program_page(mechanism):
        return _hash(org_key.strip().casefold(), "")
    return _hash(org_key.strip().casefold(), normalize_mechanism(mechanism))


def occurrence_key(series: str, date: str | None) -> str:
    """Identifies one dated instance of a series."""
    if not series.strip():
        raise ValueError("occurrence_key requires a series_key")
    return _hash(series.strip(), (date or "recurring").strip())


# --- fuzzy fallback --------------------------------------------------------

SAME_ORG_THRESHOLD = 92


def _qualifiers_conflict(name_a: str, name_b: str) -> bool:
    """True only when both names carry a place AND the places differ.

    An *absent* qualifier is not evidence of a different chapter — it is usually
    an abbreviation. "Technology Association of Georgia" yields ``georgia``
    while "TAG Supply Chain Society" yields nothing, and they are one body. But
    ``charleston`` versus ``atlanta`` is two SAME posts, and ``georgia`` versus
    ``alabama`` is two HFMA chapters.

    org_identity() cannot express this — a single key is either equal or not —
    so the strict key groups the obvious cases and this tolerant comparison
    catches abbreviations during resolution.
    """
    qa, qb = chapter_qualifier(name_a), chapter_qualifier(name_b)
    return bool(qa) and bool(qb) and qa != qb


def same_organization(
    name_a: str, url_a: str, name_b: str, url_b: str, *, threshold: int = SAME_ORG_THRESHOLD
) -> bool:
    """Whether two rows name the same organization.

    The rule, in order:

    1. A **distinct-entity marker** on one side only ends it. A chamber and its
       foundation are two bodies.
    2. **Conflicting places** end it. Charleston and Atlanta are two SAME posts.
    3. Otherwise, **an organization's own shared domain is sufficient**. It is
       strong evidence and names are not: "Technology Association of Georgia"
       and "TAG Supply Chain, Logistics & Manufacturing Society" share almost no
       tokens and are plainly one body on ``tagonline.org``. Requiring name
       similarity here rejected that pair.
    4. With no usable domain — absent, or a platform both parties merely rent —
       fall back to the name, at a high bar. Two unrelated organizations both
       cite ``glueup.com`` in the real data.
    """
    if entity_marker(name_a) != entity_marker(name_b):
        return False
    if _qualifiers_conflict(name_a, name_b):
        return False

    domain_a, domain_b = registrable_domain(url_a), registrable_domain(url_b)
    own_domain = domain_a and domain_a == domain_b and domain_a not in PLATFORM_HOSTS
    if own_domain:
        return True

    return fuzz.token_set_ratio(normalize_org(name_a), normalize_org(name_b)) >= threshold
