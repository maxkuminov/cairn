"""Import a legacy photo-tripwire ``manifest.tsv`` as a pre-existing, UNSTAMPED baseline.

Cairn reaches parity with Max's bash tripwire (DESIGN.md §8) by loading its existing manifest
into the ``files`` table WITHOUT re-hashing 1.4 TiB and WITHOUT treating long-existing files as
brand-new. Every imported row is inserted as ``status='ok'`` with the manifest's known SHA-256 and
``ots_state='none'``, and NO ``added`` event is written. Because the scanner only stamps files it
classifies ``added`` (first-seen) or content-``modified``, an ``ok`` row with a known hash takes
the fast-path / metadata branch on the next scan and is never stamped. A file first-seen AFTER the
import has no imported row, so the scanner classifies it ``added`` and stamps it (perfile collection)
as usual. The whole "stamp new photos only" rule thus falls out of the data, not a code branch.

The on-disk column layout of the legacy manifest is not pinned in this repo, so the parser is
tolerant and auto-detecting (see :func:`parse_manifest`).
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.db import Collection, FileEntry
from . import scanner

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_FLOAT_RE = re.compile(r"^\d+\.\d+$")  # a decimal number, e.g. a high-precision epoch mtime

# A plausible Unix mtime (epoch seconds) window: ~2001-09-09 .. ~2033-05-18. An integer field
# inside this window is treated as the mtime; the remaining integer is the size in bytes. The
# window is narrow enough that a real photo's byte size (a few MiB) does not look like an mtime,
# yet wide enough to cover any timestamp the legacy tripwire could have written.
_MTIME_MIN = 1_000_000_000
_MTIME_MAX = 2_000_000_000


@dataclass
class ManifestRow:
    relpath: str
    sha256: str
    size: int | None = None
    mtime: float | None = None


@dataclass
class ImportResult:
    imported: int = 0
    updated: int = 0
    skipped: int = 0
    # (relpath, manifest_hash, actual_hash) for files whose re-hashed bytes differ (--rehash).
    mismatches: list[tuple[str, str, str]] = field(default_factory=list)
    # relpaths present in the manifest but absent on disk during --rehash.
    missing: list[str] = field(default_factory=list)


def parse_manifest(text: str) -> tuple[list[ManifestRow], int]:
    """Parse a tolerant TSV manifest into ``(rows, skipped_count)``.

    Per line: blank or ``#``-comment lines are ignored (not counted). Otherwise split on TAB; if
    that yields a single field, fall back to splitting on whitespace runs (handles ``sha256sum``'s
    ``<hash>  <path>``). Among the fields, the one matching ``^[0-9a-fA-F]{64}$`` is the SHA-256
    (lowercased); purely-integer fields are candidate size/mtime (the larger is size in bytes, an
    epoch-ish value is mtime — both optional); the remaining field is the relative path. A line
    with no valid SHA-256 OR no relpath is skipped and counted.
    """
    rows: list[ManifestRow] = []
    skipped = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = raw.split("\t")
        if len(fields) == 1:
            fields = raw.split()
        fields = [f.strip() for f in fields if f.strip()]

        sha: str | None = None
        ints: list[int] = []
        floats: list[float] = []
        rest: list[str] = []
        for f in fields:
            if sha is None and _SHA256_RE.match(f):
                sha = f.lower()
            elif _is_int(f):
                ints.append(int(f))
            elif _FLOAT_RE.match(f):
                # A decimal field (e.g. the legacy tripwire's high-precision epoch mtime). Captured
                # here so it is NOT mistaken for the relpath by the longest-field rule below.
                floats.append(float(f))
            else:
                rest.append(f)

        if sha is None or not rest:
            skipped += 1
            continue

        # The relpath is the remaining (longest) non-hash, non-numeric field.
        relpath = max(rest, key=len)
        size, mtime = _classify_numeric(ints, floats)
        rows.append(ManifestRow(relpath=relpath, sha256=sha, size=size, mtime=mtime))
    return rows, skipped


def _is_int(value: str) -> bool:
    return bool(value) and (value[1:] if value[0] in "+-" else value).isdigit()


def _jailed_relpath(relpath: str, root: Path) -> str | None:
    """Return ``relpath`` if it stays under ``root``, else ``None``.

    The manifest is untrusted input: a crafted relpath like ``../../etc/passwd`` or
    ``/etc/shadow`` would otherwise be persisted verbatim and resolve outside the read-only
    collection jail (``--rehash`` would read it, stamp/export would copy it out). Reject any absolute
    path or one with a ``..`` component, then confirm ``root/relpath`` resolves under ``root``.
    """
    rel = relpath.lstrip("/")  # drop a leading '/' so '/etc/shadow' can't be an absolute escape
    if not rel or PurePosixPath(rel).is_absolute() or ".." in PurePosixPath(rel).parts:
        return None
    try:
        if not (root / rel).resolve().is_relative_to(root.resolve()):
            return None
    except (OSError, ValueError):
        return None
    return rel


def _classify_numeric(ints: list[int], floats: list[float]) -> tuple[int | None, float | None]:
    """Map the numeric fields to (size, mtime).

    The legacy manifest layout is ``relpath <TAB> size <TAB> mtime <TAB> sha256`` where size is a
    plain integer and mtime a high-precision epoch float. mtime is therefore preferentially the
    epoch-window float; failing that, an epoch-window integer (a manifest with an integer mtime).
    size is an integer not consumed as mtime (preferring a non-epoch integer). Each is optional —
    a sparser layout (e.g. ``relpath <TAB> size <TAB> sha256``) just leaves mtime ``None`` for the
    first scan to backfill.
    """
    mtime: float | None = None
    epoch_floats = [f for f in floats if _MTIME_MIN <= f <= _MTIME_MAX]
    if epoch_floats:
        mtime = epoch_floats[0]
    else:
        epoch_ints = [n for n in ints if _MTIME_MIN <= n <= _MTIME_MAX]
        if epoch_ints:
            mtime = float(epoch_ints[0])
            ints = [n for n in ints if n != epoch_ints[0]]  # consume it so it is not also the size

    non_epoch_ints = [n for n in ints if not (_MTIME_MIN <= n <= _MTIME_MAX)]
    if non_epoch_ints:
        size = max(non_epoch_ints)
    elif ints:
        size = max(ints)
    else:
        size = None
    return size, mtime


async def import_manifest(
    session: AsyncSession,
    collection: Collection,
    path: str | Path,
    *,
    rehash: bool = False,
) -> ImportResult:
    """Import a manifest into ``collection`` as a pre-existing, unstamped baseline.

    Upserts one ``files`` row per parsed entry by ``(collection_id, relpath)``: a new row is INSERTed
    (or an existing one UPDATEd in place — refresh sha256/size/mtime, leave ``status='ok'``). No
    ``added`` events are written. With ``rehash=True`` each file under ``collection.root/relpath`` is
    streamed through SHA-256 (off the event loop) and any mismatch with the manifest is recorded;
    a missing file is recorded too. Re-hash never aborts the import and never changes the no-stamp
    behavior. Commits and returns the counts.
    """
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    rows, skipped = parse_manifest(text)
    result = ImportResult(skipped=skipped)

    existing: dict[str, FileEntry] = {
        f.relpath: f
        for f in await session.scalars(
            select(FileEntry).where(FileEntry.collection_id == collection.id)
        )
    }
    root = Path(collection.root)
    now = scanner._utcnow()

    for row in rows:
        # The manifest is untrusted: reject any relpath that escapes the collection jail BEFORE it is
        # read (--rehash) or persisted (it would later be stamped/exported). A rejected row is
        # counted as skipped, never tracked.
        relpath = _jailed_relpath(row.relpath, root)
        if relpath is None:
            result.skipped += 1
            continue

        sha = row.sha256
        if rehash:
            full = root / relpath
            if not full.is_file():
                result.missing.append(relpath)
            else:
                actual = await asyncio.to_thread(scanner.sha256_file, full)
                if actual != row.sha256:
                    # The on-disk bytes already differ from the manifest hash. Do NOT seed an
                    # 'ok' row with the (now-wrong) manifest hash — the next scan's size+mtime
                    # fast-path would then trust it forever. Skip the row so the scanner sees the
                    # file as 'added' and classifies/stamps it afresh.
                    result.mismatches.append((relpath, row.sha256, actual))
                    result.skipped += 1
                    continue

        entry = existing.get(relpath)
        if entry is None:
            session.add(
                FileEntry(
                    collection_id=collection.id,
                    relpath=relpath,
                    size=row.size if row.size is not None else 0,
                    mtime=row.mtime,
                    sha256=sha,
                    status="ok",
                    first_seen=now,
                    last_checked=now,
                    ots_state="none",
                )
            )
            result.imported += 1
        else:
            entry.sha256 = sha
            if row.size is not None:
                entry.size = row.size
            if row.mtime is not None:
                entry.mtime = row.mtime
            entry.status = "ok"
            entry.last_checked = now
            result.updated += 1

    await session.commit()
    return result
