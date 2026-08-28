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
from datetime import datetime, timezone
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
    root: Path, *, ots_state: str = "complete", sha256: str | None = "d" * 64
) -> int:
    """One collection with a single on-disk, stamped file. Returns the collection id.

    ``sha256`` is the file's RECORDED BASELINE — the digest the last scan wrote. It is the
    tiebreaker both consumers use to attribute a digest disagreement (design D1), so the tests that
    exercise blame set it deliberately: equal to the live hash of the on-disk bytes means the file
    is intact, different means it changed, ``None`` means there is no baseline to compare against.
    """
    from src.database import get_sessionmaker
    from src.models.db import FileEntry

    cid = await seed_collection(root)
    now = datetime.now(timezone.utc)
    async with get_sessionmaker()() as s:
        s.add(FileEntry(
            collection_id=cid, relpath="doc.txt", size=5, sha256=sha256,
            status="ok", ots_state=ots_state, ots_path=str(root.parent / "p" / "doc.txt.ots"),
            ots_stamped_at=now, first_seen=now, last_checked=now,
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
    assert "submitted to Bitcoin but isn't confirmed yet" in html


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
        assert "No files have been anchored yet" in html


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


def _cmd_verify_output(cairn_env, capsys, result, *, ots_state="complete", sha256="d" * 64):
    """Run `cairn verify doc.txt` with `ots.verify` stubbed; return (rc, stdout, stderr)."""
    import src.cli as cli
    from src.services import ots as ots_svc

    root = cairn_env / "cliv"
    root.mkdir()
    (root / "doc.txt").write_text("hello")

    async def seed():
        await _seed_one_anchored_file(root, ots_state=ots_state, sha256=sha256)

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
def no_baseline_client(cairn_env):
    """Panel client for a row with no recorded baseline digest at all."""
    root = cairn_env / "nobase"
    root.mkdir()
    (root / "doc.txt").write_text("hello")
    with _make_client(lambda: _seed_one_anchored_file(root, sha256=None)) as client:
        yield client


def test_a_disagreement_over_an_intact_file_blames_the_proof_not_the_file(
    intact_file_client, monkeypatch
):
    """The file still hashes to its recorded baseline, so the PROOF is what disagrees.

    Pre-fix this rendered "the file has changed since it was stamped. The proof itself is intact"
    — both halves false — for a `.ots` with one flipped byte in its serialized digest.
    """
    html = _post_verify(intact_file_client, _result(digest_mismatch=True), monkeypatch)
    assert "This proof does not match this file" in html
    assert "may be corrupted" in html
    assert "not evidence that" in html
    # The two accusations the pre-fix card made, neither of which was established:
    assert "has changed since it was stamped" not in html
    assert "The proof itself is intact" not in html


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


def test_cli_disagreement_over_an_intact_file_blames_the_proof(cairn_env, capsys):
    rc, out, err = _cmd_verify_output(
        cairn_env, capsys, _result(digest_mismatch=True), sha256=_LIVE_SHA_OF_HELLO
    )
    assert rc == 1
    combined = out + err
    assert "PROOF DOES NOT MATCH THIS FILE" in combined
    assert "NOT evidence the file changed" in combined
    assert "CHANGED —" not in combined  # the pre-fix accusation


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
