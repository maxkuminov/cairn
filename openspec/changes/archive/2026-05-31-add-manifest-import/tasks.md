## 1. Manifest parser

- [x] 1.1 `src/services/manifest.py`: `parse_manifest(text) -> tuple[list[ManifestRow], int]` returning parsed rows + a skipped count. `ManifestRow(relpath, sha256, size: int|None, mtime: float|None)`. Per line: skip blank/`#`; split on tab, else on whitespace runs (sha256sum style); detect the `^[0-9a-fA-F]{64}$` field as sha256; integer fields as size/mtime; the remaining field as relpath. A line missing sha256 or relpath is skipped (counted).

## 2. Importer

- [x] 2.1 `async import_manifest(session, corpus, path, *, rehash=False) -> ImportResult`: read the file, parse, and upsert by `(corpus_id, relpath)` — insert new rows or update existing — with `status='ok'`, `ots_state='none'`, `first_seen=now`, `last_checked=now`, `sha256`/`size`/`mtime` from the manifest. Do NOT write `added` events. Return counts `{imported, updated, skipped, mismatches}`.
- [x] 2.2 `--rehash` path: for each row, stream `corpus.root/relpath` via `scanner.sha256_file` (in a thread), compare to the manifest hash; collect `(relpath, manifest_hash, actual_hash)` mismatches and missing files; warn + return them; never abort; do not change the no-stamp behavior.

## 3. CLI

- [x] 3.1 `cairn import-manifest --corpus NAME --file PATH [--rehash]` (remove `import-manifest` is a NEW subcommand; keep `status` in PLANNED). Resolve the corpus by name (implicit user); run the import; print `imported/updated/skipped` (+ mismatch lines on `--rehash`). Exit non-zero if the corpus/file is missing or on mismatches when `--rehash`.

## 4. Verification

- [x] 4.1 `tests/test_manifest.py` (temp dir + temp DB; mirror tests/test_scanner.py): write a temp manifest; import into a corpus and assert rows are `status='ok'`, `ots_state='none'`, hash from manifest, and NO `added` events exist. Then run a scan and assert imported files are NOT stamped (perfile corpus, `ots._run_ots` mocked) while a brand-new file added afterward IS marked pending/stamped. Re-import the same manifest and assert idempotency (no duplicate rows, `updated` count). Parser test: tab form, `sha256sum` `<hash>  <path>` form, and a malformed line counted as skipped. `--rehash` test: a tampered file is reported as a mismatch.
- [x] 4.2 `openspec validate add-manifest-import --strict`, `.venv/bin/ruff check src tests`, `PYTHONPATH=. .venv/bin/pytest -q`. Spawn the `openspec-verifier`; resolve drift. Update `CLAUDE.md` (mark `import-manifest` implemented; note Phase 1 parity). Archive.
