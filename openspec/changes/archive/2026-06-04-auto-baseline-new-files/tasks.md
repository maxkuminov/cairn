# Tasks — auto-baseline intact new files on the deep-verify pass

## 1. Schema & migration
- [ ] 1.1 Add `auto_baseline_new: Mapped[bool]` (default False, not null) to `Collection` in
  `src/models/db.py`.
- [ ] 1.2 Add Alembic `0010_auto_baseline_new` (down_revision `0009_…`): `op.add_column` with
  `server_default="0"`, NOT NULL; downgrade drops it. Round-trip upgrade/downgrade on a fresh DB.

## 2. Service
- [ ] 2.1 `create_collection` and `update_collection` in `src/services/collections.py` accept
  `auto_baseline_new: bool = False` and persist it.

## 3. Scanner
- [ ] 3.1 Add `baselined: int = 0` to `RunSummary`.
- [ ] 3.2 In `scan_collection`, when `deep` and `collection.auto_baseline_new`, after the
  missing-sweep and move reconciliation (before the final commit) promote every pre-existing row
  still `status == "new"` and present this scan to `ok` (skip rows created this scan); increment
  `summary.baselined`. Never touch `modified`/`missing`; never re-stamp.
- [ ] 3.3 Include the baselined count in the scan summary log line.

## 4. Panel + CLI
- [ ] 4.1 `collection_form.html`: add an "Auto-baseline new files" On/Off select next to Deep verify.
- [ ] 4.2 `collection_create` / `collection_update` parse the field and pass `auto_baseline_new`;
  `collection_edit` exposes the current value in the `existing` dict.
- [ ] 4.3 `cairn add-collection` gains `--auto-baseline` (store_true), passed through to
  `create_collection`.

## 5. Tests
- [ ] 5.1 Deep scan with the flag on promotes an intact, pre-existing `new` file to `ok`.
- [ ] 5.2 A quick (non-deep) scan with the flag on does NOT promote.
- [ ] 5.3 With the flag on, a `new` file whose bytes changed becomes `modified` (not auto-baselined),
  and a missing file stays `missing`.
- [ ] 5.4 With the flag off (default), a deep scan leaves `new` files `new`.

## 6. Verification
- [ ] 6.1 `openspec validate auto-baseline-new-files --strict` passes; full suite green.
- [ ] 6.2 After deploy + migrate, enable `auto_baseline_new` on the Photos collection.
