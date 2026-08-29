# Delta: integrity-scanning (relocate-proofs-on-move)

## MODIFIED Requirements

### Requirement: Content-addressed move/rename reconciliation

A scan SHALL reconcile moved/renamed files before it routes alerts, stamps proofs, or finalizes the
run. After the missing-sweep, a file newly classified `missing` whose stored SHA-256 **and** size
match **exactly one** file newly classified `added` in the same scan — where that content key is
shared by no other `missing` or `added` file in the run — SHALL be treated as a single moved file
rather than an independent deletion plus addition. Reconciliation SHALL preserve the original file's
identity: one surviving `files` row SHALL carry the new `relpath` with status `ok` while retaining
its `first_seen`, `sha256`, and OpenTimestamps proof (`ots_path`, `ots_state`, `ots_stamped_at`,
and the recorded proof provenance `ots_digest`, all unchanged). A
reconciled move SHALL emit a single informational `moved` event recording the old and new paths,
SHALL NOT raise a `missing` or `added` event, SHALL NOT be counted as missing or added on the run,
and SHALL increment the run's `moved` count. Reconciliation SHALL be conservative: empty
(zero-byte) files, and any content key matching more than one candidate on either side, SHALL NOT be
reconciled and SHALL retain the existing `missing` + `added` behavior (logged for visibility).
Reconciliation SHALL rewrite only the index — never the corpus bytes, and never the proof store —
and SHALL NOT re-queue the moved file for OTS stamping.

After reconciliation the retained `ots_path` names the old relpath's canonical proof location —
still the true location of the proof. The notarization capability owns what follows: its
referenced-slot stamp guard SHALL prevent any stamp from displacing that proof while the pointer
names it, and its healing sweep SHALL later relocate the proof to the new relpath's canonical
location. The scan itself SHALL NOT move proof files.

#### Scenario: A 1:1 move is reconciled to a single event

- **WHEN** a tracked file is moved/renamed to a previously-unseen path within the same corpus, its
  content unchanged, and no other file in the run shares that content
- **THEN** the scan SHALL produce one `moved` event (old → new path) and no `missing` or `added`
  event, and a single surviving `files` row SHALL hold the new `relpath` with status `ok`

#### Scenario: Moved file keeps its proof and is not re-stamped

- **WHEN** an already-stamped file in a `perfile` corpus is reconciled as a move
- **THEN** the surviving row SHALL retain its `ots_path`, `ots_state`, `ots_stamped_at`, and
  `ots_digest` unchanged, and the scan SHALL NOT mark it `pending`, stamp a new proof for it, or
  touch the proof store

#### Scenario: Ambiguous content does not reconcile

- **WHEN** a `missing` file's content (sha256 + size) matches more than one `added` file, or is a
  zero-byte file
- **THEN** the scan SHALL NOT reconcile it and SHALL retain the `missing` + `added` classification,
  logging the fallback

#### Scenario: A real deletion is unaffected

- **WHEN** a tracked file is deleted and no `added` file in the run matches its content
- **THEN** the scan SHALL still classify it `missing` and write a `missing` event
