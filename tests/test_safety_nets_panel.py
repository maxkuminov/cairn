"""Panel half of `add-scan-safety-nets`: what the review row, the guidance, the policy text, the
failed-scan note and the activity summary say once the scanner reports possible corruption, keeps
the contents a change replaced, and refuses an unreachable folder.

Spec: openspec web-panel — "The review view lists issues by folder with the facts needed to
decide" (possible corruption, the earlier size, the exact condition for a changed file), "Policies
and collection status are named in plain words", "A scan that could not run says why".
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import update

from tests.test_ux_review import (
    _help,
    _make_client,
    _photos,
    _review,
    _row_html,
    _text,
)

DIGESTS = "recorded " + "a" * 64 + " → found " + "b" * 64


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- the row -------------------------------------------------------------------------------


def test_possible_corruption_is_labelled_on_the_row_as_an_alarm(cairn_env):
    seed = _photos(cairn_env, [
        ("2019/a.jpg", "modified", {"kind": "corrupted"}),
        ("2019/b.jpg", "modified"),
    ])
    with _make_client(cairn_env, seed) as client:
        body, r = _review(client)

    kinds = {row["relpath"]: row["kind"] for row in r["rows"]}
    assert kinds == {"2019/a.jpg": "corrupted", "2019/b.jpg": "changed"}
    corrupt, plain = _row_html(body, 1), _row_html(body, 2)
    said = _text(corrupt)
    assert "Possible corruption" in said
    assert "contents changed, size and date did not" in said
    assert "review-row__alarm" in corrupt and "review-row__icon--danger" in corrupt
    # Never drawn as an ordinary change — and the ordinary change is not drawn as corruption.
    assert "Contents changed" not in said
    assert "Possible corruption" not in _text(plain) and "review-row__icon--warn" in plain
    # It is still a changed file: the one action is accepting the change.
    assert "Accept change" in corrupt and "Accept deletion" not in corrupt


def test_came_back_different_outranks_possible_corruption(cairn_env):
    """A file carrying both alerts shows the one with the digest record."""
    seed = _photos(cairn_env, [
        ("a.jpg", "modified", {
            "kind": "corrupted",
            "history": [("restored_changed", _now() - timedelta(days=1), False)],
        }),
    ])

    async def seed_with_detail():
        from src.database import get_sessionmaker
        from src.models.db import Event

        await seed()
        async with get_sessionmaker()() as s:
            await s.execute(
                update(Event).where(Event.kind == "restored_changed").values(detail=DIGESTS)
            )
            await s.commit()

    with _make_client(cairn_env, seed_with_detail) as client:
        body, r = _review(client)

    assert r["rows"][0]["kind"] == "came_back"
    row = _row_html(body, 1)
    assert "Came back different" in _text(row) and "Possible corruption" not in _text(row)
    assert "a" * 64 in row and "b" * 64 in row


def test_a_changed_row_says_what_the_size_was(cairn_env):
    seed = _photos(cairn_env, [
        ("grew.txt", "modified", {"size": 5632}),
        ("same.txt", "modified", {"size": 4096}),
        ("unknown.txt", "modified", {"size": 100}),
        ("gone.txt", "missing", {"size": 2048}),
    ])

    async def seed_priors():
        from src.database import get_sessionmaker
        from src.models.db import FileEntry

        await seed()
        async with get_sessionmaker()() as s:
            for relpath, prior in (("grew.txt", 5427), ("same.txt", 4096), ("gone.txt", 999)):
                await s.execute(
                    update(FileEntry).where(FileEntry.relpath == relpath).values(prior_size=prior)
                )
            await s.commit()

    with _make_client(cairn_env, seed_priors) as client:
        body, r = _review(client)

    by_path = {row["relpath"]: row for row in r["rows"]}
    assert by_path["grew.txt"]["was_size"] == "5.3 KiB"
    assert "5.5 KiB (was 5.3 KiB)" in _text(_row_html(body, by_path["grew.txt"]["id"]))
    # Unchanged size, no size on record, or a missing file: nothing to say.
    for relpath in ("same.txt", "unknown.txt", "gone.txt"):
        assert by_path[relpath]["was_size"] is None
        assert "(was " not in _text(_row_html(body, by_path[relpath]["id"]))


def test_the_earlier_size_never_enters_a_fingerprint(cairn_env):
    """`prior_size` is display only: changing it must not refuse an accept that is otherwise
    unchanged (it is not a statement about which records the action covers)."""
    seed = _photos(cairn_env, [("a.txt", "modified", {"size": 10})])
    with _make_client(cairn_env, seed) as client:
        _body, before = _review(client)

        async def bump():
            from src.database import get_sessionmaker
            from src.models.db import FileEntry

            async with get_sessionmaker()() as s:
                await s.execute(update(FileEntry).values(prior_size=7))
                await s.commit()

        import asyncio

        asyncio.run(bump())
        _body, after = _review(client)

    assert after["rows"][0]["was_size"] == "7 B"
    assert after["rows"][0]["fp"] == before["rows"][0]["fp"]
    assert after["accept_changes"]["fp"] == before["accept_changes"]["fp"]


# --- guidance and vocabulary ---------------------------------------------------------------


def test_recovery_guidance_states_the_exact_condition_for_a_changed_file(cairn_env):
    seed = _photos(cairn_env, [("a.txt", "modified"), ("b.txt", "missing")])
    with _make_client(cairn_env, seed) as client:
        body, _r = _review(client)
        learn = _text(client.get("/learn").text)

    said = _text(_help(body))
    assert "exactly as it was before the change" in said
    assert "Any other version shows up as one more change." in said
    # The old statements are gone: they are no longer true.
    assert "No earlier version is kept" not in said
    assert "putting an earlier version back shows up as one more change" not in said
    # ...and it does not over-promise: it is not "any earlier version".
    assert "restoring an earlier version" not in said.lower()
    assert "exactly as it was before the change" in learn


def test_the_working_policy_names_everything_it_flags(cairn_env):
    seed = _photos(cairn_env, [("a.txt", "ok")], mode="churn")
    with _make_client(cairn_env, seed) as client:
        detail = _text(client.get("/collection/1").text)
        form = _text(client.get("/collection/1/edit").text)

    for page in (detail, form):
        assert "Ordinary edits are not flagged." in page
        assert "comes back different" in page
        assert "possible corruption" in page
        assert "unusually large change in one scan" in page
        assert "nor, yet" not in page  # the caveat this change makes untrue


# --- a scan that could not run says why ------------------------------------------------------


def _seed_runs(cairn_env, runs):
    """`photos` with one healthy file and the given scan runs, oldest first:
    ``(result, minutes_ago, errors, error_sample_list_or_None)``."""
    seed = _photos(cairn_env, [("a.txt", "ok")])

    async def go():
        from src.database import get_sessionmaker
        from src.models.db import Run

        await seed()
        async with get_sessionmaker()() as s:
            for result, minutes_ago, errors, sample in runs:
                at = _now() - timedelta(minutes=minutes_ago)
                s.add(Run(
                    collection_id=1, kind="scan", result=result, started=at, finished=at,
                    errors=errors, error_sample=json.dumps(sample) if sample is not None else None,
                ))
            await s.commit()

    return go


UNREACHABLE = "the folder /corpora/photos is not reachable"


def test_an_unreachable_folder_is_named_on_the_card_and_the_page(cairn_env):
    seed = _seed_runs(cairn_env, [
        ("ok", 120, 0, None),
        ("error", 5, 0, [f"unreachable: {UNREACHABLE}"]),
    ])
    with _make_client(cairn_env, seed) as client:
        pages = [_text(client.get(u).text) for u in ("/", "/collections", "/collection/1")]

    for page in pages:
        assert f"the latest scan could not run — {UNREACHABLE}" in page
        assert "Nothing was marked missing" in page
        assert "check the logs" not in page
        # The collection's files are not described as missing.
        assert "1 missing" not in page and "All clear" in page


def test_the_reason_shown_belongs_to_the_failed_run(cairn_env):
    """The last COMPLETED scan skipped a file; a later scan was refused. The failure note states
    the refusal's reason, and the completed scan's sample is not presented as that reason."""
    seed = _seed_runs(cairn_env, [
        ("partial", 120, 1, ["b'bad\\xffname.jpg'"]),
        ("error", 5, 0, ["empty: the folder /corpora/photos is empty but 1 file is tracked"]),
    ])
    with _make_client(cairn_env, seed) as client:
        page = client.get("/collection/1").text

    said = _text(page)
    assert "the latest scan could not run — the folder /corpora/photos is empty but 1 file is tracked" in said
    note = page.split("the latest scan could not run")[1].split("</div>")[0]
    assert "bad" not in note
    # The completed scan's own note is still there, about its own skipped file.
    assert "partial — 1 file skipped" in said


def test_a_failure_with_no_recorded_reason_keeps_the_generic_note(cairn_env):
    seed = _seed_runs(cairn_env, [("ok", 120, 0, None), ("error", 5, 0, None)])
    with _make_client(cairn_env, seed) as client:
        said = _text(client.get("/collection/1").text)

    assert "a later scan failed — check the logs" in said
    assert "could not run" not in said


def test_a_scan_that_could_not_list_a_folder_does_not_say_files_were_skipped(cairn_env):
    seed = _seed_runs(cairn_env, [
        ("partial", 5, 2, ["unreadable: 'clients/2024'", "unreadable: 'clients/2025'"]),
    ])
    with _make_client(cairn_env, seed) as client:
        said = _text(client.get("/collection/1").text)

    assert "some folders could not be read" in said
    assert "the files beneath them were not checked" in said
    assert "nothing was marked missing" in said
    assert "2 files skipped" not in said


# --- recent activity -----------------------------------------------------------------------


def test_recent_activity_names_possible_corruption(cairn_env):
    seed = _photos(cairn_env, [
        ("a.jpg", "modified", {"kind": "corrupted"}),
        ("b.jpg", "modified"),
        ("c.jpg", "modified"),
    ])
    with _make_client(cairn_env, seed) as client:
        page = client.get("/").text

    activity = _text(page[page.index('id="recent-activity"'):].split("</div>")[0] + "</div>")
    whole = _text(page[page.index('id="recent-activity"'):])
    assert "1 possibly corrupted" in whole
    assert "2 changed" in whole and "3 changed" not in whole
    assert activity is not None
