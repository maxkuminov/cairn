## ADDED Requirements

### Requirement: Deep verify re-hashes every tracked file

A deep scan SHALL recompute the SHA-256 of every tracked, non-missing file regardless of its size
and mtime, so that a content change that leaves size and mtime unchanged (silent bit-rot) is
detected — a case the size+mtime fast-path cannot catch. A deep scan SHALL reuse the standard
classification: a recomputed hash that differs from the stored hash SHALL be treated as a content
modification (a `modified` nag in worm mode, a silent re-baseline in churn mode) and re-queued for
OTS stamping in `perfile` corpora; a recomputed hash that matches SHALL leave the file `ok`,
refresh `last_checked`, and SHALL NOT re-queue it for stamping. Each run SHALL record whether it
was a deep pass.

#### Scenario: Silent bit-rot is detected on a deep pass

- **WHEN** a tracked file's bytes change but its size and mtime are unchanged
- **THEN** a normal (non-deep) scan SHALL NOT detect it, AND a deep scan SHALL recompute its hash,
  detect the mismatch, and in worm mode set status `modified` and write a `modified` event

#### Scenario: Intact file on a deep pass is not re-stamped

- **WHEN** a deep scan recomputes the hash of a file whose bytes are unchanged
- **THEN** the file SHALL stay `ok`, its `last_checked` SHALL refresh, and it SHALL NOT be
  re-queued for OTS stamping

#### Scenario: Deep pass is recorded on the run

- **WHEN** a corpus is scanned in deep mode
- **THEN** its `runs` row SHALL record that it was a deep pass (and a non-deep scan SHALL record
  that it was not)

### Requirement: Hash throughput benchmark estimates deep-scan cost

The system SHALL provide a read-only benchmark that measures local SHA-256 throughput and SHALL
optionally estimate the deep-scan duration of each corpus as the corpus's total tracked size
divided by the measured throughput. The benchmark SHALL NOT modify any file, proof, or database
row.

#### Scenario: Benchmark reports throughput and a per-corpus estimate

- **WHEN** the operator runs the benchmark with the estimate option
- **THEN** it SHALL print a measured MB/s throughput and, for each corpus, an estimated deep-scan
  duration derived from that throughput and the corpus's total size
