## Context

`scan_collection` walks the root, compares each tracked file by size + mtime (or always, on a deep
pass), hashes what moved, and classifies. On a change it overwrites `files.sha256/size/mtime` with
what it found and then branches on the collection's mode: `worm` sets `modified` and writes an open
`modified` event; `churn` sets `ok` and writes nothing. Files the walk did not see become
`missing`. Batches of 500 rows are committed as the walk goes; alerts are dispatched after the
final commit.

Since `unify-issue-model` the panel counts exactly one thing — files recorded `modified` or
`missing` — and the scanner guarantees that a file with an open change alert is never recorded
healthy (design D12 there). This change keeps those two properties and builds on them.

Constraints: the watched folders are read-only and may be large (200k files, 1.4 TiB); a scan must
never fail because of an alert; migrations run on container start; SQLite, single writer.

## Goals / Non-Goals

**Goals:** no false "everything deleted"; corruption and disasters are never absorbed by a policy;
an unaccepted change keeps the contents it replaced on record, so restoring them closes the issue.

**Non-Goals:** path rules, history UI, informational events for ordinary Working edits, repair.

## Decisions

### D1 — Refuse an unreachable or empty root

Two checks, both before the pass has changed anything about a file:

- **Before the walk:** the root must be an existing directory. (`collections.validate_root` already
  enforces this at create/edit time; nothing re-checks it at scan time.)
- **After the walk, before the missing sweep:** if the walk saw **no file at all** and the
  collection holds at least one record that is not `missing`, the pass refuses. An empty mount
  point is the common shape of a volume that failed to mount, and it is indistinguishable from
  "the operator deleted everything" — so the safe reading wins and the rare one gets an explicit
  override, `cairn scan --allow-empty` (a parameter of `scan_collection`, default off; the
  scheduler and the panel never pass it).

A refusal raises inside the pass and is finalised like any failed scan — `result='error'` — with
one difference: `runs.error_sample` carries a one-element list with the human reason
("folder /x is not reachable" / "folder /x is empty but N files are tracked"), and `runs.errors`
stays 0 (it counts skipped files). Nothing was written before the refusal: with zero files seen,
no batch had anything to commit. Freshness is untouched — `compute_health` keys on completed
`ok`/`partial` scans, so the collection goes stale on schedule (the dead-man's switch still works).

The refusal alert is rate-limited, not deduplicated: it is dispatched unless an earlier scan of
the collection was refused — for either reason, unreachable or empty — within the previous 24
hours (the reason entries carry a fixed prefix, so the check is one query over `runs`). A pass
with unreadable directories alerts under the same limit, keyed on earlier passes with unreadable
directories. Dispatch is best-effort, so
"once per outage" could lose the only alert to one SMTP hiccup; a daily repeat cannot. It carries
no paths and links to the collection page.

**A walk that did not complete marks nothing missing.** `os.walk` swallows directory-listing
errors by default; the walk gets an `onerror` hook that records each unreadable directory. A pass
with any such error skips the missing sweep and move reconciliation entirely, counts the
directories in `runs.errors` with diagnostic entries, and ends `partial`. Files in readable
directories are classified as usual (batches already committed stand — they are true statements
about files the pass did read). Walk errors are checked **before** the empty-walk refusal: no file
seen because nothing could be listed is "unreadable", not "empty". `RunSummary.walk_incomplete`
records it.

**A deep pass that did not cover the collection is still owed.** The scheduler advances
`last_full_scan_at` only when the deep scan it ran ended `ok` or `partial` **and its walk was
complete**; today it advances it whenever it called one. (A `partial` caused only by individual
files that could not be read or stored still counts — the reference deployment has a collection
that is `partial` on every scan for exactly that reason, and it must not be re-hashed every tick.) *Alternative considered:* a sentinel file
that must exist under the root. Rejected: it asks every operator to plant a file in folders Cairn
promises never to need write access to.

A partially missing tree (a nested mount gone, a deleted folder) is not refused: those files are
reported `missing`, as they are.

### D2 — Possible corruption is its own event kind

In the hash-compare branch, a digest that differs from the recorded one **while the size and the
modification time both equal the recorded ones** is the corruption signature. It can only be seen
by a deep pass (the quick pass does not hash such a file). It is classified before the mode is
consulted: `status='modified'`, an open event of the new kind `corrupted`, an alarm — in `worm`
and `churn` alike. `corrupted` joins `modified` and `restored_changed` as a change-alert kind
(`UNRESOLVED_CHANGE_KINDS`), so everything D12 guarantees applies to it.

*Alternative considered:* reuse `modified` with a marker in `detail`. Rejected: the alert summary,
the activity summary and the review row all branch on kind, and a string convention inside a text
column is how a finding gets mislabelled later. The cost is one CHECK-constraint rebuild (the
pattern migrations 0005 and 0011 already use).

A file with no recorded digest (`sha256 IS NULL`, a manifest import that never hashed) is not a
change at all and is unaffected.

### D3 — The contents on record before an unaccepted change

Three nullable columns on `files`: `prior_sha256`, `prior_size`, `prior_mtime`. "Prior" means *the
contents on record when the file was last healthy*.

- **Set only when nothing is kept yet** (`prior_sha256 IS NULL`) and a change takes the row out of
  the healthy state — a `worm` change, a `corrupted` finding, a mass-change conversion (D4) — to
  the values the row held before the scan overwrote them; and when a file that was healthy when it
  went missing reappears different (`restored_changed`), to the digest recorded when it went
  missing. **Never overwritten** while set: later changes, a disappearance, a reappearance as a
  third version are all still unaccepted changes away from the same last-accepted contents. (If
  the file was already `modified` when it went missing, the digest on record at that moment is the
  unaccepted version — adopting it as "prior" would let that version close the issue later.)
- **Cleared** by every accept (`accept_collection`, `accept_file`, `accept_files`) and by a return
  (below). A healthy row never carries them.

**Return.** Whenever a pass has a digest in hand for a row whose `prior_sha256` is set and the two
are equal, the file is back as it was:

- hash-compare branch (a `modified` row whose size or mtime moved, or any row on a deep pass);
- reappearance branch (a `missing` row that comes back): checked **after** "matches the recorded
  digest" and **before** "came back different".

The row becomes `ok` with the current size/mtime/digest, the prior columns are cleared, **all of
the file's open change alerts are acknowledged by the system in the same commit** (and, on the
reappearance branch, its `missing` alerts, as today), one informational `restored` event is written
(born acknowledged, `detail` saying the file returned to the contents on record before it changed),
`RunSummary.reverted` counts it, and no alarm is raised. The id leaves the pass's open-alert set so
D12's helper and end-of-pass sweep agree. In a `perfile` collection the row is queued for a stamp
like any change of contents (the proof at its path commits to the changed bytes).

This is what makes the panel's promise true for changed files: put the original back and the issue
closes by itself; and a restore-from-backup of the *original* after an unaccepted change no longer
reads as "came back different".

**Each change event records what changed.** `modified` and `corrupted` events get a JSON `detail`:
`{"path": relpath, "from": {"sha256","size","mtime"}, "to": {...}}` (plus `"mass": N` under D4).
The path is included because `_detach_ack_backfill` only fills an empty `detail`, and a removed
file's events must keep the path they refer to. `restored_changed` keeps its existing text form.

### D4 — A ceiling on silent change

While a pass runs it remembers each change it accepts silently under `churn` (row id + the values
it replaced). After the walk, before the end-of-pass D12 sweep: if their number `Q` satisfies
`Q >= mass_change_min_files` **and** `Q * 100 >= mass_change_percent * tracked` (tracked = records
the pass held, missing ones excluded), every one of them is converted: `status='modified'`, prior
columns set from the remembered values, an open `modified` event whose detail carries `"mass": Q`,
and an alarm. Defaults: 200 files and 25 % — on the reference deployment the largest legitimate
single-scan edit burst in a Working collection was 881 of 20,584 files (4.3 %), and typical weeks
are under 0.4 %. `mass_change_percent = 0` disables the rule.

*Accepted limitation:* the conversion happens at the end of the pass, so batches committed during
the walk hold those rows as `ok` until then, and a pass that dies mid-walk has absorbed what it
saw. Bounding that needs the pass to defer every `churn` re-baseline to its end, which changes the
memory profile of a 200k-file scan; the next pass still sees further changes.

The rule does not apply to `worm` (every change is already an issue) or to deletions (already
alarmed per file).

### D5 — Alerts

`RunSummary` gains `corrupted`, `reverted`, `mass_change`. The batched alert's summary names, in
order: missing, modified (with "— unusually many for one scan" when the mass rule fired),
possibly corrupted, came back changed. A refused root dispatches its own alert (D1). Reverts never
alert.

### D6 — Panel

- Review row: a file whose open events include `corrupted` reads "Possible corruption — contents
  changed, size and date did not" with alarm weight (precedence: came back different, then
  corruption, then ordinary change). A changed row whose `prior_size` differs from its size says
  "was <size>". Both facts ride the single population read (three more columns), display only.
- Guidance: "a changed file put back exactly as it was closes its own issue on the next scan that
  re-reads it" replaces the "shows up as one more change" sentence, in `review_help.html` and
  `learn.html`; the "no earlier version is kept" line goes.
- Working policy text: ordinary edits are not flagged; deletions, a file that comes back
  different, possible corruption, and an unusually large change in one scan are.
- A failed scan states its reason where the panel already notes "a later scan failed".
- Recent activity names "possibly corrupted".

### D7 — Migration 0013

`ALTER TABLE files ADD COLUMN` ×3 (no rebuild); batch rebuild of `events` to extend the kind CHECK
(as 0005 / 0011). Downgrade refuses while any `corrupted` event exists, and drops the columns.

## Review record

Spec round 1 (Codex): one contradiction (a corrupted file restored to its original must close, not
stay), and gaps folded in — keep the existing prior digest across a disappearance, walk errors must
not read as deletions, the failure reason must come from the failed run, a refused deep pass is
still owed, the refusal alert is rate-limited rather than once-per-outage, the mass rule's per-pass
limit is stated in the requirement. **Declined:** specifying a new outcome for the reappearance of
a file that never had a digest on record — pre-existing behaviour under its own requirement, and
nothing in this change can compare bytes that were never hashed.

Spec round 2: unreadable takes precedence over empty; an incomplete walk is not a deep
verification (while a per-file `partial` still is); an incomplete walk alerts, since `partial`
keeps the collection "fresh"; the alert key aligned. **Accepted limitation:** a file moved during
a pass whose walk was incomplete is recorded at its new path as an addition, and the next complete
pass reports the old path missing instead of pairing them (a false alarm, cleared by accepting the
deletion; the proof stays with the old record's history).

Implementation review, scope A (prior contents, return, corruption): one fix — a same-size
digest change on a record with no modification time is reported as possible corruption instead of
being absorbed under `churn`. **Declined:** "a `new` file's first-seen digest should not be kept as
the prior, because the operator never accepted it". Since `unify-issue-model` a new file is not
work and is never accepted by hand — it is healthy from the scan that found it — so its first-seen
contents are exactly what "the contents on record when the file was last healthy" means, and a
return to them is a return. Refusing it would leave such a file's issue closable only by accepting
a change the operator has just undone.

Implementation review, scope B (refusal, unreadable folders, mass change, alerts): a file that
is listed but cannot be examined is present, not missing (and is not graduated by a deep pass);
"empty" means "the walk found no file Cairn can track" — a symlink or an un-storable name lying in
an unmounted mount point does not make it pass. Two rounds; the second was verification and found
two slips in the first round's own fixes, fixed with tests. The class — inferring deletion from
incomplete coverage — is now one stated rule: a file is marked missing only by a pass whose walk
listed every directory, that found at least one trackable file, and that did not list that file's
name.

**Interpretations settled during implementation.** "Healthy before a change" means status `ok` or
`new` **with no open change alert**; a row already `modified`, or one whose alerts were
acknowledged without an accept under the old panel, never has a prior set. A record with no digest
on record that is hashed for the first time just records the digest — no event, under either
policy (previously `worm` raised a `modified` alarm for it). Wherever a pass records an existing
row healthy (`_healthy_status` returning `ok`) it also clears any prior the row carried, so "a
healthy row never carries a prior" holds on every path, not only the accepts. A refused pass skips
the stamp tail as well, so it really does change no file record. Counts: corrupted and
mass-converted files are included in `runs.modified` (the convention `restored_changed` already
follows) and named separately in the alert text.

## Risks / Trade-offs

- **A legitimately emptied collection now needs the CLI flag** → stated in the refusal reason.
- **A tool that rewrites files in place preserving size and mtime** (rare) will read as possible
  corruption on a deep pass → that is the correct alarm; one accept clears it.
- **Mass-change false alarm** on a very large legitimate reorganisation → thresholds are settings;
  one bulk accept per folder clears it.
- **Classify-path change** → mandatory adversarial review; the false-negative direction (a change
  wrongly closed as a "return") is the one to attack: a return requires digest equality with the
  recorded prior digest, nothing weaker.

## Migration Plan

Deploy as one PR. Migration 0013 runs on start. Rollback: previous image + `alembic downgrade`
(refused while `corrupted` events exist — accept or delete them first).
