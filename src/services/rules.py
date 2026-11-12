"""Path rules: per-collection patterns naming files whose ordinary changes are expected
(add-path-rules, design D1/D2/D4).

A rule is a standing instruction NOT to raise an alarm, so its reach must be predictable from
reading it. The grammar is deliberately small and differs from the exclude globs (which match any
path *part*):

- a pattern with no ``/`` is a **file name**, matched against the last path segment in any folder;
- any other pattern is a path **anchored at the collection root**, matched segment by segment;
- a trailing ``/`` — or a final ``**`` segment — means every file beneath that folder, any depth;
- a non-final ``**`` segment matches zero or more whole folders;
- within a segment ``*`` matches any run of characters and ``?`` exactly one, never crossing ``/``;
- every other character is literal (no character classes, no escapes).

Matching is case-sensitive, full-match, against the collection-relative POSIX path exactly as it
is stored. The scanner is the only consumer of :meth:`RuleSet.matches`, and only in its
hash-compare branch (design D3): nothing here can remove a record, acknowledge an alert or resolve
an issue.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.db import Collection, Event, FileEntry

MAX_RULES = 100
MAX_PATTERN_BYTES = 512
EFFECT_EDITS = "edits"
ALARM_WINDOW_DAYS = 90

# The only whitespace stripped from around a submitted pattern: ASCII space and tab. Line breaks
# and other control characters are refused on the RAW input, before any stripping, so that a
# pattern pasted with a trailing newline is refused rather than silently altered.
_ASCII_WS = " \t"
# NUL and every character `str.splitlines` treats as a line boundary.
_FORBIDDEN = frozenset("\x00\n\r\x0b\x0c\x1c\x1d\x1e\x85  ")
_WILDCARDS = frozenset("*?/")

_MESSAGES = {
    "empty": "The pattern is empty",
    "absolute": "A pattern is relative to the collection folder and cannot begin with /",
    "segment": "A pattern cannot contain an empty folder name (//) or a . or .. folder",
    "control": "A pattern cannot contain a NUL character or a line break",
    "too_long": f"A pattern can be at most {MAX_PATTERN_BYTES} bytes long",
    "double_star": (
        "** may only be used as a whole folder in a path, as in docs/**/drafts, "
        "never inside a name such as **.md"
    ),
    "match_all": (
        "This pattern matches every file; to stop flagging changes across the whole collection, "
        "change the collection's policy instead"
    ),
    "duplicate": "The collection already has a rule with this pattern",
    "too_many": f"A collection can hold at most {MAX_RULES} rules",
}


class RuleError(ValueError):
    """A pattern or rule that cannot be stored. ``reason`` is a stable machine-readable key."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(_MESSAGES[reason])


def validate_pattern(pattern: str) -> str:
    """Return the pattern to store (surrounding spaces/tabs stripped) or raise RuleError."""
    if not isinstance(pattern, str):
        raise RuleError("empty")
    if any(ch in _FORBIDDEN for ch in pattern):
        raise RuleError("control")
    p = pattern.strip(_ASCII_WS)
    if not p:
        raise RuleError("empty")
    if len(p.encode("utf-8", "surrogatepass")) > MAX_PATTERN_BYTES:
        raise RuleError("too_long")
    if p.startswith("/"):
        raise RuleError("absolute")
    segments = _segments(p)
    if any(s in ("", ".", "..") for s in segments):
        raise RuleError("segment")
    if all(ch in _WILDCARDS for ch in p):
        raise RuleError("match_all")
    if "/" not in p:
        if "**" in p:
            raise RuleError("double_star")
    elif any("**" in s and s != "**" for s in segments):
        raise RuleError("double_star")
    return p


def _segments(p: str) -> list[str]:
    """Segments of a (non-absolute) pattern; one trailing ``/`` marks a folder, not a segment."""
    body = p[:-1] if p.endswith("/") else p
    return body.split("/")


# --- compilation ----------------------------------------------------------------------------
#
# Every expression emitted here is built so that matching cannot backtrack super-linearly: a
# stalled match would stall a scan and the panel. Two facts make that possible without changing
# what matches.
#
# 1. A segment pattern can only ever end at the next `/` or at the end of the path, so once it has
#    matched at a given start it has exactly one possible end. It is therefore wrapped in an atomic
#    group with a boundary lookahead: nothing outside can make it retry.
# 2. Inside a segment, `c0*c1*…*ck` (each ci a fixed-length run of literals and `?`) matches iff c0
#    is a prefix, ck is a suffix and the middle chunks occur in order in between; placing each
#    middle chunk at its LEFTMOST occurrence leaves the most room for the rest (the standard greedy
#    glob argument), so each is an atomic lazy search. The same argument applies one level up to
#    runs of whole segments between non-final `**` segments.
#
# Always used with `fullmatch`.

_FOLDER = "[^/]+/"
# A final `**` (or trailing `/`): one or more whole segments. `[^/]` also matches a line break.
_REMAINDER = "[^/]+(?:/[^/]+)*"


def _chunk_regex(chunk: str) -> str:
    return "".join("[^/]" if ch == "?" else re.escape(ch) for ch in chunk)


def _segment_regex(seg: str) -> str:
    """One concrete segment (no `/`, no `**`), atomic, ending at a segment boundary."""
    if seg == "*":
        return "[^/]+"
    chunks = seg.split("*")
    if len(chunks) == 1:
        body = _chunk_regex(chunks[0])
    else:
        middle = "".join(f"(?>[^/]*?{_chunk_regex(c)})" for c in chunks[1:-1])
        body = _chunk_regex(chunks[0]) + middle + "[^/]*" + _chunk_regex(chunks[-1])
    return f"(?>{body}(?=/|\\Z))"


def _run_regex(segs: list[str], trailing_slash: bool) -> str:
    joined = "/".join(_segment_regex(s) for s in segs)
    return joined + "/" if trailing_slash and segs else joined


def compile_pattern(pattern: str) -> re.Pattern[str]:
    """Compile a pattern (validated here) to one regular expression, for ``fullmatch``."""
    p = validate_pattern(pattern)
    if "/" not in p:
        # A file name, in any folder: every folder is consumed possessively, so the name can only
        # be tried against the last segment.
        return re.compile(f"(?>(?:{_FOLDER})*)" + _segment_regex(p))
    segments = _segments(p)
    if p.endswith("/"):
        # A trailing slash is the same as a final `**`: every file beneath, at any depth.
        segments.append("**")
    collapsed: list[str] = []
    for seg in segments:
        if seg == "**" and collapsed and collapsed[-1] == "**":
            continue
        collapsed.append(seg)
    final_star = collapsed[-1] == "**"
    body = collapsed[:-1] if final_star else collapsed
    # Split on the (now all non-final) `**` segments into runs of concrete segments.
    runs: list[list[str]] = [[]]
    for seg in body:
        if seg == "**":
            runs.append([])
        else:
            runs[-1].append(seg)
    parts: list[str] = []
    last = len(runs) - 1
    for i, run in enumerate(runs):
        if i == 0:
            # Anchored at the start. Followed by more (a `**` or the final remainder) → ends in `/`.
            parts.append(_run_regex(run, trailing_slash=(last > 0 or final_star)))
        elif i < last or final_star:
            # Between two `**`s: leftmost placement, never retried.
            parts.append(f"(?>(?:{_FOLDER})*?{_run_regex(run, trailing_slash=True)})")
        else:
            # The pattern's last element after a `**`: end-aligned. Linear, since all before it
            # is atomic and the run itself has a fixed number of segments.
            parts.append(f"(?:{_FOLDER})*" + _run_regex(run, trailing_slash=False))
    if final_star:
        parts.append(_REMAINDER)
    return re.compile("".join(parts))


class RuleSet:
    """The recognised rules of one collection, compiled once (the scanner builds one per pass)."""

    def __init__(self, patterns: list[str] | None = None) -> None:
        self.patterns: list[str] = []
        self._compiled: list[re.Pattern[str]] = []
        for p in patterns or []:
            self.patterns.append(p)
            self._compiled.append(compile_pattern(p))

    def matches(self, relpath: str) -> bool:
        return any(r.fullmatch(relpath) is not None for r in self._compiled)

    def __bool__(self) -> bool:
        return bool(self._compiled)

    def __len__(self) -> int:
        return len(self._compiled)


def _raw_entries(collection: Collection) -> list[Any]:
    try:
        data = json.loads(getattr(collection, "rules_json", None) or "[]")
    except (TypeError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _recognised_pattern(entry: Any) -> str | None:
    """The entry's pattern if it is a well-formed, recognised rule, else None (inert)."""
    if not isinstance(entry, dict) or entry.get("effect") != EFFECT_EDITS:
        return None
    p = entry.get("pattern")
    if not isinstance(p, str):
        return None
    try:
        # Strict: a stored pattern must already be in its stored form. One that only validates
        # after stripping was not written by `add_rule` and is treated as inert.
        return p if validate_pattern(p) == p else None
    except RuleError:
        return None


def load(collection: Collection) -> RuleSet:
    """The collection's recognised rules. Never raises; anything unrecognised is inert."""
    try:
        patterns: list[str] = []
        for entry in _raw_entries(collection):
            p = _recognised_pattern(entry)
            if p is not None and p not in patterns:
                patterns.append(p)
        return RuleSet(patterns)
    except Exception:  # pragma: no cover - defensive: a scan must never die on its rules
        return RuleSet()


def list_rules(collection: Collection) -> list[dict]:
    """Every stored entry in order, for the rules page (``recognised`` False = inert)."""
    out: list[dict] = []
    for entry in _raw_entries(collection):
        d = entry if isinstance(entry, dict) else {}
        pattern = d.get("pattern")
        effect = d.get("effect")
        created = d.get("created_at")
        out.append(
            {
                "pattern": pattern if isinstance(pattern, str) else "",
                "effect": effect if isinstance(effect, str) else "",
                "created_at": created if isinstance(created, str) else None,
                "recognised": _recognised_pattern(entry) is not None,
            }
        )
    return out


async def _fresh_entries(session: AsyncSession, collection: Collection) -> list[Any]:
    # Re-read the column so two quick edits from different sessions don't lose one another.
    await session.refresh(collection, attribute_names=["rules_json"])
    return _raw_entries(collection)


async def add_rule(session: AsyncSession, collection: Collection, pattern: str) -> str:
    """Validate and append one ``edits`` rule; commit; return the stored pattern."""
    p = validate_pattern(pattern)
    entries = await _fresh_entries(session, collection)
    if any(isinstance(e, dict) and e.get("pattern") == p for e in entries):
        raise RuleError("duplicate")
    if len(entries) >= MAX_RULES:
        raise RuleError("too_many")
    entries.append(
        {
            "pattern": p,
            "effect": EFFECT_EDITS,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    )
    collection.rules_json = json.dumps(entries)
    await session.commit()
    return p


async def remove_rule(session: AsyncSession, collection: Collection, pattern: str) -> bool:
    """Remove the entry whose pattern is exactly ``pattern``; False (no write) if there is none."""
    entries = await _fresh_entries(session, collection)
    kept = [e for e in entries if not (isinstance(e, dict) and e.get("pattern") == pattern)]
    if len(kept) == len(entries):
        return False
    collection.rules_json = json.dumps(kept)
    await session.commit()
    return True


async def reach(session: AsyncSession, collection: Collection, pattern: str) -> dict:
    """What a candidate pattern would cover, from the datastore only (design D4). Read-only."""
    return (await reach_many(session, collection, [pattern]))[pattern]


def _try_compile(pattern: str) -> re.Pattern[str] | None:
    try:
        return compile_pattern(pattern)
    except RuleError:
        return None


async def reach_many(
    session: AsyncSession, collection: Collection, patterns: list[str], sample: int = 0
) -> dict[str, dict]:
    """:func:`reach` for each pattern, in ONE pass over the files and ONE over the events.

    A pattern that does not compile gets zero counts (with the real ``total``) instead of raising.
    With ``sample`` > 0 each result also carries ``"sample"``: the first ``sample`` matching
    paths in path order — so the operator can see WHICH files a pattern reaches, not only how
    many — collected in the same pass.
    """
    if not patterns:
        return {}
    compiled: dict[str, re.Pattern[str] | None] = {}
    for p in patterns:
        if p not in compiled:
            compiled[p] = _try_compile(p)
    live = [(p, rx) for p, rx in compiled.items() if rx is not None]
    out: dict[str, dict] = {
        p: {"tracked": 0, "total": 0, "open_changed": 0, "alarms_90d": 0} for p in compiled
    }
    if sample > 0:
        for counts in out.values():
            counts["sample"] = []
    cid = collection.id

    total = 0
    rows = await session.execute(
        select(FileEntry.relpath, FileEntry.status)
        .where(FileEntry.collection_id == cid, FileEntry.status != "missing")
        .order_by(FileEntry.relpath)
    )
    for relpath, status in rows:
        total += 1
        for p, rx in live:
            if rx.fullmatch(relpath) is not None:
                out[p]["tracked"] += 1
                if sample > 0 and len(out[p]["sample"]) < sample:
                    out[p]["sample"].append(relpath)
                if status == "modified":
                    out[p]["open_changed"] += 1
    for counts in out.values():
        counts["total"] = total

    since = datetime.now(timezone.utc) - timedelta(days=ALARM_WINDOW_DAYS)
    events = await session.execute(
        select(Event.detail, FileEntry.relpath)
        .select_from(Event)
        .outerjoin(FileEntry, FileEntry.id == Event.file_id)
        .where(
            Event.collection_id == cid,
            Event.kind == "modified",
            Event.detected_at >= since,
        )
    )
    for detail, file_relpath in events:
        path = _alarm_path(detail, file_relpath)
        if path is None:
            continue
        for p, rx in live:
            if rx.fullmatch(path) is not None:
                out[p]["alarms_90d"] += 1
    return out


def _alarm_path(detail: str | None, file_relpath: str | None) -> str | None:
    """The path an ordinary `modified` event is about, or None if it is not counted.

    The path the event recorded when its detail is a JSON object carrying a string ``path``;
    otherwise the event's file's current path. An event written by the mass-change rule (its
    detail carries ``mass``) is not an ordinary alarm and is never counted.
    """
    parsed: Any = None
    if detail:
        try:
            parsed = json.loads(detail)
        except ValueError:
            parsed = None
    if isinstance(parsed, dict):
        if "mass" in parsed:
            return None
        path = parsed.get("path")
        if isinstance(path, str):
            return path
    elif file_relpath is None and detail:
        # The file's record is gone (its deletion was accepted): the accept kept the path it was
        # about as the event's plain-text detail, which is the only record of it left.
        return detail
    return file_relpath
