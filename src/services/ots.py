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
"""

from __future__ import annotations

import binascii
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
    """The proof output path cannot be written — e.g. its final component exceeds the filesystem's
    per-name byte limit (ENAMETOOLONG), or the destination is otherwise un-writable.

    Distinct from a transient failure (an unreachable calendar, a timeout): a path a filesystem
    refuses will never succeed on retry, so callers skip-and-count the one file instead of leaving
    it ``pending`` to re-attempt — and re-flood — on every subsequent scan.
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
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _is_pending(text: str) -> bool:
    low = text.lower()
    return any(marker in low for marker in _PENDING_MARKERS)


def _proof_output_writable(out_ots_path: Path) -> bool:
    """Whether ``out_ots_path``'s final component fits the filesystem's per-name byte limit.

    The *byte* length is what matters (NAME_MAX is bytes, not characters): a short-looking multi-byte
    name — a Cyrillic filename plus its extension plus ``.ots`` — can still exceed it. This is a cheap
    pre-check so an un-writable proof is skipped before a symlink or a calendar round-trip is spent on
    it; :func:`_place_proof` is the authoritative backstop for any limit this pre-check does not model
    (a smaller NAME_MAX, name-inflating filesystems like eCryptfs, a too-long parent component).
    """
    try:
        return len(os.fsencode(out_ots_path.name)) <= _NAME_MAX_BYTES
    except (ValueError, TypeError):  # pragma: no cover - defensive
        return False


def _place_proof(staged_ots: Path, out_ots_path: Path) -> None:
    """Move a produced staging proof to its final ``out_ots_path`` (creating parent dirs first).

    Only a **permanent** refusal — ENAMETOOLONG, when a path component exceeds the filesystem's
    per-name byte limit — is re-raised as :class:`OtsPathError`, so the caller skips just this one
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
) -> Path:
    """Stamp ``real_path`` and place the proof at ``out_ots_path`` without writing beside it.

    ``ots stamp`` writes ``<input>.ots`` next to its input and has no output flag, but collection
    files live on a read-only mount. So we symlink ``staging_dir/<uuid>`` -> ``real_path``, stamp
    the symlink (``ots`` reads the real bytes, writes ``<uuid>.ots`` in the writable staging dir),
    then atomically move that ``.ots`` to ``out_ots_path`` and remove the symlink.
    """
    real_path = Path(real_path)
    out_ots_path = Path(out_ots_path)
    staging_dir = Path(staging_dir)
    # Fail fast on an un-writable proof name before spending a symlink or a calendar round-trip.
    if not _proof_output_writable(out_ots_path):
        raise OtsPathError(
            f"proof output name too long to store "
            f"({len(os.fsencode(out_ots_path.name))} bytes > {_NAME_MAX_BYTES}): {out_ots_path!r}"
        )
    staging_dir.mkdir(parents=True, exist_ok=True)

    link = staging_dir / uuid.uuid4().hex
    staged_ots = link.with_name(link.name + ".ots")
    try:
        link.symlink_to(real_path)
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
        for stray in (link, staged_ots):
            try:
                stray.unlink()
            except FileNotFoundError:
                pass
    return out_ots_path


def stamp_batch_via_symlink(
    items: list[tuple[str | os.PathLike[str], str | os.PathLike[str]]],
    calendars: list[str],
    staging_dir: str | os.PathLike[str],
    timeout: int = DEFAULT_TIMEOUT,
) -> list[bool]:
    """Stamp many files in ONE ``ots stamp`` call; return per-item success aligned with ``items``.

    ``items`` is a list of ``(real_path, out_ots_path)``. One staging symlink is built per item and
    ``ots stamp <link1> … <linkN>`` is invoked once: OpenTimestamps aggregates the N digests into a
    single calendar commitment, yet still writes an independent ``<linkI>.ots`` per input. Each
    produced proof is moved to its ``out_ots_path``.

    Success is decided by *filesystem truth* — whether each ``<linkI>.ots`` was actually produced —
    not by the process exit code, so a whole-batch failure, a timeout, or one unreadable file
    aborting the run leaves the unaffected members stamped and the rest reported ``False`` for the
    caller to retry individually. Links and stray ``.ots`` are always cleaned up in ``finally``.
    """
    pairs = [(Path(real), Path(out)) for real, out in items]
    if not pairs:
        return []
    staging_dir = Path(staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)

    # Parallel to ``pairs``: (symlink, staged .ots) for a member we submit, or ``None`` for one whose
    # proof output name can't be written — it is neither symlinked nor sent to the calendar. Such a
    # member stays ``False``; the caller's single-file fallback re-checks it and records it as a
    # permanent skip rather than re-attempting it forever.
    links: list[tuple[Path, Path] | None] = []
    results = [False] * len(pairs)
    try:
        for real, out in pairs:
            if not _proof_output_writable(out):
                links.append(None)
                continue
            link = staging_dir / uuid.uuid4().hex
            link.symlink_to(real)
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
                try:
                    stray.unlink()
                except FileNotFoundError:
                    pass
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
    proof = info(ots_path)
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
    rc, out, err = _run_ots(args, timeout=timeout)
    combined = f"{out}\n{err}"
    match = _VERIFY_SUCCESS_RE.search(combined)
    if match:
        return VerifyResult(
            verified=True,
            state="complete",
            block_height=int(match.group(1)),
            existed_by=match.group(2).strip(),
            calendars=proof.calendars,
            message=combined.strip(),
        )
    # No success line: either still pending (valid, just not verifiable yet) or a real failure.
    return VerifyResult(
        verified=False,
        state=proof.state,
        block_height=proof.block_height,
        calendars=proof.calendars,
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
    The earliest confirmed block time is the "existed by" date. A merkle mismatch means the file or
    proof was altered (not-verified); an unreachable explorer is reported as not-verified with the
    network error (never a false "verified").
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
        return VerifyResult(verified=False, state="none", message=f"unreadable proof: {exc}")

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
        # The file no longer hashes to what the proof stamped → the proof doesn't cover these bytes.
        state = "complete" if bitcoin else ("incomplete" if pending else "none")
        return VerifyResult(
            verified=False,
            state=state,
            calendars=pending,
            message="file digest does not match the stamped proof (file changed since stamping)",
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

    if mismatch:
        return VerifyResult(
            verified=False,
            state="complete",
            calendars=pending,
            message="Bitcoin merkle root does not match the proof — the file or proof may be altered",
        )
    if best is None:
        return VerifyResult(
            verified=False,
            state="complete",
            calendars=pending,
            message="; ".join(errors) or "could not reach the block explorer",
        )

    block_time, height = best
    existed_by = datetime.datetime.fromtimestamp(
        block_time, datetime.timezone.utc
    ).strftime("%Y-%m-%d %H:%M UTC")
    return VerifyResult(
        verified=True,
        state="complete",
        block_height=height,
        existed_by=existed_by,
        calendars=pending,
        message=f"Bitcoin block {height} attests existence as of {existed_by}",
    )


def _fetch_block_merkleroot(api: str, height: int, timeout: int) -> tuple[bytes, int]:
    """Return ``(merkle_root_internal_bytes, block_time)`` for the block at ``height``.

    Two esplora calls: the canonical block hash at the height, then that block's header. The
    explorer reports the merkle root in display (big-endian) hex; reverse it to the internal byte
    order an OTS ``BitcoinBlockHeaderAttestation`` commits to.
    """
    block_hash = _http_get_text(f"{api}/block-height/{height}", timeout)
    if not re.fullmatch(r"[0-9a-fA-F]{64}", block_hash):
        raise OtsError(f"explorer returned no block at height {height}")
    block = _http_get_json(f"{api}/block/{block_hash}", timeout)
    try:
        merkle_root = bytes.fromhex(block["merkle_root"])[::-1]
        block_time = int(block["timestamp"])
    except (KeyError, ValueError, TypeError) as exc:
        raise OtsError(f"explorer returned a malformed block header: {exc}") from exc
    return merkle_root, block_time


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
