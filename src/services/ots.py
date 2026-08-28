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
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

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


def _place_proof(staged_ots: Path, out_ots_path: Path) -> None:
    """Move a produced staging proof to its final ``out_ots_path`` (creating parent dirs first).

    This is the one runtime place a permanent verdict is reached, and it is legitimate here because
    the operand IS the final output path: its location is fully determined by the file's relpath, so
    even a whole-path (PATH_MAX) overflow is permanent for this member. Only a **permanent** refusal
    — ENAMETOOLONG — is re-raised as :class:`OtsPathError`, so the caller skips just this one
    file and never re-attempts it. Every other ``OSError`` (a full or read-only proof store, a
    cross-device staging dir, an I/O error) is **transient**: it is re-raised as a generic
    :class:`OtsError` so the caller leaves the file ``pending`` for retry rather than silently
    dropping a proof it could take later. The rest of a batch is unaffected either way.
    """
    try:
        out_ots_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged_ots, out_ots_path)
    except OSError as exc:
        if exc.errno == errno.ENAMETOOLONG:
            raise OtsPathError(f"cannot write proof to {out_ots_path!r}: {exc}") from exc
        raise OtsError(f"failed to place proof at {out_ots_path!r}: {exc}") from exc


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
) -> Path:
    """Stamp ``real_path`` and place the proof at ``out_ots_path`` without writing beside it.

    ``ots stamp`` writes ``<input>.ots`` next to its input and has no output flag, but collection
    files live on a read-only mount. So we symlink ``staging_dir/<uuid>`` -> ``real_path``, stamp
    the symlink (``ots`` reads the real bytes, writes ``<uuid>.ots`` in the writable staging dir),
    then atomically move that ``.ots`` to ``out_ots_path`` and remove the symlink.

    ``store_root`` is the proof-store root: only the components below it (the ones Cairn creates)
    are pre-checked for the per-name byte limit. Staging-side failures are always transient.
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
        _place_proof(staged_ots, out_ots_path)
    finally:
        for stray in created:
            # Best-effort: cleanup must never mask or replace the classified exception.
            with contextlib.suppress(OSError):
                stray.unlink()
    return out_ots_path


def stamp_batch_via_symlink(
    items: list[tuple[str | os.PathLike[str], str | os.PathLike[str]]],
    calendars: list[str],
    staging_dir: str | os.PathLike[str],
    timeout: int = DEFAULT_TIMEOUT,
    *,
    store_root: str | os.PathLike[str] | None = None,
) -> list[bool]:
    """Stamp many files in ONE ``ots stamp`` call; return per-item success aligned with ``items``.

    ``items`` is a list of ``(real_path, out_ots_path)``. One staging symlink is built per item and
    ``ots stamp <link1> … <linkN>`` is invoked once: OpenTimestamps aggregates the N digests into a
    single calendar commitment, yet still writes an independent ``<linkI>.ots`` per input. Each
    produced proof is moved to its ``out_ots_path``.

    Success is decided by *filesystem truth* — whether each ``<linkI>.ots`` was actually produced —
    not by the process exit code, so a whole-batch failure, a timeout, or one unreadable file
    aborting the run leaves the unaffected members stamped and the rest reported ``False`` for the
    caller to retry individually. Links and stray ``.ots`` are always cleaned up (best-effort) in
    ``finally``. ``store_root`` bounds the output-path pre-check to the components Cairn creates.
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
    results = [False] * len(pairs)
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
                continue  # unwritable output name — skipped, results[i] stays False
            _link, staged_ots = entry
            if staged_ots.exists():
                try:
                    _place_proof(staged_ots, out)
                    results[i] = True
                except OtsError:
                    # Could not place this proof — an unwritable path the pre-check did not model, or
                    # a transient store error (full / read-only). Leave ``False`` so the single-file
                    # fallback re-raises the right class and the caller classifies it permanent vs.
                    # transient; the staged proof is cleaned up below. One bad member never aborts the
                    # batch.
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
        return VerifyResult(
            verified=False,
            state="none",
            calendars=proof.calendars,
            message=f"no usable proof at {ots_path}",
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

    try:
        want = binascii.unhexlify(digest)
    except (binascii.Error, ValueError):
        return VerifyResult(verified=False, state="none", message="invalid digest")

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
            transport_error=transport_error,
            transport_failures=transport_failures,
            message="Bitcoin merkle root does not match the proof — the proof or the explorer's block data may be wrong (this is not evidence the file changed)",
        )

    return VerifyResult(
        verified=False,
        state="complete",
        calendars=pending,
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
