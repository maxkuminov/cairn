"""Path rules in the scanner (add-path-rules, design D3).

Acceptance criteria: ``openspec/changes/add-path-rules/specs/integrity-scanning/spec.md``
("Path rules decide which ordinary changes are flagged", the MODIFIED mass-change and kept-contents
requirements) and ``specs/alerting/spec.md``. One test per scenario, named after it.

Run from the repo root: ``PYTHONPATH=. pytest tests/test_scan_rules.py``
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import update

from tests.test_scan_safety_nets import (
    _collection,
    _corrupt,
    _edit,
    _events,
    _file,
    _files,
    _open_events,
    _scan,
    _sha,
    _snapshot,
)


@pytest.fixture
def alerts(monkeypatch):
    """Capture every alert dispatch (the scanner imports `dispatch` at call time)."""
    from src.notify import dispatch as dispatch_mod

    sent: list = []

    async def _capture(alert, collection, settings):
        sent.append(alert)

    monkeypatch.setattr(dispatch_mod, "dispatch", _capture)
    return sent


@pytest.fixture
def mass(monkeypatch):
    from src.config import get_settings

    def _set(percent: int, min_files: int) -> None:
        monkeypatch.setenv("CAIRN_MASS_CHANGE_PERCENT", str(percent))
        monkeypatch.setenv("CAIRN_MASS_CHANGE_MIN_FILES", str(min_files))
        get_settings.cache_clear()

    yield _set
    get_settings.cache_clear()


async def _add_rule(cid: int, pattern: str) -> None:
    from src.database import get_sessionmaker
    from src.models.db import Collection
    from src.services.rules import add_rule

    async with get_sessionmaker()() as s:
        await add_rule(s, await s.get(Collection, cid), pattern)


async def _remove_rule(cid: int, pattern: str) -> bool:
    from src.database import get_sessionmaker
    from src.models.db import Collection
    from src.services.rules import remove_rule

    async with get_sessionmaker()() as s:
        return await remove_rule(s, await s.get(Collection, cid), pattern)


async def _set_perfile_complete(cid: int) -> None:
    """Make the collection notarized with every proof settled, so a re-queue is observable."""
    from src.database import get_sessionmaker
    from src.models.db import Collection, FileEntry

    async with get_sessionmaker()() as s:
        await s.execute(update(Collection).where(Collection.id == cid).values(ots_mode="perfile"))
        await s.execute(
            update(FileEntry).where(FileEntry.collection_id == cid).values(ots_state="complete")
        )
        await s.commit()


@pytest.fixture
def no_stamping(monkeypatch):
    from src.services import proofs

    async def _noop(session, collection, settings=None, *, progress=None, run_id=None):
        return 0

    monkeypatch.setattr(proofs, "stamp_pending", _noop)


def _db(cairn_env):
    return cairn_env / "db" / "cairn.db"


# ================================================================================================
# Requirement: Path rules decide which ordinary changes are flagged
# ================================================================================================


@pytest.mark.asyncio
async def test_a_rule_covered_change_in_a_worm_collection_is_not_flagged(
    cairn_env, alerts, no_stamping
):
    root, cid = await _collection(cairn_env, "w", {"notes/NOTES.md": "one", "keep.txt": "k"})
    await _set_perfile_complete(cid)
    await _add_rule(cid, "NOTES.md")
    _edit(root / "notes/NOTES.md", "two, longer")
    alerts.clear()

    summary = await _scan(cid)

    row = await _file(cid, "notes/NOTES.md")
    assert row.status == "ok" and row.sha256 == _sha("two, longer")
    assert row.prior_sha256 is None
    assert row.ots_state == "pending", "queued for a stamp in a notarized collection"
    assert await _open_events(cid, row.id) == []
    assert await _events(cid, kind="modified") == [], "no event for a quiet change"
    assert summary.modified == 0 and summary.mass_change == 0
    # Alerting: a rule-covered change does not alert.
    assert alerts == []


@pytest.mark.asyncio
async def test_a_file_the_rule_does_not_match_is_still_flagged(cairn_env, alerts):
    root, cid = await _collection(cairn_env, "w2", {"a/NOTES.md": "one", "a/other.md": "x"})
    await _add_rule(cid, "NOTES.md")
    _edit(root / "a/NOTES.md", "two!")
    _edit(root / "a/other.md", "y, changed")
    alerts.clear()

    summary = await _scan(cid)

    other = await _file(cid, "a/other.md")
    assert other.status == "modified"
    (ev,) = await _events(cid, kind="modified")
    assert ev.file_id == other.id and ev.acknowledged_at is None
    assert (await _file(cid, "a/NOTES.md")).status == "ok"
    assert summary.modified == 1
    assert len(alerts) == 1 and alerts[0].paths == ["a/other.md"]


@pytest.mark.asyncio
async def test_a_rule_covered_file_is_still_watched(cairn_env, alerts, monkeypatch):
    from src.services import scanner

    root, cid = await _collection(cairn_env, "watch", {"c/x.db": "x", "y.txt": "y"})
    await _add_rule(cid, "c/")
    hashed: list[str] = []
    real = scanner._hash

    async def _spy(path):
        hashed.append(path.name)
        return await real(path)

    monkeypatch.setattr(scanner, "_hash", _spy)
    await _scan(cid, deep=True)
    assert sorted(hashed) == ["x.db", "y.txt"]


@pytest.mark.asyncio
async def test_a_deletion_is_flagged_in_spite_of_a_rule(cairn_env, alerts):
    root, cid = await _collection(cairn_env, "del", {"c/x.db": "x", "y.txt": "y"})
    await _add_rule(cid, "c/")
    (root / "c/x.db").unlink()
    alerts.clear()

    summary = await _scan(cid)

    row = await _file(cid, "c/x.db")
    assert row is not None and row.status == "missing"
    (ev,) = await _events(cid, kind="missing")
    assert ev.file_id == row.id and ev.acknowledged_at is None
    assert summary.missing == 1
    # Alerting: a rule does not silence what it never covers.
    assert len(alerts) == 1 and alerts[0].paths == ["c/x.db"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["worm", "churn"])
async def test_corruption_is_flagged_in_spite_of_a_rule(cairn_env, alerts, mode):
    root, cid = await _collection(cairn_env, f"cor-{mode}", {"c/x.db": "AAAAA", "y": "y"}, mode=mode)
    await _add_rule(cid, "c/")
    _corrupt(root / "c/x.db", "BBBBB")
    alerts.clear()

    summary = await _scan(cid, deep=True)

    row = await _file(cid, "c/x.db")
    assert row.status == "modified" and row.prior_sha256 == _sha("AAAAA")
    (ev,) = await _events(cid, kind="corrupted")
    assert ev.file_id == row.id and ev.acknowledged_at is None
    assert summary.corrupted == 1
    assert len(alerts) == 1 and alerts[0].summary == "1 possibly corrupted"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["worm", "churn"])
async def test_a_changed_reappearance_is_flagged_in_spite_of_a_rule(cairn_env, alerts, mode):
    root, cid = await _collection(cairn_env, f"re-{mode}", {"c/x.db": "one", "y": "y"}, mode=mode)
    await _add_rule(cid, "c/")
    (root / "c/x.db").unlink()
    await _scan(cid)
    (root / "c/x.db").write_text("something else entirely")
    alerts.clear()

    summary = await _scan(cid)

    row = await _file(cid, "c/x.db")
    assert row.status == "modified"
    (ev,) = await _events(cid, kind="restored_changed")
    assert ev.file_id == row.id and ev.acknowledged_at is None
    assert summary.restored_changed == 1
    assert len(alerts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["modified", "corrupted"])
async def test_an_open_change_alert_survives_a_rule_covered_edit(cairn_env, alerts, first):
    root, cid = await _collection(cairn_env, f"open-{first}", {"c/x.db": "AAAAA", "y": "y"})
    if first == "modified":
        _edit(root / "c/x.db", "changed once")
        await _scan(cid)
    else:
        _corrupt(root / "c/x.db", "BBBBB")
        await _scan(cid, deep=True)
    row = await _file(cid, "c/x.db")
    assert row.status == "modified" and row.prior_sha256 == _sha("AAAAA")
    (open_ev,) = await _open_events(cid, row.id)

    await _add_rule(cid, "c/")
    _edit(root / "c/x.db", "changed again, differently")
    alerts.clear()
    summary = await _scan(cid)

    row = await _file(cid, "c/x.db")
    assert row.status == "modified"
    assert row.sha256 == _sha("changed again, differently")
    assert row.prior_sha256 == _sha("AAAAA"), "kept contents unchanged"
    assert [e.id for e in await _open_events(cid, row.id)] == [open_ev.id]
    assert len(await _events(cid, file_id=row.id, kind=first)) == 1, "no further alert"
    assert summary.mass_change == 0
    assert alerts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["worm", "churn"])
async def test_an_open_restored_changed_alert_survives_a_rule_covered_edit(cairn_env, alerts, mode):
    root, cid = await _collection(cairn_env, f"rc-{mode}", {"c/x.db": "one", "y": "y"}, mode=mode)
    await _add_rule(cid, "c/")
    (root / "c/x.db").unlink()
    await _scan(cid)
    (root / "c/x.db").write_text("came back different")
    await _scan(cid)
    row = await _file(cid, "c/x.db")
    assert row.status == "modified"
    (rc,) = await _events(cid, kind="restored_changed", file_id=row.id)
    assert rc.acknowledged_at is None
    kept = row.prior_sha256
    assert kept == _sha("one")

    _edit(root / "c/x.db", "and then edited again")
    alerts.clear()
    summary = await _scan(cid)

    row = await _file(cid, "c/x.db")
    assert row.status == "modified" and row.sha256 == _sha("and then edited again")
    assert row.prior_sha256 == kept
    assert [e.id for e in await _open_events(cid, row.id)] == [rc.id]
    assert await _events(cid, kind="modified", file_id=row.id) == []
    assert summary.mass_change == 0
    assert alerts == []


@pytest.mark.asyncio
async def test_something_else_again_under_a_rule_stays_modified_quietly(cairn_env, alerts):
    """MODIFIED "Something else again is still a change": quiet under a rule, still `modified`."""
    root, cid = await _collection(cairn_env, "else-rule", {"a.txt": "original", "b": "b"})
    _edit(root / "a.txt", "changed")
    await _scan(cid)
    await _add_rule(cid, "a.txt")
    _edit(root / "a.txt", "yet another thing")
    alerts.clear()

    summary = await _scan(cid)

    row = await _file(cid, "a.txt")
    assert row.status == "modified" and summary.reverted == 0
    assert row.prior_sha256 == _sha("original")
    assert len(await _events(cid, kind="modified", file_id=row.id)) == 1
    assert len(await _open_events(cid, row.id)) == 1
    assert alerts == []


@pytest.mark.asyncio
async def test_a_return_to_the_kept_contents_closes_the_issue_under_a_rule(cairn_env, alerts):
    root, cid = await _collection(cairn_env, "ret", {"a.txt": "original", "b": "b"})
    _edit(root / "a.txt", "changed")
    await _scan(cid)
    fid = (await _file(cid, "a.txt")).id
    await _add_rule(cid, "a.txt")
    _edit(root / "a.txt", "original")
    alerts.clear()

    summary = await _scan(cid)

    row = await _file(cid, "a.txt")
    assert row.status == "ok" and row.prior_sha256 is None and summary.reverted == 1
    assert await _open_events(cid, fid) == []
    assert alerts == []


@pytest.mark.asyncio
async def test_adding_a_rule_resolves_nothing(cairn_env, alerts):
    root, cid = await _collection(cairn_env, "add", {"c/x.db": "one", "y": "y"})
    _edit(root / "c/x.db", "two, changed")
    await _scan(cid)
    before = _snapshot(_db(cairn_env), cid)

    await _add_rule(cid, "c/")
    assert _snapshot(_db(cairn_env), cid) == before, "adding a rule changes no file or event"

    await _scan(cid)  # nothing changed on disk
    await _scan(cid, deep=True)
    row = await _file(cid, "c/x.db")
    assert row.status == "modified" and row.prior_sha256 == _sha("one")
    assert len(await _open_events(cid, row.id)) == 1


@pytest.mark.asyncio
async def test_removing_a_rule_changes_no_record(cairn_env, alerts):
    root, cid = await _collection(cairn_env, "rm", {"c/x.db": "one", "y": "y"})
    await _add_rule(cid, "c/")
    _edit(root / "c/x.db", "two, quiet")
    await _scan(cid)
    assert (await _file(cid, "c/x.db")).status == "ok"
    before = _snapshot(_db(cairn_env), cid)

    assert await _remove_rule(cid, "c/") is True
    assert _snapshot(_db(cairn_env), cid) == before

    _edit(root / "c/x.db", "three, flagged")
    alerts.clear()
    await _scan(cid)
    row = await _file(cid, "c/x.db")
    assert row.status == "modified"
    assert len(await _open_events(cid, row.id)) == 1
    assert len(alerts) == 1


@pytest.mark.asyncio
async def test_a_rule_added_during_a_scan_applies_from_the_next_one(cairn_env, alerts, monkeypatch):
    from src.services import scanner

    root, cid = await _collection(cairn_env, "mid", {"a.txt": "a", "b.txt": "b"})
    _edit(root / "a.txt", "a, edited")
    _edit(root / "b.txt", "b, edited")
    real = scanner._hash
    added: list[bool] = []

    async def _hook(path):
        if not added:
            added.append(True)
            await _add_rule(cid, "*.txt")  # stored while this pass is running
        return await real(path)

    monkeypatch.setattr(scanner, "_hash", _hook)
    summary = await _scan(cid)
    monkeypatch.setattr(scanner, "_hash", real)

    assert added and summary.modified == 2
    assert {f.status for f in (await _files(cid)).values()} == {"modified"}

    # The next pass uses it.
    await _scan(cid)  # no change
    _edit(root / "a.txt", "a, edited again")
    alerts.clear()
    await _scan(cid)
    assert len(await _events(cid, kind="modified", file_id=(await _file(cid, "a.txt")).id)) == 1
    assert alerts == []


@pytest.mark.asyncio
async def test_inert_rules_change_nothing(cairn_env, alerts):
    from src.database import get_sessionmaker
    from src.models.db import Collection

    root, cid = await _collection(cairn_env, "inert", {"a.txt": "a", "b": "b"})
    async with get_sessionmaker()() as s:
        await s.execute(
            update(Collection)
            .where(Collection.id == cid)
            .values(
                rules_json=json.dumps(
                    [{"pattern": "a.txt", "effect": "comes_and_goes"}, {"pattern": "*"}]
                )
            )
        )
        await s.commit()
    _edit(root / "a.txt", "a, edited")
    summary = await _scan(cid)
    assert summary.modified == 1 and (await _file(cid, "a.txt")).status == "modified"


# ================================================================================================
# MODIFIED Requirement: A mass change is not absorbed
# ================================================================================================


@pytest.mark.asyncio
async def test_a_mass_of_rule_covered_changes_in_a_worm_collection_becomes_issues(
    cairn_env, alerts, mass
):
    mass(50, 3)
    files = {f"f{i}.txt": f"file {i}" for i in range(4)}
    root, cid = await _collection(cairn_env, "wmass", files)
    await _add_rule(cid, "f?.txt")
    before = await _files(cid)
    for i in range(3):
        _edit(root / f"f{i}.txt", f"file {i} rewritten")
    alerts.clear()

    summary = await _scan(cid)

    assert summary.mass_change == 3 and summary.modified == 3
    files_after = await _files(cid)
    for i in range(3):
        row = files_after[f"f{i}.txt"]
        assert row.status == "modified"
        assert row.prior_sha256 == before[f"f{i}.txt"].sha256
        (ev,) = await _events(cid, kind="modified", file_id=row.id)
        assert ev.acknowledged_at is None and json.loads(ev.detail)["mass"] == 3
    assert files_after["f3.txt"].status == "new"
    assert len(alerts) == 1
    assert alerts[0].summary == "3 modified — unusually many for one scan"


@pytest.mark.asyncio
async def test_rule_covered_changes_under_the_ceiling_are_absorbed_in_worm(cairn_env, alerts, mass):
    mass(50, 3)
    files = {f"f{i}.txt": f"file {i}" for i in range(4)}
    root, cid = await _collection(cairn_env, "wmass2", files)
    await _add_rule(cid, "f?.txt")
    for i in range(2):
        _edit(root / f"f{i}.txt", f"file {i} rewritten")
    alerts.clear()

    summary = await _scan(cid)
    assert summary.mass_change == 0 and summary.modified == 0
    assert await _events(cid, kind="modified") == [] and alerts == []


@pytest.mark.asyncio
async def test_unruled_worm_changes_do_not_count_toward_the_ceiling(cairn_env, alerts, mass):
    mass(50, 2)
    files = {f"f{i}.txt": f"file {i}" for i in range(4)}
    root, cid = await _collection(cairn_env, "wmass3", files)
    await _add_rule(cid, "f0.txt")
    for i in range(3):
        _edit(root / f"f{i}.txt", f"file {i} rewritten")

    summary = await _scan(cid)
    # Only one quiet change (f0) — under the minimum of 2; f1/f2 are reported individually.
    assert summary.mass_change == 0 and summary.modified == 2
    assert (await _file(cid, "f0.txt")).status == "ok"
    assert all("mass" not in json.loads(e.detail) for e in await _events(cid, kind="modified"))
