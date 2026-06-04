# integrity-scanning Specification (delta)

## MODIFIED Requirements

### Requirement: Deep verify re-hashes every tracked file

A deep scan SHALL recompute the SHA-256 of every tracked, non-missing file regardless of its size
and mtime, so that a content change that leaves size and mtime unchanged (silent bit-rot) is
detected — a case the size+mtime fast-path cannot catch. A deep scan SHALL reuse the standard
classification: a recomputed hash that differs from the stored hash SHALL be treated as a content
modification (a `modified` nag in worm mode, a silent re-baseline in churn mode) and re-queued for
OTS stamping in `perfile` collections; a recomputed hash that matches SHALL leave the file `ok`,
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
  detect the mismatch, and in worm mode set status `modified` and write a `modified` event

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
