## ADDED Requirements

### Requirement: A scan refuses an unreachable or empty root

A scan SHALL NOT interpret a collection root it cannot read as a collection whose files were all
deleted. Before walking, the scan SHALL verify that the root is an existing directory. After
walking, and before any tracked file is classified missing, the scan SHALL check whether it saw
any file at all: where it saw none while the collection holds at least one record that is not
`missing`, it SHALL refuse the pass. An empty mount point is the usual appearance of a volume that
did not mount, and it cannot be told apart from a deliberate deletion of everything, so the
reading that destroys nothing is the default.

A refused pass SHALL change no file record and write no event. Its run SHALL be recorded with
result `error` and SHALL carry a human-readable reason naming the root and which of the two checks
failed. It SHALL NOT refresh the collection's scan freshness, so a root that stays unreachable
still ages into the stale state the health check reports.

An operator who really has emptied a collection SHALL be able to say so explicitly: the
command-line scan SHALL accept an option that permits a pass over an empty root, and only a pass
given that option SHALL mark the collection's files missing in that situation. Scheduled scans and
scans started from the panel SHALL never run with it.

A root that is reachable and holds some files SHALL be scanned normally, however many tracked
files it no longer holds: a folder that was deleted, or a nested volume that is gone, is reported
as missing files.

#### Scenario: A root that does not exist

- **WHEN** a collection whose root path no longer exists, or is no longer a directory, is scanned
- **THEN** no file record SHALL change, no event SHALL be written, and the run SHALL end `error`
  with a reason naming the root

#### Scenario: A root that is empty while files are tracked

- **WHEN** a collection that tracks files is scanned and its root exists but holds no file
- **THEN** no tracked file SHALL be marked `missing`, no event SHALL be written, and the run SHALL
  end `error` with a reason saying the folder is empty while files are tracked

#### Scenario: The override marks an emptied collection missing

- **WHEN** the command-line scan is run with the allow-empty option on a collection whose root is
  an existing, empty directory
- **THEN** the tracked files SHALL be marked `missing` as an ordinary scan would

#### Scenario: A collection that tracks nothing is not refused

- **WHEN** a collection with no tracked files (or only `missing` records) is scanned over an empty
  root
- **THEN** the pass SHALL complete normally

#### Scenario: A partly missing tree is scanned

- **WHEN** a collection's root holds some of its tracked files and not others
- **THEN** the pass SHALL complete, the absent files SHALL be reported `missing`, and the run SHALL
  NOT be refused

#### Scenario: A refused pass does not refresh freshness

- **WHEN** a collection's only recent scan runs were refused
- **THEN** the health check SHALL treat the collection as it would with no completed scan in that
  period

### Requirement: Possible corruption is its own finding

A scan SHALL report possible corruption when it finds a tracked file's digest different from the
recorded one **while its size and its modification time both equal the recorded ones**: it SHALL
set the file `modified` and write an unacknowledged event of kind `corrupted`. An edit moves the
modification time; a digest that changed behind an unchanged size and time is the signature of
bit-rot or of a write that bypassed the file system's bookkeeping, and it is never the routine
editing the `churn` policy exists to exempt. The finding SHALL therefore be raised under `worm` and
under `churn` alike, and SHALL be distinguishable from an ordinary modification everywhere it is
reported.

A `corrupted` event is a change alert: every rule that applies to an open `modified` or
`restored_changed` event — in particular that a file carrying one is never recorded healthy — SHALL
apply to it. A file with no recorded digest SHALL NOT be reported as corrupted: there is nothing to
compare against.

#### Scenario: Corruption in a churn collection is flagged

- **WHEN** a deep scan of a `churn` collection finds a file whose bytes changed while its size and
  modification time did not
- **THEN** the file SHALL be `modified`, an unacknowledged `corrupted` event SHALL exist for it,
  and the file SHALL NOT have been silently re-baselined

#### Scenario: Corruption in a worm collection is named as such

- **WHEN** a deep scan of a `worm` collection finds the same signature
- **THEN** the event written SHALL be of kind `corrupted`, not `modified`

#### Scenario: An edit that preserved the size is still an edit

- **WHEN** a file's bytes and modification time changed while its size did not
- **THEN** the change SHALL be classified as an ordinary modification, not as corruption

#### Scenario: A file with no recorded digest is not corrupted

- **WHEN** a scan hashes a tracked file for the first time because no digest was on record
- **THEN** no `corrupted` or `modified` event SHALL be written for it

#### Scenario: A corrupted file stays an issue

- **WHEN** a file with an open `corrupted` event is renamed, or deleted and restored with the
  recorded bytes
- **THEN** it SHALL still be recorded `modified` after the scan that sees it

### Requirement: The contents on record before an unaccepted change are kept

When a scan finds that a healthy file has changed and records it `modified`, it SHALL keep the
digest, size and modification time that were on record before the change, alongside the new ones,
until the change is accepted. Further changes to a file that is already `modified` SHALL NOT
replace what was kept: they are further unaccepted changes away from the same last-healthy state.
Where a missing file reappears with different bytes, what is kept SHALL be the digest recorded
when it went missing. Every accept of the file SHALL discard what was kept, and a healthy file
SHALL never carry any.

Each `modified` and `corrupted` event SHALL record what the file was and what it became — digest,
size and modification time on both sides — together with the file's path, so that the event still
says what it was about after the file's record has been removed.

**A file that returns to the kept contents closes its own issue.** Whenever a scan holds a digest
for a file that equals the kept one — because a changed file was put back, or because a file that
had changed and then gone missing was restored from a copy of the original — the scan SHALL record
the file `ok` with what it found, discard what was kept, acknowledge every open change alert of
that file as a system acknowledgement in the same committed transaction, and write one
informational event saying the file returned to the contents on record before it changed. It SHALL
raise no alarm. Equality of digests is the only test: a matching size or name SHALL NOT be treated
as a return. On reappearance the order of tests SHALL be: equals the recorded digest (the file is
back as it was last seen); equals the kept digest (the file is back as it was before it changed);
otherwise it came back different.

#### Scenario: A change keeps what it replaced

- **WHEN** a healthy file in a `worm` collection changes and is scanned
- **THEN** the file SHALL be `modified`, its record SHALL hold both the new digest and the digest,
  size and modification time it had before, and its `modified` event SHALL record both sides and
  the path

#### Scenario: A second change does not replace what was kept

- **WHEN** a `modified` file changes again before it is accepted
- **THEN** the kept digest SHALL still be the one from before the first change

#### Scenario: Putting the original back closes the issue

- **WHEN** a `modified` file is restored to exactly the bytes it had before it changed, and scanned
- **THEN** the file SHALL be `ok`, nothing SHALL be kept for it any more, its open change alerts
  SHALL be acknowledged with no acknowledging user, an informational event SHALL record the return,
  and no alert SHALL be dispatched

#### Scenario: A changed file that was deleted and restored from the original closes too

- **WHEN** a file is found changed and not accepted, then found missing, then found again with the
  bytes it had before it changed
- **THEN** the file SHALL be `ok`, its `modified` and `missing` alerts SHALL be acknowledged, and no
  `restored_changed` event SHALL be written

#### Scenario: Coming back as the changed version is still unresolved

- **WHEN** a file is found changed and not accepted, then found missing, then found again with the
  changed bytes
- **THEN** the file SHALL be `modified`, and what was kept SHALL still be the digest from before the
  change

#### Scenario: Something else again is still a change

- **WHEN** a `modified` file changes to bytes that match neither the recorded digest nor the kept
  one
- **THEN** it SHALL stay `modified` and a further alert SHALL be raised as for any change

#### Scenario: Accepting discards what was kept

- **WHEN** a `modified` file is accepted — on its own, as part of a set, or with its collection
- **THEN** its record SHALL hold no kept contents, and a later return to the old bytes SHALL be
  reported as a change

#### Scenario: Same size is not a return

- **WHEN** a `modified` file changes to different bytes that happen to have the size it had before
  the first change
- **THEN** the file SHALL NOT be recorded `ok`

### Requirement: A mass change is not absorbed

A scan of a `churn` collection SHALL NOT silently accept an unusually large change. Where the
content changes a pass would re-baseline silently number at least a configured minimum **and** at
least a configured share of the files the collection tracks, the scan SHALL instead record every
one of them `modified`, keep the contents each replaced, write an unacknowledged `modified` event
for each that states how many files changed in that pass, and raise an alarm. Below either
threshold the pass SHALL behave as before. The policy tells the system to expect edits; it does not
tell it to expect the whole collection to be rewritten overnight, and a rule with no ceiling
absorbs a disaster as readily as a typo.

The share SHALL be computed over the records the collection tracked when the pass began, missing
ones excluded. Setting the share to zero SHALL disable the rule. The rule SHALL NOT apply to
`worm` collections, where every change is already reported, nor to deletions, which are already
reported file by file.

#### Scenario: A burst over both thresholds becomes issues

- **WHEN** a pass over a `churn` collection finds changed files numbering at least the minimum and
  at least the configured share of the collection
- **THEN** every one of those files SHALL be `modified` with an unacknowledged `modified` event,
  and the scan SHALL count them as a mass change

#### Scenario: A burst under the share is absorbed

- **WHEN** the changed files reach the minimum count but not the configured share
- **THEN** they SHALL be re-baselined silently, as ordinary edits

#### Scenario: A burst under the minimum is absorbed

- **WHEN** most of a small collection changes but the number of changed files is below the minimum
- **THEN** they SHALL be re-baselined silently

#### Scenario: The rule can be disabled

- **WHEN** the configured share is zero
- **THEN** no number of changes in one pass SHALL be converted

#### Scenario: Converted files keep what they replaced

- **WHEN** the mass-change rule converts a file
- **THEN** its record SHALL hold the digest it had before the pass, so putting it back closes the
  issue

## MODIFIED Requirements

### Requirement: WORM and churn modes differ in nagging

In `worm` mode a content modification SHALL raise an unacknowledged `modified` event (a nag). In
`churn` mode an ordinary content modification SHALL silently re-baseline the stored hash/size/mtime
with no nag event. Two findings are NOT ordinary modifications and SHALL NOT be absorbed by `churn`
mode: a change that carries the corruption signature (see "Possible corruption is its own
finding") and a change that is part of an unusually large change in one scan (see "A mass change
is not absorbed"). A `missing` file SHALL raise an unacknowledged event in BOTH modes, and so SHALL a file
that reappears with bytes that do not match the digest recorded for it (`restored_changed`): a file
that was absent and came back different is not the ordinary editing that `churn` mode exempts, and
suppressing it there would be a silent false negative in exactly the mode where the operator has
told the system to expect edits. The informational
kinds `added` and `restored` SHALL NOT nag in either mode — they are written already acknowledged.

#### Scenario: Churn modification does not nag

- **WHEN** a file in a `churn` corpus changes content, its size or modification time having
  changed with it, and the scan's silent changes stay below the mass-change ceiling
- **THEN** the scan SHALL update its stored hash and leave status `ok` with no unacknowledged
  `modified` event

#### Scenario: Missing always nags

- **WHEN** a file goes missing in a `churn` corpus
- **THEN** the scan SHALL still write an unacknowledged `missing` event

#### Scenario: A changed reappearance always nags

- **WHEN** a file recorded `missing` in a `churn` corpus reappears with bytes that do not match the
  digest recorded for it
- **THEN** the scan SHALL write an unacknowledged `restored_changed` event and SHALL NOT silently
  re-baseline the file

#### Scenario: Informational events do not nag

- **WHEN** a scan in either mode writes an `added` or `restored` event
- **THEN** the event SHALL be acknowledged at creation and SHALL NOT increase the corpus's count
  of unacknowledged events

### Requirement: Deep verify re-hashes every tracked file

A deep scan SHALL recompute the SHA-256 of every tracked, non-missing file regardless of its size
and mtime, so that a content change that leaves size and mtime unchanged (silent bit-rot) is
detected — a case the size+mtime fast-path cannot catch. A deep scan SHALL reuse the standard
classification for a file whose size or modification time moved: a recomputed hash that differs
from the stored hash SHALL be treated as a content modification (a `modified` nag in worm mode, a
silent re-baseline in churn mode). A recomputed hash that differs while size and modification time
are both unchanged SHALL be reported as possible corruption under either mode (see "Possible
corruption is its own finding"). Either way the file SHALL be re-queued for OTS stamping in
`perfile` collections; a recomputed hash that matches SHALL leave the file `ok`,
refresh `last_checked`, and SHALL NOT re-queue it for stamping. Each run SHALL record whether it
was a deep pass.

When the collection has `auto_baseline_new` enabled, a deep scan SHALL additionally promote to `ok`
every file that, after classification and the missing-sweep, is still `new` and was present and
intact this pass (its re-hash matched). Files reclassified `modified` or `missing` this pass, and
files first discovered by this pass, SHALL NOT be promoted. The promotion SHALL apply only on a deep
pass (a quick scan SHALL NOT auto-baseline) and SHALL NOT re-stamp the file (a `new` file was already
stamped when first seen). When `auto_baseline_new` is disabled (the default), a deep scan SHALL leave
`new` files `new`.

#### Scenario: Silent bit-rot is detected on a deep pass

- **WHEN** a tracked file's bytes change but its size and mtime are unchanged
- **THEN** a normal (non-deep) scan SHALL NOT detect it, AND a deep scan SHALL recompute its hash,
  detect the mismatch, set status `modified` and write an unacknowledged `corrupted` event, in worm
  mode and in churn mode alike

#### Scenario: Intact file on a deep pass is not re-stamped

- **WHEN** a deep scan recomputes the hash of a file whose bytes are unchanged
- **THEN** the file SHALL stay `ok` (or, when `auto_baseline_new` is on, graduate `new → ok`), its
  `last_checked` SHALL refresh, and it SHALL NOT be re-queued for OTS stamping

#### Scenario: Deep pass is recorded on the run

- **WHEN** a collection is scanned in deep mode
- **THEN** its `runs` row SHALL record that it was a deep pass (and a non-deep scan SHALL record
  that it was not)

#### Scenario: Auto-baseline graduates intact new files on a deep pass

- **WHEN** a collection with `auto_baseline_new` enabled is deep-scanned and a file already tracked
  as `new` re-hashes intact
- **THEN** that file SHALL be promoted to `ok`, while any file reclassified `modified` or `missing`
  this pass SHALL be left as-is and SHALL NOT be auto-accepted

#### Scenario: Auto-baseline is off by default and quick scans never promote

- **WHEN** a collection has `auto_baseline_new` disabled, OR any collection is scanned with a quick
  (non-deep) pass
- **THEN** `new` files SHALL remain `new`
