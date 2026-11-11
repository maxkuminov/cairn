"""Path rules (add-path-rules): grammar, refusals, storage and reach.

Acceptance criteria: ``openspec/changes/add-path-rules/specs/corpus-management/spec.md``
("A collection carries path rules") and design D1/D2/D4. One test per scenario, named after it.

Run from the repo root: ``PYTHONPATH=. pytest tests/test_rules.py``
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event as sa_event
from sqlalchemy import select

from src.services.rules import (
    MAX_RULES,
    RuleError,
    RuleSet,
    compile_pattern,
    list_rules,
    load,
    validate_pattern,
)
from tests.conftest import seed_collection


def _m(pattern: str, path: str) -> bool:
    return compile_pattern(pattern).fullmatch(path) is not None


def _refused(pattern: str) -> str:
    with pytest.raises(RuleError) as exc:
        validate_pattern(pattern)
    msg = str(exc.value)
    assert msg and "\n" not in msg
    return exc.value.reason


# ================================================================================================
# Matching scenarios
# ================================================================================================


def test_a_name_matches_in_any_folder():
    for p in ("NOTES.md", "a/NOTES.md", "a/b/NOTES.md"):
        assert _m("NOTES.md", p)
    for p in ("a/NOTES.md.bak", "a/MY-NOTES.md"):
        assert not _m("NOTES.md", p)


def test_a_name_may_use_wildcards_within_the_name():
    assert _m("*-Internal.xlsx", "a/Acme-Internal.xlsx")
    assert not _m("*-Internal.xlsx", "a/Internal.xlsx")
    assert not _m("*-Internal.xlsx", "a/Acme-Internal.xlsx.tmp")


def test_a_path_is_anchored_at_the_root():
    assert _m("clients/*/NOTES.md", "clients/acme/NOTES.md")
    for p in ("clients/NOTES.md", "clients/acme/2025/NOTES.md", "archive/clients/acme/NOTES.md"):
        assert not _m("clients/*/NOTES.md", p)


def test_a_trailing_slash_covers_the_folder_anchored_at_the_root():
    assert _m("Agents/Queue/", "Agents/Queue/a.md")
    assert _m("Agents/Queue/", "Agents/Queue/x/y.md")
    for p in ("Agents/Queue.md", "Agents/Queue2/a.md", "Other/Agents/Queue/a.md"):
        assert not _m("Agents/Queue/", p)


def test_a_single_folder_name_with_a_trailing_slash_is_a_root_level_folder():
    assert _m("drafts/", "drafts/a.md")
    assert not _m("drafts/", "work/drafts/a.md")


def test_a_double_star_segment_spans_folders():
    for p in ("cache/a", "x/cache/a", "x/y/cache/b/c"):
        assert _m("**/cache/**", p)
    assert not _m("**/cache/**", "cache")
    assert not _m("**/cache/**", "x/cache")


def test_wildcards_do_not_cross_a_separator():
    assert _m("reports/*.csv", "reports/a.csv")
    assert not _m("reports/*.csv", "reports/2025/a.csv")
    assert _m("reports/?.csv", "reports/a.csv")
    assert not _m("reports/?.csv", "reports/ab.csv")
    assert not _m("a?b", "a/b")


def test_other_characters_are_literal():
    assert _m("data[1].txt", "data[1].txt")
    assert not _m("data[1].txt", "data1.txt")


@pytest.mark.parametrize(
    "pattern, hit, misses",
    [
        ("a.b", "a.b", ["axb"]),
        ("a+b", "a+b", ["aab", "ab"]),
        ("f(1)", "f(1)", ["f1"]),
        ("^x$", "^x$", ["x"]),
        ("a|b", "a|b", ["a", "b"]),
        ("a\\b", "a\\b", ["ab", "a/b"]),
        ("{a,b}.txt", "{a,b}.txt", ["a.txt"]),
    ],
)
def test_regex_metacharacters_are_literal(pattern, hit, misses):
    assert _m(pattern, hit)
    for miss in misses:
        assert not _m(pattern, miss)


def test_a_name_pattern_never_matches_a_folder_name():
    # `drafts` names a FILE; files inside a folder named `drafts` are not matched.
    assert not _m("drafts", "drafts/a.md")
    assert not _m("drafts", "x/drafts/a.md")
    assert _m("drafts", "x/drafts")


def test_final_double_star_is_the_same_as_a_trailing_slash():
    paths = ["a", "a/x", "a/x/y", "b/a/x", "ab/x", "a.txt"]
    for p in paths:
        assert _m("a/**", p) == _m("a/", p), p
    assert _m("a/**", "a/x/y") and not _m("a/**", "a")


def test_a_leading_double_star_matches_at_the_root():
    assert _m("**/x", "x")
    assert _m("**/x", "a/b/x")
    assert not _m("**/x", "a/xx")


def test_a_middle_double_star_matches_zero_or_more_folders():
    for p in ("a/b", "a/x/b", "a/x/y/b"):
        assert _m("a/**/b", p)
    assert not _m("a/**/b", "a/xb")


def test_matching_is_case_sensitive():
    assert not _m("NOTES.md", "notes.md")


def test_surrounding_whitespace_is_stripped():
    assert validate_pattern("  NOTES.md \t\n") == "NOTES.md"
    # Interior spaces are literal.
    assert validate_pattern("my notes.md") == "my notes.md"
    assert _m(" my notes.md ", "x/my notes.md")


# ================================================================================================
# Refusals
# ================================================================================================


@pytest.mark.parametrize("pattern", ["a**b", "**.md", "docs/**.md"])
def test_a_double_star_that_is_not_a_whole_segment_is_refused(pattern):
    assert _refused(pattern) == "double_star"


@pytest.mark.parametrize("pattern", ["*", "**", "**/*", "*/**", "?*", "*/", "**/", "*/?/*"])
def test_a_match_all_pattern_is_refused(pattern):
    assert _refused(pattern) == "match_all"


@pytest.mark.parametrize(
    "pattern, reason",
    [
        ("", "empty"),
        ("   ", "empty"),
        ("/etc/x", "absolute"),
        ("a/../b", "segment"),
        ("../b", "segment"),
        ("a/./b", "segment"),
        ("a//b", "segment"),
        ("a//", "segment"),
        ("a\x00b", "control"),
        ("a\nb", "control"),
        ("a\rb", "control"),
        ("a b", "control"),
        ("x" * 513, "too_long"),
        ("é" * 257, "too_long"),  # 514 bytes, 257 characters
    ],
)
def test_a_malformed_pattern_is_refused(pattern, reason):
    assert _refused(pattern) == reason


def test_a_pattern_of_exactly_512_bytes_is_accepted():
    assert validate_pattern("x" * 512) == "x" * 512


def test_refusal_messages_do_not_echo_the_pattern():
    with pytest.raises(RuleError) as exc:
        validate_pattern("secret**name")
    assert "secret" not in str(exc.value)


# ================================================================================================
# load / list_rules
# ================================================================================================


class _C:
    def __init__(self, rules_json):
        self.rules_json = rules_json


def test_load_ignores_inert_unknown_effect_and_malformed_entries():
    stored = json.dumps(
        [
            {"pattern": "NOTES.md", "effect": "edits", "created_at": "2026-01-01T00:00:00+00:00"},
            {"pattern": "*.log", "effect": "comes_and_goes"},
            {"pattern": "*", "effect": "edits"},
            {"pattern": " padded.txt", "effect": "edits"},
            {"pattern": 5, "effect": "edits"},
            {"effect": "edits"},
            "drafts/",
            None,
            ["x"],
        ]
    )
    rs = load(_C(stored))
    assert rs.patterns == ["NOTES.md"] and len(rs) == 1 and rs
    assert rs.matches("a/NOTES.md")
    assert not rs.matches("x.log") and not rs.matches("anything") and not rs.matches("drafts/a")

    listed = list_rules(_C(stored))
    assert len(listed) == 9
    assert [r["recognised"] for r in listed] == [True] + [False] * 8
    assert listed[0] == {
        "pattern": "NOTES.md",
        "effect": "edits",
        "created_at": "2026-01-01T00:00:00+00:00",
        "recognised": True,
    }
    assert listed[1]["effect"] == "comes_and_goes"
    assert listed[6]["pattern"] == ""


@pytest.mark.parametrize("raw", ["not json", "{}", '"x"', "", None, "null", "[1"])
def test_load_never_raises_on_malformed_json(raw):
    rs = load(_C(raw))
    assert not rs and len(rs) == 0 and not rs.matches("a")
    assert isinstance(list_rules(_C(raw)), list)


def test_an_empty_ruleset_matches_nothing():
    assert not RuleSet().matches("a")


# ================================================================================================
# add_rule / remove_rule / reach (datastore)
# ================================================================================================


async def _session():
    from src.database import get_sessionmaker

    return get_sessionmaker()()


@pytest.mark.asyncio
async def test_add_and_remove_round_trip(cairn_env):
    from src.models.db import Collection
    from src.services.rules import add_rule, remove_rule

    root = cairn_env / "c"
    root.mkdir()
    cid = await seed_collection(root, ots_mode="none")
    async with await _session() as s:
        c = await s.get(Collection, cid)
        assert c.rules_json == "[]"
        before = (c.name, c.mode, c.exclude_globs_json, c.alert_json)
        assert await add_rule(s, c, "  NOTES.md ") == "NOTES.md"
        assert await add_rule(s, c, "drafts/") == "drafts/"
    async with await _session() as s:
        c = await s.get(Collection, cid)
        stored = json.loads(c.rules_json)
        assert [e["pattern"] for e in stored] == ["NOTES.md", "drafts/"]
        assert all(e["effect"] == "edits" for e in stored)
        created = datetime.fromisoformat(stored[0]["created_at"])
        assert created.tzinfo is not None
        assert (c.name, c.mode, c.exclude_globs_json, c.alert_json) == before
        assert load(c).patterns == ["NOTES.md", "drafts/"]

        assert await remove_rule(s, c, "nope") is False
        assert await remove_rule(s, c, "NOTES.md ") is False  # exact pattern only
        assert await remove_rule(s, c, "NOTES.md") is True
    async with await _session() as s:
        c = await s.get(Collection, cid)
        assert [e["pattern"] for e in json.loads(c.rules_json)] == ["drafts/"]


@pytest.mark.asyncio
async def test_a_duplicate_or_a_hundred_and_first_rule_is_refused(cairn_env):
    from src.models.db import Collection
    from src.services.rules import add_rule

    root = cairn_env / "c"
    root.mkdir()
    cid = await seed_collection(root, ots_mode="none")
    async with await _session() as s:
        c = await s.get(Collection, cid)
        await add_rule(s, c, "NOTES.md")
        with pytest.raises(RuleError) as exc:
            await add_rule(s, c, " NOTES.md")
        assert exc.value.reason == "duplicate"
        for i in range(1, MAX_RULES):
            await add_rule(s, c, f"f{i}.txt")
        snapshot = c.rules_json
        assert len(json.loads(snapshot)) == MAX_RULES
        with pytest.raises(RuleError) as exc:
            await add_rule(s, c, "one-more.txt")
        assert exc.value.reason == "too_many"
        with pytest.raises(RuleError) as exc:
            await add_rule(s, c, "*")
        assert exc.value.reason == "match_all"
    async with await _session() as s:
        assert (await s.get(Collection, cid)).rules_json == snapshot


async def _seed_reach(cairn_env):
    """A collection with tracked, modified and missing files and a spread of events."""
    from src.models.db import Collection, Event, FileEntry

    root = cairn_env / "reach"
    root.mkdir()
    cid = await seed_collection(root, ots_mode="none")
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=120)

    def detail(path, **extra):
        return json.dumps({"path": path, "from": {}, "to": {}, **extra})

    async with await _session() as s:
        def f(relpath, status):
            row = FileEntry(
                collection_id=cid, relpath=relpath, size=1, sha256="x", status=status,
                ots_state="none",
            )
            s.add(row)
            return row

        n1 = f("a/NOTES.md", "modified")
        n2 = f("b/NOTES.md", "ok")
        f("c/NOTES.md", "missing")
        f("a/other.txt", "ok")
        moved = f("archive/renamed.md", "ok")  # was a/NOTES.md-era file, recorded path differs
        f("z.txt", "new")
        await s.flush()

        def ev(kind, file_id, d, when=now):
            s.add(Event(collection_id=cid, file_id=file_id, kind=kind, detail=d, detected_at=when))

        ev("modified", n1.id, detail("a/NOTES.md"))  # counted
        ev("modified", n2.id, None)  # no detail → by current path, counted
        ev("modified", moved.id, detail("old/NOTES.md"))  # by RECORDED path, counted
        ev("modified", n1.id, detail("a/NOTES.md"), when=old)  # outside window
        ev("corrupted", n1.id, detail("a/NOTES.md"))  # not a modified event
        ev("restored_changed", n2.id, "recorded aa → found bb")
        ev("modified", n2.id, detail("b/NOTES.md", mass=40))  # mass-change event
        ev("modified", None, detail("gone/NOTES.md"))  # detached, recorded path → counted
        ev("modified", None, None)  # neither → not counted
        ev("modified", n1.id, "free text, not json")  # → current path a/NOTES.md, counted
        await s.commit()
        return cid, await s.get(Collection, cid)


@pytest.mark.asyncio
async def test_reach_counts(cairn_env):
    from src.models.db import Collection
    from src.services.rules import reach

    cid, _ = await _seed_reach(cairn_env)
    async with await _session() as s:
        c = await s.get(Collection, cid)
        r = await reach(s, c, "NOTES.md")
        assert r == {"tracked": 2, "total": 5, "open_changed": 1, "alarms_90d": 5}
        # The moved file is matched by the path its event recorded, not by where it is now.
        r = await reach(s, c, "archive/")
        assert r == {"tracked": 1, "total": 5, "open_changed": 0, "alarms_90d": 0}
        r = await reach(s, c, "old/")
        assert r == {"tracked": 0, "total": 5, "open_changed": 0, "alarms_90d": 1}
        r = await reach(s, c, "a/")
        assert r == {"tracked": 2, "total": 5, "open_changed": 1, "alarms_90d": 2}
        # Read-only.
        assert c.rules_json == "[]"


@pytest.mark.asyncio
async def test_reach_many_equals_reach_and_uses_a_bounded_number_of_statements(cairn_env):
    from src.database import get_engine
    from src.models.db import Collection
    from src.services.rules import reach, reach_many

    cid, _ = await _seed_reach(cairn_env)
    patterns = ["NOTES.md", "a/", "old/"]
    async with await _session() as s:
        c = await s.get(Collection, cid)
        singles = {p: await reach(s, c, p) for p in patterns}

        statements: list[str] = []

        def _count(conn, cursor, statement, *args):
            statements.append(statement)

        engine = get_engine().sync_engine
        sa_event.listen(engine, "before_cursor_execute", _count)
        try:
            many = await reach_many(s, c, patterns)
            n_three = len(statements)
            statements.clear()
            await reach_many(s, c, [f"f{i}.txt" for i in range(50)] + patterns)
            n_many = len(statements)
            statements.clear()
            assert await reach_many(s, c, []) == {}
            assert statements == []
        finally:
            sa_event.remove(engine, "before_cursor_execute", _count)
        assert many == singles
        assert n_three == n_many <= 2
        # A pattern that does not compile: zeros, with the real total.
        bad = await reach_many(s, c, ["*", "NOTES.md"])
        assert bad["*"] == {"tracked": 0, "total": 5, "open_changed": 0, "alarms_90d": 0}
        assert bad["NOTES.md"] == singles["NOTES.md"]


@pytest.mark.asyncio
async def test_reach_selects_columns_not_orm_rows(cairn_env):
    """`reach` must not materialise FileEntry objects (a 200k-row collection)."""
    from src.models.db import Collection, FileEntry
    from src.services.rules import reach

    cid, _ = await _seed_reach(cairn_env)
    async with await _session() as s:
        c = await s.get(Collection, cid)
        await reach(s, c, "NOTES.md")
        loaded = [o for o in s.identity_map.values() if isinstance(o, FileEntry)]
        assert loaded == []
        assert (await s.scalar(select(FileEntry.id).limit(1))) is not None
