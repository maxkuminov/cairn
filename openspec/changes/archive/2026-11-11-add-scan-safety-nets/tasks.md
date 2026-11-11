## 1. Schema and settings

- [x] 1.1 Migration `0013`: `files.prior_sha256`, `prior_size`, `prior_mtime` (nullable, plain ADD COLUMN); rebuild the `events` kind CHECK to add `corrupted`; downgrade refuses while a `corrupted` event exists
- [x] 1.2 ORM: the three columns on `FileEntry`; the event-kind CHECK
- [x] 1.3 Settings `mass_change_percent` (0–100, default 25) and `mass_change_min_files` (>= 0, default 200)
- [x] 1.4 `tests/test_migration_0013.py`: upgrade with existing rows, `corrupted` insertable, downgrade refusal and clean downgrade

## 2. Scanner

- [x] 2.1 Root check before the walk; empty-walk check before the missing sweep; `allow_empty` parameter; refusal finalised as `error` with the (prefixed) reason in `runs.error_sample`, nothing else written
- [x] 2.1b Walk `onerror`: any unreadable directory → no missing sweep and no move reconciliation this pass, directories counted and sampled in the run's errors, result `partial`, `RunSummary.walk_incomplete`; checked before the empty-walk refusal; alert under the 24 h limit
- [x] 2.1c Scheduler: advance `last_full_scan_at` only when the deep scan ended `ok` / `partial` **and** its walk was complete
- [x] 2.2 Corruption signature → `corrupted` event + `modified` in both modes; `corrupted` joins the change-alert kinds
- [x] 2.3 Prior contents: set on healthy → `modified` (worm change, corruption, came back different, mass conversion); never overwritten while `modified`; cleared by all three accepts and by `import-manifest` when it leaves a row `ok`
- [x] 2.4 Return detection in the hash-compare branch and the reappearance branch (order: recorded, prior, different); system-acknowledge the file's open change alerts in the same commit; informational `restored` event; `RunSummary.reverted`
- [x] 2.5 JSON `detail` (`path`, `from`, `to`, optional `mass`) on `modified` / `corrupted` events
- [x] 2.6 Mass-change conversion at the end of the pass, before the unresolved-change sweep; `RunSummary.mass_change`
- [x] 2.7 Alert summary wording for corruption and mass change; the unreachable-root alert, at most once per 24 h per collection (query over refused runs); reverts never alert
- [x] 2.8 CLI: `cairn scan --allow-empty`; the scan summary line prints corrupted / reverted / mass-change counts and the refusal reason
- [x] 2.9 `tests/test_scan_safety_nets.py`: one test per scenario of the four new requirements and the modified mode / deep-verify scenarios; update `test_deep_bitrot_churn_rebaselines`; alert tests

## 3. Panel

- [x] 3.1 Population read carries `prior_size`; row kind `corrupted` with precedence; "was <size>"
- [x] 3.2 Guidance and learn page: a changed file put back exactly as it was closes itself; Working description; drop "no earlier version is kept"
- [x] 3.3 Failed-scan reason, taken from the failed run itself, on the card and the collection page; recent activity names possible corruption
- [x] 3.4 Panel tests for the row label and precedence, the size fact, the guidance wording, the policy text, the failed-scan reason

## 4. Verify and document

- [x] 4.1 Full suite; `openspec validate add-scan-safety-nets --strict`
- [x] 4.2 Sandbox: scan a copy with an absent root (refused, nothing missing), a corrupted file, a revert
- [x] 4.3 `README.md`, `DESIGN.md` §5, `CLAUDE.md`
