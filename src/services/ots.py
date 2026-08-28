"""Thin subprocess wrappers around the maintained ``ots`` CLI (DESIGN.md §5/§6).

Subprocessing decouples Cairn from the OpenTimestamps library's API churn. Every CLI call goes
through :func:`_run_ots`, which captures stdout+stderr and applies a timeout. A "pending" proof
(``ots`` exits non-zero with "Pending confirmation") is a *normal* lifecycle state, not an error,
so it is never raised; genuine failures (calendar unreachable, malformed ``.ots``) raise
:class:`OtsError`.

The state machine (see DESIGN.md §6):

    none  ->  incomplete  ->  complete
            (after stamp)   (after upgrade, once Bitcoin confirms)

``info`` classifies an existing ``.ots`` OFFLINE by attestation type; ``verify``/``upgrade`` hit
the network.

**Proof-path failure classification — the governing rule.** A *permanent* failure
(:class:`OtsPathError`) drops a file out of the stamp queue for good (the caller sets
``ots_state='none'``), so a wrong "permanent" is a silent loss of notarization, while a wrong
"transient" costs only a retry. Therefore:

    Only a failure on the FINAL proof output path may be classified permanent
    (:class:`OtsPathError`). Every staging-side failure is transient (:class:`OtsError`).
    The pre-check measures only the path components Cairn itself creates below the proof-store
    root — never the store root's own components.

Concretely: the staging dir, its symlinks and their cleanup can only ever raise/suppress transient
errors (an overlong *staging* pathname is a property of the deployment, not of the file), and
:func:`_place_proof` on the final output path is the one place a permanent verdict is reached at
runtime.
"""

from __future__ import annotations

import binascii
import contextlib
import datetime
import errno
import fcntl
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("cairn.ots")

# Default per-call timeout (seconds) for the ``ots`` CLI.
DEFAULT_TIMEOUT = 60

# ext4/xfs and most Linux filesystems cap a single path COMPONENT at 255 *bytes* (NAME_MAX), not
# characters. A multi-byte name (e.g. Cyrillic, 2 bytes/char in UTF-8) can blow past that while
# looking short — and a proof name is the file's own name plus ``.ots``, so an already-long name
# tips over. ``os.replace`` onto such a path then raises ``OSError`` (ENAMETOOLONG, errno 36), which
# must be skipped-and-counted, never allowed to abort a whole stamp batch.
_NAME_MAX_BYTES = 255

# Default block-explorer base. esplora-compatible (blockstream.info / mempool.space): the
# REST routes ``/api/block-height/<n>`` and ``/api/block/<hash>`` give the canonical block hash
# at a height and that block's header (merkle root + time) — enough to verify an OTS attestation
# without running a Bitcoin node.
DEFAULT_EXPLORER_URL = "https://blockstream.info"

# A Bitcoin block time can only fall between the genesis block (2009-01-03) and, generously, the
# year 2100. A value outside that is a malformed explorer response, not a block: it is rejected at
# the fetch boundary so it can never reach the attestation comparison (which would read as a proof
# mismatch) or be formatted into an "existed by" date the operator is invited to rely on.
_MIN_BLOCK_TIME = 1231006505
_MAX_BLOCK_TIME = 4102444800


def _ots_bin() -> str:
    """Resolve the ``ots`` executable.

    ``opentimestamps-client`` installs ``ots`` into the same bin dir as the running interpreter,
    so prefer the one next to ``sys.executable`` (works when ``cairn`` is launched by absolute
    path from a venv without activation). Then fall back to ``PATH``; an explicit
    ``CAIRN_OTS_BIN`` overrides everything.
    """
    override = os.environ.get("CAIRN_OTS_BIN")
    if override:
        return override
    candidate = Path(sys.executable).parent / "ots"
    if candidate.exists():
        return str(candidate)
    return shutil.which("ots") or "ots"

# Phrases the CLI prints for a still-pending (not-yet-confirmed) proof. Their presence on a
# non-zero exit means "incomplete", which is a valid state — not an error.
_PENDING_MARKERS = ("pending confirmation", "pending attestation", "not complete")

# ``ots info`` lines (offline). Calendars listed via PendingAttestation('<url>'); a confirmed
# proof carries a BitcoinBlockHeaderAttestation(<height>) line instead/as well.
_PENDING_ATTESTATION_RE = re.compile(r"PendingAttestation\(['\"]([^'\"]+)['\"]\)")
_BITCOIN_ATTESTATION_RE = re.compile(r"BitcoinBlockHeaderAttestation\((\d+)\)")

# ``ots verify`` success line, e.g. "Success! Bitcoin block 358391 attests existence as of
# 2015-05-28 CEST". Emitted via logging to stderr.
_VERIFY_SUCCESS_RE = re.compile(
    r"Bitcoin block (\d+) attests existence as of (.+?)\s*$",
    re.MULTILINE,
)


class OtsError(Exception):
    """A genuine failure of the ``ots`` CLI (not a normal pending state)."""


class LockContended(OtsError):
    """The collection's proof-store lock is held by someone else right now.

    A distinct class because two callers act on it in OPPOSITE directions and must not confuse it
    with a real failure: a placer treats it as transient (wait for the next pass), while claim
    reclamation treats it as PROOF OF LIFE — a held lock means the claim's holder is alive inside a
    proof critical section, so the claim must not be reclaimed out from under it (design D1). Every
    other lock failure stays a plain :class:`OtsError`.
    """


class OtsPathError(OtsError):
    """The FINAL proof output path cannot be written — a component Cairn creates below the proof
    store exceeds the filesystem's per-name byte limit (ENAMETOOLONG), or the destination is
    otherwise permanently un-writable.

    Distinct from a transient failure (an unreachable calendar, a timeout, anything staging-side):
    a final path a filesystem refuses will never succeed on retry, so callers skip-and-count the one
    file instead of leaving it ``pending`` to re-attempt — and re-flood — on every subsequent scan.
    Raised ONLY by the output-path pre-check and by :func:`_place_proof`; see the module docstring.
    """


@dataclass
class ProofInfo:
    """Offline classification of an ``.ots`` file (no network)."""

    state: str  # 'none' | 'incomplete' | 'complete'
    calendars: list[str] = field(default_factory=list)
    block_height: int | None = None


@dataclass
class VerifyResult:
    """Outcome of verifying a stored proof against a digest."""

    verified: bool
    state: str  # 'none' | 'incomplete' | 'complete'
    block_height: int | None = None
    block_hash: str | None = None  # not populated yet (node-backend refinement)
    existed_by: str | None = None  # "existed by" UTC/local date string from the CLI
    calendars: list[str] = field(default_factory=list)
    message: str = ""
    # Why verification did not succeed. Reason and blame are separate signals, so they are
    # separate fields with separate copy (design D1/D2). Defaults keep every existing
    # construction site valid.
    #
    # `digest_mismatch` is the transport of a *neutral* finding: the live digest and the digest the
    # proof commits to DISAGREE. Parsing the `.ots` establishes the disagreement, never which of the
    # two artifacts moved — a flipped byte inside a structurally valid serialized `file_digest`
    # produces exactly the same signal as a modified file. Blame is assigned by the callers, which
    # hold the third data point this module does not: the file's recorded baseline digest
    # (`files.sha256`). See design D1.
    digest_mismatch: bool = False  # live digest and the proof's committed digest DISAGREE (explorer)
    proof_mismatch: bool = False  # attestation commitment != block merkle root      (explorer)
    transport_error: str | None = None  # backend unreachable; nothing was established
    # How many lookups produced `transport_error`, carried structurally rather than recovered by
    # splitting the human-readable text (one error may itself contain the "; " join separator).
    transport_failures: int = 0
    inconclusive: bool = False  # this backend cannot tell pending/mismatch/unreachable apart
    # The `.ots` could not be parsed at all: nothing — about the file OR the proof's content — was
    # established. Distinct from every "not verified" outcome, which all presuppose a readable proof.
    unreadable_proof: bool = False
    # The digest the parsed proof commits to, when the proof was parsed. Set ONLY by the explorer
    # backend, which is the only backend that establishes a digest disagreement at all (the node
    # backend has no mismatch site — sprint-1 D1). It exists to make verify's blame attribution
    # PROVABLE: with `files.ots_digest` recorded, "the .ots at this path is not the proof Cairn
    # placed" and "the proof Cairn placed predates this version" stop being indistinguishable
    # (design D7). It is displayed nowhere.
    proof_digest: str | None = None


def failed_lookup_count(result: VerifyResult | None) -> int:
    """How many attestation lookups failed on ``result`` (0 when there was no transport failure).

    Read from the structural ``transport_failures`` counter, never recovered by splitting the joined
    ``transport_error`` text: a single human-readable error may itself contain the ``"; "`` the join
    uses (``HTTP Error 503: retry later; overloaded``), and reporting one failed lookup as two
    overstates how much of a proof went unchecked. Both consumers of :class:`VerifyResult` (the panel
    card and ``cairn verify``) disclose this count under a verdict that outranks the transport
    failure, so the accessor lives here rather than in each of them.

    A result carrying a reason but no count (a hand-built fallback at a call site) reports one
    failure — the reason itself. Never zero, which would silently drop the disclosure.
    """
    if result is None:
        return 0
    if result.transport_failures:
        return result.transport_failures
    return 1 if result.transport_error else 0


def _run_ots(args: list[str], timeout: int = DEFAULT_TIMEOUT) -> tuple[int, str, str]:
    """Run ``ots <args>``; return ``(returncode, stdout, stderr)``.

    Captures both streams. Raises :class:`OtsError` only if the binary is missing or times out;
    a non-zero exit is returned as-is so callers can distinguish a pending proof from a failure.
    """
    try:
        proc = subprocess.run(
            [_ots_bin(), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:  # pragma: no cover - environment guard
        raise OtsError("the 'ots' CLI is not installed or not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise OtsError(f"ots {args[0] if args else ''} timed out after {timeout}s") from exc
    except OSError as exc:  # pragma: no cover - environment guard (EACCES, ENOEXEC, EMFILE...)
        # Every other process-start failure is normalised to the module's own error type, so the
        # callers' `except OtsError` transport boundaries actually cover it instead of letting a
        # raw OSError escape into a caller that has no catch at all (`cairn verify`).
        raise OtsError(f"could not run the 'ots' CLI: {exc}") from exc
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _is_pending(text: str) -> bool:
    low = text.lower()
    return any(marker in low for marker in _PENDING_MARKERS)


def _proof_output_writable(out_ots_path: Path, *, below: Path | None = None) -> bool:
    """Whether the components Cairn CREATES for ``out_ots_path`` fit the per-name byte limit.

    The *byte* length is what matters (NAME_MAX is bytes, not characters): a short-looking multi-byte
    name — a Cyrillic filename plus its extension plus ``.ots`` — can still exceed it. Checking only
    the final component is not enough: the proof path mirrors the file's *relpath*, so an overlong
    **directory** component (a deep Cyrillic folder name) is equally un-writable — ``mkdir`` refuses
    it with ENAMETOOLONG — and would otherwise slip past this pre-check and burn a batch calendar
    round-trip plus a single-file retry before :func:`_place_proof` classified it.

    ``below`` is the proof-store root, the boundary between "path Cairn is given" and "path Cairn
    creates". Only ``out_ots_path`` relative to it — ``<collection_id>/<relpath>.ots`` — is measured.
    The store root's OWN components are never validated: it already exists on a filesystem that
    accepted it, and applying our hard 255-byte assumption to it would declare every descendant proof
    permanently unwritable and silently drop a whole collection to ``ots_state='none'``. Without
    ``below`` (or if the path is not under it) only the final ``.ots`` name — the one component
    unambiguously ours — is measured; :func:`_place_proof` still catches the rest at runtime.

    This is a cheap pre-check so an un-writable proof is skipped before a symlink or a calendar
    round-trip is spent on it; :func:`_place_proof` remains the authoritative backstop for any limit
    this pre-check does not model (a smaller NAME_MAX, name-inflating filesystems like eCryptfs).
    """
    try:
        parts: tuple[str, ...] = (out_ots_path.name,)
        if below is not None:
            with contextlib.suppress(ValueError):
                parts = out_ots_path.relative_to(below).parts
        return all(len(os.fsencode(part)) <= _NAME_MAX_BYTES for part in parts)
    except (ValueError, TypeError):  # pragma: no cover - defensive
        return False


def _prepare_staging_dir(staging_dir: Path) -> None:
    """Create the shared staging dir, mapping any failure to a **transient** :class:`OtsError`.

    The staging dir is shared by every file in the store, so a failure here says nothing about any
    individual file's path: classifying it permanent would drop the whole pending set to ``none``
    over an un-writable (but fixable) proof volume. Transient ⇒ everything stays ``pending``.
    """
    try:
        staging_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OtsError(f"cannot prepare the stamp staging dir {str(staging_dir)!r}: {exc}") from exc


# --- Superseded-proof archive (design D1) -----------------------------------------------------

# Archive root under the proof store. Cannot shadow a collection directory (those are integers),
# exactly as ``.staging`` cannot.
SUPERSEDED_DIRNAME = ".superseded"

# A directory `fsync` that fails with EXACTLY one of these is the filesystem saying it cannot make
# directory entries durable (SMB/CIFS, FUSE, FAT-derived stores accept create/rename/file-`fsync`
# and reject this). That is DETERMINISTIC, not transient: it answers the same on every retry, so
# classifying it transient would wedge the notary forever and grow the archive family on every
# doomed retry. Every OTHER errno (EIO, ENOSPC, EACCES, ...) stays a real, transient failure.
_DIR_SYNC_UNSUPPORTED = frozenset(
    {errno.EINVAL, errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)}
)

# Proof stores (by root path) whose filesystem cannot flush directory entries. Detected LAZILY AND
# IN-BAND on the first directory sync actually attempted for that store — no probe, no startup
# check, no schema, no setting. Process memory only.
_BEST_EFFORT_DIR_SYNC: set[str] = set()


@dataclass(frozen=True)
class StoredProofFacts:
    """What one OFFLINE parse of a stored ``.ots`` establishes: its digest and its anchor syntax.

    ``anchored`` means the proof CARRIES a ``BitcoinBlockHeaderAttestation`` — it does **not** mean
    the anchor was checked against the chain. This module never touches the network for placement,
    so a caller that needs "is this anchor real?" must ask :func:`verify` and pass the answer in.
    """

    readable: bool
    digest: str | None = None  # lower-case hex of the file digest the proof commits to
    anchored: bool = False


@dataclass(frozen=True)
class StampOutcome:
    """What :func:`_place_proof` did, so the caller knows what it may record (design D1).

    * ``placed``   — the freshly staged proof is at the canonical path; record it with ``now``.
    * ``kept``     — an existing, caller-CONFIRMED anchored proof was kept; record ``complete``
      and leave ``ots_stamped_at`` alone (its attestation carries the real date).
    * ``deferred`` — nothing was decided (a same-digest anchored proof that nothing confirmed or
      disproved). Both artifacts survive, the caller records NOTHING and the row stays ``pending``.
      A deferral is an outcome, never a failure: it does not raise.
    """

    kind: str  # 'placed' | 'kept' | 'deferred'
    digest: str | None = None
    state: str | None = None  # 'incomplete' | 'complete' | None


def read_proof_facts(ots_path: str | os.PathLike[str]) -> StoredProofFacts:
    """Parse a stored ``.ots`` OFFLINE with the OpenTimestamps library: digest + anchor, one read.

    Deliberately not ``ots info``: that costs a process spawn per occupied proof path. Uses the same
    lazy import and the same deserialization as :func:`_verify_via_explorer`, so this module stays
    importable without the library present.

    NEVER raises. An absent, truncated, malformed or non-timestamp file is ``readable=False`` —
    "nothing was established", which every caller must treat as "preserve it, do not reason about
    it". Deleting a proof this build cannot parse is precisely the failure #15 exists to prevent.
    """
    try:
        from opentimestamps.core.notary import BitcoinBlockHeaderAttestation
        from opentimestamps.core.serialize import StreamDeserializationContext
        from opentimestamps.core.timestamp import DetachedTimestampFile

        with Path(ots_path).open("rb") as fh:
            detached = DetachedTimestampFile.deserialize(StreamDeserializationContext(fh))
        anchored = any(
            isinstance(att, BitcoinBlockHeaderAttestation)
            for _msg, att in detached.timestamp.all_attestations()
        )
        return StoredProofFacts(
            readable=True, digest=detached.file_digest.hex().lower(), anchored=anchored
        )
    except Exception:  # unreadable / absent / malformed / library missing
        return StoredProofFacts(readable=False)


def _fsync_dir(path: Path, *, store_key: str) -> None:
    """Flush a directory's ENTRIES so a name created in it survives a power cut.

    A file ``fsync`` commits the file's BYTES and says nothing about the directory entry that names
    them; the entry and the source's ``unlink`` are independent metadata operations a filesystem may
    persist in either order. Without this, a crash can persist the unlink of the canonical proof
    while the archive's new name never lands — both names gone, the only proof destroyed. That is
    the one failure this whole design exists to prevent, so preservation syncs directories too, and
    syncs them BEFORE the source is removed.

    **Unsupported-operation degrade (accepted limitation).** Some writable stores (CIFS/SMB, FUSE,
    FAT-derived) accept ``open``/``write``/``rename``/file-``fsync`` and refuse a directory
    ``fsync`` with ``EINVAL``/``ENOTSUP``/``EOPNOTSUPP``. That is deterministic — it will answer the
    same forever — so treating it as transient would refuse every occupied-path placement, wedge
    the collection's stamping permanently, and pile up one more suffixed archive slot per doomed
    retry: a durability nicety costing the notary its ability to notarize. On the FIRST such result
    per proof store we log one WARNING and record the store as best-effort; thereafter every
    directory sync for that store returns immediately, without error and without repeating the
    warning. Only *name* durability degrades — the proof's bytes are still flushed, the ordering of
    the sequence is unchanged, and the source is still unlinked last.

    Every other errno propagates, keeping the caller's transient classification: the placement is
    refused, the member stays ``pending``, and the source is never unlinked. The three errnos are
    enumerated exactly so a genuine I/O error can never be laundered into "best effort".
    """
    if store_key in _BEST_EFFORT_DIR_SYNC:
        return
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    except OSError as exc:
        if exc.errno in _DIR_SYNC_UNSUPPORTED:
            _BEST_EFFORT_DIR_SYNC.add(store_key)
            log.warning(
                "proof store %s: the filesystem does not support flushing directory entries (%s), "
                "so a power loss in the instant between archiving a proof and placing the new one "
                "could lose the newest archive entry's name; canonical proofs and all previously "
                "synced entries are unaffected. Proceeding with best-effort durability.",
                store_key,
                exc.strerror or exc,
            )
            return
        raise
    finally:
        os.close(fd)


def _dir_chain(target: Path, root: Path | None) -> list[Path]:
    """``target`` then each ancestor up to and including ``root``, deepest-first.

    Durability of a NAME belongs to the directory that holds it, so making a freshly created path
    durable means flushing every directory on the chain — not just the deepest one. The chain is
    computed from the path itself rather than from "what this call happened to create", because a
    directory created by an EARLIER, FAILED attempt is indistinguishable from a durable one: a retry
    would see it as pre-existing, flush only the deepest parent, unlink the source and commit — and a
    power loss could still lose the ancestor's name (Codex B2). Anchoring on the proof-store root
    instead is both simpler and immune to that residue: the store root's own name predates every
    proof (it is created by ``cairn init``), so everything above it is already durable.

    ``root`` that is not an ancestor of ``target`` (defensive; not reachable from ``_place_proof``)
    yields the target alone rather than climbing to the filesystem root.
    """
    chain = [target]
    if root is None:
        return chain
    root_key = os.path.abspath(root)
    if os.path.abspath(target) == root_key:
        return chain
    for ancestor in target.parents:
        chain.append(ancestor)
        if os.path.abspath(ancestor) == root_key:
            return chain
    return [target]


def _sync_dir_chain(target: Path, root: Path | None, *, store_key: str) -> None:
    """Flush ``target``'s entries and those of every ancestor up to ``root``, deepest-first.

    Order is load-bearing (parent-after-child): a directory's ``fsync`` makes durable the entries IT
    holds, so it is flushed only once the child entry exists. Bounded by the depth of the proof
    store (a handful of descriptors), and every flush goes through :func:`_fsync_dir`, so the
    unsupported-operation degrade applies per directory exactly as specified.
    """
    for directory in _dir_chain(target, root):
        _fsync_dir(directory, store_key=store_key)


def superseded_root(store_root: str | os.PathLike[str], collection_id: str | int) -> Path:
    """``<proof_store>/.superseded/<collection_id>`` — the archive subtree for one collection.

    Kept per-collection even though a digest alone would be unique, so a collection's proofs move
    or back up as one subtree and a future prune has an obvious counterpart.
    """
    return Path(store_root) / SUPERSEDED_DIRNAME / str(collection_id)


def _archive_dir_for(archive_root: Path, facts: StoredProofFacts) -> tuple[Path, str]:
    """Return ``(directory, basename stem)`` for archiving a proof with ``facts``.

    Content-addressed: a ``.ots`` attests BYTES, not a path, so the digest selects the archive
    FAMILY — ``<digest[:2]>/<digest>``. An unreadable proof has no digest to address it by, so it
    goes to ``unknown/<uuid>``. Both stems are FIXED LENGTH, so no watched filename can influence
    the archive path's length: the archive can never be the thing that trips ENAMETOOLONG, and the
    output-path pre-check keeps applying only to the canonical path (design D1).
    """
    if facts.readable and facts.digest:
        return archive_root / facts.digest[:2], facts.digest
    return archive_root / "unknown", uuid.uuid4().hex


def _write_all(fd: int, payload: bytes) -> None:
    """Write EVERY byte of ``payload`` to ``fd``, or raise ``OSError``.

    ``os.write`` is allowed to write fewer bytes than it was handed — a signal, a full disk, a
    network filesystem's own buffering — and returns how many it took. A single unchecked call can
    therefore archive a PREFIX of a proof, which is then fsynced, named, and followed by the unlink
    of the intact original: the only copy of the evidence silently replaced by a truncated one
    (Codex B1). So the write loops until the payload is exhausted, and a call that makes no progress
    is a failure, not a retry forever — it raises, which ``_place_proof`` classifies transient, so
    the source is never unlinked and the file stays queued.
    """
    view = memoryview(payload)
    written = 0
    while written < len(view):
        n = os.write(fd, view[written:])
        if n <= 0:
            raise OSError(
                errno.EIO,
                f"archive write made no progress after {written} of {len(payload)} bytes",
            )
        written += n
    if written != len(payload):  # pragma: no cover - defensive
        raise OSError(
            errno.EIO, f"archive write covered {written} of {len(payload)} bytes"
        )


def _next_archive_index(directory: Path, stem: str) -> int:
    """First index past every ``<stem>[.N].ots`` already in ``directory`` (one directory read).

    Only a SEED for the exclusive-create loop, never the decision: the ``O_EXCL`` claim is what makes
    the choice race-safe. Reading the family once means a large family costs one ``scandir`` instead
    of one failed ``open`` per occupied slot.
    """
    highest = 0
    prefix = f"{stem}."
    with os.scandir(directory) as entries:
        for entry in entries:
            name = entry.name
            if not name.startswith(prefix) or not name.endswith(".ots"):
                continue
            middle = name[len(prefix) : -len(".ots")]
            if middle.isascii() and middle.isdigit():
                highest = max(highest, int(middle))
    return highest + 1


def _claim_archive_slot(directory: Path, stem: str) -> tuple[int, Path]:
    """Exclusively create the first free ``<stem>[.N].ots`` in ``directory``; return ``(fd, path)``.

    There is deliberately **no ceiling**. A fixed bound (this used to refuse past 10,000) turns a
    long-lived deferral loop or a prepopulated store into a proof store that can never preserve
    anything again — every later placement refuses, permanently, on a path whose whole purpose is to
    never lose a proof (Codex M1). The search is still cheap: the common case is one ``open``, and
    only a collision pays for one directory read to skip past the whole existing family.
    """
    index = 0
    seeded = False
    while True:
        candidate = directory / (f"{stem}.ots" if index == 0 else f"{stem}.{index}.ots")
        try:
            fd = os.open(str(candidate), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            if seeded:
                index += 1
            else:
                index = max(index + 1, _next_archive_index(directory, stem))
                seeded = True
            continue
        return fd, candidate


def _archive_copy(
    source: Path,
    archive_root: Path,
    facts: StoredProofFacts,
    *,
    store_key: str,
    sync_root: Path | None = None,
) -> Path:
    """COPY ``source`` into the content-addressed archive durably; leave ``source`` in place.

    The whole of :func:`_preserve_proof` except its final ``unlink`` — split out because the
    relocation sequence (design D4 phase 5) needs the archive copy to exist *before* it removes a
    source that is already published elsewhere, and needs to keep deciding for itself when (and
    whether) the removal happens. Every durability property documented on :func:`_preserve_proof`
    is established here; that function is this one plus the removal.

    Returns the archive path the copy now occupies.
    """
    directory, stem = _archive_dir_for(archive_root, facts)
    directory.mkdir(parents=True, exist_ok=True)
    payload = source.read_bytes()

    fd, candidate = _claim_archive_slot(directory, stem)
    try:
        try:
            _write_all(fd, payload)
            # The complete payload is on the descriptor before anything is made durable and long
            # before the source is unlinked: a short write must never be fsynced and named as if it
            # were the proof.
            if os.fstat(fd).st_size != len(payload):  # pragma: no cover - defensive
                raise OSError(
                    errno.EIO,
                    f"archived proof is {os.fstat(fd).st_size} bytes, expected {len(payload)}",
                )
            os.fsync(fd)
        finally:
            os.close(fd)
    except BaseException:
        # This name was created by THIS call's exclusive create, so the partial file is ours and
        # nothing else's proof: remove it so a doomed retry loop cannot leave a trail of truncated
        # archive slots. Best-effort — the source is untouched either way, which is what matters.
        with contextlib.suppress(OSError):
            os.unlink(candidate)
        raise
    # The file's BYTES are durable; its NAME is not until every directory on the chain holding it is
    # synced — including any created by an earlier failed attempt, hence the chain to the store root.
    _sync_dir_chain(
        directory, sync_root if sync_root is not None else archive_root, store_key=store_key
    )
    return candidate


def _preserve_proof(
    source: Path,
    archive_root: Path,
    facts: StoredProofFacts,
    *,
    store_key: str,
    sync_root: Path | None = None,
) -> Path:
    """Move ``source`` into the content-addressed archive, never discarding, never overwriting.

    Two proofs for one digest are NOT interchangeable — one may carry a Bitcoin attestation the
    other lacks — and deciding which is stronger is a judgement an archive must not make. So a taken
    name is not a collision to resolve by dropping one: the incoming proof gets the next free
    monotonic slot (``<digest>.ots``, ``<digest>.1.ots``, ...). Nothing here is ever replaced or
    removed.

    The write is an EXCLUSIVE create (``O_CREAT|O_EXCL``) — ``os.replace`` silently overwrites and
    is therefore unusable — followed by a COMPLETE byte copy (:func:`_write_all`: a short
    ``os.write`` that archived a prefix and then unlinked the original would destroy the evidence
    just as surely as an overwrite). ``os.link`` is deliberately NOT used: it needs hard-link
    support, which the proof store's "writable" contract does not promise, and on a CIFS/FAT/FUSE
    store it would turn every occupied-path placement into a permanent retry loop reported as
    transient. Copying a sub-kilobyte file works everywhere ``open`` does.

    Ordering (design D1, "Crash-safety of the shuffle"): create -> write EVERY byte -> verify the
    written size -> fsync the FILE -> close -> fsync the archive's directory chain deepest-first,
    from the archive directory up to and including ``sync_root`` (the proof-store root, whose own
    name predates every proof) -> and ONLY THEN unlink the source. An interruption anywhere before
    that unlink leaves the proof intact at its original path; after it, the archived name is durable.
    "Both names gone" is unreachable — and it stays unreachable across retries, because the chain is
    derived from the path, not from which attempt happened to create each directory.

    Returns the archive path the proof now occupies.
    """
    candidate = _archive_copy(
        source, archive_root, facts, store_key=store_key, sync_root=sync_root
    )
    # Only now may the only other copy go away.
    os.unlink(source)
    return candidate


# --- Placement serialization: the fence AT the resource (design D10) ---------------------------

# The lock file guarding one collection's proof subtree: ``<proof_store>/<collection_id>/.lock``.
# It lives in the collection's own proof directory -- outside the ``.staging`` and ``.superseded``
# namespaces -- and can never be mistaken for a proof: every proof Cairn writes is named
# ``<relpath>.ots``, and this name has no ``.ots`` suffix. It is never read, never parsed and never
# archived; only its lock state matters, so an empty file is the whole of it.
COLLECTION_LOCK_NAME = ".lock"

# How long a placer waits for that lock before giving up. The wait is BOUNDED so a wedged holder can
# never stall a pass forever, and a timeout is TRANSIENT (:class:`OtsError`): nothing is placed, the
# files stay ``pending``/``incomplete``, and the next pass takes them. Sixty seconds is far longer
# than any real critical section (a local parse plus one or two renames) and far shorter than the
# 15-minute lease interval, so a timeout means "someone else genuinely holds the resource", never
# "this placement was slow".
PLACEMENT_LOCK_TIMEOUT_SECONDS = 60.0

# The wait is a poll, not a blocking ``flock`` plus a signal: ``signal.alarm`` only works on the
# main thread and these acquisitions run in a worker thread (``asyncio.to_thread`` at the call
# sites), so an alarm-based timeout would silently never fire.
_LOCK_POLL_SECONDS = 0.05

# ``flock`` answers with exactly one of these when the FILESYSTEM cannot lock at all (some network
# and FUSE stores). Deterministic, exactly like :data:`_DIR_SYNC_UNSUPPORTED`: it answers the same
# on every retry, so classifying it transient would wedge stamping on such a store forever. Every
# OTHER errno (EIO, ENOSPC, EACCES, ...) stays a real, transient failure.
_FLOCK_UNSUPPORTED = frozenset(
    {
        errno.ENOLCK,
        errno.ENOSYS,
        errno.EINVAL,
        errno.ENOTSUP,
        getattr(errno, "EOPNOTSUPP", errno.ENOTSUP),
    }
)

# The lock is simply held by someone else. Not a failure -- wait and retry until the deadline.
_LOCK_CONTENDED = frozenset({errno.EWOULDBLOCK, errno.EAGAIN})

# Proof stores (by root path) whose filesystem cannot lock. Detected LAZILY AND IN-BAND on the first
# acquisition actually attempted for that store -- no probe, no startup check, no schema, no
# setting. Process memory only, exactly like :data:`_BEST_EFFORT_DIR_SYNC`.
_BEST_EFFORT_PLACEMENT_LOCK: set[str] = set()


def collection_lock_path(store_root: str | os.PathLike[str], collection_id: str | int) -> Path:
    """``<proof_store>/<collection_id>/.lock`` -- the placement lock for one collection's proofs."""
    return Path(store_root) / str(collection_id) / COLLECTION_LOCK_NAME


class CollectionProofLock:
    """An advisory, cross-process lock over one collection's proof subtree (design D10).

    The DB claim (:func:`src.services.collections.claim_run`) serializes Cairn's *operations*; this
    serializes *mutation of the resource itself*. It exists because the lease fence is a
    check-then-act: a pass can read ``lease_held() is True``, be reclaimed a microsecond later, and
    walk into placement beside the replacement claimant -- two writers, one canonical path, one
    ``os.replace`` each, and the loser's proof destroyed with no trace. Holding this lock across the
    placement makes the two take their turns, and the post-acquisition re-read of the lease (at the
    call sites) makes the one that lost its claim abort instead of writing.

    ``fcntl.flock`` is the right primitive here because Cairn is deployed as **one machine** (a
    self-hosted app plus a host CLI over one proof-store filesystem, DESIGN.md): container and host
    share one kernel and one filesystem, so an advisory lock on a file in that store is visible to
    both. It is NOT a substitute for the DB claim -- the claim is what serializes across *hosts*
    sharing a datastore -- it is the last rung of the ladder below it.

    The lock lives on the open file DESCRIPTION, not on a thread, so acquiring it in one worker
    thread and releasing it from another (or from the event loop) is correct and deliberate: the
    call sites acquire through ``asyncio.to_thread`` because the wait can block, and release inline
    because ``LOCK_UN`` cannot.

    Failure handling follows the module's classification rule and :func:`_fsync_dir`'s errno
    discipline:

    * the lock file cannot be created/opened -> transient :class:`OtsError` (the store is writable
      by contract; if it is not, the placement about to happen would fail anyway);
    * ``flock`` says the filesystem cannot lock -> **degrade**: one WARNING per proof store, then
      proceed with the datastore fence alone (accepted limitation);
    * the deadline passes with the lock still held elsewhere -> :class:`LockContended` (an
      :class:`OtsError`, so every existing handler still treats it as the transient refusal it is;
      the subclass exists for the reclamation probe, which reads a held lock as proof of life);
    * any other errno -> transient :class:`OtsError`.

    Never a permanent (:class:`OtsPathError`) verdict: the lock is not the final output path, so it
    can never drop a file out of the stamp queue.
    """

    def __init__(
        self,
        store_root: str | os.PathLike[str],
        collection_id: str | int,
        *,
        timeout: float | None = None,
    ) -> None:
        self.path = collection_lock_path(store_root, collection_id)
        self._store_key = str(Path(store_root))
        # Read the module constant at call time rather than binding it as a default: the call sites
        # never pass a timeout, and a default bound at import would make the knob unturnable.
        self._timeout = PLACEMENT_LOCK_TIMEOUT_SECONDS if timeout is None else timeout
        self._fd: int | None = None
        #: True when this store's filesystem cannot lock and the lock degraded to a no-op.
        self.degraded = False

    def acquire(self) -> None:
        """Take the lock, waiting up to ``timeout``. Raises :class:`OtsError` on failure.

        Safe to call from a worker thread; the caller must pair it with :meth:`release` in a
        ``finally``.
        """
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)
        except OSError as exc:
            raise OtsError(
                f"cannot open the proof placement lock {str(self.path)!r}: {exc}"
            ) from exc

        if self._store_key in _BEST_EFFORT_PLACEMENT_LOCK:
            os.close(fd)
            self.degraded = True
            return

        deadline = time.monotonic() + self._timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in _LOCK_CONTENDED:
                    if time.monotonic() >= deadline:
                        os.close(fd)
                        raise LockContended(
                            f"timed out after {self._timeout:g}s waiting for the proof placement "
                            f"lock {str(self.path)!r}; another placer holds it"
                        ) from exc
                    time.sleep(_LOCK_POLL_SECONDS)
                    continue
                os.close(fd)
                if exc.errno in _FLOCK_UNSUPPORTED:
                    _BEST_EFFORT_PLACEMENT_LOCK.add(self._store_key)
                    log.warning(
                        "proof store %s: the filesystem does not support advisory locking (%s), so "
                        "proof placement cannot be serialized at the resource; Cairn falls back to "
                        "the datastore operation claim alone. Two Cairn processes racing over one "
                        "proof path on this store are guarded by that claim only. Proceeding.",
                        self._store_key,
                        exc.strerror or exc,
                    )
                    self.degraded = True
                    return
                raise OtsError(
                    f"cannot take the proof placement lock {str(self.path)!r}: {exc}"
                ) from exc
            self._fd = fd
            return

    def release(self) -> None:
        """Release the lock. Never raises -- it runs in a ``finally`` beside a classified error."""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            with contextlib.suppress(OSError):
                os.close(fd)

    def __enter__(self) -> CollectionProofLock:
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def acquire_proof_lock_now(
    store_root: str | os.PathLike[str], collection_id: str | int
) -> CollectionProofLock:
    """Take a collection's proof lock WITHOUT waiting, or raise :class:`LockContended` (design D1).

    The reclamation probe. A stamping operation holds this lock across its whole
    guard-through-placement critical section, so "the lock is held" is the one signal that
    distinguishes a claim holder that is ALIVE inside that section from one that died: process
    death releases an ``flock``, a failing DB keepalive does not. Claim reclamation therefore
    probes here first and refuses while the lock is held — the lock, not the heartbeat, is what
    proves the batch between its guard and its placement is still running.

    Non-blocking by construction (``timeout=0``): a reclaimer must never queue behind a live
    holder — it is answering a question, not taking a turn. A store whose filesystem cannot lock
    degrades exactly as the placement path does (one WARNING per store, ``degraded`` set, no lock
    held), so reclamation there falls back to the guarded UPDATE alone.

    The caller must release the returned lock in a ``finally``: the reclaiming UPDATE runs while it
    is held, so a holder cannot slip into its critical section between the probe and the write.
    """
    lock = CollectionProofLock(store_root, collection_id, timeout=0.0)
    lock.acquire()
    return lock


def _path_occupied(path: Path) -> bool:
    """Whether something already occupies ``path``, treating an un-stat-able path as unoccupied.

    A ``stat`` that fails is not evidence that a proof is there — and, crucially, it must not turn a
    PERMANENT refusal of the final output path into a transient preservation error. An overlong
    ``.ots`` name makes ``Path.exists()`` itself raise ``ENAMETOOLONG``; swallowed here, the
    placement below reaches the same errno on ``mkdir``/``os.replace`` and classifies it as the
    permanent skip it is, keeping the module's rule that the final output path is classified in
    exactly one place. Any other stat failure is likewise re-encountered and classified there.
    """
    try:
        return path.exists()
    except OSError:
        return False


@dataclass(frozen=True)
class _StoreContext:
    """The four proof-store coordinates every write into the store needs, derived once.

    ``store`` is the proof-store root (``None`` only for a direct call that named none),
    ``store_key`` keys the per-store degrade sets, ``sync_root`` bounds the directory-sync chain,
    and ``archive_root`` is where a displaced proof is preserved.
    """

    store: Path | None
    store_key: str
    sync_root: Path
    archive_root: Path


def _store_context(
    target: Path, store_root: str | os.PathLike[str] | None, collection_id: str | int | None = None
) -> _StoreContext:
    """Derive the store coordinates for a write at ``target``.

    ``collection_id`` names the archive subtree explicitly. Without it the id is read from
    ``target``'s first component below the store root, which is right for a canonical proof path
    (``<store>/<collection_id>/<relpath>.ots``) and wrong for anything else — the relocation
    holding slot (``<store>/.relocating/…``) is under the store but under no collection, so its
    callers always pass the id rather than letting it be inferred as ``.relocating``.
    """
    store = Path(store_root) if store_root is not None else None
    # Keyed per proof store: the directory-sync degrade is a property of the store's filesystem.
    store_key = str(store if store is not None else target.parent)
    # Everything made durable is flushed up to (and including) the proof-store root: anchoring on a
    # directory whose name predates every proof is what makes durability independent of which
    # attempt created the intermediate directories (Codex B2).
    sync_root = store if store is not None else target.parent
    if store is not None:
        if collection_id is None:
            try:
                collection_id = target.relative_to(store).parts[0]
            except (ValueError, IndexError):  # pragma: no cover - defensive
                collection_id = "unknown"
        archive_root = superseded_root(store, collection_id)
    else:
        # No store root named (only reachable from a direct call that did not pass one). Archive
        # beside the canonical tree rather than not archiving at all — losing a proof is the one
        # outcome these functions may never produce.
        archive_root = target.parent / SUPERSEDED_DIRNAME
    return _StoreContext(
        store=store, store_key=store_key, sync_root=sync_root, archive_root=archive_root
    )


def _place_proof(
    staged_ots: Path,
    out_ots_path: Path,
    *,
    store_root: str | os.PathLike[str] | None = None,
    verdict: str | None = None,
) -> StampOutcome:
    """Put a produced staging proof at its final ``out_ots_path`` WITHOUT ever destroying a proof.

    This used to be a bare ``os.replace``, so any second stamp of the same relpath landed on top of
    whatever was there — including a years-old Bitcoin anchor, whose bytes were then gone from the
    disk (GitHub #15). It is now: inspect the canonical path, decide, preserve, place.

    The rule when the canonical path is OCCUPIED (design D1):

    ==============================================  ==========================================
    existing canonical proof                        action
    ==============================================  ==========================================
    unreadable                                      archive under ``unknown/``; place staged
    digest != staged digest                         archive under its digest; place staged
    same digest, anchored, ``verdict='confirmed'``  KEEP existing, discard staged (``kept``)
    same digest, anchored, ``verdict='disproven'``  archive existing; place staged
    same digest, anchored, no verdict               DEFER: archive the STAGED proof, keep existing
    same digest, not anchored                       archive existing; place staged
    ==============================================  ==========================================

    ``verdict`` is the CALLER's answer about the existing proof's Bitcoin anchor. This function is
    offline by construction — it parses a local file and checks no chain — so "anchored" here means
    only *carries a BitcoinBlockHeaderAttestation*. Without the caller's verdict, keeping such a
    proof would let a fabricated attestation hold the canonical path and discard the real proof
    produced seconds earlier, so keep-existing requires ``'confirmed'`` and an unconfirmed anchor
    DEFERS rather than asserting anything (design D1a).

    Ordering is load-bearing and forbidden to reverse: **archive first, place second**. An
    interruption between them leaves the canonical path absent and the old proof safe in the
    archive, which is recoverable — the caller has written no row state, so the file is still
    ``pending`` and the next pass re-stamps. The reverse order destroys the proof before preserving
    it, which is the bug.

    Failure classification is unchanged and still governed by the module docstring: only a
    **permanent** refusal of the FINAL output path (ENAMETOOLONG) raises :class:`OtsPathError`.
    Every other ``OSError`` — including any preservation failure, because the archive is not the
    final output path — is a transient :class:`OtsError`, so a proof that could not be preserved
    REFUSES the placement and leaves the member ``pending`` rather than proceeding over it.
    """
    staged_ots = Path(staged_ots)
    out_ots_path = Path(out_ots_path)
    ctx = _store_context(out_ots_path, store_root)
    store_key, sync_root, archive_root = ctx.store_key, ctx.sync_root, ctx.archive_root

    staged_facts: StoredProofFacts | None = None
    try:
        if _path_occupied(out_ots_path):
            existing_facts = read_proof_facts(out_ots_path)
            staged_facts = read_proof_facts(staged_ots)
            same_digest = bool(
                existing_facts.readable
                and staged_facts.readable
                and existing_facts.digest
                and existing_facts.digest == staged_facts.digest
            )
            if same_digest and existing_facts.anchored and verdict == "confirmed":
                # #15's headline case. A fresh stamp is always `incomplete`; replacing a confirmed
                # Bitcoin anchor with a same-bytes pending proof is a strict downgrade of the claim.
                log.warning(
                    "keeping the existing confirmed proof at %s and discarding the freshly staged "
                    "%s (same digest %s)",
                    out_ots_path,
                    staged_ots,
                    existing_facts.digest,
                )
                return StampOutcome(kind="kept", digest=existing_facts.digest, state="complete")
            if same_digest and existing_facts.anchored and verdict != "disproven":
                # The outage branch. Nothing confirmed this anchor and nothing disproved it.
                # Recording `complete` would make a completed notarization purchasable by anyone who
                # can take the backend offline; discarding the staged proof would lose evidence;
                # demoting the existing one would throw away a probably-genuine anchor over a
                # network blip. Keep BOTH and decide on a pass whose backend answers.
                slot = _preserve_proof(
                    staged_ots, archive_root, staged_facts,
                    store_key=store_key, sync_root=sync_root,
                )
                log.warning(
                    "deferring proof placement for %s: the existing proof's anchor was neither "
                    "confirmed nor disproven, so it stays canonical and the freshly staged proof is "
                    "preserved at %s (nothing recorded; the file stays pending)",
                    out_ots_path,
                    slot,
                )
                return StampOutcome(kind="deferred")
            slot = _preserve_proof(
                out_ots_path, archive_root, existing_facts,
                store_key=store_key, sync_root=sync_root,
            )
            log.warning(
                "superseded proof preserved: %s -> %s (%s)",
                out_ots_path,
                slot,
                (
                    f"digest {existing_facts.digest}"
                    if existing_facts.readable
                    else "unreadable proof, archived under an opaque name"
                ),
            )
    except OSError as exc:
        # Preservation is NOT the final output path, so it is always transient (module docstring):
        # the member stays `pending` and is retried, never dropped to `none`. Crucially the source is
        # unlinked last inside `_preserve_proof`, so a failure here leaves the existing proof intact
        # at the canonical path and nothing has been placed over it.
        raise OtsError(
            f"refusing to place a proof at {out_ots_path!r}: could not preserve the existing "
            f"proof ({exc})"
        ) from exc

    try:
        out_ots_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged_ots, out_ots_path)
        # A rename is not durable until the directory holding it is synced. Without this the caller
        # can record a proof path naming an entry that did not survive a crash, while the preserved
        # copy sits under a name no row points at. Same chain rule as preservation: up to the store
        # root, so a directory left by an earlier failed attempt is never assumed durable.
        _sync_dir_chain(out_ots_path.parent, sync_root, store_key=store_key)
    except OSError as exc:
        if exc.errno == errno.ENAMETOOLONG:
            raise OtsPathError(f"cannot write proof to {out_ots_path!r}: {exc}") from exc
        raise OtsError(f"failed to place proof at {out_ots_path!r}: {exc}") from exc

    if staged_facts is None:
        # Read from the canonical path: the staged file has been renamed away. One sub-kilobyte
        # local parse, so the caller can record WHICH digest the proof it just placed commits to.
        staged_facts = read_proof_facts(out_ots_path)
    return StampOutcome(kind="placed", digest=staged_facts.digest, state="incomplete")


# --- Proof relocation: a proof's location follows its file (design D4, GitHub #39) -------------
#
# These four functions are the ONLY code in Cairn that moves a stored proof from one location to
# another, and they exist for exactly one caller: the healing sweep in `services.proofs`. Nothing on
# the scan path may reach them — a scan rewrites the index and never the proof store.
#
# They are deliberately DB-free. The two decisions that need the datastore stay with the caller:
# whether a destination is another row's recorded `ots_path` (threaded in as the `referenced`
# predicate, re-consulted whenever the destination rules restart), and the fenced compare-and-set
# that commits the pointer BETWEEN publication and source removal. That split is what makes the
# pointer invariant — `ots_path` always names a location actually holding this row's proof — hold
# across a crash at every phase boundary.

# Where a cycle-breaking relocation parks a proof (design D4 rule 1). Under the store, outside every
# canonical slot, and unable to shadow a collection directory (those are integers) exactly as
# `.staging` and `.superseded` cannot.
RELOCATING_DIRNAME = ".relocating"

# `os.link` answering with one of these means the filesystem cannot make a second name for this
# file — a store without hard links (SMB/FAT/FUSE), a cross-device destination, or a link count at
# its limit. Deterministic, so the publication falls back to a link-free exclusive create rather
# than reporting a failure that would repeat forever. Every OTHER errno stays a real failure.
_LINK_UNSUPPORTED = frozenset(
    {
        errno.EPERM,
        errno.EXDEV,
        errno.ENOSYS,
        errno.ENOTSUP,
        getattr(errno, "EOPNOTSUPP", errno.ENOTSUP),
        errno.EMLINK,
    }
)

# A parsed proof digest is lower-case hex. Validated before it is ever used to build a glob so a
# malformed value can never widen an archive search into a metacharacter pattern.
_HEX_DIGEST_RE = re.compile(r"\A[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class RelocationPublication:
    """What phases 1–3 of a relocation established, so the caller knows what it may commit.

    * ``aliased``  — source and destination are ONE directory entry (a case-only rename on a
      case-insensitive store). The proof is already where it belongs: commit the canonical
      SPELLING and remove nothing, because there may be only one entry to remove.
    * ``published``— the destination now holds a second, durable copy. Commit the pointer, then
      call :func:`finish_relocation` to remove the redundant source.
    * ``adopted``  — the destination already held a byte-identical copy (an earlier attempt
      interrupted between publication and the pointer commit). Same follow-up as ``published``;
      the destination's directory chain has been synced here, because the interrupted attempt's
      own sync may never have run.
    * ``deferred`` — the destination is recorded as another row's proof. Nothing was touched and
      nothing may be committed; the caller retries after that row has moved on.
    """

    kind: str  # 'aliased' | 'published' | 'adopted' | 'deferred'
    archived: Path | None = None  # where an occupant displaced by rule (c) was preserved


def holding_slot(store_root: str | os.PathLike[str], row_id: int | str) -> Path:
    """``<proof_store>/.relocating/<row_id>.ots`` — the cycle-breaking holding location (D4 rule 1).

    A path swap makes two rows each block the other's destination, which deferral alone can never
    resolve. Parking ONE member's proof here vacates a canonical slot so the rest converge; the
    parked pointer is as truthful as any other and the held proof reaches its own canonical slot on
    a later sweep. The name is fixed-length and ASCII, so it can never be the thing that trips the
    per-name byte limit.
    """
    return Path(store_root) / RELOCATING_DIRNAME / f"{row_id}.ots"


def same_directory_entry(a: str | os.PathLike[str], b: str | os.PathLike[str]) -> bool:
    """Whether two paths name ONE directory entry, by filesystem identity (device + inode).

    The portable truth for "a case-insensitive store treats these two spellings as the same slot".
    An un-stat-able path answers ``False``: absence is not identity, and no caller may act on a
    claim the filesystem refused to make.

    *Accepted limitation:* identity cannot distinguish one entry from two hard links to one file —
    which Cairn's own relocation can leave behind across a crash. Every caller therefore treats a
    positive answer as a reason to be MORE conservative (defer a stamp; refuse to unlink), never as
    a licence to remove something.
    """
    try:
        sa, sb = os.lstat(a), os.lstat(b)
    except OSError:
        return False
    return (sa.st_dev, sa.st_ino) == (sb.st_dev, sb.st_ino)


def find_archived_proof(
    store_root: str | os.PathLike[str], collection_id: int | str, digest: str
) -> Path | None:
    """An archived copy under ``.superseded`` whose committed digest is ``digest``, or ``None``.

    The archive is content-addressed, so the family for a digest is one directory read; each
    candidate is still PARSED before it is offered, because the name is an index and only the bytes
    are evidence. Used by the restore leg (design D4b) to republish a proof whose recorded entry has
    gone missing from the store.
    """
    digest = (digest or "").strip().lower()
    if not _HEX_DIGEST_RE.match(digest):
        return None
    family = superseded_root(store_root, collection_id) / digest[:2]
    try:
        candidates = sorted(family.glob(f"{digest}*.ots"))
    except OSError:  # pragma: no cover - defensive
        return None
    for candidate in candidates:
        if read_proof_facts(candidate).digest == digest:
            return candidate
    return None


def _classify_destination_error(exc: OSError, dst: Path) -> OtsError:
    """Map a failure ON THE FINAL OUTPUT PATH to the module's two classes (see the module docstring).

    ``ENAMETOOLONG`` is the one permanent verdict — the filesystem will refuse this name forever.
    Note what a permanent verdict means HERE, though: the healing sweep treats it as a per-row
    warning and leaves the row's proof, pointer and provenance exactly as they were (design D5). It
    is emphatically NOT the stamp path's drop-to-``none``, which would discard a placed proof's
    provenance to tidy up a location problem.
    """
    if exc.errno == errno.ENAMETOOLONG:
        return OtsPathError(f"cannot write a proof to {str(dst)!r}: {exc}")
    return OtsError(f"failed to write a proof to {str(dst)!r}: {exc}")


def _copy_no_replace(payload: bytes, dst: Path) -> None:
    """Create ``dst`` exclusively and write ``payload`` into it durably; never replace.

    The same primitive the superseded archive uses (:func:`_archive_copy`), pointed at a canonical
    slot: ``O_CREAT|O_EXCL`` so an occupant that appeared since the destination was classified
    raises ``FileExistsError`` for the caller to re-classify rather than being silently overwritten,
    and :func:`_write_all` so a short write can never be fsynced and named as if it were a proof.
    No temp file is involved — the exclusive create IS the reservation — so a crash mid-write leaves
    only a partial file at an unreferenced slot, which the next pass classifies as an occupant and
    archives rather than trusting.

    A failure after the create removes the partial file: it was made by this call and is nobody
    else's proof. If even that removal is refused, the partial file is WARNED about by name — it
    occupies a slot a caller may be recording a pointer to, and a silently truncated proof left at
    a canonical path is exactly the kind of thing this module exists to never do. Callers that
    cannot tolerate that window at all (the restore leg, which publishes at a path a row ALREADY
    records) stage into a temp file and publish by link instead; see :func:`republish_proof`.
    """
    fd = os.open(str(dst), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    try:
        try:
            _write_all(fd, payload)
            if os.fstat(fd).st_size != len(payload):  # pragma: no cover - defensive
                raise OSError(
                    errno.EIO,
                    f"published proof is {os.fstat(fd).st_size} bytes, expected {len(payload)}",
                )
            os.fsync(fd)
        finally:
            os.close(fd)
    except BaseException:
        try:
            os.unlink(dst)
        except OSError as cleanup_exc:
            log.warning(
                "a proof write to %s failed and the partial file could not be removed (%s) — that "
                "path is now occupied by INCOMPLETE bytes; it is never trusted as a proof (the "
                "next placement there classifies it as an occupant and archives it), but remove it "
                "by hand if a row records that path",
                dst,
                cleanup_exc,
            )
        raise


def _stage_proof_bytes(payload: bytes, directory: Path) -> Path:
    """Write ``payload`` durably to a fresh, exclusively-created TEMP name inside ``directory``.

    The staging half of "stage, then publish". The temp name is non-colliding
    (``uuid4``), cannot be mistaken for a proof (no ``.ots`` suffix), and is fsynced before the
    caller publishes it — so publication is a single link/rename of bytes that are already complete
    and durable, and a failed WRITE can never leave a partial file at the destination the caller is
    about to name. Every handled failure removes the temp; one left by a crash is debris the store
    ignores (design D4 phase 3).

    Raises ``OSError`` for the caller to classify.
    """
    directory.mkdir(parents=True, exist_ok=True)
    staged = directory / f".restore-{uuid.uuid4().hex}.tmp"
    fd = os.open(str(staged), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    try:
        try:
            _write_all(fd, payload)
            if os.fstat(fd).st_size != len(payload):  # pragma: no cover - defensive
                raise OSError(
                    errno.EIO,
                    f"staged proof is {os.fstat(fd).st_size} bytes, expected {len(payload)}",
                )
            os.fsync(fd)
        finally:
            os.close(fd)
    except BaseException:
        try:
            os.unlink(staged)
        except OSError as cleanup_exc:
            # Undisclosed temp debris accumulates across retries (every sweep re-stages), so a
            # refused cleanup must be loud and name the path.
            log.warning(
                "could not remove the staged proof temp %s after a failed staging: %s",
                staged,
                cleanup_exc,
            )
        raise
    return staged


def publish_relocation(
    src: str | os.PathLike[str],
    dst: str | os.PathLike[str],
    *,
    store_root: str | os.PathLike[str] | None = None,
    collection_id: int | str | None = None,
    referenced: Callable[[Path], bool] | None = None,
) -> RelocationPublication:
    """Make ``dst`` durably hold the proof at ``src``, destroying nothing (design D4, phases 1–3).

    Returns without touching the datastore: the caller commits the pointer (a fenced
    compare-and-set) and only then calls :func:`finish_relocation`. The source is still there and
    still the truthful location when this returns, which is precisely why a crash here is harmless.

    Phase 1 — **aliasing**. If ``src`` and ``dst`` resolve to one directory entry, the proof is
    already in place. Identity alone does NOT decide that: network filesystems can report identity
    they do not have, so it is confirmed by a byte comparison, and an identity claim the bytes
    contradict refuses the whole relocation rather than committing a pointer on the strength of it.
    An alias returns immediately — nothing is removed, because there may be only one entry.

    Phase 2 — **destination rules, in this order**:

    1. a destination the caller's ``referenced`` predicate claims for a different row → ``deferred``:
       no branch here may ever place over, unlink, or otherwise disturb another row's proof;
    2. an occupant BYTE-IDENTICAL to the source → ``adopted``: this is the published half of an
       interrupted relocation. Its directory chain is synced here even though nothing was written,
       because the attempt that published it may have died before its own sync. Committed-digest
       equality would NOT be enough — two distinct proofs can commit to one file digest while
       differing in attestation value, and neither may be discarded for the other;
    3. any other occupant → archived to ``.superseded`` (never discarded), then publication proceeds.

    Phase 3 — **publication**, atomic and non-replacing: a hard link where the filesystem supports
    one, else a link-free exclusive create plus a full copy, then the directory-sync chain to the
    store root. A destination that appears between classification and publication (``EEXIST``)
    restarts phase 2 instead of overwriting, re-asking ``referenced`` and re-inspecting the
    occupant. What the restart re-reads is the FILESYSTEM; ``referenced`` may legitimately be a
    frozen snapshot (the sweep passes one) because the caller holds the collection's operation
    claim AND its proof-store lock for the whole relocation, so no other Cairn writer can create a
    pointer at the destination while it is in flight — the answer cannot go stale under it. What
    can change under it is the directory entry, and that is exactly what the restart re-classifies.

    Raises :class:`OtsPathError` for a permanently refused destination name and :class:`OtsError`
    for every other failure; both leave the source proof readable and the caller's pointer truthful.
    """
    src = Path(src)
    dst = Path(dst)
    ctx = _store_context(dst, store_root, collection_id)

    try:
        os.lstat(src)
    except OSError as exc:
        raise OtsError(f"cannot read the proof to relocate at {str(src)!r}: {exc}") from exc

    # --- Phase 1: aliasing ---------------------------------------------------------------------
    if same_directory_entry(src, dst):
        try:
            if src.read_bytes() != dst.read_bytes():
                # The filesystem says one entry; the bytes say two. Believe neither and change
                # nothing: committing the pointer here could name a slot holding someone else's
                # proof, and removing anything could destroy the only copy of this one.
                raise OtsError(
                    f"refusing to relocate {str(src)!r} to {str(dst)!r}: the filesystem reports "
                    f"them as one directory entry but their contents differ"
                )
            return RelocationPublication(kind="aliased")
        except OSError as exc:
            raise OtsError(
                f"cannot confirm the aliased proof at {str(dst)!r}: {exc}"
            ) from exc
    # The per-name byte-limit pre-check on the components Cairn creates below the store root. It
    # runs BEFORE the destination rules so a row the filesystem permanently refuses is reported as
    # REFUSED rather than as deferred-by-reference — cycle breaking may only ever select a row that
    # no other rule refuses to move, and that selection reads these outcomes.
    if not _proof_output_writable(dst, below=ctx.store):
        raise OtsPathError(
            f"proof destination name too long to store "
            f"({len(os.fsencode(dst.name))} bytes > {_NAME_MAX_BYTES}): {str(dst)!r}"
        )

    while True:
        # --- Phase 2: destination rules --------------------------------------------------------
        if referenced is not None and referenced(dst):
            return RelocationPublication(kind="deferred")

        try:
            payload = src.read_bytes()
        except OSError as exc:
            raise OtsError(f"cannot read the proof to relocate at {str(src)!r}: {exc}") from exc

        archived: Path | None = None
        if _path_occupied(dst):
            try:
                occupant = dst.read_bytes()
            except OSError as exc:
                raise OtsError(
                    f"cannot inspect the proof occupying {str(dst)!r}: {exc}"
                ) from exc
            if occupant == payload:
                try:
                    _sync_dir_chain(dst.parent, ctx.sync_root, store_key=ctx.store_key)
                except OSError as exc:
                    raise OtsError(
                        f"cannot make the already-published proof at {str(dst)!r} durable: {exc}"
                    ) from exc
                return RelocationPublication(kind="adopted")
            try:
                archived = _preserve_proof(
                    dst,
                    ctx.archive_root,
                    read_proof_facts(dst),
                    store_key=ctx.store_key,
                    sync_root=ctx.sync_root,
                )
            except OSError as exc:
                raise OtsError(
                    f"refusing to relocate a proof to {str(dst)!r}: could not preserve the proof "
                    f"already there ({exc})"
                ) from exc
            log.warning(
                "proof displaced by a relocation preserved: %s -> %s", dst, archived
            )

        # --- Phase 3: publication --------------------------------------------------------------
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise _classify_destination_error(exc, dst) from exc

        try:
            os.link(src, dst)
        except FileExistsError:
            continue  # something took the slot; re-classify it, never overwrite
        except OSError as exc:
            if exc.errno not in _LINK_UNSUPPORTED:
                raise _classify_destination_error(exc, dst) from exc
            try:
                _copy_no_replace(payload, dst)
            except FileExistsError:
                continue
            except OSError as copy_exc:
                raise _classify_destination_error(copy_exc, dst) from copy_exc

        try:
            _sync_dir_chain(dst.parent, ctx.sync_root, store_key=ctx.store_key)
        except OSError as exc:
            raise OtsError(
                f"cannot make the relocated proof at {str(dst)!r} durable: {exc}"
            ) from exc
        return RelocationPublication(kind="published", archived=archived)


def finish_relocation(
    src: str | os.PathLike[str],
    dst: str | os.PathLike[str],
    *,
    store_root: str | os.PathLike[str] | None = None,
    collection_id: int | str | None = None,
) -> None:
    """Remove the now-redundant source entry, loss-proof (design D4, phase 5).

    Called ONLY after the pointer commit succeeded, so ``dst`` is what the index names and ``src``
    is a duplicate. "Duplicate" is still not "expendable": the sequence is archive a COPY of the
    source, then unlink it, then re-verify that ``dst`` really still holds the proof — restoring
    from that archive copy if it does not. The re-verification is the defence against a filesystem
    whose identity reporting lies: if ``src`` and ``dst`` were secretly one entry after all, the
    unlink took the destination with it, and the archive copy made a moment earlier is what puts it
    back.

    Every failure here leaves the committed (truthful) destination pointer alone and raises for the
    caller to warn about: a post-commit problem must never roll a row back to a pointer the proof
    may be about to leave. A source that survives is harmless — it sits in an unreferenced slot, and
    a future stamp there archives it under the never-destroy rules.

    **Nothing here is a silent success.** A failed unlink or source-directory sync is not suppressed
    into a clean return: the destination is verified first (so the caller learns the pointer is
    still truthful) and then the removal failure is raised as the post-commit warning. A failed
    RESTORATION likewise raises — and the state it leaves is deliberately the restore leg's
    admission shape (a committed pointer naming an absent entry, with the corroborated copy durable
    in the archive), so the next sweep republishes it at the recorded path.
    """
    src = Path(src)
    dst = Path(dst)
    ctx = _store_context(dst, store_root, collection_id)

    try:
        archived = _archive_copy(
            src,
            ctx.archive_root,
            read_proof_facts(src),
            store_key=ctx.store_key,
            sync_root=ctx.sync_root,
        )
    except OSError as exc:
        raise OtsError(
            f"refusing to remove the relocated proof's source {str(src)!r}: it could not be "
            f"archived first ({exc})"
        ) from exc

    # The bytes now exist in three places. Only here may one of them go away — and a removal that
    # does not happen is REPORTED, not suppressed: the caller's per-row warning is the only thing
    # that tells an operator a redundant copy is still sitting in an unreferenced slot. It is
    # remembered rather than raised on the spot, because the destination must be verified first:
    # what the operator needs to hear about a post-commit failure depends on whether the pointer
    # they now hold still resolves.
    removal_error: OSError | None = None
    try:
        os.unlink(src)
    except FileNotFoundError:
        pass  # the source is already gone — the goal of this step, not a failure of it
    except OSError as exc:
        removal_error = exc
    if removal_error is None:
        try:
            _sync_dir_chain(src.parent, ctx.sync_root, store_key=ctx.store_key)
        except OSError as exc:
            removal_error = exc

    if _path_occupied(dst):
        try:
            same = dst.read_bytes() == archived.read_bytes()
        except OSError as exc:  # pragma: no cover - defensive
            raise OtsError(
                f"cannot re-verify the relocated proof at {str(dst)!r}: {exc}"
            ) from exc
        if same:
            if removal_error is not None:
                raise OtsError(
                    f"the relocated proof is at {str(dst)!r} exactly as recorded, but its old copy "
                    f"at {str(src)!r} could not be removed ({removal_error}); nothing was lost — "
                    f"the leftover sits in an unreferenced slot and a future stamp there preserves "
                    f"it rather than overwriting it"
                )
            return
        # Something else is at the destination the pointer already names. Nothing is removed and
        # nothing is overwritten: two proofs survive and the operator is told.
        raise OtsError(
            f"the relocated proof at {str(dst)!r} no longer matches the proof that was moved "
            f"there; a copy is preserved at {str(archived)!r} and nothing was overwritten"
        )

    # The destination went away with the source — the identity-lying filesystem case. Put it back
    # from the copy made before the unlink; the pointer already names this path.
    log.warning(
        "the relocated proof at %s vanished when its source entry was removed (the filesystem "
        "reported them as distinct entries); restoring it from the archived copy %s",
        dst,
        archived,
    )
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        _copy_no_replace(archived.read_bytes(), dst)
        _sync_dir_chain(dst.parent, ctx.sync_root, store_key=ctx.store_key)
    except OSError as exc:
        # Loud, never silent: the pointer the caller committed now names an absent entry, and only
        # this exception makes the sweep say so. The state left behind is the restore leg's own
        # admission shape — recorded path absent, corroborated copy durable in the archive — so the
        # next sweep republishes the proof there rather than the loss waiting for someone to verify.
        raise OtsError(
            f"could not restore the relocated proof at {str(dst)!r} from {str(archived)!r} "
            f"({exc}); the recorded pointer names an entry that is now ABSENT, and the corroborated "
            f"copy at {str(archived)!r} is what the next sweep's restore leg republishes there"
        ) from exc


def republish_proof(
    source: str | os.PathLike[str],
    dst: str | os.PathLike[str],
    *,
    store_root: str | os.PathLike[str] | None = None,
    collection_id: int | str | None = None,
) -> None:
    """Publish ``source``'s bytes at the ABSENT path ``dst``, never replacing (design D4b).

    The restore leg's primitive: a row's recorded ``ots_path`` names nothing on disk and the archive
    holds a copy the row's own corroboration rules vouch for. Publication is non-replacing, so a
    slot that turns out to be occupied after all refuses instead of overwriting whatever is there.

    **Stage, then publish**, and the order is load-bearing here in a way it is not for an ordinary
    placement: this destination is a path a row ALREADY records as its proof, and the restore leg's
    admission test is "the recorded entry does not exist". A write that failed partway and could not
    clean up after itself would therefore leave the canonical slot holding a PREFIX of a proof, the
    row still claiming a complete one, and the sweep never admitting the row again — a silent,
    permanent corruption discovered only if someone happened to verify that file. So the bytes are
    written to an exclusive temp in the destination's own directory and fsynced FIRST
    (:func:`_stage_proof_bytes`), and only a complete, durable file is published — by ``os.link``,
    which either names it or fails, never half-names it. Every cleanup path touches only the temp.

    A store without hard links falls back to the direct exclusive create (the only publication
    primitive such a store has); there the partial-write window survives, narrowed to "the write
    failed AND its cleanup was refused", and :func:`_copy_no_replace` warns by name when it happens.
    """
    source = Path(source)
    dst = Path(dst)
    ctx = _store_context(dst, store_root, collection_id)
    if not _proof_output_writable(dst, below=ctx.store):
        raise OtsPathError(
            f"proof destination name too long to store "
            f"({len(os.fsencode(dst.name))} bytes > {_NAME_MAX_BYTES}): {str(dst)!r}"
        )
    try:
        payload = source.read_bytes()
    except OSError as exc:
        raise OtsError(f"cannot read the archived proof {str(source)!r}: {exc}") from exc

    try:
        staged = _stage_proof_bytes(payload, dst.parent)
    except OSError as exc:
        # Nothing was published and `dst` was never created: the destination is exactly as absent
        # as it was, so the next sweep admits this row again and retries.
        raise _classify_destination_error(exc, dst) from exc

    try:
        try:
            os.link(staged, dst)
        except FileExistsError as exc:
            raise OtsError(
                f"refusing to restore a proof at {str(dst)!r}: something occupies it now"
            ) from exc
        except OSError as exc:
            if exc.errno not in _LINK_UNSUPPORTED:
                raise _classify_destination_error(exc, dst) from exc
            try:
                _copy_no_replace(payload, dst)
            except FileExistsError as copy_exc:
                raise OtsError(
                    f"refusing to restore a proof at {str(dst)!r}: something occupies it now"
                ) from copy_exc
            except OSError as copy_exc:
                raise _classify_destination_error(copy_exc, dst) from copy_exc
        try:
            _sync_dir_chain(dst.parent, ctx.sync_root, store_key=ctx.store_key)
        except OSError as exc:
            raise OtsError(
                f"cannot make the restored proof at {str(dst)!r} durable: {exc}"
            ) from exc
    finally:
        # Only ever the temp. `dst` is either the published proof or was never created.
        try:
            os.unlink(staged)
        except FileNotFoundError:
            pass
        except OSError as cleanup_exc:
            log.warning(
                "could not remove the staged proof temp %s after republishing %s: %s",
                staged,
                dst,
                cleanup_exc,
            )


def info(ots_path: str | os.PathLike[str]) -> ProofInfo:
    """Classify an ``.ots`` proof OFFLINE via ``ots info`` (no network).

    A ``BitcoinBlockHeaderAttestation`` line means the proof is ``complete``; otherwise any
    ``PendingAttestation`` lines mean ``incomplete``. A missing/unparseable file is ``none``.
    """
    ots_path = Path(ots_path)
    if not ots_path.exists():
        return ProofInfo(state="none")

    rc, out, err = _run_ots(["info", str(ots_path)])
    combined = f"{out}\n{err}"
    if rc != 0 and not (_BITCOIN_ATTESTATION_RE.search(combined) or
                        _PENDING_ATTESTATION_RE.search(combined)):
        # info is offline; a non-zero exit with no attestations means an unreadable proof.
        return ProofInfo(state="none")

    calendars = _PENDING_ATTESTATION_RE.findall(combined)
    bitcoin = _BITCOIN_ATTESTATION_RE.search(combined)
    if bitcoin:
        return ProofInfo(
            state="complete",
            calendars=calendars,
            block_height=int(bitcoin.group(1)),
        )
    if calendars:
        return ProofInfo(state="incomplete", calendars=calendars)
    return ProofInfo(state="none")


def stamp_via_symlink(
    real_path: str | os.PathLike[str],
    out_ots_path: str | os.PathLike[str],
    calendars: list[str],
    staging_dir: str | os.PathLike[str],
    timeout: int = DEFAULT_TIMEOUT,
    *,
    store_root: str | os.PathLike[str] | None = None,
    verdict: str | None = None,
) -> StampOutcome:
    """Stamp ``real_path`` and place the proof at ``out_ots_path`` without writing beside it.

    ``ots stamp`` writes ``<input>.ots`` next to its input and has no output flag, but collection
    files live on a read-only mount. So we symlink ``staging_dir/<uuid>`` -> ``real_path``, stamp
    the symlink (``ots`` reads the real bytes, writes ``<uuid>.ots`` in the writable staging dir),
    then atomically move that ``.ots`` to ``out_ots_path`` and remove the symlink.

    ``store_root`` is the proof-store root: only the components below it (the ones Cairn creates)
    are pre-checked for the per-name byte limit. Staging-side failures are always transient.

    ``verdict`` is the caller's finding about any proof ALREADY at ``out_ots_path`` (``'confirmed'``
    / ``'disproven'`` / ``None``), forwarded to :func:`_place_proof`. Returns that function's
    :class:`StampOutcome` — the caller's row update differs per branch, and ``deferred`` means
    "record nothing" rather than "failed".
    """
    real_path = Path(real_path)
    out_ots_path = Path(out_ots_path)
    staging_dir = Path(staging_dir)
    root = Path(store_root) if store_root is not None else None
    # Fail fast on an un-writable proof name before spending a symlink or a calendar round-trip.
    if not _proof_output_writable(out_ots_path, below=root):
        raise OtsPathError(
            f"proof output name too long to store "
            f"({len(os.fsencode(out_ots_path.name))} bytes > {_NAME_MAX_BYTES}): {out_ots_path!r}"
        )
    _prepare_staging_dir(staging_dir)

    link = staging_dir / uuid.uuid4().hex
    staged_ots = link.with_name(link.name + ".ots")
    # Only what we actually created gets cleaned up — cleaning a path that was never made can itself
    # raise (an overlong staging pathname refuses `unlink` exactly as it refused `symlink`), and that
    # raw OSError would replace the classified exception on the way out of `finally`.
    created: list[Path] = []
    try:
        try:
            link.symlink_to(real_path)
        except OSError as exc:
            # ALWAYS transient (module docstring): the operand here is the STAGING pathname, whose
            # length/permissions are a property of the deployment, not of this file — even
            # ENAMETOOLONG. Permanence is decided only by the output-path pre-check above and by
            # `_place_proof` below. A raw OSError must never escape either: the caller classifies
            # stamp failures by exception type and would abort its whole pass.
            raise OtsError(
                f"cannot create the stamp staging link for {real_path}: {exc}"
            ) from exc
        created = [link, staged_ots]
        args = ["stamp"]
        for cal in calendars:
            args += ["-c", cal]
        args += ["--timeout", str(timeout), str(link)]
        rc, out, err = _run_ots(args, timeout=timeout + 10)
        if not staged_ots.exists():
            raise OtsError(
                f"ots stamp produced no proof for {real_path} "
                f"(rc={rc}): {(err or out).strip()}"
            )
        outcome = _place_proof(
            staged_ots, out_ots_path, store_root=root, verdict=verdict
        )
    finally:
        for stray in created:
            # Best-effort: cleanup must never mask or replace the classified exception.
            with contextlib.suppress(OSError):
                stray.unlink()
    return outcome


def stamp_batch_via_symlink(
    items: list[tuple[str | os.PathLike[str], str | os.PathLike[str]]],
    calendars: list[str],
    staging_dir: str | os.PathLike[str],
    timeout: int = DEFAULT_TIMEOUT,
    *,
    store_root: str | os.PathLike[str] | None = None,
    verdicts: list[str | None] | None = None,
) -> list[StampOutcome | None]:
    """Stamp many files in ONE ``ots stamp`` call; return per-item outcomes aligned with ``items``.

    ``items`` is a list of ``(real_path, out_ots_path)``. One staging symlink is built per item and
    ``ots stamp <link1> … <linkN>`` is invoked once: OpenTimestamps aggregates the N digests into a
    single calendar commitment, yet still writes an independent ``<linkI>.ots`` per input. Each
    produced proof is moved to its ``out_ots_path``.

    Success is decided by *filesystem truth* — whether each ``<linkI>.ots`` was actually produced —
    not by the process exit code, so a whole-batch failure, a timeout, or one unreadable file
    aborting the run leaves the unaffected members stamped and the rest reported ``None`` for the
    caller to retry individually. Links and stray ``.ots`` are always cleaned up (best-effort) in
    ``finally``. ``store_root`` bounds the output-path pre-check to the components Cairn creates.

    Each element of the result is a :class:`StampOutcome` (``placed`` / ``kept`` / ``deferred``) or
    ``None``, which still means exactly what ``False`` meant: "this member failed, fall back to a
    single-file stamp". A ``deferred`` member is NOT a failure — it needs no fallback, and the caller
    simply records nothing for it. Per-member placement failure is still isolated: one member whose
    old proof cannot be archived is left for the fallback while the rest of the batch is placed.
    """
    pairs = [(Path(real), Path(out)) for real, out in items]
    if not pairs:
        return []
    staging_dir = Path(staging_dir)
    root = Path(store_root) if store_root is not None else None
    # A staging dir we cannot create fails the whole call transiently (OtsError): no member is
    # symlinked, nothing is submitted, and the caller leaves every file `pending` for retry.
    _prepare_staging_dir(staging_dir)

    # Parallel to ``pairs``: (symlink, staged .ots) for a member we submit, or ``None`` for one whose
    # proof output name can't be written — it is neither symlinked nor sent to the calendar. Such a
    # member stays ``False``; the caller's single-file fallback re-checks it and records it as a
    # permanent skip rather than re-attempting it forever.
    links: list[tuple[Path, Path] | None] = []
    results: list[StampOutcome | None] = [None] * len(pairs)
    try:
        for real, out in pairs:
            if not _proof_output_writable(out, below=root):
                links.append(None)
                continue
            link = staging_dir / uuid.uuid4().hex
            try:
                link.symlink_to(real)
            except OSError:
                # One member we could not stage (an un-writable staging dir, a staging pathname the
                # OS refuses). Drop it from the submission and leave its result ``False`` — the
                # caller's single-file fallback re-raises the properly classified (always transient,
                # for a staging failure) error for this one file. A raw OSError here would abort the
                # batch AND the caller's whole stamp pass. Nothing was created, so nothing to clean.
                links.append(None)
                continue
            links.append((link, link.with_name(link.name + ".ots")))

        submit = [entry for entry in links if entry is not None]
        if submit:
            args = ["stamp"]
            for cal in calendars:
                args += ["-c", cal]
            args += ["--timeout", str(timeout)]
            args += [str(link) for link, _ in submit]
            try:
                _run_ots(args, timeout=timeout + 10)
            except OtsError:
                # A missing binary or a timeout aborts the whole call; fall through to filesystem
                # truth so any proofs already written are still harvested and the rest fall back.
                pass

        for i, ((_real, out), entry) in enumerate(zip(pairs, links)):
            if entry is None:
                continue  # unwritable output name — skipped, results[i] stays None
            _link, staged_ots = entry
            if staged_ots.exists():
                try:
                    results[i] = _place_proof(
                        staged_ots,
                        out,
                        store_root=root,
                        verdict=(verdicts[i] if verdicts is not None else None),
                    )
                except OtsError:
                    # Could not place this proof — an unwritable path the pre-check did not model, a
                    # transient store error (full / read-only), or an existing proof that could not
                    # be preserved. Leave ``None`` so the single-file fallback re-raises the right
                    # class and the caller classifies it permanent vs. transient; the staged proof is
                    # cleaned up below. One bad member never aborts the batch.
                    pass
    finally:
        for entry in links:
            if entry is None:
                continue
            link, staged_ots = entry
            for stray in (link, staged_ots):
                # Best-effort: a cleanup failure must never replace a member's classified outcome
                # (or, worse, escape as a raw OSError and abort the caller's whole stamp pass).
                with contextlib.suppress(OSError):
                    stray.unlink()
    return results


def upgrade(ots_path: str | os.PathLike[str], timeout: int = DEFAULT_TIMEOUT) -> bool:
    """Upgrade an incomplete proof in place; return True iff it is now complete.

    ``ots upgrade`` contacts the calendars and, if Bitcoin has confirmed, rewrites the proof
    (leaving a ``.bak``). A still-pending proof exits non-zero and leaves the file unchanged —
    that is normal, so we return False without raising. We remove the ``.bak`` after a successful
    upgrade to keep the store clean and re-check completeness offline via :func:`info`.
    """
    ots_path = Path(ots_path)
    if not ots_path.exists():
        raise OtsError(f"no proof to upgrade: {ots_path}")

    rc, out, err = _run_ots(["upgrade", str(ots_path)], timeout=timeout)
    combined = f"{out}\n{err}"
    if rc != 0 and not _is_pending(combined):
        raise OtsError(f"ots upgrade failed for {ots_path}: {combined.strip()}")

    now_complete = info(ots_path).state == "complete"
    if now_complete:
        bak = ots_path.with_name(ots_path.name + ".bak")
        try:
            bak.unlink()
        except FileNotFoundError:
            pass
    return now_complete


def verify(
    ots_path: str | os.PathLike[str],
    digest: str,
    *,
    backend: str = "explorer",
    explorer_url: str = DEFAULT_EXPLORER_URL,
    node_rpc_url: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> VerifyResult:
    """Verify a stored proof against ``digest`` (hex SHA-256) without the original file.

    Two backends (DESIGN §6, "verify defaults to a block-explorer lookup, configurable to a
    Bitcoin node"):

    * ``"explorer"`` (default) — parse the ``.ots`` locally and confirm each Bitcoin attestation's
      commitment equals the real block's merkle root, fetched from an esplora-compatible block
      explorer. This is implemented here (:func:`_verify_via_explorer`) because the maintained
      ``ots`` CLI can ONLY verify against a Bitcoin Core node — without one it exits with "Could
      not connect to Bitcoin node", so every complete proof would otherwise read as unverifiable.
    * ``"node"`` — shell out to ``ots verify -d`` (optionally ``--bitcoin-node <url>``), which
      talks to a Bitcoin node: fully trustless, but needs a reachable node.
    """
    if backend == "node":
        return _verify_via_cli(ots_path, digest, node_rpc_url, timeout)
    return _verify_via_explorer(ots_path, digest, explorer_url, timeout)


def _verify_via_cli(
    ots_path: str | os.PathLike[str],
    digest: str,
    node_rpc_url: str | None,
    timeout: int,
) -> VerifyResult:
    """Node-backed verify: ``ots verify -d <digest> <proof>`` (needs a reachable Bitcoin node).

    Exit 0 + "Success! Bitcoin block N attests …" when complete; exit non-zero + "Pending
    confirmation …" when not yet anchored; a digest mismatch is reported as not-verified. Offline
    :func:`info` supplies the state and calendars.
    """
    ots_path = Path(ots_path)
    try:
        # `info` shells out too (`ots info`), so a missing binary or a timeout here fails exactly
        # the way the verification call does — and must land on the same typed transport result.
        # Left outside the boundary it escaped as an exception: the panel's outer `except OtsError`
        # masked it, but `cairn verify` has no catch and aborted instead of printing COULD NOT CHECK.
        proof = info(ots_path)
    except OtsError as exc:
        return VerifyResult(
            verified=False,
            state="none",
            transport_error=str(exc),
            transport_failures=1,
            message=str(exc),
        )
    if proof.state == "none":
        # `info` collapses "no such file" and "exists but `ots info` could not parse it" into the
        # same `none`. They are different findings for the operator, so re-separate them here on
        # the one fact `info` discarded: the file's existence. An EXISTING proof that cannot be
        # parsed is `unreadable_proof` — nothing whatever was established — exactly as the explorer
        # backend reports it. Without this the node backend fell through to the untyped "no usable
        # proof" result, and the panel then offered file-change possibilities this check never
        # examined (the very false alarm the typed flag exists to prevent).
        unreadable = ots_path.exists()
        return VerifyResult(
            verified=False,
            state="none",
            calendars=proof.calendars,
            unreadable_proof=unreadable,
            message=(
                f"unreadable proof at {ots_path}" if unreadable else f"no usable proof at {ots_path}"
            ),
        )

    args: list[str] = []
    if node_rpc_url:
        # `--bitcoin-node` is a global option, so it must precede the `verify` subcommand.
        args += ["--bitcoin-node", node_rpc_url]
    args += ["verify", "-d", digest, str(ots_path)]
    try:
        rc, out, err = _run_ots(args, timeout=timeout)
    except OtsError as exc:
        # Missing binary / timeout: nothing was established about the file. Report it as transport
        # rather than letting it propagate into the caller's `except OtsError`, which historically
        # rebuilt a result carrying the proof's own state and so read as "pending confirmation".
        return VerifyResult(
            verified=False,
            state="none",
            calendars=proof.calendars,
            transport_error=str(exc),
            transport_failures=1,
            message=str(exc),
        )
    combined = f"{out}\n{err}"
    match = _VERIFY_SUCCESS_RE.search(combined)
    if rc == 0:
        # The PROCESS EXIT STATUS is the success contract, not the shape of a stdout line. `ots
        # verify -d` exits 0 only when the attestation checked out against the node; regexing the
        # wording instead both swallowed a successful exit whose phrasing changed and — far worse —
        # could return `verified=True` over a NON-ZERO exit whose stderr happened to quote a
        # success line. The regex is now consulted for OPTIONAL provenance metadata only, and a
        # verified result with no parsed block/date must render correctly in both consumers.
        return VerifyResult(
            verified=True,
            state="complete",
            block_height=int(match.group(1)) if match else None,
            existed_by=match.group(2).strip() if match else None,
            calendars=proof.calendars,
            message=combined.strip(),
        )
    # Non-zero exit. `ots verify -d` reports an unanchored proof, a changed file and a dead
    # Bitcoin node identically, so this backend cannot say which happened: `inconclusive`, never a
    # guessed mismatch and never the old reassuring "pending" (design D1). Deliberately asymmetric
    # with `_verify_via_explorer`, which parses the proof locally and therefore *knows* whether the
    # digest matched and whether each attestation checks out — it sets the two mismatch flags
    # because it establishes them; classifying this exit by regexing the CLI's wording would be a
    # guess, and a false mismatch is a false alarm on the product's core signal.
    # `block_height` here is what the proof *claims*, read offline from `ots info` — nothing
    # confirmed it against the Bitcoin record, and on a changed file it belongs to the digest the
    # proof was made from, not to the live one. Consumers MUST label it as proof-declared and
    # unverified, and must never juxtapose it with the live fingerprint (design D1, BLOCKER 3).
    return VerifyResult(
        verified=False,
        state=proof.state,
        block_height=proof.block_height,
        calendars=proof.calendars,
        inconclusive=True,
        message=combined.strip(),
    )


def _verify_via_explorer(
    ots_path: str | os.PathLike[str],
    digest: str,
    explorer_url: str,
    timeout: int,
) -> VerifyResult:
    """Explorer-backed verify: confirm the proof's Bitcoin attestation(s) against a block explorer.

    Parses the ``.ots`` with the OpenTimestamps library, checks the supplied ``digest`` is the file
    hash the proof commits to, then for each ``BitcoinBlockHeaderAttestation`` fetches the real
    block at that height and confirms the attestation's commitment equals the block's merkle root.
    The earliest confirmed block time is the "existed by" date.

    A disagreement between the supplied digest and the proof's committed digest sets
    ``digest_mismatch`` — a NEUTRAL finding (design D1): the two disagree, and this function cannot
    tell whether the file's bytes moved or the proof's serialized digest did, so it assigns no
    blame and the callers do (they hold the file's recorded baseline). A merkle mismatch with no
    validated attestation sets ``proof_mismatch`` (the proof or the explorer's block data is wrong —
    not evidence about the file); a proof that cannot be parsed sets ``unreadable_proof`` (nothing
    was established at all). Every fetch failure is accumulated into ``transport_error`` (counted in
    ``transport_failures``) and attached to whichever terminal result is returned, so an unreachable
    explorer is never a false "verified" and never a silent one either — and a *malformed* explorer
    response is a fetch failure too, never a mismatch.
    """
    # Imported lazily so the module stays importable (and the node path / tests stay network-free)
    # without the OpenTimestamps library present.
    from opentimestamps.core.notary import (
        BitcoinBlockHeaderAttestation,
        PendingAttestation,
    )
    from opentimestamps.core.serialize import StreamDeserializationContext
    from opentimestamps.core.timestamp import DetachedTimestampFile

    ots_path = Path(ots_path)
    if not ots_path.exists():
        return VerifyResult(verified=False, state="none", message=f"no usable proof at {ots_path}")
    try:
        with ots_path.open("rb") as fh:
            detached = DetachedTimestampFile.deserialize(StreamDeserializationContext(fh))
    except Exception as exc:  # malformed / truncated / not a timestamp file
        # Nothing was established — not about the file, not about the proof's content. Flagged so
        # the consumers can say exactly that instead of falling through to copy that offers
        # "the file may have changed, or the proof may not be confirmed yet" as possibilities.
        return VerifyResult(
            verified=False,
            state="none",
            unreadable_proof=True,
            message=f"unreadable proof: {exc}",
        )

    # The proof parsed, so the digest it commits to is now an established fact and travels on
    # every terminal result below. Callers that hold the row's recorded provenance (`ots_digest`)
    # use it to tell "this .ots is not the proof Cairn placed" from "the proof Cairn placed predates
    # this version" — a distinction that was previously undecidable (design D7).
    proof_digest = detached.file_digest.hex().lower()

    try:
        want = binascii.unhexlify(digest)
    except (binascii.Error, ValueError):
        return VerifyResult(
            verified=False, state="none", proof_digest=proof_digest, message="invalid digest"
        )

    pending: list[str] = []
    bitcoin: list[tuple[int, bytes]] = []  # (block height, committed digest = expected merkleroot)
    for msg, att in detached.timestamp.all_attestations():
        if isinstance(att, BitcoinBlockHeaderAttestation):
            bitcoin.append((att.height, msg))
        elif isinstance(att, PendingAttestation):
            pending.append(att.uri)

    if want != detached.file_digest:
        # The live digest and the digest this proof commits to DISAGREE. That is all this
        # comparison establishes: a `.ots` whose serialized `file_digest` had one byte flipped
        # deserializes perfectly and lands here just as a genuinely modified file does, and no
        # attestation has been validated at this point either. Blame needs a third data point this
        # module does not have — the file's recorded baseline digest — so it is assigned by the
        # callers (design D1), and the message stays neutral about which artifact moved.
        state = "complete" if bitcoin else ("incomplete" if pending else "none")
        return VerifyResult(
            verified=False,
            state=state,
            calendars=pending,
            digest_mismatch=True,
            proof_digest=proof_digest,
            message=(
                "the file's digest does not match the digest this proof commits to; "
                "the proof alone cannot say which of the two changed"
            ),
        )

    if not bitcoin:
        state = "incomplete" if pending else "none"
        return VerifyResult(
            verified=False,
            state=state,
            calendars=pending,
            proof_digest=proof_digest,
            message="proof is not yet anchored to Bitcoin",
        )

    api = explorer_url.rstrip("/") + "/api"
    best: tuple[int, int] | None = None  # (block time, height) of the earliest matching attestation
    mismatch = False
    errors: list[str] = []
    for height, msg in bitcoin:
        try:
            merkle_root, block_time = _fetch_block_merkleroot(api, height, timeout)
        except OtsError as exc:
            errors.append(str(exc))
            continue
        if merkle_root == msg:
            if best is None or block_time < best[0]:
                best = (block_time, height)
        else:
            mismatch = True

    # Computed once, before any return, and passed to all three terminal results below: a fetch
    # failure is never dropped because another outcome was decided first, and a later edit cannot
    # reintroduce a return that silently discards it (design D2).
    transport_error = "; ".join(errors) or None
    # The COUNT is carried structurally beside the joined text; recovering it by splitting the text
    # miscounts any single error that itself contains the separator (design D2 / MINOR 7).
    transport_failures = len(errors)

    # A validated attestation wins. OTS verification is *existential* — one attestation confirmed
    # against its real block IS the proof, and a proof may legitimately carry several. So a
    # mismatched sibling is diagnostic detail in `message`, never a verdict; testing `mismatch`
    # first (as this did) renders a red "this proof does not check out" over a genuinely anchored
    # proof, a false alarm on the core signal.
    if best is not None:
        block_time, height = best
        existed_by = datetime.datetime.fromtimestamp(
            block_time, datetime.timezone.utc
        ).strftime("%Y-%m-%d %H:%M UTC")
        message = f"Bitcoin block {height} attests existence as of {existed_by}"
        if mismatch:
            message += " (a sibling attestation did not match its block's merkle root)"
        return VerifyResult(
            verified=True,
            state="complete",
            block_height=height,
            existed_by=existed_by,
            calendars=pending,
            proof_digest=proof_digest,
            transport_error=transport_error,
            transport_failures=transport_failures,
            message=message,
        )

    if mismatch:
        # No attestation validated and at least one mismatched. The live digest matched (checked
        # above), so this blames the proof or the explorer's block data — never the file.
        return VerifyResult(
            verified=False,
            state="complete",
            calendars=pending,
            proof_mismatch=True,
            proof_digest=proof_digest,
            transport_error=transport_error,
            transport_failures=transport_failures,
            message="Bitcoin merkle root does not match the proof — the proof or the explorer's block data may be wrong (this is not evidence the file changed)",
        )

    return VerifyResult(
        verified=False,
        state="complete",
        calendars=pending,
        proof_digest=proof_digest,
        transport_error=transport_error,
        transport_failures=transport_failures,
        message=transport_error or "could not reach the block explorer",
    )


def _fetch_block_merkleroot(api: str, height: int, timeout: int) -> tuple[bytes, int]:
    """Return ``(merkle_root_internal_bytes, block_time)`` for the block at ``height``.

    Two esplora calls: the canonical block hash at the height, then that block's header. The
    explorer reports the merkle root in display (big-endian) hex; reverse it to the internal byte
    order an OTS ``BitcoinBlockHeaderAttestation`` commits to.

    **Every field is validated to be a well-formed block header value before it is returned.** The
    caller's only use for the merkle root is an equality test against an attestation's commitment,
    and an inequality there is reported as a *proof mismatch* — a red "this proof does not check
    out" over the operator's evidence. So a well-formed-but-wrong value and a malformed one must
    never both reach that comparison: ``{"merkle_root": "00"}`` is not a merkle root that differs,
    it is an explorer that answered badly. Anything malformed raises :class:`OtsError` and joins the
    accumulated transport failures, where "nothing was established" is the honest verdict.
    """
    block_hash = _http_get_text(f"{api}/block-height/{height}", timeout)
    if not re.fullmatch(r"[0-9a-fA-F]{64}", block_hash):
        raise OtsError(f"explorer returned no block at height {height}")
    block = _http_get_json(f"{api}/block/{block_hash}", timeout)
    raw_root = block.get("merkle_root") if isinstance(block, dict) else None
    if not isinstance(raw_root, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", raw_root):
        raise OtsError(
            f"explorer returned a malformed merkle root for block {height}: {raw_root!r}"
        )
    raw_time = block.get("timestamp")
    if isinstance(raw_time, bool):  # bool is an int subclass; never a block time
        raw_time = None
    if isinstance(raw_time, str) and raw_time.strip().isdigit():
        raw_time = int(raw_time)
    if not isinstance(raw_time, int) or not (_MIN_BLOCK_TIME <= raw_time <= _MAX_BLOCK_TIME):
        raise OtsError(
            f"explorer returned a malformed timestamp for block {height}: {block.get('timestamp')!r}"
        )
    return bytes.fromhex(raw_root)[::-1], raw_time


def _http_get(url: str, timeout: int) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "cairn-ots-verify"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except (urllib.error.URLError, OSError) as exc:
        raise OtsError(f"block explorer request failed ({url}): {exc}") from exc


def _http_get_text(url: str, timeout: int) -> str:
    return _http_get(url, timeout).decode("utf-8", "replace").strip()


def _http_get_json(url: str, timeout: int) -> dict:
    try:
        return json.loads(_http_get(url, timeout))
    except json.JSONDecodeError as exc:
        raise OtsError(f"block explorer returned non-JSON ({url}): {exc}") from exc
