"""Unicode-aware normalization/similarity and the release-group ranking rule
(tasks/task-19.md's own "first album" definition, fixed here per its instruction not
to be reinterpreted).

Deliberately: casefold + NFKC only. No ASCII-encode step anywhere - that was Task 14's
transliteration bug (it silently broke every Cyrillic/Hebrew/Japanese match). Cyrillic
names must compare equal-to-themselves, not get mangled into an empty or wrong string.
"""
import unicodedata
import difflib

TITLE_THRESHOLD = 0.80
ARTIST_THRESHOLD = 0.80


def normalize(s: str) -> str:
    s = s or ""
    s = unicodedata.normalize("NFKC", s)
    s = s.casefold()
    s = " ".join(s.split())  # collapse whitespace, nothing else
    return s


def similarity(a: str, b: str) -> float:
    a, b = normalize(a), normalize(b)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def passes_gate(local_title, local_artist, candidate_title, candidate_artist):
    t = similarity(local_title, candidate_title)
    a = similarity(local_artist, candidate_artist)
    return (t >= TITLE_THRESHOLD and a >= ARTIST_THRESHOLD), t, a


def rank_of(release_group: dict) -> int:
    """1 = Album/no secondary type, 2 = Album/with secondary type, 3 = EP, 4 = Single,
    5 = anything else (Broadcast, Other, or no primary type) - lowest priority, only
    used if nothing in 1-4 exists."""
    primary = (release_group.get("primary-type") or "").strip()
    secondary = release_group.get("secondary-type-list") or []
    if primary == "Album" and not secondary:
        return 1
    if primary == "Album" and secondary:
        return 2
    if primary == "EP":
        return 3
    if primary == "Single":
        return 4
    return 5


def choose_release_group(release_groups: list):
    """release_groups: list of dicts with at least id, title, primary-type,
    secondary-type-list, first-release-date, and release_count (how many releases of
    this release-group were seen, for the tie-break).

    Returns (chosen_rg_or_None, rank_used, is_ambiguous, candidates_in_best_rank).
    """
    if not release_groups:
        return None, None, False, []

    by_rank = {}
    seen_ids = set()
    deduped = []
    for rg in release_groups:
        rgid = rg.get("id")
        if rgid and rgid in seen_ids:
            continue
        if rgid:
            seen_ids.add(rgid)
        deduped.append(rg)

    for rg in deduped:
        by_rank.setdefault(rank_of(rg), []).append(rg)

    for rank in (1, 2, 3, 4, 5):
        candidates = by_rank.get(rank)
        if not candidates:
            continue

        def sort_key(rg):
            date_str = rg.get("first-release-date") or "9999-99-99"
            # Pad partial dates (just a year, or year-month) so string sort is still
            # chronological: "1993" -> "1993-99-99" sorts after "1993-01-01" within the
            # same year, which is the conservative (least-presumptuous) direction.
            parts = date_str.split("-")
            while len(parts) < 3:
                parts.append("99")
            return "-".join(parts)

        earliest = min(sort_key(rg) for rg in candidates)
        earliest_group = [rg for rg in candidates if sort_key(rg) == earliest]

        if len(earliest_group) == 1:
            return earliest_group[0], rank, False, candidates

        max_releases = max(rg.get("release_count", 0) for rg in earliest_group)
        by_count = [rg for rg in earliest_group if rg.get("release_count", 0) == max_releases]

        if len(by_count) == 1:
            return by_count[0], rank, False, candidates

        # Still tied after both tie-breaks - genuinely ambiguous, don't guess.
        return None, rank, True, candidates

    return None, None, False, []
