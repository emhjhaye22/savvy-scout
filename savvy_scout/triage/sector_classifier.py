"""Buyer/notice -> sector classification, keyword-based (config_sector_keywords).
Heuristic pending a confirmed buyer list per sector (SPEC.md open question 2).
Shared by Gate 1 and the expiry radar so both agree on what counts as
"in scope sectors".

Two keyword categories, the same "generic needs coupling" pattern Gate 2
already uses (triage.gates.gate2_type_of_work):
- identity: a buyer/company name or specific-enough term (easyjet, national
  grid, ofgem) -- matches the sector on its own.
- generic_needs_coupling: bare industry vocabulary (energy, electricity,
  bank, payments, rail, airline...) that also matches unrelated non-IT work
  from the same buyer/industry (steel fittings, apprenticeship payments,
  academy-trust electricity contracts, grounds maintenance). Only counts as
  a confident sector match alongside a real capability or product-coupling
  term from config_coupling_terms. A generic keyword with no coupling
  evidence is not "no signal" (SPEC.md: never fail if unsure) -- it's an
  uncoupled candidate, surfaced separately so Gate 1 can FLAG it rather than
  FAIL it outright."""

import re
import sqlite3

# A few config_coupling_terms rows are themselves the same bare industry word
# as a generic_needs_coupling keyword (e.g. "energy" is listed as Energy's own
# sector coupling term, for Gate 2's looser purposes). Counting those here
# would make the coupling check circular -- the bare word "proving" its own
# match -- so they're excluded for sector classification specifically. Gate
# 2's own use of config_coupling_terms is unaffected.
_CIRCULAR_COUPLING_TERMS = {"energy", "airline", "rail", "transport", "fintech"}


def _haystack(buyer: str | None, text_blob: str) -> str:
    return f"{(buyer or '').lower()}\n{text_blob.lower()}"


def contains_keyword(haystack: str, keyword: str) -> bool:
    """Word-boundary match, not a bare substring 'in' check (2026-08-10
    fix): a short keyword can otherwise match inside an unrelated common
    word -- confirmed live, "lner" (an identity keyword for Rail and
    Transport, meant to catch the train operator LNER) was matching inside
    "vulnerable", routine language in council social-care tenders having
    nothing to do with trains. Same class of bug the "ai"/"maintenance"
    collision was already worked around for as a one-off (see
    _CIRCULAR_COUPLING_TERMS's sibling comment on bare "ai"); this fixes it
    generally, for every keyword, everywhere keywords are checked (see also
    triage.gates.gate2_type_of_work). haystack is assumed already
    lowercased; keyword is lowercased here for callers that haven't."""
    return re.search(r"\b" + re.escape(keyword.lower()) + r"\b", haystack) is not None


def _has_coupling(conn: sqlite3.Connection, sector: str, haystack: str) -> bool:
    rows = conn.execute(
        "SELECT term FROM config_coupling_terms WHERE kind = 'capability' OR sector = ?",
        (sector,),
    ).fetchall()
    return any(
        row["term"].lower() not in _CIRCULAR_COUPLING_TERMS and contains_keyword(haystack, row["term"])
        for row in rows
    )


def _has_exclusion(conn: sqlite3.Connection, sector: str, haystack: str) -> bool:
    rows = conn.execute(
        "SELECT term FROM config_exclusion_terms WHERE sector = ?", (sector,)
    ).fetchall()
    return any(contains_keyword(haystack, row["term"]) for row in rows)


class _Classification:
    __slots__ = ("matched", "uncoupled")

    def __init__(self, matched: set[str], uncoupled: set[str]):
        self.matched = matched
        self.uncoupled = uncoupled


def _classify(conn: sqlite3.Connection, buyer: str | None, text_blob: str) -> _Classification:
    haystack = _haystack(buyer, text_blob)
    rows = conn.execute("SELECT sector, keyword, category FROM config_sector_keywords").fetchall()

    identity_sectors: set[str] = set()
    generic_sectors: set[str] = set()
    for row in rows:
        if not contains_keyword(haystack, row["keyword"]):
            continue
        if row["category"] == "generic_needs_coupling":
            generic_sectors.add(row["sector"])
        else:
            identity_sectors.add(row["sector"])

    # An identity match is authoritative over a DIFFERENT sector's generic
    # match, even a coupled one (2026-08-10 finding): a council buyer
    # routinely procures things that mention "health" (social care, public
    # health), "payments" (council tax), "transport" or "energy" (leisure
    # centre heating, street lighting) as ordinary council business -- that
    # incidental text-based signal was being treated as equally strong
    # evidence as the buyer literally being named "X Council," contesting
    # an otherwise-clean identity match on the majority of real council
    # notices (confirmed live: 9/9 sampled genuine council buyers were
    # wrongly FLAGged this way). Two identity matches still genuinely
    # contest each other -- that case is real and stays flagged; only a
    # generic+coupling match from a sector with NO identity match of its
    # own is suppressed here.
    matched = set(identity_sectors)
    uncoupled: set[str] = set()
    for sector in generic_sectors - identity_sectors:
        if _has_coupling(conn, sector, haystack):
            if not identity_sectors:
                matched.add(sector)
        else:
            uncoupled.add(sector)

    # An exclusion term overrides any path to matching that sector (identity
    # or coupling), and also rules it out as an "uncoupled candidate" -- an
    # excluded sector isn't ambiguous, it's a confident non-match.
    matched = {s for s in matched if not _has_exclusion(conn, s, haystack)}
    uncoupled = {s for s in uncoupled if not _has_exclusion(conn, s, haystack)}

    return _Classification(matched=matched, uncoupled=uncoupled)


def classify_sector(conn: sqlite3.Connection, buyer: str | None, text_blob: str) -> str | None:
    """Returns the matched sector name, or None if no keyword matched, or
    only a generic keyword matched with no coupling evidence (ambiguous, see
    uncoupled_candidate_sectors), or more than one sector matched (contested,
    see is_contested)."""
    matched = _classify(conn, buyer, text_blob).matched
    if len(matched) == 1:
        return next(iter(matched))
    return None


def is_contested(conn: sqlite3.Connection, buyer: str | None, text_blob: str) -> bool:
    """True when more than one sector counts as a confident match (as
    opposed to zero)."""
    return len(_classify(conn, buyer, text_blob).matched) > 1


def classify_general_market_sector(conn: sqlite3.Connection, cpv_primary: str | None) -> str | None:
    """CPV-division fallback for a notice no Trifork sector keyword matched
    at all (2026-09-21). Only consulted by Gate 1 after classify_sector and
    uncoupled_candidate_sectors both come up empty -- never overrides a real
    keyword match, a contested match, or an uncoupled-candidate FLAG. Lets
    the general/admin market view show a real sector name (e.g.
    "Construction") instead of blank/"Unclassified" for the large share of
    swept notices outside Trifork's own 6 curated sectors."""
    if not cpv_primary or len(cpv_primary) < 2:
        return None
    row = conn.execute(
        "SELECT sector_name FROM config_cpv_division_sectors WHERE cpv_prefix = ? AND enabled = 1",
        (cpv_primary[:2],),
    ).fetchone()
    return row["sector_name"] if row else None


def uncoupled_candidate_sectors(conn: sqlite3.Connection, buyer: str | None, text_blob: str) -> set[str]:
    """Sectors with a bare industry-word mention but no coupling evidence and
    no identity match either. Used by Gate 1 to FLAG ("mentions the
    industry, unclear if it's IT work") instead of FAILing a notice that
    classify_sector couldn't confidently place."""
    classification = _classify(conn, buyer, text_blob)
    # If something else already matched confidently, this isn't the ambiguous
    # no-sector case Gate 1 needs -- classify_sector/is_contested already
    # cover it.
    if classification.matched:
        return set()
    return classification.uncoupled
