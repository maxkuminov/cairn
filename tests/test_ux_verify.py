"""Regression tests for the verify path's honesty (fix-ux-audit-sprint1, slice A).

Covers issues #13 (a changed file reported as "pending confirmation"), #19 (the anchored list's
hardcoded green badge), #23 (one name per proof state), #32 (the /verify empty state) and #34
(the asserted "Verified by explorer lookup") — across BOTH consumers of `VerifyResult`: the panel
card and `cairn verify`.

The product is a trust claim, so the expensive failure here is a *false* one in either direction:
telling the operator to wait for a confirmation when the bytes have changed, or crying "this proof
does not check out" over a network blip. Every test below pins one of those.

Run from the repo root: ``PYTHONPATH=. pytest tests/test_ux_verify.py``
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.conftest import seed_collection

# --- harness --------------------------------------------------------------------------------


def _csrf_token(client) -> str:
    html = client.get("/").text
    m = re.search(r'name="csrf-token" content="([^"]+)"', html)
    assert m, "no csrf token in page"
    return m.group(1)


def _make_client(seed_coro):
    """Seed on a throwaway loop, drop the engine, return a TestClient (see tests/test_panel.py)."""
    from fastapi.testclient import TestClient

    from src import database
    from src.main import app

    asyncio.run(seed_coro())
    database.reset_engine()
    return TestClient(app)


async def _seed_one_anchored_file(
    root: Path,
    *,
    ots_state: str = "complete",
    sha256: str | None = "d" * 64,
    status: str = "ok",
    stamped_days_ago: int = 0,
) -> int:
    """One collection with a single on-disk, stamped file. Returns the collection id.

    ``sha256`` is the file's RECORDED BASELINE — the digest the last scan wrote. It is the
    tiebreaker both consumers use to attribute a digest disagreement (design D1), so the tests that
    exercise blame set it deliberately: equal to the live hash of the on-disk bytes means the file
    is intact, different means it changed, ``None`` means there is no baseline to compare against.

    ``status``/``ots_state`` are the second half of that tiebreak: a scan overwrites the baseline
    on modification BEFORE a replacement proof exists, so a row that is `modified`/`new` or whose
    proof is `pending` is in the re-stamp window, where "live == baseline, != proof" means the
    proof simply predates this version rather than being at fault.
    """
    from src.database import get_sessionmaker
    from src.models.db import FileEntry

    cid = await seed_collection(root)
    now = datetime.now(timezone.utc)
    # `stamped_days_ago` ages the submission: the panel's awaiting-confirmation copy branches on it
    # at `CAIRN_INCOMPLETE_PROOF_ALARM_DAYS`, the same threshold `cairn upgrade` warns at.
    stamped = now - timedelta(days=stamped_days_ago)
    async with get_sessionmaker()() as s:
        s.add(FileEntry(
            collection_id=cid, relpath="doc.txt", size=5, sha256=sha256,
            status=status, ots_state=ots_state, ots_path=str(root.parent / "p" / "doc.txt.ots"),
            ots_stamped_at=stamped, first_seen=now, last_checked=now,
        ))
        await s.commit()
    return cid


def _post_verify(client, result, monkeypatch):
    """Run POST /verify with `ots.verify` stubbed to return `result`; return the response text."""
    from src.services import ots as ots_svc

    monkeypatch.setattr(ots_svc, "verify", lambda *a, **k: result)
    token = _csrf_token(client)
    r = client.post("/verify", data={"csrf_token": token, "file_id": 1})
    assert r.status_code == 200, r.text
    return r.text


@pytest.fixture
def verify_client(cairn_env):
    root = cairn_env / "vault"
    root.mkdir()
    (root / "doc.txt").write_text("hello")
    with _make_client(lambda: _seed_one_anchored_file(root)) as client:
        yield client


def _result(**kw):
    from src.services.ots import VerifyResult

    kw.setdefault("verified", False)
    kw.setdefault("state", "complete")
    return VerifyResult(**kw)


# --- #13: the panel card must not launder a mismatch as "pending" ---------------------------


def test_digest_mismatch_renders_the_mismatch_card_never_pending(verify_client, monkeypatch):
    # The exact event Cairn exists to detect. It used to render "Proof pending confirmation ·
    # usually settles within a few hours" because the route branched on `state` first.
    html = _post_verify(
        verify_client,
        _result(digest_mismatch=True, message="file digest does not match the stamped proof"),
        monkeypatch,
    )
    assert "File no longer matches its proof" in html
    assert "pending" not in html.lower()
    assert 'verdict--warn' not in html
    assert 'verdict--danger' in html
    assert "has changed since it was stamped" in html


def test_proof_mismatch_card_does_not_claim_the_file_changed(verify_client, monkeypatch):
    html = _post_verify(verify_client, _result(proof_mismatch=True), monkeypatch)
    assert "This proof does not check out" in html
    assert "not evidence that" in html  # blames the proof / the explorer's block data
    assert "the proof may be corrupt" in html
    assert "has changed since it was stamped" not in html


def test_returned_transport_error_is_neutral_never_danger(verify_client, monkeypatch):
    html = _post_verify(
        verify_client, _result(transport_error="explorer request failed"), monkeypatch
    )
    assert "Couldn&#39;t check right now" in html or "Couldn't check right now" in html
    assert "verdict--unavailable" in html
    assert "verdict--danger" not in html and "verdict--warn" not in html
    assert "pending" not in html.lower()


def test_raised_ots_error_lands_on_the_same_neutral_branch(verify_client, monkeypatch):
    # The route's `except OtsError` net must not inherit the file's `ots_state` any more.
    from src.services import ots as ots_svc

    def boom(*a, **k):
        raise ots_svc.OtsError("ots binary not found")

    monkeypatch.setattr(ots_svc, "verify", boom)
    token = _csrf_token(verify_client)
    r = verify_client.post("/verify", data={"csrf_token": token, "file_id": 1})
    assert r.status_code == 200, r.text
    assert "verdict--unavailable" in r.text
    assert "verdict--danger" not in r.text and "verdict--warn" not in r.text
    assert "pending" not in r.text.lower()


def test_verified_result_outranks_a_proof_mismatch_flag(verify_client, monkeypatch):
    # Belt-and-braces on the source-level rule: no caller may turn a bad sibling into a verdict.
    html = _post_verify(
        verify_client,
        _result(verified=True, proof_mismatch=True, block_height=811111,
                existed_by="2024-02-14 18:35 UTC"),
        monkeypatch,
    )
    assert "Proof verified" in html
    assert "This proof does not check out" not in html
    assert "verdict--ok" in html


def test_verified_result_discloses_a_riding_transport_error(verify_client, monkeypatch):
    html = _post_verify(
        verify_client,
        _result(verified=True, block_height=811111, existed_by="2024-02-14 18:35 UTC",
                transport_error="failed at 822222; failed at 833333", transport_failures=2),
        monkeypatch,
    )
    assert "verdict--ok" in html and "Proof verified" in html
    assert "verdict--unavailable" not in html
    # Precedence decides the headline, not what the card may say.
    assert "2 attestation lookups failed" in html
    assert "based on the attestations reached" in html


def test_proof_mismatch_discloses_a_riding_transport_error(verify_client, monkeypatch):
    html = _post_verify(
        verify_client,
        _result(proof_mismatch=True, transport_error="failed at 822222"),
        monkeypatch,
    )
    assert "This proof does not check out" in html
    assert "verdict--unavailable" not in html
    assert "1 attestation lookup failed" in html
    assert "only over the attestations that could be fetched" in html


def test_inconclusive_names_all_three_possibilities(verify_client, monkeypatch):
    html = _post_verify(verify_client, _result(inconclusive=True, state="incomplete"), monkeypatch)
    assert "Couldn&#39;t confirm" in html or "Couldn't confirm" in html
    assert "verdict--unavailable" in html
    assert "not yet confirmed" in html
    assert "no longer matches it" in html
    assert "Bitcoin node could not be reached" in html
    assert "Pending confirmation" not in html


# --- #23 / design D13: two not-yet-confirmed states, two names ------------------------------


def test_incomplete_reads_pending_confirmation(verify_client, monkeypatch):
    html = _post_verify(verify_client, _result(state="incomplete"), monkeypatch)
    assert "Pending confirmation" in html
    # Live-pass M5: the card now dates the submission. A proof stamped just now keeps the
    # reassurance, because for a fresh proof it is true.
    assert "was submitted to Bitcoin on" in html
    assert "This usually settles within a few hours." in html


def test_pending_reads_queued_to_stamp_with_no_awaiting_wording(verify_client, monkeypatch):
    html = _post_verify(verify_client, _result(state="pending"), monkeypatch)
    assert "Queued to stamp" in html
    assert "Pending confirmation" not in html
    assert "confirmed yet" not in html
    assert "awaiting" not in html.lower()
    assert "has not been submitted" in html


# --- D13 (post-audit): a file with no proof YET is not a verification failure ----------------


async def _seed_one_unstamped_file(root, *, ots_state: str) -> int:
    """One collection, one on-disk file that has no `.ots` proof (so `verify` never runs)."""
    from src.database import get_sessionmaker
    from src.models.db import FileEntry

    cid = await seed_collection(root)
    now = datetime.now(timezone.utc)
    async with get_sessionmaker()() as s:
        s.add(FileEntry(
            collection_id=cid, relpath="doc.txt", size=5, sha256="d" * 64,
            status="ok", ots_state=ots_state, ots_path=None,
            first_seen=now, last_checked=now,
        ))
        await s.commit()
    return cid


def _verify_unstamped(cairn_env, name, ots_state):
    root = cairn_env / name
    root.mkdir()
    (root / "doc.txt").write_text("hello")
    with _make_client(lambda: _seed_one_unstamped_file(root, ots_state=ots_state)) as client:
        token = _csrf_token(client)
        r = client.post("/verify", data={"csrf_token": token, "file_id": 1})
        assert r.status_code == 200, r.text
        return r.text


def test_a_queued_file_with_no_proof_yet_reads_queued_not_a_red_failure(cairn_env):
    """`ots_state='pending'` and nothing on disk: `verify` is never called, so there is no
    `VerifyResult` — which used to land on the red "Could not verify" fallback. Nothing is wrong
    with the file; the stamp simply has not been made yet."""
    html = _verify_unstamped(cairn_env, "queued", "pending")
    assert "Queued to stamp" in html
    assert "Could not verify" not in html
    assert "verdict--danger" not in html
    assert "verdict--warn" in html
    assert "has not been submitted" in html
    # Nothing to download, so no button that would 409.
    assert "/verify/export/1" not in html


def test_a_never_stamped_file_reads_not_notarized_not_a_red_failure(cairn_env):
    html = _verify_unstamped(cairn_env, "unstamped", "none")
    assert "Not notarized yet" in html
    assert "Could not verify" not in html
    assert "verdict--danger" not in html
    assert "verdict--unavailable" in html
    assert "No timestamp proof has been made" in html
    assert "/verify/export/1" not in html


def test_a_stamped_file_still_offers_its_proof_download(verify_client, monkeypatch):
    """The download button is gated on a stored proof, not removed."""
    html = _post_verify(
        verify_client,
        _result(verified=True, block_height=1, existed_by="2024-02-14 18:35 UTC"),
        monkeypatch,
    )
    assert "/verify/export/1" in html


# --- #19: the anchored list renders the real per-row state ----------------------------------


def test_anchored_list_badge_reflects_the_real_state(cairn_env):
    root = cairn_env / "anchored"
    root.mkdir()
    (root / "doc.txt").write_text("hello")

    async def seed():
        from src.database import get_sessionmaker
        from src.models.db import FileEntry

        cid = await seed_collection(root)
        now = datetime.now(timezone.utc)
        async with get_sessionmaker()() as s:
            s.add(FileEntry(
                collection_id=cid, relpath="f1.txt", size=5, sha256="1" * 64,
                status="ok", ots_state="incomplete", ots_path="/p/f1.ots",
                ots_stamped_at=now, first_seen=now, last_checked=now,
            ))
            await s.commit()

    with _make_client(seed) as client:
        html = client.get("/verify").text
        # The newest-first list used to hardcode the green "Anchored" pill on every row, so the
        # *least* confirmed proofs sat on top wearing it. The real state is now rendered.
        assert "Pending confirmation" in html
        assert "Anchored</span>" not in html  # the pill label, not the page copy


def test_pending_row_badge_reads_queued_to_stamp(cairn_env):
    root = cairn_env / "queued"
    root.mkdir()

    async def seed():
        from src.database import get_sessionmaker
        from src.models.db import FileEntry

        cid = await seed_collection(root)
        now = datetime.now(timezone.utc)
        async with get_sessionmaker()() as s:
            s.add(FileEntry(
                collection_id=cid, relpath="q.txt", size=5, sha256="9" * 64,
                status="new", ots_state="pending", ots_path="/p/q.ots",
                first_seen=now, last_checked=now,
            ))
            await s.commit()

    with _make_client(seed) as client:
        # `pending` rows are not in the anchored list, so exercise the macro through the search
        # partial's contract instead: the badge label itself must be the queued wording.
        from src.control_panel.routes import templates

        tpl = templates.get_template("_macros.html")
        module = tpl.make_module({})
        assert "Queued to stamp" in module.ots_badge("pending", "sm")
        assert "Pending confirmation" in module.ots_badge("incomplete", "sm")
        assert "Anchored" in module.ots_badge("complete", "sm")
        assert client.get("/verify").status_code == 200


# --- #32: a real empty state when nothing is anchored ---------------------------------------


def test_verify_page_empty_state_when_nothing_anchored(cairn_env):
    root = cairn_env / "bare"
    root.mkdir()

    async def seed():
        await seed_collection(root)

    with _make_client(seed) as client:
        html = client.get("/verify").text
        # Sprint 2 rewords this from "No files have been anchored yet": the listing's population is
        # submitted proofs, half of which are not anchored yet, so its own empty state may not
        # claim otherwise either. The requirement — a distinct empty state, separate from the
        # no-search-matches message — is unchanged.
        assert "No proofs have been submitted yet" in html
        assert "anchored" not in html.lower().split("how this works")[0]


# --- #34: the closing sentence is derived, not asserted -------------------------------------


def test_checked_using_sentence_follows_the_configured_backend(verify_client, monkeypatch):
    html = _post_verify(
        verify_client,
        _result(verified=True, block_height=1, existed_by="2024-02-14 18:35 UTC"),
        monkeypatch,
    )
    assert "Verified by explorer lookup." not in html
    assert "Checked using" in html


# --- the CLI consumer: the same false negative, the other surface ---------------------------


def _cmd_verify_output(
    cairn_env, capsys, result, *, ots_state="complete", sha256="d" * 64, status="ok"
):
    """Run `cairn verify doc.txt` with `ots.verify` stubbed; return (rc, stdout, stderr)."""
    import src.cli as cli
    from src.services import ots as ots_svc

    root = cairn_env / "cliv"
    root.mkdir()
    (root / "doc.txt").write_text("hello")

    async def seed():
        await _seed_one_anchored_file(
            root, ots_state=ots_state, sha256=sha256, status=status
        )

    asyncio.run(seed())

    from src import database

    database.reset_engine()

    real_verify = ots_svc.verify
    ots_svc.verify = lambda *a, **k: result
    try:
        rc = cli.main(["verify", "doc.txt"])
    finally:
        ots_svc.verify = real_verify
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def test_cli_digest_mismatch_prints_the_mismatch_not_pending(cairn_env, capsys):
    rc, out, err = _cmd_verify_output(cairn_env, capsys, _result(digest_mismatch=True))
    assert rc == 1
    combined = out + err
    assert "CHANGED" in combined
    assert "pending" not in combined.lower()


def test_cli_transport_error_prints_neither_pending_nor_verified(cairn_env, capsys):
    rc, out, err = _cmd_verify_output(
        cairn_env, capsys, _result(transport_error="explorer request failed")
    )
    assert rc == 1
    combined = out + err
    assert "COULD NOT CHECK" in combined
    assert "pending" not in combined.lower()
    assert "VERIFIED" not in combined


def test_cli_inconclusive_names_all_three_and_never_says_pending(cairn_env, capsys):
    rc, out, err = _cmd_verify_output(
        cairn_env, capsys, _result(inconclusive=True, state="incomplete")
    )
    assert rc == 1
    combined = out + err
    assert "INCONCLUSIVE" in combined
    assert "not yet confirmed" in combined
    assert "no longer matches" in combined
    assert "Bitcoin node could not be reached" in combined
    assert "pending" not in combined.lower()
    assert "VERIFIED" not in combined


def test_cli_pending_state_prints_queued_without_awaiting_wording(cairn_env, capsys):
    rc, out, err = _cmd_verify_output(
        cairn_env, capsys, _result(state="pending"), ots_state="pending"
    )
    assert rc == 1
    combined = out + err
    assert "queued to stamp" in combined
    assert "not yet submitted to a calendar" in combined
    assert "awaiting" not in combined.lower()
    assert "confirmation" not in combined.lower()


def test_cli_incomplete_state_prints_pending_confirmation(cairn_env, capsys):
    rc, out, err = _cmd_verify_output(
        cairn_env, capsys, _result(state="incomplete"), ots_state="incomplete"
    )
    assert rc == 1
    assert "pending confirmation" in (out + err)
    assert "awaiting Bitcoin confirmation" in (out + err)


def test_cli_verified_with_transport_error_prints_the_note_and_still_exits_zero(
    cairn_env, capsys
):
    rc, out, err = _cmd_verify_output(
        cairn_env,
        capsys,
        _result(verified=True, block_height=811111, existed_by="2024-02-14 18:35 UTC",
                transport_error="failed at 822222; failed at 833333", transport_failures=2),
    )
    assert rc == 0  # the note never changes the exit status the verdict already set
    assert "VERIFIED" in out
    assert "2 attestation lookups failed" in out


def test_cli_proof_mismatch_with_transport_error_prints_the_note_and_exits_nonzero(
    cairn_env, capsys
):
    rc, out, err = _cmd_verify_output(
        cairn_env, capsys, _result(proof_mismatch=True, transport_error="failed at 822222")
    )
    assert rc == 1
    combined = out + err
    assert "PROOF DOES NOT CHECK OUT" in combined
    assert "NOT evidence the file changed" in combined
    assert "1 attestation lookup failed" in combined


# --- post-audit hardening: blame, provenance and honest containers (sprint 1 §8) -------------
#
# `ots.verify` reports a digest disagreement neutrally — it cannot tell a modified file from a
# corrupted `.ots`. Both consumers attribute it from the file's RECORDED BASELINE, and each wrong
# attribution is a live false alarm: accusing an intact file of changing, or reassuring the
# operator that a proof is fine when it is not this file's proof.

_LIVE_SHA_OF_HELLO = hashlib.sha256(b"hello").hexdigest()


@pytest.fixture
def intact_file_client(cairn_env):
    """Panel client whose seeded baseline digest EQUALS the live hash of the file on disk."""
    root = cairn_env / "intact"
    root.mkdir()
    (root / "doc.txt").write_text("hello")
    with _make_client(
        lambda: _seed_one_anchored_file(root, sha256=_LIVE_SHA_OF_HELLO)
    ) as client:
        yield client


@pytest.fixture
def restamp_window_client(cairn_env):
    """The modified-awaiting-re-stamp window: baseline already rewritten, proof not yet replaced."""
    root = cairn_env / "restamp"
    root.mkdir()
    (root / "doc.txt").write_text("hello")
    with _make_client(
        lambda: _seed_one_anchored_file(
            root, sha256=_LIVE_SHA_OF_HELLO, status="modified", ots_state="pending"
        )
    ) as client:
        yield client


@pytest.fixture
def pending_restamp_client(cairn_env):
    """Same window, arrived at from the proof side: status settled, the re-stamp still queued."""
    root = cairn_env / "pending"
    root.mkdir()
    (root / "doc.txt").write_text("hello")
    with _make_client(
        lambda: _seed_one_anchored_file(
            root, sha256=_LIVE_SHA_OF_HELLO, status="ok", ots_state="pending"
        )
    ) as client:
        yield client


@pytest.fixture
def no_baseline_client(cairn_env):
    """Panel client for a row with no recorded baseline digest at all."""
    root = cairn_env / "nobase"
    root.mkdir()
    (root / "doc.txt").write_text("hello")
    with _make_client(lambda: _seed_one_anchored_file(root, sha256=None)) as client:
        yield client


def test_a_disagreement_over_an_intact_file_never_accuses_the_file(
    intact_file_client, monkeypatch
):
    """The file still hashes to its recorded baseline, so the FILE is not what the card blames.

    Pre-fix this rendered "the file has changed since it was stamped. The proof itself is intact"
    — both halves false — for a `.ots` with one flipped byte in its serialized digest. Nor does it
    swing to the opposite accusation: with the row settled (`ok` + `complete`) Cairn has no record
    of which digest THIS proof was made from, so it says exactly that and blames neither.
    """
    html = _post_verify(intact_file_client, _result(digest_mismatch=True), monkeypatch)
    assert "This proof does not match this file" in html
    assert "may be from an earlier version of this file, or it may be corrupted" in html
    assert "cannot tell which without per-proof records" in html
    assert "neither the file nor the proof is blamed here" in html
    # The two accusations the pre-fix card made, neither of which was established:
    assert "has changed since it was stamped" not in html
    assert "The proof itself is intact" not in html


def test_a_disagreement_in_the_restamp_window_reads_as_a_proof_that_predates_the_file(
    restamp_window_client, monkeypatch
):
    """A `modified` row: the scan already overwrote the baseline, the re-stamp has not run yet.

    Live bytes == recorded baseline and != the proof's digest is the EXPECTED state of that window
    — the proof is good, it just covers the previous version. Calling it corrupt or misfiled here
    accuses a valid proof of the ordinary consequence of editing a file.
    """
    html = _post_verify(restamp_window_client, _result(digest_mismatch=True), monkeypatch)
    assert "Proof predates this version of the file" in html
    assert "matches its current recorded baseline" in html
    assert "a re-stamp is still pending" in html
    assert "It is not evidence against the current file." in html
    assert "verdict--warn" in html
    assert "verdict--danger" not in html
    # ...and it does not vouch for the older proof while doing so: nothing on this path validated
    # it, so "the older proof keeps covering the earlier version" was a promise about an artifact
    # this check never looked at.
    assert "keeps covering" not in html
    assert "did not validate that proof's Bitcoin attestations" in html
    # Neither accusation: not the file, and not the proof.
    assert "has changed since it was stamped" not in html
    assert "may be corrupted" not in html


def test_a_pending_restamp_state_also_reads_as_a_proof_that_predates_the_file(
    pending_restamp_client, monkeypatch
):
    """The other half of the window: the row is back to `ok` but the re-stamp is still queued."""
    html = _post_verify(pending_restamp_client, _result(digest_mismatch=True), monkeypatch)
    assert "Proof predates this version of the file" in html
    assert "a re-stamp is still pending" in html
    assert "may be corrupted" not in html


def test_no_card_ever_states_the_proof_is_corrupted_or_misfiled(
    intact_file_client, restamp_window_client, monkeypatch
):
    """The definitive accusation is gone from the surface, not merely re-routed.

    `files.sha256` is not the digest the stored proof was made from (a scan overwrites it before a
    replacement proof exists), so no branch may present proof corruption as established fact.
    """
    for client in (intact_file_client, restamp_window_client):
        html = _post_verify(client, _result(digest_mismatch=True), monkeypatch)
        assert "another file's proof" not in html
        assert "the proof may be corrupted, or it may be another file" not in html
        assert "This is not evidence that" not in html


def test_a_disagreement_over_a_changed_file_still_blames_the_file(verify_client, monkeypatch):
    """The other direction must keep working: baseline "d"*64 vs a live hash of "hello"."""
    html = _post_verify(verify_client, _result(digest_mismatch=True), monkeypatch)
    assert "File no longer matches its proof" in html
    assert "has changed since it was stamped" in html
    # ...but the card no longer certifies a proof it never validated.
    assert "The proof itself is intact" not in html
    assert "did not validate the proof itself" in html


def test_a_disagreement_with_no_baseline_names_both_possibilities(
    no_baseline_client, monkeypatch
):
    """No recorded baseline ⇒ no tiebreaker ⇒ no blame. Both possibilities are named."""
    html = _post_verify(no_baseline_client, _result(digest_mismatch=True), monkeypatch)
    assert "Fingerprint and proof disagree" in html
    assert "cannot tell whether the file's bytes changed or the proof is the wrong one" in html
    assert "has changed since it was stamped" not in html


def test_an_inconclusive_card_never_presents_a_confirmed_block(verify_client, monkeypatch):
    """BLOCKER 3: the proof-declared block must not be juxtaposed with the live fingerprint.

    On an inconclusive node result the card shows the LIVE SHA-256 and used to caption the proof's
    block as "where this fingerprint is permanently recorded" — false whenever the file changed,
    since that block pertains to the digest the proof was made from. The copied report repeated it.
    """
    html = _post_verify(
        verify_client, _result(inconclusive=True, state="complete", block_height=811111),
        monkeypatch,
    )
    assert "verdict--unavailable" in html
    assert "where this fingerprint is permanently recorded" not in html
    assert "Block in the proof" in html
    assert "recorded in the proof (unverified)" in html
    # ...and the copy-paste report carries the same qualification, not a bare "Bitcoin block".
    assert "Block recorded in the proof (UNVERIFIED" in html
    assert "\nBitcoin block: #811,111" not in html


def test_a_verified_card_still_presents_confirmed_provenance(verify_client, monkeypatch):
    """The gate must not swallow the real thing: a verified result keeps its block and date."""
    html = _post_verify(
        verify_client,
        _result(verified=True, block_height=811111, existed_by="2024-02-14 18:35 UTC"),
        monkeypatch,
    )
    assert "where this fingerprint is permanently recorded" in html
    assert "recorded in the proof (unverified)" not in html
    assert "Existed by" in html


def test_a_verified_card_without_block_metadata_renders(verify_client, monkeypatch):
    """MAJOR 5's consumer side: the node backend can verify on rc==0 with nothing parsed."""
    html = _post_verify(verify_client, _result(verified=True), monkeypatch)
    assert "verdict--ok" in html and "Proof verified" in html
    assert "still matches its proof" in html
    assert "Bitcoin block" not in html  # nothing was parsed; nothing is claimed


def test_an_unreadable_proof_reaches_no_conclusion_about_the_file(verify_client, monkeypatch):
    """MINOR 6: the established reason must not be replaced by unestablished possibilities."""
    html = _post_verify(
        verify_client,
        _result(unreadable_proof=True, state="none", message="unreadable proof: bad magic"),
        monkeypatch,
    )
    assert "Proof file could not be read" in html
    assert "Nothing was concluded about the file" in html
    assert "verdict--unavailable" in html
    # The generic fallback's two guesses, neither of which was established here:
    assert "Its contents may have changed since it was recorded" not in html
    assert "verdict--danger" not in html


def test_the_proof_list_heading_covers_both_proof_states(cairn_env):
    """MINOR 8: the query includes `incomplete` rows, so the container cannot say "anchored"."""
    root = cairn_env / "heading"
    root.mkdir()

    async def seed():
        await _seed_one_anchored_file(root, ots_state="incomplete")

    with _make_client(seed) as client:
        html = client.get("/verify").text
        assert "Recent proofs" in html
        assert "Recently anchored" not in html
        assert "Pending confirmation" in html  # the per-row badge is untouched


# --- the CLI consumer: the same blame tiebreak, the other surface ---------------------------


def test_cli_disagreement_over_an_intact_file_blames_neither(cairn_env, capsys):
    rc, out, err = _cmd_verify_output(
        cairn_env, capsys, _result(digest_mismatch=True), sha256=_LIVE_SHA_OF_HELLO
    )
    assert rc == 1
    combined = out + err
    assert "PROOF DOES NOT MATCH THIS FILE" in combined
    assert "cannot tell which without per-proof records" in combined
    assert "CHANGED —" not in combined  # the pre-fix accusation against the file
    assert "corrupted or misfiled" not in combined  # ...and the one against the proof


def test_cli_disagreement_in_the_restamp_window_prints_the_stale_proof_line(cairn_env, capsys):
    """Mirrors the panel: a `modified` row whose re-stamp has not run is not a proof fault."""
    rc, out, err = _cmd_verify_output(
        cairn_env, capsys, _result(digest_mismatch=True),
        sha256=_LIVE_SHA_OF_HELLO, status="modified", ots_state="pending",
    )
    assert rc == 1
    combined = out + err
    assert "PROOF PREDATES THIS VERSION" in combined
    assert "a re-stamp is still pending" in combined
    assert "NOT evidence against the current file" in combined
    assert "CHANGED —" not in combined
    assert "corrupted" not in combined


def test_cli_disagreement_with_no_baseline_blames_neither(cairn_env, capsys):
    rc, out, err = _cmd_verify_output(
        cairn_env, capsys, _result(digest_mismatch=True), sha256=None
    )
    assert rc == 1
    combined = out + err
    assert "FINGERPRINT AND PROOF DISAGREE" in combined
    assert "which of the two moved" in combined


def test_cli_changed_file_line_no_longer_certifies_the_proof(cairn_env, capsys):
    rc, out, err = _cmd_verify_output(cairn_env, capsys, _result(digest_mismatch=True))
    assert rc == 1
    combined = out + err
    assert "CHANGED" in combined
    assert "the proof still attests the earlier version" not in combined
    assert "The proof itself was not checked here" in combined


def test_cli_unreadable_proof_prints_its_own_line(cairn_env, capsys):
    rc, out, err = _cmd_verify_output(
        cairn_env, capsys,
        _result(unreadable_proof=True, state="none", message="unreadable proof: bad magic"),
    )
    assert rc == 1
    combined = out + err
    assert "UNREADABLE PROOF" in combined
    assert "no conclusion was reached about the file" in combined
    assert "pending" not in combined.lower()


def test_cli_verified_without_block_metadata_still_reports_verified(cairn_env, capsys):
    rc, out, err = _cmd_verify_output(cairn_env, capsys, _result(verified=True))
    assert rc == 0
    assert "VERIFIED" in out
    assert "None" not in out  # never "Bitcoin block None, existed by None"


def test_cli_node_backend_missing_binary_prints_could_not_check(cairn_env, capsys, monkeypatch):
    """End-to-end (`ots.verify` NOT stubbed): a missing binary must not abort the command.

    `_verify_via_cli` calls `info()` before the transport boundary, so `OtsError` escaped `verify()`
    and `cairn verify` — which has no catch of its own — died with a traceback.
    """
    import src.cli as cli
    from src import database
    from src.config import get_settings
    from src.services import ots as ots_svc

    monkeypatch.setenv("CAIRN_VERIFY_BACKEND", "node")
    get_settings.cache_clear()

    root = cairn_env / "clinode"
    root.mkdir()
    (root / "doc.txt").write_text("hello")
    proof = cairn_env / "doc.txt.ots"
    proof.write_bytes(b"stub")

    async def seed():
        from src.database import get_sessionmaker
        from src.models.db import FileEntry

        cid = await seed_collection(root)
        now = datetime.now(timezone.utc)
        async with get_sessionmaker()() as s:
            s.add(FileEntry(
                collection_id=cid, relpath="doc.txt", size=5, sha256="d" * 64,
                status="ok", ots_state="complete", ots_path=str(proof),
                ots_stamped_at=now, first_seen=now, last_checked=now,
            ))
            await s.commit()

    asyncio.run(seed())
    database.reset_engine()

    def boom(args, timeout=ots_svc.DEFAULT_TIMEOUT):
        raise ots_svc.OtsError("the 'ots' CLI is not installed or not on PATH")
    monkeypatch.setattr(ots_svc, "_run_ots", boom)

    try:
        rc = cli.main(["verify", "doc.txt"])
    finally:
        get_settings.cache_clear()
    combined = capsys.readouterr()
    text = combined.out + combined.err
    assert rc == 1
    assert "COULD NOT CHECK" in text
    assert "pending" not in text.lower()


# --- live UX pass: M1 / M5 / #15 -------------------------------------------------------------
#
# Three findings from driving the deployed panel, all the same shape: a card that says more than
# the check established. The file that could not be read got the generic red fallback speculating
# about its contents; a proof stuck since March was told it usually settles within a few hours;
# and the backend was named as having been "used" on cards where nothing was ever looked up.


def _unavailable_client(cairn_env, *, status: str = "ok"):
    """A client whose one tracked file is NOT on disk, so the live re-hash cannot happen."""
    root = cairn_env / "gone"
    root.mkdir()  # deliberately no doc.txt
    return _make_client(lambda: _seed_one_anchored_file(root, status=status))


def test_a_file_that_cannot_be_read_never_speculates_about_its_contents(cairn_env, monkeypatch):
    """M1: `live_unavailable` set the verdict but was absent from the template context.

    The card therefore fell through to the generic fallback — "Its contents may have changed since
    it was recorded" — which is speculation about tampering built on a check that never ran, on the
    one page an operator opens to answer exactly that question.
    """
    with _unavailable_client(cairn_env) as client:
        html = _post_verify(client, _result(verified=True), monkeypatch)

    assert "File unavailable — cannot verify" in html
    assert "contents may have changed" not in html
    assert "could not read" in html
    assert "nothing was compared" in html.lower()
    assert "says nothing about whether the file was altered" in html


def test_a_file_recorded_missing_says_so_and_points_at_review(cairn_env, monkeypatch):
    """M1: where the row already says `missing`, the card names that and offers the way forward."""
    with _unavailable_client(cairn_env, status="missing") as client:
        html = _post_verify(client, _result(verified=True), monkeypatch)

    assert "already lists this file as <strong>missing</strong>" in html
    assert 'href="/collection/1/review"' in html
    assert "contents may have changed" not in html


def test_an_unreadable_file_shows_its_last_recorded_fingerprint(cairn_env, monkeypatch):
    """#15: "(unknown)" threw away the one fact Cairn still holds about a file that has gone."""
    with _unavailable_client(cairn_env, status="missing") as client:
        html = _post_verify(client, _result(verified=True), monkeypatch)

    assert "(unknown)" not in html
    assert "Last recorded fingerprint" in html
    assert "d" * 64 in html
    assert "not</strong> compared with anything in this check" in html


def test_the_backend_is_not_named_where_no_lookup_happened(cairn_env, monkeypatch):
    """#15: "Checked using <explorer>" on a card where nothing was ever fetched.

    A file that could not be read never reached the network, so naming a backend reports a check
    that did not run — and the closing strip repeated the same claim.
    """
    with _unavailable_client(cairn_env) as client:
        html = _post_verify(client, _result(verified=True), monkeypatch)

    assert "Checked using" not in html
    assert "Verified via:" not in html


def test_the_backend_is_not_named_on_a_never_notarized_file(cairn_env, monkeypatch):
    root = cairn_env / "unstamped"
    root.mkdir()
    (root / "doc.txt").write_text("hello")

    async def seed():
        cid = await _seed_one_anchored_file(root, ots_state="none")
        from src.database import get_sessionmaker
        from src.models.db import FileEntry

        async with get_sessionmaker()() as s:
            fe = await s.get(FileEntry, 1)
            fe.ots_path = None  # nothing to verify -> `result` is None
            await s.commit()
        return cid

    with _make_client(seed) as client:
        token = _csrf_token(client)
        html = client.post("/verify", data={"csrf_token": token, "file_id": 1}).text

    assert "Not notarized yet" in html
    assert "Checked using" not in html


def test_the_backend_is_named_on_a_verified_card(verify_client, monkeypatch):
    """The other half of #15: where a lookup DID happen, the strip must stay."""
    html = _post_verify(
        verify_client,
        _result(verified=True, block_height=880000, existed_by="2026-02-14 18:35 UTC"),
        monkeypatch,
    )
    assert "Checked using" in html
    assert "Verified via:" in html


def test_a_long_unconfirmed_proof_drops_the_few_hours_reassurance(cairn_env, monkeypatch):
    """M5: "usually settles within a few hours" is false — and actively misleading — at 90 days.

    Threshold mirrors `cairn upgrade`'s stale-incomplete warning
    (`CAIRN_INCOMPLETE_PROOF_ALARM_DAYS`, default 7) so the two surfaces cannot disagree about when
    a submitted proof has waited too long.
    """
    root = cairn_env / "stuck"
    root.mkdir()
    (root / "doc.txt").write_text("hello")
    with _make_client(
        lambda: _seed_one_anchored_file(root, ots_state="incomplete", stamped_days_ago=90)
    ) as client:
        html = _post_verify(client, _result(state="incomplete"), monkeypatch)

    assert "Pending confirmation" in html
    assert "usually settles within a few hours" not in html
    assert "90 days ago" in html
    assert "unusually long" in html
    assert "cairn upgrade" in html
    assert "calendar servers" in html


def test_a_freshly_submitted_proof_keeps_the_reassurance_and_gains_a_date(cairn_env, monkeypatch):
    """M5's other side: at one day old the reassurance is true and must survive."""
    root = cairn_env / "fresh"
    root.mkdir()
    (root / "doc.txt").write_text("hello")
    with _make_client(
        lambda: _seed_one_anchored_file(root, ots_state="incomplete", stamped_days_ago=1)
    ) as client:
        html = _post_verify(client, _result(state="incomplete"), monkeypatch)

    assert "was submitted to Bitcoin on" in html
    assert "This usually settles within a few hours." in html
    assert "unusually long" not in html


def test_the_trustless_route_names_the_env_settings_not_the_settings_page(
    verify_client, monkeypatch
):
    """#8: Settings renders the verification backend read-only, so sending the operator there is a
    dead end. Name the two environment settings that actually select the node backend."""
    html = _post_verify(verify_client, _result(verified=True), monkeypatch)

    assert "CAIRN_VERIFY_BACKEND=node" in html
    assert "CAIRN_NODE_RPC_URL" in html
    assert "Bitcoin node in Settings" not in html


def test_every_copy_control_on_the_verify_card_uses_the_shared_helper(verify_client, monkeypatch):
    """M4: `navigator.clipboard && …writeText(…)` is a silent no-op on an insecure origin, which is
    where a self-hosted panel normally runs. Both buttons go through the one shared helper."""
    html = _post_verify(verify_client, _result(verified=True), monkeypatch)

    assert "navigator.clipboard && navigator.clipboard.writeText" not in html
    assert html.count("cairnCopy(") >= 2  # the fingerprint button and the report button

    # The card is an htmx fragment, so the helper it calls has to already be on the page it swaps
    # into: base.html includes it everywhere, with the insecure-origin fallback and the visible
    # result. Asserted on the full page, which is what the fragment actually lands in.
    page = verify_client.get("/verify").text
    assert "window.cairnCopy" in page
    assert "execCommand" in page and ".catch(" in page
    assert "Couldn't copy" in page


# =============================================================================================
# UX audit sprint 2 (#36, #41): the search covers every tracked file, and every badge reaches it
# =============================================================================================
#
# The defect both issues share is a surface that silently withholds files: a search filtered to
# submitted proofs, and a badge that links only in the confirmed state. Between them, the verify
# card's guidance for a never-stamped or queued file — the only card that carries an action — was
# reachable only by hand-typing a URL.


async def _seed_tracked(root, specs, *, ots_mode: str = "perfile") -> int:
    """One collection seeded from ``(relpath, status, ots_state)`` triples; returns its id."""
    from src.database import get_sessionmaker
    from src.models.db import FileEntry

    cid = await seed_collection(root, ots_mode=ots_mode)
    now = datetime.now(timezone.utc)
    async with get_sessionmaker()() as s:
        for relpath, status, ots_state in specs:
            stamped = ots_state in ("incomplete", "complete")
            s.add(FileEntry(
                collection_id=cid, relpath=relpath, size=5, sha256="a" * 64,
                status=status, ots_state=ots_state,
                ots_path=(f"/p/{relpath}.ots" if ots_state != "none" else None),
                ots_stamped_at=(now if stamped else None),
                first_seen=now, last_checked=now,
            ))
        await s.commit()
    return cid


_MIXED = [
    ("anchored-report.pdf", "ok", "complete"),
    ("submitted-report.pdf", "ok", "incomplete"),
    ("queued-report.pdf", "new", "pending"),
    ("raw-report.pdf", "new", "none"),
]


def test_search_returns_files_in_every_proof_state_with_the_state_visible(cairn_env):
    """The widened population (design D2) — and it is visibly widened, row by row."""
    root = cairn_env / "mixed"
    root.mkdir()

    with _make_client(lambda: _seed_tracked(root, _MIXED)) as client:
        html = client.get("/verify/search", params={"q": "report"}).text

    for relpath, _, _ in _MIXED:
        assert relpath in html
    # Each row wears its own state, so an unstamped row is visibly unstamped in the list.
    assert "Not stamped" in html
    assert "Queued to stamp" in html
    assert "Pending confirmation" in html
    # Every row — not only the confirmed one — posts into the per-file verify flow.
    for file_id in (1, 2, 3, 4):
        assert '{"file_id": %d}' % file_id in html
    assert "4 matches" in html


def test_a_blank_query_renders_the_recent_listing_on_both_routes(cairn_env):
    """Blank stays default (design D2): clearing the box restores the page, never the wide list."""
    root = cairn_env / "blank"
    root.mkdir()

    with _make_client(lambda: _seed_tracked(root, _MIXED)) as client:
        partial = client.get("/verify/search", params={"q": "   "}).text
        page = client.get("/verify", params={"q": "  "}).text

    for html in (partial, page):
        assert "Recent proofs" in html
        # The two submitted proofs, and only those: the widened population is unreachable without
        # a non-blank query, or clearing the input leaks every unstamped file under a heading that
        # says "proofs".
        assert "anchored-report.pdf" in html
        assert "submitted-report.pdf" in html
        assert "queued-report.pdf" not in html
        assert "raw-report.pdf" not in html


def test_a_capped_result_set_states_its_true_total_and_orders_by_path(cairn_env):
    """A silent cap on a search whose purpose is finding one file re-hides files one level down."""
    from src.control_panel.routes import VERIFY_SEARCH_ROW_LIMIT as CAP

    root = cairn_env / "many"
    root.mkdir()
    over = CAP + 7
    specs = [(f"doc-{i:03d}.txt", "ok", "complete") for i in range(over)]
    # The one file the recency order would bury: alphabetically first, never stamped.
    specs.append(("doc-000-unstamped.txt", "new", "none"))

    with _make_client(lambda: _seed_tracked(root, specs)) as client:
        html = client.get("/verify/search", params={"q": "doc-"}).text

    assert f"Showing {CAP} of {over + 1} matches" in html
    assert f"Only the first {CAP} of {over + 1} matching files are listed" in html
    assert "Narrow the search" in html
    assert "file browser" in html
    # Path order, not recency of stamping — which is what makes the never-stamped file reachable
    # at all: it sorts first, where a `ots_stamped_at DESC` order left it past the cap forever.
    assert html.index("doc-000-unstamped.txt") < html.index("doc-000.txt")
    # The page is the first CAP rows of the path order and stops there: 'doc-000-unstamped.txt'
    # sorts ahead of 'doc-000.txt', so the last row shown is 'doc-048.txt'.
    assert "doc-048.txt" in html and "doc-049.txt" not in html


def test_identical_paths_across_collections_order_by_collection_and_name_it(cairn_env):
    a, b = cairn_env / "alpha", cairn_env / "beta"
    a.mkdir()
    b.mkdir()

    async def seed():
        await _seed_tracked(a, [("same.txt", "ok", "complete")])
        await _seed_tracked(b, [("same.txt", "ok", "none")])

    with _make_client(seed) as client:
        first = client.get("/verify/search", params={"q": "same.txt"}).text
        second = client.get("/verify/search", params={"q": "same.txt"}).text

    assert first == second  # the order key is unique, so repeated searches render identically
    assert "2 matches" in first
    # Each row names its collection, which is the only thing telling the two rows apart.
    assert first.index("alpha") < first.index("beta")


def test_search_copy_describes_tracked_files_for_an_all_unstamped_owner(cairn_env):
    """An operator whose files are all unstamped must not read a zero count of proofs."""
    root = cairn_env / "unstamped-only"
    root.mkdir()

    specs = [("a.txt", "new", "none"), ("b.txt", "new", "none"), ("c.txt", "new", "none")]
    with _make_client(lambda: _seed_tracked(root, specs)) as client:
        page = client.get("/verify").text
        hit = client.get("/verify/search", params={"q": "a.txt"}).text
        miss = client.get("/verify/search", params={"q": "zzz"}).text

    assert "Search 3 tracked files by path or collection" in page
    assert "files with proofs" not in page
    assert "1 match" in hit and "a.txt" in hit
    assert "No tracked files match “zzz”" in miss
    for html in (hit, miss):
        assert "anchored" not in html.lower()


def test_the_recent_listing_keeps_its_population_and_never_says_anchored(cairn_env):
    root = cairn_env / "recent"
    root.mkdir()

    with _make_client(lambda: _seed_tracked(root, _MIXED)) as client:
        html = client.get("/verify").text

    assert "Recent proofs" in html
    assert "queued-report.pdf" not in html and "raw-report.pdf" not in html
    # Its population includes a proof awaiting confirmation, so its own chrome may not call it
    # anchored — only the confirmed row's badge may.
    assert "Recently anchored" not in html
    assert "anchored files" not in html.lower()


def test_verify_search_is_scoped_to_the_owner_in_multi_mode(cairn_env, monkeypatch):
    from src.config import get_settings

    mine, theirs = cairn_env / "mine", cairn_env / "theirs"
    mine.mkdir()
    theirs.mkdir()

    async def seed():
        await _seed_tracked(mine, [("shared-name.txt", "ok", "none")])

        from src.database import get_sessionmaker
        from src.models.db import FileEntry, User
        from src.services.collections import create_collection

        async with get_sessionmaker()() as s:
            u = User(username="bob", is_admin=False)
            s.add(u)
            await s.commit()
            c = await create_collection(
                s, user_id=u.id, name="Bobs Files", root=str(theirs), ots_mode="perfile"
            )
            now = datetime.now(timezone.utc)
            s.add(FileEntry(
                collection_id=c.id, relpath="shared-name.txt", size=5, sha256="b" * 64,
                status="ok", ots_state="complete", ots_path="/p/bob.ots",
                ots_stamped_at=now, first_seen=now, last_checked=now,
            ))
            await s.commit()

    with _make_client(seed) as client:
        monkeypatch.setenv("CAIRN_AUTH_MODE", "multi")
        monkeypatch.setenv("CAIRN_SECRET_KEY", "0" * 64)
        get_settings.cache_clear()
        try:
            html = client.get("/verify/search", params={"q": "shared-name"}).text
            page = client.get("/verify", params={"q": "shared-name"}).text
        finally:
            get_settings.cache_clear()

    for body in (html, page):
        assert "Bobs Files" not in body
        # The disclosed total is the owner's own count, not the fleet's — a truncation notice
        # counting another user's files would leak their existence.
        assert "1 match" in body


# --- #36: the top-bar search is a real control ------------------------------------------------


def test_the_topbar_search_is_a_form_that_promises_only_paths(cairn_env):
    root = cairn_env / "chrome"
    root.mkdir()

    with _make_client(lambda: _seed_tracked(root, _MIXED)) as client:
        html = client.get("/").text

    assert '<form class="topbar__search" action="/verify" method="get"' in html
    assert 'name="q"' in html
    assert "Search files and paths…" in html
    # The backend is a path LIKE, so the box may not advertise a digest lookup: a pasted hash would
    # come back "no matches", which reads as "this digest is not tracked".
    assert "hashes" not in html


def test_the_verify_page_runs_a_submitted_query_and_prefills_the_input(cairn_env):
    root = cairn_env / "fromtopbar"
    root.mkdir()

    with _make_client(lambda: _seed_tracked(root, _MIXED)) as client:
        html = client.get("/verify", params={"q": "queued-report"}).text

    assert 'value="queued-report"' in html
    assert "queued-report.pdf" in html
    assert "anchored-report.pdf" not in html
    assert "1 match" in html


def test_a_top_bar_query_with_no_matches_renders_the_search_empty_state(cairn_env):
    root = cairn_env / "nomatch"
    root.mkdir()

    with _make_client(lambda: _seed_tracked(root, _MIXED)) as client:
        r = client.get("/verify", params={"q": "no-such-file"})

    assert r.status_code == 200
    assert "No tracked files match “no-such-file”" in r.text
    assert "raw-report.pdf" not in r.text


# --- #41: the badge links in every proof state -----------------------------------------------


def test_the_proof_badge_links_in_every_state_in_both_browser_views(cairn_env):
    root = cairn_env / "browser"
    root.mkdir()

    with _make_client(lambda: _seed_tracked(root, _MIXED)) as client:
        html = client.get("/collection/1").text

    # The detail page renders the flat list AND the folder tree, both through `file_row`, so each
    # file's badge appears twice — and every one of them is wrapped in the link.
    assert html.count('class="ots-badge"') == 8
    for file_id in (1, 2, 3, 4):
        assert html.count(f'href="/verify?file={file_id}"') == 2
    assert "Verify this proof for the block-confirmed date" in html  # the confirmed state's title
    assert "Check this file's notarization status" in html  # every other state's


def test_a_tripwire_collections_never_stamped_card_does_not_advise_stamp_all(cairn_env):
    """The widened search reaches `none` rows in tripwire collections, where stamping is refused.

    The card's guidance has to be an action the operator can take: a tripwire collection has no
    "Stamp all" control and its stamp route refuses it, so pointing at one is the same dead end
    #41 is about, one surface along.
    """
    root = cairn_env / "tripwire"
    root.mkdir()
    (root / "doc.txt").write_text("hello")

    async def seed():
        await _seed_tracked(root, [("doc.txt", "ok", "none")], ots_mode="none")

    with _make_client(seed) as client:
        token = _csrf_token(client)
        html = client.post("/verify", data={"csrf_token": token, "file_id": 1}).text

    assert "Not notarized yet" in html
    assert "tripwire-only collection" in html
    assert "Stamp all" not in html
    assert "verdict--danger" not in html


def test_a_notarized_collections_never_stamped_card_still_points_at_stamp_all(cairn_env):
    html = _verify_unstamped(cairn_env, "perfile-unstamped", "none")
    assert "Stamp all" in html
    assert "tripwire-only collection" not in html
