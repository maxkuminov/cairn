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
        from .services.collections import get_collection_by_name, list_collections
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
            for collection in collections:
                s = await scan_collection(session, collection)
                print(
                    f"[{collection.name}] added={s.added} modified={s.modified} "
                    f"missing={s.missing} restored={s.restored} baselined={s.baselined} "
                    f"ok={s.ok} errors={s.errors} -> {s.result}"
                )
                if s.result == "error":
                    rc = 1
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
            if result.verified:
                print(
                    f"[{args.relpath}] VERIFIED — Bitcoin block {result.block_height}, "
                    f"existed by {result.existed_by}"
                )
                return 0
            if result.state == "incomplete":
                print(f"[{args.relpath}] pending (proof not yet anchored to Bitcoin)")
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
    async def run() -> int:
        from .config import get_settings
        from .database import get_sessionmaker
        from .services.proofs import stale_incomplete, upgrade_incomplete

        settings = get_settings()
        async with get_sessionmaker()() as session:
            await _implicit_user_id(session)
            result = await upgrade_incomplete(session)
            print(
                f"upgraded={result['upgraded']} "
                f"still_incomplete={result['still_incomplete']}"
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
    async def run() -> int:
        from .database import get_sessionmaker
        from .services import proofs

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
            marked = 0
            if args.all:
                marked = await proofs.mark_unstamped_pending(session, collection)
            stamped = await proofs.stamp_pending(session, collection)
            if args.all:
                print(f"[{collection.name}] queued {marked} unstamped file(s); stamped {stamped}.")
            else:
                print(f"[{collection.name}] stamped {stamped} pending file(s).")
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

    p_accept = sub.add_parser("accept", help="Re-baseline acknowledged changes")
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
