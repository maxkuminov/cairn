"""The ``cairn`` command-line entrypoint.

``init`` and ``serve`` (foundation), ``scan``, ``accept``, and ``add-collection`` (scanner),
``verify``, ``export``, ``upgrade``, and ``stamp`` (notary), ``import-manifest`` (manifest baseline
import), and ``bench`` (hash-throughput / deep-scan estimate) are functional. ``status`` is stubbed
until the web-panel change.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from . import __version__

# Subcommands whose implementation lands in later OpenSpec changes.
PLANNED = ("status",)


def _run(coro) -> int:
    return asyncio.run(coro)


async def _implicit_user_id(session) -> int:
    """Ensure (single mode) and return the implicit owner's id."""
    from sqlalchemy import select

    from .database import ensure_implicit_user
    from .models.db import User

    await ensure_implicit_user(session)
    return await session.scalar(select(User.id).order_by(User.id).limit(1))


def _cmd_init(args: argparse.Namespace) -> int:
    from .database import ensure_dirs, run_migrations

    ensure_dirs()
    run_migrations()
    print("Cairn initialized: data dir + proof store created, database migrated to head.")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .config import get_settings

    settings = get_settings()
    uvicorn.run(
        "src.main:app",
        host=args.host or settings.host,
        port=args.port or settings.port,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
    return 0


def _cmd_add_collection(args: argparse.Namespace) -> int:
    async def run() -> int:
        from .database import ensure_dirs, get_sessionmaker
        from .services.collections import create_collection

        ensure_dirs()
        async with get_sessionmaker()() as session:
            uid = await _implicit_user_id(session)
            collection = await create_collection(
                session,
                user_id=uid,
                name=args.name,
                root=args.root,
                mode=args.mode,
                ots_mode=args.ots_mode,
                hash_cadence_seconds=args.cadence,
                verify_cadence_seconds=args.verify_cadence,
                auto_baseline_new=args.auto_baseline,
                exclude_globs=args.exclude or [],
            )
            print(
                f"Created collection #{collection.id}: {collection.name} -> {collection.root} "
                f"(mode={collection.mode}, ots={collection.ots_mode}, cadence={collection.hash_cadence_seconds}s, "
                f"verify_cadence={collection.verify_cadence_seconds}s, "
                f"auto_baseline_new={collection.auto_baseline_new})"
            )
        return 0

    try:
        return _run(run())
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _cmd_scan(args: argparse.Namespace) -> int:
    async def run() -> int:
        from .database import get_sessionmaker
        from .services.collections import blocking_run, get_collection_by_name, list_collections
        from .services.scanner import scan_collection

        async with get_sessionmaker()() as session:
            await _implicit_user_id(session)
            if args.collection:
                collection = await get_collection_by_name(session, args.collection)
                if collection is None:
                    print(f"no such collection: {args.collection}", file=sys.stderr)
                    return 1
                collections = [collection]
            else:
                collections = await list_collections(session)
            if not collections:
                print("no collections configured (use: cairn add-collection).")
                return 0
            rc = 0
            examined = 0  # collections this invocation actually scanned
            skipped_names: list[str] = []
            for collection in collections:
                # A refused scan rolls back the claim, expiring the ORM object — so the name the
                # refusal line prints is read before the call, not after it.
                name = collection.name
                # Advisory pre-check, mirroring the scheduler and the panel routes: it reports the
                # refusal without entering the scan at all. `scan_collection`'s own `claim_run` is
                # still the race-free authority, and its `result='skipped'` is handled identically
                # below — this is a cheaper, clearer path to the same message for the common case.
                # `blocking_run`, not `active_run`: the gate must also release a claim abandoned by
                # a killed process, or a CLI-only deployment (no scheduler, no web startup, so no
                # reaper ever runs) would refuse this collection's scans for good.
                if await blocking_run(session, collection.id) is not None:
                    skipped_names.append(name)
                    continue
                s = await scan_collection(session, collection)
                if s.result == "skipped":
                    skipped_names.append(name)
                    continue
                examined += 1
                # A restored-changed file is ALSO counted in `modified` (its status is `modified`),
                # so the line would otherwise report a wrong restore as an ordinary edit. Appended
                # only when non-zero, so the ordinary line stays byte-identical.
                came_back = (
                    f" restored_changed={s.restored_changed}" if s.restored_changed else ""
                )
                print(
                    f"[{name}] added={s.added} modified={s.modified} "
                    f"missing={s.missing} restored={s.restored}{came_back} "
                    f"baselined={s.baselined} "
                    f"ok={s.ok} errors={s.errors} -> {s.result}"
                )
                if s.result == "error":
                    rc = 1
            for name in skipped_names:
                # The collection's single-operation slot was held (design D10), so nothing was
                # walked, hashed or classified. The ordinary result line would print all zeroes,
                # which reads as a clean integrity pass over a collection this run never looked at —
                # the exact false negative a cron job must never record.
                print(
                    f"[{name}] SKIPPED — an operation is already in progress for this "
                    f"collection; nothing was scanned. Re-run when it finishes.",
                    file=sys.stderr,
                )
            if examined == 0:
                # Every requested collection was refused: this run examined nothing, so it must not
                # exit 0 and let a scheduler record a successful integrity check. One busy
                # collection among several, by contrast, is a success that names the skip.
                print(
                    "no collection was scanned — every requested collection already had an "
                    "operation in progress",
                    file=sys.stderr,
                )
                return 1
            return rc

    return _run(run())


def _cmd_accept(args: argparse.Namespace) -> int:
    async def run() -> int:
        from .database import get_sessionmaker
        from .services.collections import get_collection_by_name, list_collections
        from .services.scanner import accept_collection

        async with get_sessionmaker()() as session:
            uid = await _implicit_user_id(session)
            if args.collection:
                collection = await get_collection_by_name(session, args.collection)
                if collection is None:
                    print(f"no such collection: {args.collection}", file=sys.stderr)
                    return 1
                collections = [collection]
            else:
                collections = await list_collections(session)
            for collection in collections:
                r = await accept_collection(session, collection, uid)
                print(
                    f"[{collection.name}] accepted={r['accepted']} removed_missing={r['removed']} "
                    f"events_acknowledged={r['events_ack']}"
                )
        return 0

    return _run(run())


def _cmd_import_manifest(args: argparse.Namespace) -> int:
    async def run() -> int:
        from pathlib import Path

        from .database import ensure_dirs, get_sessionmaker
        from .services.collections import get_collection_by_name
        from .services.manifest import import_manifest

        path = Path(args.file)
        if not path.is_file():
            print(f"no such file: {args.file}", file=sys.stderr)
            return 1

        ensure_dirs()
        async with get_sessionmaker()() as session:
            uid = await _implicit_user_id(session)
            collection = await get_collection_by_name(session, args.collection, uid)
            if collection is None:
                print(f"no such collection: {args.collection}", file=sys.stderr)
                return 1
            result = await import_manifest(session, collection, path, rehash=args.rehash)
            print(
                f"[{collection.name}] imported={result.imported} updated={result.updated} "
                f"skipped={result.skipped}"
            )
            if args.rehash:
                for relpath, manifest_hash, actual_hash in result.mismatches:
                    print(
                        f"  MISMATCH {relpath}: manifest={manifest_hash} actual={actual_hash}",
                        file=sys.stderr,
                    )
                for relpath in result.missing:
                    print(f"  MISSING {relpath}", file=sys.stderr)
                if result.mismatches:
                    return 1
        return 0

    return _run(run())


async def _resolve_collection(session, name: str | None):
    """Resolve a collection by name, or the single configured collection when name is omitted."""
    from .services.collections import get_collection_by_name, list_collections

    if name:
        collection = await get_collection_by_name(session, name)
        if collection is None:
            print(f"no such collection: {name}", file=sys.stderr)
        return collection
    collections = await list_collections(session)
    if not collections:
        print("no collections configured (use: cairn add-collection).", file=sys.stderr)
        return None
    if len(collections) > 1:
        print("multiple collections exist; pass --collection NAME.", file=sys.stderr)
        return None
    return collections[0]


async def _find_file(session, collection_id: int, relpath: str):
    from sqlalchemy import select

    from .models.db import FileEntry

    return await session.scalar(
        select(FileEntry).where(
            FileEntry.collection_id == collection_id, FileEntry.relpath == relpath
        )
    )


def _cmd_verify(args: argparse.Namespace) -> int:
    async def run() -> int:
        from pathlib import Path

        from .config import get_settings
        from .database import get_sessionmaker
        from .services import ots
        from .services.scanner import sha256_file

        async with get_sessionmaker()() as session:
            await _implicit_user_id(session)
            collection = await _resolve_collection(session, args.collection)
            if collection is None:
                return 1
            entry = await _find_file(session, collection.id, args.relpath)
            if entry is None:
                print(f"no such file in collection: {args.relpath}", file=sys.stderr)
                return 1
            if not entry.ots_path or entry.ots_state == "none":
                print(f"[{args.relpath}] not stamped")
                return 1

            # Re-hash from the read-only store. We MUST verify the proof against the *live* bytes —
            # never fall back to the recorded digest, or a deleted/unreadable file would trivially
            # "verify" (the proof was built over that exact digest): the worst false assurance an
            # integrity tool can give.
            source = Path(collection.root) / entry.relpath
            if not source.is_file():
                print(
                    f"[{args.relpath}] UNAVAILABLE — file is missing from disk; "
                    f"cannot verify against live bytes",
                    file=sys.stderr,
                )
                return 1
            try:
                digest = sha256_file(source)
            except OSError as exc:
                print(
                    f"[{args.relpath}] UNAVAILABLE — file is unreadable ({exc.strerror or exc}); "
                    f"cannot verify against live bytes",
                    file=sys.stderr,
                )
                return 1
            if not digest:
                print(f"[{args.relpath}] no digest available to verify", file=sys.stderr)
                return 1

            settings = get_settings()
            result = ots.verify(
                entry.ots_path,
                digest,
                backend=settings.verify_backend,
                explorer_url=settings.explorer_url,
                node_rpc_url=settings.node_rpc_url,
            )
            # Branch by *reason*, in the same order as the panel's verdict chain (design D2).
            # `VerifyResult` has two consumers; reading the proof's lifecycle state before asking
            # why verification failed is what printed "pending" for a file whose bytes had changed.
            n = ots.failed_lookup_count(result)
            note = (
                f"  {n} attestation lookup{'' if n == 1 else 's'} failed; "
                f"the verdict is based on the attestations reached"
                if n
                else None
            )

            # A digest disagreement establishes only that the live digest and the digest the proof
            # commits to DIFFER (design D1). The tiebreaker is the baseline recorded for this file
            # at its last scan: live bytes that no longer hash to it mean the FILE changed; live
            # bytes that still do mean the file is not what moved. Blaming the file for a bad
            # `.ots` is a false alarm on the product's core signal, so the two get different lines
            # — and neither claims the proof or its attestation was validated, because at this
            # point nothing validated either.
            #
            # `files.ots_digest` — the digest the proof CAIRN PLACED at `ots_path` commits to —
            # is what makes the attribution provable rather than heuristic (design D7). The ladder
            # is ORDERED, and the order is the correctness content: "this .ots is not the proof
            # Cairn recorded placing" is asked BEFORE any staleness reading. Evaluating staleness
            # first produces the A/B/C false reassurance — recorded provenance A, live == baseline
            # B, an on-disk proof committing to a third digest C — where `ots_digest != live` holds
            # and the card would say "simply an older proof of your file" about an `.ots` that is
            # neither the recorded proof nor this file's proof. The staleness reading is reachable
            # only once the on-disk proof has been shown to commit to exactly the recorded
            # provenance — and even then it establishes only WHICH DIGEST that `.ots` commits to.
            # Rows with no recorded provenance keep sprint 1's heuristic and its explicit
            # undecidability, verbatim.
            # Mirrors the panel's `mismatch_blame` — the two surfaces must never disagree.
            stored_sha = (entry.sha256 or "").strip().lower()
            live_sha = (digest or "").strip().lower()
            recorded_proof_sha = (entry.ots_digest or "").strip().lower()
            parsed_proof_sha = (result.proof_digest or "").strip().lower()
            if result.digest_mismatch and live_sha and stored_sha and live_sha != stored_sha:
                print(
                    f"[{args.relpath}] CHANGED — the live bytes differ from the fingerprint Cairn "
                    f"recorded for this file AND from the digest this proof commits to; the file "
                    f"has changed since it was stamped. The proof itself was not checked here",
                    file=sys.stderr,
                )
                return 1
            # Provenance branch 1 (D7 row 3), evaluated FIRST: the `.ots` on disk is not the
            # proof Cairn recorded placing. Established, not guessed.
            if (
                result.digest_mismatch
                and live_sha
                and stored_sha
                and recorded_proof_sha
                and parsed_proof_sha
                and parsed_proof_sha != recorded_proof_sha
            ):
                print(
                    f"[{args.relpath}] THIS IS NOT THE PROOF CAIRN PLACED — the file matches its "
                    f"recorded baseline, but the .ots stored for it commits to "
                    f"{parsed_proof_sha}, not to {recorded_proof_sha}, the digest Cairn recorded "
                    f"placing a proof for. The proof file is corrupted, swapped or misfiled. This "
                    f"says nothing against the file's bytes, which match their baseline",
                    file=sys.stderr,
                )
                return 1
            # D7 row 4: Cairn recorded placing a proof for exactly THESE bytes, and the stored proof
            # disagrees with them. The same conclusion, reachable without a parsed proof digest.
            if result.digest_mismatch and live_sha and stored_sha and recorded_proof_sha == live_sha:
                print(
                    f"[{args.relpath}] THIS IS NOT THE PROOF CAIRN PLACED — Cairn recorded placing "
                    f"a proof for exactly these bytes ({recorded_proof_sha}), and the .ots stored "
                    f"at that path commits to something else. The proof file is corrupted, swapped "
                    f"or misfiled. This says nothing against the file's bytes, which match their "
                    f"baseline",
                    file=sys.stderr,
                )
                return 1
            # D7 row 5: the `.ots` on disk commits to exactly the digest Cairn recorded for the
            # proof it placed here — this file's PREVIOUSLY recorded fingerprint. That, and only
            # that, is what the comparison establishes: digest equality is not artifact identity
            # (any `.ots` over the same earlier bytes commits to the same digest), and verification
            # exits on the digest disagreement before any attestation is checked, so nothing about
            # Bitcoin was validated. The line claims neither.
            if (
                result.digest_mismatch
                and live_sha
                and stored_sha
                and recorded_proof_sha
                and recorded_proof_sha != live_sha
                and parsed_proof_sha == recorded_proof_sha
            ):
                # From the proof state alone, never from the status: a `perfile` collection
                # switched to `ots_mode="none"` after a modification stays `modified` with nothing
                # queued, and promising a re-stamp there is a promise nothing will keep.
                pending_clause = (
                    "; a re-stamp is queued, and once it runs the contents on disk today will get "
                    "their own proof"
                    if entry.ots_state == "pending"
                    else ""
                )
                print(
                    f"[{args.relpath}] PROOF COMMITS TO THE PREVIOUSLY RECORDED FINGERPRINT — the "
                    f"file matches its current recorded baseline, and the .ots stored for it "
                    f"commits to {recorded_proof_sha}, the fingerprint Cairn previously recorded "
                    f"for this file, not to the current one. That is all this check established: "
                    f"the proof's Bitcoin attestations were not validated here{pending_clause}. "
                    f"This is NOT evidence against the current file",
                    file=sys.stderr,
                )
                return 1
            # D7 row 7 (legacy, `ots_digest` NULL) — sprint 1's heuristic, unchanged. Row 6 (
            # provenance recorded but nothing parsed) deliberately falls through to here too: with
            # no parsed digest neither "swapped" nor "stale" is established, and asserting staleness
            # would be the A/B/C error with the evidence merely absent instead of contradictory.
            if (
                result.digest_mismatch
                and live_sha
                and stored_sha
                and not recorded_proof_sha
                and (entry.ots_state == "pending" or entry.status in ("modified", "new"))
            ):
                print(
                    f"[{args.relpath}] PROOF PREDATES THIS VERSION — the file matches its current "
                    f"recorded baseline, but the stored proof commits to different bytes; the "
                    f"proof predates this version of the file and a re-stamp is still pending. "
                    f"Cairn has no record of which fingerprint the proof it placed here commits "
                    f"to, and did not validate that proof's Bitcoin attestations here. "
                    f"This is NOT evidence against the current file",
                    file=sys.stderr,
                )
                return 1
            if result.digest_mismatch and live_sha and stored_sha:
                print(
                    f"[{args.relpath}] PROOF DOES NOT MATCH THIS FILE — the stored proof commits "
                    f"to a different digest than this file's recorded baseline; the proof may be "
                    f"from an earlier version of this file, or it may be corrupted. Cairn cannot "
                    f"tell which without per-proof records, so neither the file nor the proof is "
                    f"blamed here",
                    file=sys.stderr,
                )
                return 1
            if result.digest_mismatch:
                print(
                    f"[{args.relpath}] FINGERPRINT AND PROOF DISAGREE — the file's fingerprint is "
                    f"not the digest this proof commits to, and Cairn has no recorded baseline for "
                    f"this file to tell which of the two moved",
                    file=sys.stderr,
                )
                return 1
            if result.verified:
                # The block/date are OPTIONAL metadata: the node backend verifies on the process
                # exit status, and a successful exit whose output could not be parsed is still a
                # verification. Print what is known, claim nothing that is not.
                if result.block_height is not None and result.existed_by:
                    print(
                        f"[{args.relpath}] VERIFIED — Bitcoin block {result.block_height}, "
                        f"existed by {result.existed_by}"
                    )
                else:
                    print(
                        f"[{args.relpath}] VERIFIED — confirmed against the Bitcoin record "
                        f"(the backend reported no block details)"
                    )
                # Disclosure survives losing the verdict: a transport failure under a verdict that
                # outranks it is still printed, and never changes the exit status.
                if note:
                    print(note)
                return 0
            if result.proof_mismatch:
                print(
                    f"[{args.relpath}] PROOF DOES NOT CHECK OUT — a Bitcoin attestation does not "
                    f"match the block it points at; the proof (or the explorer's block data) may "
                    f"be wrong. This is NOT evidence the file changed — its fingerprint still "
                    f"matches what the proof records",
                    file=sys.stderr,
                )
                if note:
                    print(f"{note} (the mismatch is established only over those)", file=sys.stderr)
                return 1
            if result.transport_error:
                print(
                    f"[{args.relpath}] COULD NOT CHECK — {result.transport_error}; nothing was "
                    f"established about the file, retry when the backend is reachable",
                    file=sys.stderr,
                )
                return 1
            if result.inconclusive:
                # Deliberately prints no block height or date. The proof declares one, but nothing
                # confirmed it, and printing it beside the live fingerprint would assert that this
                # fingerprint is recorded in that block — the exact false claim design D1 forbids.
                print(
                    f"[{args.relpath}] INCONCLUSIVE — the proof is not yet confirmed, OR the file "
                    f"no longer matches it, OR the Bitcoin node could not be reached; the "
                    f"bitcoin-node backend cannot tell these apart",
                    file=sys.stderr,
                )
                return 1
            if result.unreadable_proof:
                print(
                    f"[{args.relpath}] UNREADABLE PROOF — the stored .ots could not be read or "
                    f"parsed ({result.message}); no conclusion was reached about the file. "
                    f"Re-stamp it to make a fresh proof",
                    file=sys.stderr,
                )
                return 1
            if result.state == "incomplete":
                # Two states, two lines (design D13): `incomplete` is submitted and awaiting
                # Bitcoin; `pending` was never submitted, so it must not read as awaiting anything.
                print(
                    f"[{args.relpath}] pending confirmation "
                    f"(submitted to a calendar, awaiting Bitcoin confirmation)"
                )
            elif result.state == "pending":
                print(f"[{args.relpath}] queued to stamp — not yet submitted to a calendar")
            else:
                print(f"[{args.relpath}] NOT VERIFIED — {result.message or result.state}")
            return 1

    return _run(run())


def _cmd_export(args: argparse.Namespace) -> int:
    async def run() -> int:
        from pathlib import Path

        from .database import get_sessionmaker
        from .services.proofs import export_bundle

        async with get_sessionmaker()() as session:
            await _implicit_user_id(session)
            collection = await _resolve_collection(session, args.collection)
            if collection is None:
                return 1
            entry = await _find_file(session, collection.id, args.relpath)
            if entry is None:
                print(f"no such file in collection: {args.relpath}", file=sys.stderr)
                return 1
            dest_dir = Path(args.out or ".")
            try:
                dest_file = export_bundle(entry, dest_dir, collection.root)
            except FileNotFoundError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
            print(f"exported {dest_file} and {dest_file}.ots")
            return 0

    return _run(run())


def _cmd_upgrade(args: argparse.Namespace) -> int:
    """Upgrade incomplete proofs, one collection at a time, each under its own operation claim.

    Proof mutation is single-writer per collection (design D10). This used to call
    ``upgrade_incomplete(session)`` fleet-wide with no run row at all, so it could run a second
    writer straight over a live scan or stamp. Each collection now claims a ``kind='upgrade'`` run,
    a collection whose slot is held is skipped **and named**, and nothing ever waits: blocking would
    turn a cron ``cairn upgrade`` into an unbounded stall behind a multi-hour deep scan, and the work
    is idempotent so the next invocation picks it up.

    This is also the operator's handle on the proof-location healing sweep (GitHub #39): the same
    pass that upgrades proofs relocates a moved file's proof to its current path's canonical
    location and restores a recorded proof that has gone missing from the store. A collection with
    only that kind of work — no incomplete proofs at all, tripwire mode included — is claimed and
    swept exactly like any other.
    """

    async def run() -> int:
        from .config import get_settings
        from .database import get_sessionmaker
        from .services.collections import list_collections
        from .services.proofs import stale_incomplete, upgrade_collection

        settings = get_settings()
        async with get_sessionmaker()() as session:
            await _implicit_user_id(session)
            collections = await list_collections(session)
            if not collections:
                print("no collections configured (use: cairn add-collection).")
                return 0
            upgraded = still = 0
            relocated = restored = deferred = refused_proofs = 0
            processed = 0  # collections this invocation actually upgraded or found idle
            refused: list[str] = []
            for collection in collections:
                name = collection.name  # read before a refusal can expire the ORM object
                outcome = await upgrade_collection(session, collection, settings)
                if outcome.refused:
                    refused.append(name)
                    continue
                processed += 1
                upgraded += outcome.upgraded
                still += outcome.still_incomplete
                relocated += outcome.sweep.relocated + outcome.sweep.parked
                restored += outcome.sweep.restored
                deferred += outcome.sweep.deferred
                refused_proofs += outcome.sweep.refused
            line = f"upgraded={upgraded} still_incomplete={still}"
            if relocated or restored or deferred or refused_proofs:
                # Only when the healing sweep actually did something, so the ordinary daily line
                # keeps its shape. The WARNINGs naming each row are the detail; this is the tally.
                line += (
                    f" proofs_relocated={relocated} proofs_restored={restored} "
                    f"proofs_deferred={deferred} proofs_not_healed={refused_proofs}"
                )
            print(line)
            for name in refused:
                print(
                    f"[{name}] SKIPPED — an operation is already in progress for this collection.",
                    file=sys.stderr,
                )
            stale = await stale_incomplete(session, settings.incomplete_proof_alarm_days)
            if stale:
                print(
                    f"WARNING: {len(stale)} proof(s) stuck incomplete past "
                    f"{settings.incomplete_proof_alarm_days} days:",
                    file=sys.stderr,
                )
                for entry in stale:
                    print(f"  - collection {entry.collection_id}: {entry.relpath}", file=sys.stderr)
            if processed == 0:
                # Nothing was upgraded because every collection was busy — a cron run that did no
                # work must be visible as a failure. A run that processed some and skipped others is
                # a success that names the skips.
                print(
                    "no collection was upgraded — every collection already had an operation in "
                    "progress",
                    file=sys.stderr,
                )
                return 1
        return 0

    return _run(run())


_SIZE_SUFFIXES = (
    ("kib", 1024), ("mib", 1024**2), ("gib", 1024**3), ("tib", 1024**4),
    ("kb", 1000), ("mb", 1000**2), ("gb", 1000**3), ("tb", 1000**4),
    ("k", 1024), ("m", 1024**2), ("g", 1024**3), ("t", 1024**4), ("b", 1),
)


def _parse_size(raw: str) -> int:
    """Parse a byte count with an optional binary/decimal suffix (``256MiB``, ``1G``, ``5000``)."""
    s = raw.strip().lower()
    mult = 1
    for suffix, m in _SIZE_SUFFIXES:
        if s.endswith(suffix):
            mult, s = m, s[: -len(suffix)]
            break
    return int(float(s) * mult)


def _human_bytes(n: float) -> str:
    f = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if f < 1024 or unit == "PiB":
            return f"{f:.0f} {unit}" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} PiB"  # pragma: no cover


def _human_duration(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


async def _bench_estimate(bytes_per_sec: float) -> int:
    from sqlalchemy import func, select

    from .database import get_sessionmaker
    from .models.db import Collection, FileEntry

    async with get_sessionmaker()() as session:
        await _implicit_user_id(session)
        rows = list(
            await session.execute(
                select(Collection.name, func.coalesce(func.sum(FileEntry.size), 0))
                .outerjoin(FileEntry, FileEntry.collection_id == Collection.id)
                .group_by(Collection.id)
                .order_by(Collection.id)
            )
        )
    if not rows:
        print("no collections configured (use: cairn add-collection).")
        return 0
    print("Estimated deep-verify (full re-hash) duration per collection:")
    for name, total in rows:
        secs = (int(total) / bytes_per_sec) if bytes_per_sec > 0 else 0
        print(f"  [{name}] {_human_bytes(int(total))} -> ~{_human_duration(secs)}")
    return 0


def _cmd_bench(args: argparse.Namespace) -> int:
    import hashlib
    import os
    import time
    from pathlib import Path

    from .services.scanner import CHUNK, sha256_file

    try:
        target = max(CHUNK, _parse_size(args.bytes))
    except ValueError:
        print(f"error: invalid --bytes value: {args.bytes}", file=sys.stderr)
        return 1

    if args.path:
        root = Path(args.path)
        if not root.is_dir():
            print(f"no such directory: {args.path}", file=sys.stderr)
            return 1
        hashed = 0
        start = time.perf_counter()
        for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
            for fn in filenames:
                p = Path(dirpath) / fn
                if p.is_symlink() or not p.is_file():
                    continue
                try:
                    sha256_file(p)
                    hashed += p.stat().st_size
                except OSError:
                    continue
                if hashed >= target:
                    break
            if hashed >= target:
                break
        elapsed = time.perf_counter() - start
        total, source = hashed, f"{root} (real files)"
    else:
        block = bytes(CHUNK)  # content does not affect SHA-256 speed
        reps = max(1, target // CHUNK)
        digest = hashlib.sha256()
        start = time.perf_counter()
        for _ in range(reps):
            digest.update(block)
        digest.hexdigest()
        elapsed = time.perf_counter() - start
        total, source = reps * CHUNK, "in-memory"

    if elapsed <= 0 or total <= 0:
        print("benchmark produced no measurable work.", file=sys.stderr)
        return 1
    bps = total / elapsed
    print(
        f"SHA-256 throughput: {_human_bytes(bps)}/s "
        f"({source}; hashed {_human_bytes(total)} in {elapsed:.2f}s)"
    )
    if args.estimate:
        return _run(_bench_estimate(bps))
    return 0


def _cmd_stamp(args: argparse.Namespace) -> int:
    """Stamp a collection's pending files under the collection's single-operation claim (D10).

    Stamping inspects the canonical proof path, decides, preserves and places — check-then-act, so
    two concurrent writers would be a lost-update machine and one proof would be destroyed. The claim
    is a real ``kind='stamp'`` run, so the panel's operation badge shows the CLI's work and the
    startup reaper clears it if this process is killed. A lost claim REFUSES and exits non-zero: it
    never waits, because the work is idempotent and a stall behind a deep scan is worse than a retry.
    """

    async def run() -> int:
        from sqlalchemy import func, select

        from .database import get_sessionmaker
        from .models.db import FileEntry, Run
        from .services import collections as collections_svc
        from .services import proofs
        from .services.scanner import _utcnow

        async with get_sessionmaker()() as session:
            await _implicit_user_id(session)
            collection = await _resolve_collection(session, args.collection)
            if collection is None:
                return 1
            if collection.ots_mode != "perfile":
                print(
                    f"[{collection.name}] ots_mode is '{collection.ots_mode}'; nothing to stamp "
                    f"(only per-file collections are notarized)."
                )
                return 0
            # Read before the claim: a refusal rolls back and expires the ORM object.
            name = collection.name
            busy = (
                f"[{name}] SKIPPED — an operation is already in progress for this "
                f"collection; nothing was stamped."
            )
            if args.all:
                # `run_stamp_backfill` already queues the `none` baseline, opens a `kind='stamp'`
                # run and claims the slot; reuse it rather than duplicating the claim here.
                run_row = await proofs.run_stamp_backfill(session, collection)
                # A refused backfill returns its run row still `running` (the claim was rolled
                # back), which is the one state a finished backfill can never be in.
                if run_row.result == "running":
                    print(busy, file=sys.stderr)
                    return 1
                if run_row.result == "interrupted":
                    # The claim was reclaimed while the backfill was working and its fence stopped
                    # it (design D10). Proofs already placed stand; reporting a completed backfill
                    # would claim work that was cut short.
                    print(
                        f"[{name}] STOPPED — this stamp's operation claim was reclaimed while it "
                        f"was running; proofs already placed stand and the rest stay pending.",
                        file=sys.stderr,
                    )
                    return 1
                print(
                    f"[{name}] stamped {run_row.stamped or 0} file(s) "
                    f"(including the previously unstamped baseline)."
                )
                return 0

            total = await session.scalar(
                select(func.count())
                .select_from(FileEntry)
                .where(
                    FileEntry.collection_id == collection.id,
                    FileEntry.ots_state == "pending",
                )
            )
            run_row = Run(
                collection_id=collection.id,
                kind="stamp",
                started=_utcnow(),
                result="running",
                total=int(total or 0),
            )
            if await collections_svc.claim_run(session, run_row) is None:
                print(busy, file=sys.stderr)
                return 1
            run_id = run_row.id

            async def _progress(done: int) -> None:
                # Per-batch progress AND liveness: this claim is held by a CLI process, so a long
                # stamp must keep reporting or the panel's startup reaper would treat it as
                # orphaned and revoke a claim that is still being worked (design D10).
                run_row.processed = done
                run_row.heartbeat_at = _utcnow()
                await session.commit()

            # The lease keeps its own time (design D10): a batch that stalls on a slow calendar can
            # outlast the abandonment interval, and a CLI claim starved that way would be reclaimed
            # while this process is still stamping. The keepalive refreshes it on a timer; the fence
            # inside `stamp_pending` stops the pass if it is reclaimed anyway.
            async with collections_svc.run_keepalive(run_id):
                try:
                    stamped = await proofs.stamp_pending(
                        session, collection, progress=_progress, run_id=run_id
                    )
                except collections_svc.LeaseLost:
                    print(
                        f"[{name}] STOPPED — this stamp's operation claim was reclaimed while it "
                        f"was running; proofs already placed stand and the rest stay pending.",
                        file=sys.stderr,
                    )
                    return 1
                except Exception:  # stamping must never leave the run row `running`
                    await collections_svc.finalize_if_held(
                        session, run_id, result="error", finished=_utcnow()
                    )
                    raise
            if not await collections_svc.finalize_if_held(
                session,
                run_id,
                result="ok",
                stamped=stamped,
                processed=stamped,
                finished=_utcnow(),
            ):
                print(
                    f"[{name}] STOPPED — this stamp's operation claim was reclaimed before it "
                    f"finished; the run record was left as the reclamation wrote it.",
                    file=sys.stderr,
                )
                return 1
            print(f"[{name}] stamped {stamped} pending file(s).")
        return 0

    return _run(run())


def _make_planned(name: str):
    def _run_planned(args: argparse.Namespace) -> int:
        print(
            f"`cairn {name}` is not yet implemented (tracked in the OpenSpec roadmap).",
            file=sys.stderr,
        )
        return 2

    return _run_planned


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cairn",
        description="Cairn — file-integrity monitor + OpenTimestamps notary",
    )
    parser.add_argument("--version", action="version", version=f"cairn {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p_init = sub.add_parser("init", help="Create data/proof dirs and migrate the database")
    p_init.set_defaults(func=_cmd_init)

    p_serve = sub.add_parser("serve", help="Run the web panel")
    p_serve.add_argument("--host", default=None, help="Bind host (default from config)")
    p_serve.add_argument("--port", type=int, default=None, help="Bind port (default from config)")
    p_serve.set_defaults(func=_cmd_serve)

    # ``add-corpus`` stays as a backward-compatible alias for the renamed command.
    p_add = sub.add_parser(
        "add-collection", aliases=["add-corpus"], help="Create a collection to monitor"
    )
    p_add.add_argument("--name", required=True)
    p_add.add_argument("--root", required=True, help="Directory to watch")
    p_add.add_argument("--mode", choices=("worm", "churn"), default="worm")
    p_add.add_argument("--ots-mode", dest="ots_mode", choices=("none", "perfile"), default="none")
    p_add.add_argument("--cadence", type=int, default=900, help="Scan cadence seconds")
    p_add.add_argument(
        "--verify-cadence",
        dest="verify_cadence",
        type=int,
        default=604800,
        help="Deep re-hash cadence seconds (0 = disabled; default weekly)",
    )
    p_add.add_argument("--exclude", action="append", metavar="GLOB", help="Exclude glob (repeatable)")
    p_add.add_argument(
        "--auto-baseline",
        dest="auto_baseline",
        action="store_true",
        help="Auto-promote intact new files to OK on the deep-verify pass (default off)",
    )
    p_add.set_defaults(func=_cmd_add_collection)

    p_scan = sub.add_parser("scan", help="Scan a collection (or all) for changes")
    p_scan.add_argument(
        "--collection", "--corpus", default=None, help="Collection name (default: all)"
    )
    p_scan.add_argument("--once", action="store_true", help="Single pass (cron-friendly)")
    p_scan.set_defaults(func=_cmd_scan)

    p_accept = sub.add_parser(
        "accept",
        help="Re-baseline acknowledged changes (unscoped legacy verb)",
        description=(
            "Re-baseline a collection's acknowledged changes: new and modified files become ok, "
            "missing files' records are removed, and every open event on the collection is "
            "acknowledged. This is the UNSCOPED legacy verb — it acts on all three populations at "
            "once. For the scoped verbs, each named for the one thing it does (baseline new files "
            "/ adopt changed files / stop tracking missing files, or accept a single file), use "
            "the web panel's collection and review pages."
        ),
    )
    p_accept.add_argument(
        "--collection", "--corpus", default=None, help="Collection name (default: all)"
    )
    p_accept.set_defaults(func=_cmd_accept)

    p_import = sub.add_parser(
        "import-manifest", help="Import a manifest.tsv as a pre-existing, unstamped baseline"
    )
    p_import.add_argument(
        "--collection", "--corpus", required=True, help="Target collection name"
    )
    p_import.add_argument("--file", required=True, help="Path to the manifest.tsv")
    p_import.add_argument(
        "--rehash", action="store_true", help="Recompute each file's SHA-256 and warn on mismatch"
    )
    p_import.set_defaults(func=_cmd_import_manifest)

    p_verify = sub.add_parser("verify", help="Verify a file against its stored OTS proof")
    p_verify.add_argument("relpath", help="File path relative to the collection root")
    p_verify.add_argument(
        "--collection", "--corpus", default=None, help="Collection name (default: the only one)"
    )
    p_verify.set_defaults(func=_cmd_verify)

    p_export = sub.add_parser("export", help="Export a file + its .ots proof bundle")
    p_export.add_argument("relpath", help="File path relative to the collection root")
    p_export.add_argument(
        "--collection", "--corpus", default=None, help="Collection name (default: the only one)"
    )
    p_export.add_argument("--out", default=None, help="Destination directory (default: .)")
    p_export.set_defaults(func=_cmd_export)

    p_upgrade = sub.add_parser("upgrade", help="Complete pending OTS proofs (daily pass)")
    p_upgrade.set_defaults(func=_cmd_upgrade)

    p_stamp = sub.add_parser(
        "stamp", help="Stamp pending files (or all unstamped files with --all)"
    )
    p_stamp.add_argument(
        "--collection", "--corpus", default=None, help="Collection name (default: the only one)"
    )
    p_stamp.add_argument(
        "--all",
        action="store_true",
        help="Also stamp the existing unstamped baseline (ots_state=none, non-missing)",
    )
    p_stamp.set_defaults(func=_cmd_stamp)

    p_bench = sub.add_parser(
        "bench", help="Benchmark SHA-256 throughput and estimate deep-scan cost"
    )
    p_bench.add_argument(
        "--path", default=None, help="Measure real throughput over files under DIR (default: in-memory)"
    )
    p_bench.add_argument(
        "--bytes", default="256MiB", help="Bytes to hash for the probe (suffixes: KiB/MiB/GiB)"
    )
    p_bench.add_argument(
        "--estimate", action="store_true", help="Also estimate per-collection deep-scan duration"
    )
    p_bench.set_defaults(func=_cmd_bench)

    for name in PLANNED:
        p = sub.add_parser(name, help=f"[planned] {name}")
        p.set_defaults(func=_make_planned(name))

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
