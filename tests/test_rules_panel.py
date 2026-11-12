"""Panel half of `add-path-rules`: what the review rows, the rule page, the rules page and the
collection page say and do about path rules.

Spec: openspec web-panel — "A rule is created from a changed file, with its reach shown first" and
"A collection's rules are listed and can be removed" (every scenario), design D4–D5.

A rule is a standing instruction NOT to raise an alarm, so the two failures that matter here are
the panel saying something untrue about a rule's reach, and a rule route changing a file record or
an event. Everything is exercised at route level against a seeded database: render the page,
submit the very form it rendered, and look at what the database then holds.

Covered rows, the two places a new rule returns to, the list of matching files, the "matches
nothing" warning, the share-of-the-collection warning and the description of the mass-change
ceiling were added after a usability pass (sections J–P at the end).

Written from the spec by someone who did not write the routes. A test that states what the spec
requires and fails against the code is kept and marked ``xfail(strict=True)`` with the observed
behaviour — it is a finding, not a test to bend.

Run from the repo root: ``PYTHONPATH=. pytest tests/test_rules_panel.py``
"""

from __future__ import annotations

import html as html_lib
import json
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import text

from tests.conftest import seed_collection
from tests.test_ux_review import (
    _aside,
    _capture_context,
    _done_banner,
    _forms,
    _FormParser,
    _help,
    _make_client,
    _photos,
    _review,
    _row_forms,
    _seed_files,
    _text,
    _with_session,
)

RULE_PAGE = re.compile(r"^/collection/(\d+)/rules/new$")
NOTES = "clients/acme/NOTES.md"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- reading the pages -----------------------------------------------------------------------


class _Tags(HTMLParser):
    """Every start tag with its attributes, entity-decoded the way a browser reads them."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str]]] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, {k: (v or "") for k, v in attrs}))


def _tags(fragment: str, name: str) -> list[dict[str, str]]:
    p = _Tags()
    p.feed(fragment)
    return [attrs for tag, attrs in p.tags if tag == name]


def _rule_links(fragment: str) -> list[tuple[int, str]]:
    """``(collection id, starting path)`` of every link in ``fragment`` that opens a rule page for
    a file — read as a browser would follow it (attribute unescaped, query percent-decoded)."""
    out = []
    for a in _tags(fragment, "a"):
        parts = urlsplit(a.get("href", ""))
        m = RULE_PAGE.match(parts.path)
        if m is None:
            continue
        query = parse_qs(parts.query, keep_blank_values=True, strict_parsing=True)
        assert set(query) == {"path"} and len(query["path"]) == 1, a["href"]
        assert parts.fragment == "" and parts.netloc == "", a["href"]
        out.append((int(m.group(1)), query["path"][0]))
    return out


def _rule_hrefs(fragment: str) -> list[str]:
    return [
        a["href"] for a in _tags(fragment, "a") if RULE_PAGE.match(urlsplit(a.get("href", "")).path)
    ]


_COVERED = re.compile(
    r'<a\b[^>]*\bclass="[^"]*\breview-row__rule--covered\b[^"]*"[^>]*>(.*?)</a>', re.S
)


def _covered(fragment: str) -> list[tuple[int, str]]:
    """``(collection id, pattern)`` of every "Covered by rule …" marker in ``fragment``: the
    collection whose rules page it links to and the rule it names, exactly as rendered."""
    out = []
    for m in _COVERED.finditer(fragment):
        (a,) = _tags(m.group(0), "a")
        target = re.fullmatch(r"/collection/(\d+)/rules", a.get("href", ""))
        assert target, a.get("href")
        assert _text(m.group(1)).startswith("Covered by rule "), m.group(1)
        named = re.search(r'<span class="mono">(.*?)</span>', m.group(1), re.S)
        assert named, m.group(1)
        out.append((int(target.group(1)), html_lib.unescape(named.group(1))))
    return out


def _sample(fragment: str) -> dict | None:
    """The list of matching files a reach block offers: its summary, the paths in the order
    listed (entity-decoded, whitespace intact) and how many more it says there are."""
    found = re.findall(r'<details class="rule-sample">(.*?)</details>', fragment, re.S)
    if not found:
        assert "rule-sample" not in fragment
        return None
    assert len(found) == 1, "one list per reach"
    block = found[0]
    summary = _text(re.search(r"<summary>(.*?)</summary>", block, re.S).group(1))
    listed = re.search(r'<ul class="rule-sample__list[^"]*">(.*?)</ul>', block, re.S).group(1)
    paths = [html_lib.unescape(li) for li in re.findall(r"<li>(.*?)</li>", listed, re.S)]
    more = re.search(r'<div class="rule-sample__more">(.*?)</div>', block, re.S)
    count = 0
    if more is not None:
        said = re.fullmatch(r"…and ([\d,]+) more, in path order\.", _text(more.group(1)))
        assert said, more.group(1)
        count = _int(said.group(1))
        assert count > 0
    return {"summary": summary, "paths": paths, "more": count, "html": block}


def _share(fragment: str) -> str | None:
    """The share of the collection a reach block warns about ("71.4%", "all"), or ``None``."""
    m = re.search(r"That is <strong>(.*?)</strong> of this collection", fragment, re.S)
    return _text(m.group(1)) if m else None


def _nomatch(body: str) -> str | None:
    """The "matches no file" warning of the operator's own pattern, up to its add control."""
    start = body.find('id="rule-nomatch"')
    if start == -1:
        return None
    return body[start : body.index("</form>", start) + len("</form>")]


def _row_block(body: str, file_id: int) -> str:
    """One review row in full — including what follows its accept form, where the rule link is."""
    start = body.index(f'id="review-row-{file_id}"')
    ends = [
        i
        for i in (
            body.find('<div class="review-row"', start),
            body.find("</section>", start),
            body.find('id="review-help"', start),
        )
        if i != -1
    ]
    return body[start : min(ends) if ends else len(body)]


def _content(body: str) -> str:
    """The page's own content: from its header to the scripts the shell appends (the shell's
    inline JavaScript is not something the page *says*)."""
    start = body.index('class="page-header"')
    end = body.find("<script", start)
    return body[start : end if end != -1 else len(body)]


def _said(body: str) -> str:
    return _text(_content(body))


def _get(client, url: str, template: str, **kw):
    """GET a page; return ``(body, context)`` for the one render of ``template``."""
    with _capture_context() as captured:
        resp = client.get(url, **kw)
    assert resp.status_code == 200, (url, resp.status_code)
    ctxs = [c for name, c in captured if name == template]
    assert len(ctxs) == 1, f"expected one {template} render, got {len(ctxs)}"
    return resp.text, ctxs[0]


def _rule_page(client, path: str | None = None, pattern: str | None = None, cid: int = 1) -> str:
    params = {}
    if path is not None:
        params["path"] = path
    if pattern is not None:
        params["pattern"] = pattern
    body, _ctx = _get(client, f"/collection/{cid}/rules/new", "rule_new.html", params=params)
    return body


def _rules_page(client, cid: int = 1, query: str = "") -> str:
    body, _ctx = _get(client, f"/collection/{cid}/rules{query}", "collection_rules.html")
    return body


_CARD = re.compile(r'<div class="card rule-card[^"]*" id="rule-(proposal-(\w+)|custom)">')
_REACH_TRACKED = re.compile(r"^([\d,]+) of ([\d,]+) tracked files? match(?:es)? it now$")
_REACH_LISTED = re.compile(
    r"^([\d,]+) of them (?:is|are) listed as changed right now — (?:it stays|they stay) "
    r"listed until accepted$"
)
_REACH_ALARMS = re.compile(
    r"^([\d,]+) change alarms? in the last 90 days (?:was|were) for files it matches$"
)


def _int(s: str) -> int:
    return int(s.replace(",", ""))


def _cards(body: str) -> dict[str, dict]:
    """Every pattern card on a rule page, keyed ``file`` / ``folder`` / ``name`` / ``custom``.

    ``pattern`` is the pattern the card displays, ``form`` the POST form that would store it (or
    ``None``), ``reach`` the three figures it states as ``(tracked, total, listed, alarms)`` (or
    ``None`` when the card states no reach, i.e. a refusal).
    """
    out: dict[str, dict] = {}
    for m in _CARD.finditer(body):
        end = body.find('<div class="card ', m.end())
        frag = body[m.start() : end if end != -1 else len(body)]
        kind = m.group(2) or "custom"
        assert kind not in out, f"two cards of kind {kind}"
        shown = re.search(r'<div class="rule-card__pattern mono">(.*?)</div>', frag, re.S)
        label = re.search(r'<div class="rule-card__label">(.*?)</div>', frag, re.S)
        post = [f for f in _forms(frag) if re.fullmatch(r"/collection/\d+/rules", f["action"])]
        assert len(post) <= 1
        lines = None
        reach = None
        ul = re.search(r'<ul class="rule-reach">(.*?)</ul>', frag, re.S)
        if ul is not None:
            lines = [_text(li) for li in re.findall(r"<li>(.*?)</li>", ul.group(1), re.S)]
            assert len(lines) == 3, lines
            tracked = _REACH_TRACKED.match(lines[0])
            assert tracked, lines[0]
            if lines[1] == "none of them is listed as changed right now":
                listed = 0
            else:
                lm = _REACH_LISTED.match(lines[1])
                assert lm, lines[1]
                listed = _int(lm.group(1))
                assert listed > 0
            alarms = _REACH_ALARMS.match(lines[2])
            assert alarms, lines[2]
            reach = (_int(tracked.group(1)), _int(tracked.group(2)), listed, _int(alarms.group(1)))
        out[kind] = {
            "html": frag,
            "text": _text(frag),
            "label": _text(label.group(1)) if label else "",
            "pattern": html_lib.unescape(shown.group(1)) if shown else None,
            "form": post[0] if post else None,
            "lines": lines,
            "reach": reach,
        }
    return out


def _get_forms(fragment: str, action: str) -> list[dict]:
    p = _FormParser()
    p.feed(fragment)
    # The fragment (`#rule-custom`) only scrolls the result into view; it is not part of the address.
    return [f for f in p.forms if f["method"] == "get" and f["action"].split("#")[0] == action]


def _free_field(body: str, cid: int = 1) -> dict:
    """The form for a pattern of the operator's own: a GET into the rule page (stores nothing)."""
    forms = [
        f for f in _get_forms(body, f"/collection/{cid}/rules/new") if "pattern" in f["fields"]
    ]
    assert len(forms) == 1, "the page offers exactly one free pattern field"
    return forms[0]


def _typed_value(body: str) -> str:
    (field,) = [i for i in _tags(body, "input") if i.get("id") == "rule-pattern"]
    assert field.get("name") == "pattern" and field.get("type", "text") == "text"
    return field.get("value", "")


def _typed(body: str) -> str:
    """What the field keeps of what was typed. The spaces and tabs around a pattern are not part
    of it, so the field may keep them or drop them; everything else must be exactly as typed."""
    return _typed_value(body).strip(" \t")


def _starting_from(body: str) -> str | None:
    """The starting path the page shows, exactly as rendered (entity-decoded), or ``None``."""
    m = re.search(r'Starting from <span class="mono">(.*?)</span>', _content(body), re.S)
    return html_lib.unescape(m.group(1)) if m else None


def _refusal(body: str) -> str | None:
    m = re.search(r'id="rule-refusal"[^>]*>(.*?)</div>', body, re.S)
    return _text(m.group(1)) if m else None


def _rule_rows(body: str) -> list[dict]:
    """The rules a rules page lists, in order: pattern, what it says about it, its remove form."""
    rows = []
    for m in re.finditer(r'<div class="rule-row" id="rule-\d+">', body):
        frag = body[m.start() : body.index("</form>", m.end()) + len("</form>")]
        shown = re.search(r'<div class="rule-row__pattern mono">(.*?)</div>', frag, re.S)
        meta = re.search(r'<div class="rule-row__meta">(.*?)</div>', frag, re.S)
        (form,) = _forms(frag)
        rows.append({
            "html": frag,
            "pattern": html_lib.unescape(shown.group(1)),
            "meta": _text(meta.group(1)),
            "form": form,
        })
    return rows


def _csrf(client, url: str = "/collections") -> str:
    m = re.search(r'name="csrf-token" content="([^"]+)"', client.get(url).text)
    assert m, "the page carries no CSRF token"
    return html_lib.unescape(m.group(1))


def _post(client, url: str, **data):
    return client.post(url, data=data, follow_redirects=False)


# --- reading and writing the datastore -------------------------------------------------------


def _rules_json(cid: int = 1) -> str:
    async def go(s):
        return await s.scalar(text("SELECT rules_json FROM collections WHERE id = :c"), {"c": cid})

    return _aside(go)


def _stored(cid: int = 1) -> list[dict]:
    return json.loads(_rules_json(cid))


def _patterns(cid: int = 1) -> list[str]:
    return [e["pattern"] for e in _stored(cid)]


def _snapshot() -> dict[str, list[tuple]]:
    """Every column of every row of the tables a rule must never touch — plus the collection rows
    with ``rules_json`` left out, so "only the rule list changed" is one comparison."""

    async def go(s):
        out = {}
        for table in ("files", "events", "runs"):
            rows = await s.execute(text(f"SELECT * FROM {table} ORDER BY id"))
            out[table] = [tuple(r) for r in rows.all()]
        rows = await s.execute(text("SELECT * FROM collections ORDER BY id"))
        keys = list(rows.keys())
        out["collections"] = [
            tuple(v for k, v in zip(keys, r) if k != "rules_json") for r in rows.all()
        ]
        return out

    return _aside(go)


def _write_rules_json(entries, cid: int = 1) -> None:
    raw = entries if isinstance(entries, str) else json.dumps(entries)

    async def go(s):
        await s.execute(
            text("UPDATE collections SET rules_json = :j WHERE id = :c"), {"j": raw, "c": cid}
        )
        await s.commit()

    _aside(go)


def _rule(pattern: str, effect: str = "edits") -> dict:
    return {"pattern": pattern, "effect": effect, "created_at": "2026-10-01T09:00:00+00:00"}


def _with_rules(seed, *patterns: str, cid: int = 1):
    """``seed``, then the given rules added through the service (the supported way in)."""

    async def go():
        from src.database import get_sessionmaker
        from src.models.db import Collection
        from src.services import rules

        await seed()
        async with get_sessionmaker()() as s:
            c = await s.get(Collection, cid)
            for p in patterns:
                await rules.add_rule(s, c, p)

    return go


async def _seed_events(cid: int, specs) -> None:
    """Extra events, ``(relpath, kind, days_ago, detail)``; ``detail`` a dict is stored as JSON.
    All acknowledged (history), so they never change what a review page lists. ``relpath`` None
    writes an event with no file (what is left of an alarm once its file's deletion is accepted)."""
    from src.database import get_sessionmaker
    from src.models.db import Event, FileEntry
    from sqlalchemy import select

    async with get_sessionmaker()() as s:
        for relpath, kind, days_ago, detail in specs:
            fid = None
            if relpath is not None:
                fid = await s.scalar(
                    select(FileEntry.id).where(
                        FileEntry.collection_id == cid, FileEntry.relpath == relpath
                    )
                )
                assert fid is not None, relpath
            at = _now() - timedelta(days=days_ago)
            s.add(Event(
                collection_id=cid, file_id=fid, kind=kind, detected_at=at, acknowledged_at=at,
                detail=json.dumps(detail) if isinstance(detail, dict) else detail,
            ))
        await s.commit()


def _matching(pattern: str, relpaths) -> list[str]:
    from src.services import rules

    rx = rules.compile_pattern(pattern)
    return [p for p in relpaths if rx.fullmatch(p) is not None]


# =============================================================================================
# A. The review row
# =============================================================================================

ROW_KINDS = [
    ("docs/changed.txt", "modified"),
    ("docs/gone.txt", "missing"),
    ("docs/rot.bin", "modified", {"kind": "corrupted"}),
    ("docs/back.txt", "modified", {"kind": "restored_changed"}),
    ("docs/fine.txt", "ok"),
]


def test_only_an_ordinary_changed_row_offers_the_rule_link(cairn_env):
    """Scenarios "A changed file offers the link and a missing file does not" and "A finding no
    rule covers does not offer the link"."""
    with _make_client(cairn_env, _photos(cairn_env, ROW_KINDS)) as client:
        body, r = _review(client)
        by_path = {row["relpath"]: row for row in r["rows"]}
        # The seed produced the four kinds of row the scenarios talk about.
        assert {p: row["kind"] for p, row in by_path.items()} == {
            "docs/changed.txt": "changed",
            "docs/gone.txt": "missing",
            "docs/rot.bin": "corrupted",
            "docs/back.txt": "came_back",
        }
        changed = _row_block(body, by_path["docs/changed.txt"]["id"])
        assert _rule_links(changed) == [(1, "docs/changed.txt")]
        for relpath in ("docs/gone.txt", "docs/rot.bin", "docs/back.txt"):
            block = _row_block(body, by_path[relpath]["id"])
            assert _rule_links(block) == [], relpath
            assert "rules/new" not in block, relpath
        # ...and nowhere else on the page either: one link, for the one ordinary change.
        assert _rule_links(body) == [(1, "docs/changed.txt")]
        assert _covered(body) == [], "no rule exists, so no row is called covered"
        # The link opens the rule page for that file.
        (href,) = _rule_hrefs(changed)
        opened = _get(client, href, "rule_new.html")[0]
        assert _starting_from(opened) == "docs/changed.txt"


def test_the_fleet_page_offers_the_rule_link_on_ordinary_changes_only(cairn_env):
    a, b = cairn_env / "alpha", cairn_env / "beta"
    a.mkdir()
    b.mkdir()

    async def seed():
        await _seed_files(await seed_collection(a), ROW_KINDS)
        await _seed_files(await seed_collection(b), [
            ("b/edit.txt", "modified"),
            ("b/lost.txt", "missing"),
            ("b/rot.bin", "modified", {"kind": "corrupted"}),
        ])

    with _make_client(cairn_env, seed) as client:
        body = _get(client, "/review", "review_fleet.html")[0]
        for name in ("gone.txt", "rot.bin", "back.txt", "lost.txt"):
            assert name in body, "the row is listed"
        # Each link names the row's OWN collection.
        assert sorted(_rule_links(body)) == [(1, "docs/changed.txt"), (2, "b/edit.txt")]
        for href in _rule_hrefs(body):
            assert client.get(href).status_code == 200


def test_rows_in_folder_groups_and_in_a_scoped_view_offer_the_link_too(cairn_env):
    """The page groups issues by folder and can be opened at one folder; a changed file's row
    offers the link wherever it is drawn, and a missing file's never does."""
    seed = _photos(cairn_env, [
        ("top.txt", "modified"),
        ("a/x.txt", "modified"),
        ("a/deep/z.txt", "modified"),
        ("a/deep/gone.txt", "missing"),
        ("b/y.txt", "modified"),
        ("b/lost.txt", "missing"),
    ])
    with _make_client(cairn_env, seed) as client:
        whole, _r = _review(client)
        scoped, _r = _review(client, "/collection/1/review?dir=a")
        only, _r = _review(client, "/collection/1/review?dir=a&deep=0")

    assert sorted(_rule_links(whole)) == [
        (1, "a/deep/z.txt"), (1, "a/x.txt"), (1, "b/y.txt"), (1, "top.txt"),
    ]
    assert sorted(_rule_links(scoped)) == [(1, "a/deep/z.txt"), (1, "a/x.txt")]
    assert _rule_links(only) == [(1, "a/x.txt")]


AWKWARD = "wild dir/a&b #1?x 'q' \"d\" é+%41;=<i>.txt"


def test_a_relpath_with_reserved_characters_round_trips_through_the_link(cairn_env):
    """`&`, `#`, `?`, a space, both quotes, `+`, a literal `%41`, markup and non-ASCII: following
    the row's link yields a rule page that starts from exactly that file — and the pattern its
    "only this file" card stores is exactly that path."""
    seed = _photos(cairn_env, [(AWKWARD, "modified"), ("wild dir/other.txt", "ok")])
    with _make_client(cairn_env, seed) as client:
        body, _r = _review(client)
        assert _rule_links(body) == [(1, AWKWARD)]
        fleet = client.get("/review").text
        assert _rule_links(fleet) == [(1, AWKWARD)]
        (href,) = _rule_hrefs(body)
        # Nothing in the address is left for a browser to reinterpret.
        assert not set(href) & set(" \"'<>#") and href.count("?") == 1 and "&" not in href
        opened, ctx = _get(client, href, "rule_new.html")
        assert ctx["start"] == AWKWARD
        assert _starting_from(opened) == AWKWARD
        assert _free_field(opened)["fields"]["path"] == AWKWARD
        assert "<i>" not in opened
        cards = _cards(opened)
        assert cards["file"]["pattern"] == AWKWARD
        assert cards["file"]["form"]["fields"]["pattern"] == AWKWARD
        assert cards["file"]["form"]["attrs"]["data-pattern"] == AWKWARD
        assert cards["folder"]["pattern"] == "wild dir/"
        resp = client.post(
            cards["file"]["form"]["action"], data=cards["file"]["form"]["fields"],
            follow_redirects=False,
        )
        assert resp.status_code == 303
    assert _patterns() == [AWKWARD]


@pytest.mark.parametrize("relpath", ["notes/line\nbreak.txt", "Album/Icon\r"], ids=["LF", "CR"])
def test_a_rule_link_never_opens_a_page_that_has_forgotten_the_file(cairn_env, relpath):
    """A file name may hold a line break (macOS writes ``Icon\\r`` into any folder given a custom
    icon). The grammar cannot name such a file exactly, but its folder is an ordinary pattern.
    Whatever the row offers, it must not be a link to a page that silently starts from nothing:
    every link a row offers opens a page that starts from that row's file and proposes at least
    one pattern matching it."""
    seed = _photos(cairn_env, [(relpath, "modified"), (relpath.split("/")[0] + "/plain.txt", "ok")])
    with _make_client(cairn_env, seed) as client:
        body, r = _review(client)
        assert [row["kind"] for row in r["rows"]] == ["changed"]
        assert _rule_links(body) in ([], [(1, relpath)])
        for href in _rule_hrefs(body):
            opened, ctx = _get(client, href, "rule_new.html")
            assert ctx["start"] == relpath
            cards = _cards(opened)
            assert cards, "the page proposes at least one pattern for the file it was opened from"
            for card in cards.values():
                assert _matching(card["pattern"], [relpath]) == [relpath]


def test_the_review_guidance_says_a_rule_accepts_nothing_and_covers_no_deletion(cairn_env):
    """D5: "Missing files' rows offer no rule link (there is no rule for deletions); the guidance
    says so"."""
    seed = _photos(cairn_env, [("a.txt", "modified"), ("b.txt", "missing")])
    with _make_client(cairn_env, seed) as client:
        body, _r = _review(client)
    said = _text(_help(body))
    assert "A rule accepts nothing already listed" in said
    assert (
        "never covers a deletion, possible corruption or a file that comes back different" in said
    )
    # The mass-change ceiling is a property of a scan, not something to say about one file's rule.
    assert "unusually large" not in said
    assert "A row marked Covered by rule only needs its change accepted." in said


# =============================================================================================
# B. What the rule page proposes
# =============================================================================================


def test_the_rule_page_proposes_the_file_its_folder_and_its_name_and_a_free_field(cairn_env):
    """Scenario "The rule page proposes patterns for a file"."""
    seed = _photos(cairn_env, [(NOTES, "modified"), ("clients/beta/NOTES.md", "ok")])
    with _make_client(cairn_env, seed) as client:
        body = _rule_page(client, path=NOTES)

    cards = _cards(body)
    assert {k: c["pattern"] for k, c in cards.items()} == {
        "file": NOTES,
        "folder": "clients/acme/",
        "name": "NOTES.md",
    }
    assert cards["file"]["label"] == "Only this file"
    assert "Everything in its folder" in cards["folder"]["label"]
    assert cards["name"]["label"] == "Every file named NOTES.md, in any folder"
    for card in cards.values():
        assert not _says_it_can_match_other_files(card), "nothing here is a wildcard"
    for card in cards.values():
        # Each is one click from being stored — a POST that carries exactly the pattern shown —
        # and each states its reach before that click.
        form = card["form"]
        assert form["action"] == "/collection/1/rules"
        assert form["fields"]["pattern"] == card["pattern"]
        assert form["attrs"]["data-pattern"] == card["pattern"]
        assert form["fields"]["csrf_token"]
        assert "confirm(" in form["attrs"]["onsubmit"]
        assert card["reach"] is not None
    assert _starting_from(body) == NOTES
    free = _free_field(body)
    assert free["fields"] == {"path": NOTES, "pattern": ""}


def test_a_file_in_the_collection_root_gets_no_only_this_file_and_no_folder_proposal(cairn_env):
    """A bare name is, by the grammar, that name in ANY folder — so for a file directly in the
    root "only this file" would be a mislabel, and its folder would be a rule for everything."""
    from src.services import rules

    seed = _photos(cairn_env, [
        ("NOTES.md", "modified"), ("sub/NOTES.md", "ok"), ("sub/other.txt", "ok"),
    ])
    with _make_client(cairn_env, seed) as client:
        body = _rule_page(client, path="NOTES.md")

    cards = _cards(body)
    # What is offered: the name in any folder, and only that — labelled as what it is.
    assert set(cards) == {"name"}
    assert cards["name"]["pattern"] == "NOTES.md"
    assert cards["name"]["label"] == "Every file named NOTES.md, in any folder"
    # ...with a count that shows it is not "only this file".
    assert cards["name"]["reach"][:2] == (2, 3)
    said = _said(body)
    assert "Only this file" not in said and "Everything in its folder" not in said
    for card in cards.values():
        assert card["pattern"] not in ("", "/", "*", "**", "**/*")
        assert rules.compile_pattern(card["pattern"]).fullmatch("NOTES.md")
    _free_field(body)


LONG = ("d" * 200 + "/") * 2 + "n" * 198 + ".txt"  # 604 bytes: longer than a pattern may be

# (starting path, other tracked files, proposals that MUST be offered)
AWKWARD_NAMES = [
    ("a b/c d.txt", ["a b/e.txt", "ab/cd.txt", "z/c d.txt"], {"file", "folder", "name"}),
    ("x/[1].txt", ["x/1.txt", "y/[1].txt", "y/1.txt"], {"file", "folder", "name"}),
    ("x/what?.md", ["x/whatX.md", "y/whatZ.md", "x/what.md"], {"folder"}),
    ("x/a*b.txt", ["x/aZZb.txt", "y/ab.txt", "x/a.txt"], {"folder"}),
    ("x/ lead.txt", ["x/lead.txt", "y/lead.txt", "y/ lead.txt"], {"folder"}),
    (LONG, ["d/" + "n" * 198 + ".txt", "other.txt"], {"name"}),
    ("x/**/y.md", ["x/y.md", "x/k/y.md", "x/other.txt", "q/y.md"], {"name"}),
    # A file literally named `*`: its name alone would be a rule for everything.
    ("x/*", ["x/a.txt", "x/k/b.txt", "y/c.txt"], {"folder"}),
    ("*/a.txt", ["k/a.txt", "k/b.txt", "a.txt"], {"name"}),
    ("x/trail ", ["x/trail", "y/trail ", "x/other"], {"folder"}),
    ("x /back\\slash$^(1){2}|.txt", ["x /backslash1.txt", "x/y.txt"], {"file", "folder", "name"}),
    ("x/tab\there.txt", ["x/tabthere.txt", "x/tab there.txt"], {"file", "folder", "name"}),
    ("x/Ére\u0301.txt", ["x/\u00c9r\u00e9.txt", "x/ere.txt"], {"file", "folder", "name"}),
]


def _says_it_can_match_other_files(card: dict) -> bool:
    """The card tells the operator its pattern holds wildcards and reaches past the one file,
    folder or name it was derived from (the sentence may be reworded; the two facts may not go)."""
    said = card["text"]
    return "wildcard" in said and re.search(r"match other files", said) is not None


def _beyond_label(kind: str, start: str, other: str) -> bool:
    """Whether ``other`` lies outside what a card of this kind is labelled as covering."""
    folder, _, name = start.rpartition("/")
    if kind == "file":
        return other != start
    if kind == "folder":
        return not other.startswith(folder + "/")
    return other.rpartition("/")[2] != name


@pytest.mark.parametrize(
    ("start", "others", "must_offer"), AWKWARD_NAMES, ids=[c[0][:24] for c in AWKWARD_NAMES]
)
def test_every_offered_proposal_is_storable_matches_the_file_and_is_honest_about_wildcards(
    cairn_env, start, others, must_offer
):
    """For real names the grammar reads differently from how they look, a proposal is either
    absent or (a) stored unchanged, (b) matches the file it was derived from; and where it can
    match MORE than its label says — a `*` or `?` in the name is a wildcard, a folder literally
    named `**` spans folders — the card says so and its count includes the extra files."""
    from src.services import rules

    tracked = [start, *others]
    seed = _photos(cairn_env, [(start, "modified")] + [(p, "ok") for p in others])
    with _make_client(cairn_env, seed) as client:
        body = _rule_page(client, path=start)
        cards = _cards(body)
        assert must_offer <= set(cards), f"offered {sorted(cards)}"
        assert _starting_from(body) == start
        for kind, card in cards.items():
            pattern = card["form"]["fields"]["pattern"]
            assert card["pattern"] == pattern, "the pattern shown is the pattern submitted"
            assert rules.validate_pattern(pattern) == pattern, (kind, pattern)
            assert rules.compile_pattern(pattern).fullmatch(start), (kind, pattern)
            hits = _matching(pattern, tracked)
            # The count is of what the pattern really matches, not of what the label suggests.
            assert card["reach"][:2] == (len(hits), len(tracked)), (kind, pattern)
            extra = [p for p in hits if _beyond_label(kind, start, p)]
            if extra:
                assert _says_it_can_match_other_files(card), (kind, pattern, extra)
        # ...and the server stores each exactly as offered.
        for card in cards.values():
            resp = client.post(
                card["form"]["action"], data=card["form"]["fields"], follow_redirects=False
            )
            assert (resp.status_code, resp.headers["location"]) == (303, "/collection/1/review")
        assert sorted(_patterns()) == sorted(c["pattern"] for c in cards.values())


def test_the_awkward_cases_really_do_reach_past_their_labels(cairn_env):
    """Guards the test above against passing vacuously: these three starting files DO produce a
    proposal whose pattern matches a file its label does not describe, and each says so."""
    cases = {
        "x/what?.md": ("file", "x/whatX.md"),
        "x/a*b.txt": ("name", "x/aZZb.txt"),
        "x/**/y.md": ("file", "x/k/y.md"),
    }
    seed = _photos(cairn_env, [(p, "ok") for p in cases] + [(o, "ok") for _k, o in cases.values()])
    with _make_client(cairn_env, seed) as client:
        for start, (kind, other) in cases.items():
            card = _cards(_rule_page(client, path=start))[kind]
            assert other in _matching(card["pattern"], [other])
            assert _says_it_can_match_other_files(card)
            assert card["reach"][0] >= 2


# =============================================================================================
# C. Reach, stated before the rule exists
# =============================================================================================

DETAIL = {"from": "a" * 64, "to": "b" * 64}


def _reach_seed(cairn_env):
    """`photos` (Locked) with a population built so every counting rule decides a number, and a
    second collection of the same shape whose files and alarms must not be counted at all."""
    seed = _photos(cairn_env, [
        (NOTES, "modified"),                                    # open alarm, no recorded path
        ("clients/acme/plan.docx", "ok"),
        ("clients/acme/sub/deep.txt", "modified", {
            "detail": json.dumps({"path": "clients/acme/sub/deep.txt", **DETAIL}),
        }),
        ("clients/beta/NOTES.md", "ok"),
        ("NOTES.md", "new"),
        ("clients/acme/gone.txt", "missing"),                  # in the folder, not tracked
        ("archive/NOTES.md", "missing"),                       # the name, not tracked
        ("other/readme.txt", "ok"),
    ])
    elsewhere = cairn_env / "elsewhere"
    elsewhere.mkdir()

    async def go():
        await seed()
        await _seed_events(1, [
            # Inside the window, on the starting file: counted by its present path.
            (NOTES, "modified", 30, None),
            (NOTES, "modified", 89, None),
            # Older than ninety days: not counted.
            (NOTES, "modified", 91, None),
            (NOTES, "modified", 400, None),
            # Not ordinary modification alarms: not counted, whatever they match.
            ("clients/acme/plan.docx", "corrupted", 10, None),
            ("clients/acme/plan.docx", "modified", 5,
             {"path": "clients/acme/plan.docx", **DETAIL, "mass": True}),
            ("NOTES.md", "restored_changed", 3, "recorded " + "a" * 64 + " → found " + "b" * 64),
            ("NOTES.md", "added", 3, None),
            (NOTES, "restored", 2, None),
            # Recorded under a path the file no longer has: counted by the RECORDED path — so it
            # counts for the folder it was in, and not for the name it has now.
            ("clients/beta/NOTES.md", "modified", 20, {"path": "clients/acme/OLD.md", **DETAIL}),
            # ...and the other way round: recorded as a NOTES.md, now called something else.
            ("other/readme.txt", "modified", 15, {"path": "elsewhere/NOTES.md", **DETAIL}),
            # An alarm whose file is no longer on record still carries the path it was raised for.
            (None, "modified", 8, {"path": "clients/acme/removed.txt", **DETAIL}),
            # ...and one with no file and no recorded path is about nothing a pattern can match.
            (None, "modified", 8, None),
        ])
        cid = await seed_collection(elsewhere)
        await _seed_files(cid, [(NOTES, "modified"), ("NOTES.md", "ok"), ("clients/acme/b", "ok")])
        await _seed_events(cid, [(NOTES, "modified", 1, None), ("NOTES.md", "modified", 1, None)])

    return go


def test_each_proposal_states_its_reach_from_the_collections_records(cairn_env):
    """Scenario "Each proposal states its reach": tracked matches out of the total, listed changed
    files, and the ninety-day change alarms for matching files — exact, for every proposal."""
    with _make_client(cairn_env, _reach_seed(cairn_env)) as client:
        before = _snapshot()
        body = _rule_page(client, path=NOTES)
        assert _snapshot() == before and _rules_json() == "[]", "stating a reach stores nothing"

    cards = _cards(body)
    # (tracked, total, listed as changed, alarms in ninety days)
    assert {k: c["reach"] for k, c in cards.items()} == {
        # One file. Its alarms: the open one and the two inside the window.
        "file": (1, 6, 1, 3),
        # The folder: three tracked (the missing one is not), two listed, and six alarms — the
        # starting file's three, the deep file's one, the one recorded as clients/acme/OLD.md and
        # the one recorded as clients/acme/removed.txt (a file no longer on record).
        "folder": (3, 6, 2, 6),
        # The name: three tracked (archive/NOTES.md is missing), one listed, four alarms — the
        # starting file's three and the one recorded as elsewhere/NOTES.md; NOT the alarm of the
        # file that is called NOTES.md today but was OLD.md when it fired.
        "name": (3, 6, 1, 4),
    }
    assert cards["file"]["lines"] == [
        "1 of 6 tracked files matches it now",
        "1 of them is listed as changed right now — it stays listed until accepted",
        "3 change alarms in the last 90 days were for files it matches",
    ]
    # The confirmation each "Add this rule" asks for repeats the card's own count, and says that
    # nothing listed is accepted.
    for card in cards.values():
        confirm = card["form"]["attrs"]["onsubmit"]
        said = re.search(r"changes to the ([\d,]+) files? it matches", confirm)
        assert said and _int(said.group(1)) == card["reach"][0], confirm
        assert "Nothing listed for review is accepted" in confirm
        assert "are not flagged, however large the change" in confirm
        assert (
            "Deletions, possible corruption and a file that comes back different still" in confirm
        )
        # The ceiling is not a check on one file, so the confirm for one rule does not cite it.
        assert "unusually large" not in confirm


def test_the_alarm_figure_is_worded_as_alarms_for_matching_files_not_as_alarms_prevented(
    cairn_env,
):
    with _make_client(cairn_env, _reach_seed(cairn_env)) as client:
        pages = [_rule_page(client, path=NOTES), _rule_page(client, path=NOTES, pattern="*.md")]

    for body in pages:
        said = _said(body).lower()
        assert "for files it matches" in said
        for claim in ("prevent", "would have", "would not have", "avoided", "saved you"):
            assert claim not in said, claim


def test_accepting_a_deletion_does_not_erase_the_files_alarms_from_the_ninety_day_figure(
    cairn_env,
):
    """"SHALL match each by the path recorded with the alarm where one was recorded": once a
    file's record is gone, the path its alarms were raised for is still on the event."""
    seed = _photos(cairn_env, [("clients/acme/old.txt", "missing"), ("clients/acme/k.txt", "ok")])

    async def go():
        await seed()
        await _seed_events(1, [("clients/acme/old.txt", "modified", 10, None)])

    with _make_client(cairn_env, go) as client:
        before = _cards(_rule_page(client, pattern="clients/acme/"))["custom"]["reach"]
        assert before == (1, 1, 0, 1)
        body, _r = _review(client)
        (accept,) = _row_forms(_row_block(body, 1))
        assert accept["button"] == "Accept deletion"
        resp = client.post(accept["action"], data=accept["fields"], follow_redirects=False)
        assert resp.status_code == 303 and "stale" not in resp.headers["location"]
        after = _cards(_rule_page(client, pattern="clients/acme/"))["custom"]["reach"]

    assert after == (1, 1, 0, 1), "the alarm of ten days ago is still an alarm of the last ninety"


def test_a_typed_patterns_reach_is_counted_the_same_way(cairn_env):
    with _make_client(cairn_env, _reach_seed(cairn_env)) as client:
        cards = _cards(_rule_page(client, pattern="clients/*/NOTES.md"))
        nothing = _cards(_rule_page(client, pattern="nowhere/*.tmp"))

    # clients/acme/NOTES.md and clients/beta/NOTES.md; one listed; the starting file's three alarms
    # (beta's alarm was recorded as clients/acme/OLD.md, which this pattern does not match).
    assert cards["custom"]["reach"] == (2, 6, 1, 3)
    assert nothing["custom"]["reach"] == (0, 6, 0, 0)
    assert "matches no file" in nothing["custom"]["text"]


# =============================================================================================
# D. What a rule does, what it never covers, and where it does nothing
# =============================================================================================


def test_the_rule_page_says_what_a_rule_never_covers(cairn_env):
    """Scenario "The page says what a rule never covers"."""
    seed = _photos(cairn_env, [(NOTES, "modified")])
    with _make_client(cairn_env, seed) as client:
        rule_page = _rule_page(client, path=NOTES)
        rules_page = _rules_page(client)

    for body in (rule_page, rules_page):
        said = _said(body)
        facts = _text(body[body.index('id="rule-facts"'):])
        # What it does.
        assert "From the next scan, an ordinary change to a matching file is recorded" in facts
        assert "not flagged" in facts
        # What it never does — each flagged "exactly as for any other" file.
        assert "These are flagged for a matching file exactly as for any other" in facts
        assert "Deletions." in facts
        assert "Possible corruption" in facts
        assert "comes back different" in facts
        assert "unusually large change" in facts
        # Still watched and notarized; listed changes stay listed until accepted.
        assert "Matching files are still watched" in facts
        assert "notarized" in facts
        assert "A rule accepts nothing." in facts
        assert "Changes already listed for review stay listed until you accept them." in facts
        # Nothing on either page says a rule clears, accepts or resolves what is listed.
        for claim in ("clears the", "resolves", "stops watching", "no longer watched"):
            assert claim not in said, claim
    assert "ordinary changes to the files it matches stop being flagged" in _said(rule_page)
    # The rules page also says what removing one does.
    removed = _text(rules_page[rules_page.index('id="rule-facts"'):])
    assert "Removing a rule changes nothing about files already recorded" in removed
    assert "later changes to the files it matched follow the collection's policy" in removed


def test_a_tripwire_collection_is_not_told_its_files_are_re_stamped(cairn_env):
    seed = _photos(cairn_env, [(NOTES, "modified")], ots_mode="none")
    with _make_client(cairn_env, seed) as client:
        body = _rule_page(client, path=NOTES)
    facts = _text(body[body.index('id="rule-facts"'):])
    assert "re-stamped" not in facts
    assert "notarized if the collection is" in facts


@pytest.mark.parametrize("with_rule", [False, True], ids=["no rules", "one rule"])
def test_a_working_collection_is_told_a_rule_changes_nothing_there(cairn_env, with_rule):
    """Scenario "A rule in a collection whose policy already expects edits says so"."""
    seed = _photos(cairn_env, [(NOTES, "modified")], mode="churn")
    if with_rule:
        seed = _with_rules(seed, "NOTES.md")
    with _make_client(cairn_env, seed) as client:
        pages = [_rule_page(client, path=NOTES), _rule_page(client), _rules_page(client)]

    for body in pages:
        note = re.search(r'id="rule-policy-note".*?</div>\s*</div>', body, re.S)
        assert note, "the page says a rule has no effect under this policy"
        said = _text(note.group(0))
        assert "Working collection" in said
        assert re.search(r"(a rule changes|these rules change) nothing here", said)
        assert "Locked" in said
        # It is said before the patterns and the list, not after them.
        assert body.index('id="rule-policy-note"') < body.index('id="rule-own"')


def test_a_locked_collection_is_not_told_a_rule_changes_nothing(cairn_env):
    seed = _with_rules(_photos(cairn_env, [(NOTES, "modified")]), "NOTES.md")
    with _make_client(cairn_env, seed) as client:
        pages = [_rule_page(client, path=NOTES), _rules_page(client)]
    for body in pages:
        assert "rule-policy-note" not in body
        assert not re.search(r"changes? nothing here", _said(body))


# =============================================================================================
# E. A pattern of the operator's own: checked before it is stored, refused with a reason
# =============================================================================================


def test_a_typed_pattern_is_checked_without_being_stored(cairn_env):
    """Scenario "A pattern of the operator's own is checked before it is stored"."""
    seed = _photos(cairn_env, [
        (NOTES, "modified"), ("clients/beta/NOTES.md", "ok"), ("clients/beta/x.txt", "ok"),
        ("gone/NOTES.md", "missing"),
    ])
    with _make_client(cairn_env, seed) as client:
        before = _snapshot()
        body = _rule_page(client, path=NOTES, pattern="  clients/*/NOTES.md ")
        again = _rule_page(client, pattern="*.md")
        assert _rules_json() == "[]", "asking what a pattern matches stores nothing"
        assert _snapshot() == before

    cards = _cards(body)
    own = cards["custom"]
    assert own["label"] == "Your pattern"
    assert own["reach"] == (2, 3, 1, 1)
    # The spaces around it are not part of the pattern: everything said is about it without them.
    assert own["pattern"] == "clients/*/NOTES.md"
    assert own["form"]["fields"]["pattern"] == "clients/*/NOTES.md"
    assert _typed(body) == "clients/*/NOTES.md"
    assert _refusal(body) is None
    # The file's own proposals are still offered beside it, and the starting path is kept.
    assert {"file", "folder", "name"} <= set(cards)
    assert _free_field(body)["fields"]["path"] == NOTES
    # With no starting file there is just the operator's pattern.
    assert set(_cards(again)) == {"custom"} and _cards(again)["custom"]["reach"] == (2, 3, 1, 1)


# (what is typed, words the stated reason must contain)
REFUSED = [
    ("", ["pattern"]),
    ("   ", ["pattern"]),
    ("/etc/passwd", ["slash"]),
    ("clients/../secret.txt", [".."]),
    ("a//b", ["empty folder"]),
    ("a**b", ["**"]),
    ("docs/**.md", ["**"]),
    ("é" * 257, ["long"]),
]


@pytest.mark.parametrize(("typed", "words"), REFUSED, ids=[repr(t[0])[:16] for t in REFUSED])
def test_a_refused_pattern_states_its_reason_keeps_what_was_typed_and_stores_nothing(
    cairn_env, typed, words
):
    """Scenario "A refused pattern states its reason" — when checked (GET) and when submitted
    (POST), which lands back on the rule page saying the same thing."""
    seed = _photos(cairn_env, [(NOTES, "modified"), ("a/b", "ok")])
    with _make_client(cairn_env, seed) as client:
        before = _snapshot()
        checked = _rule_page(client, path=NOTES, pattern=typed)

        resp = _post(
            client, "/collection/1/rules", csrf_token=_csrf(client), pattern=typed, path=NOTES
        )
        assert resp.status_code == 303
        location = urlsplit(resp.headers["location"])
        assert (location.scheme, location.netloc, location.path) == (
            "", "", "/collection/1/rules/new"
        )
        submitted = _get(client, resp.headers["location"], "rule_new.html")[0]
        review = client.get("/collection/1/review").text

        assert _rules_json() == "[]"
        assert _snapshot() == before

    for body in (checked, submitted):
        reason = _refusal(body)
        assert reason and "Not added" in reason, reason
        for word in words:
            assert word in reason, (word, reason)
        own = _cards(body)["custom"]
        assert own["form"] is None, "a refused pattern has nothing to add"
        assert own["reach"] is None, "a refused pattern is given no reach"
        assert _typed(body) == typed.strip(" \t")
        # The file the operator came from is still there to start again from.
        assert _starting_from(body) == NOTES
        assert {"file", "folder", "name"} <= set(_cards(body))
    # A refused submission is not confirmed anywhere.
    assert _done_banner(review) is None


def test_every_refusal_gives_its_own_reason(cairn_env):
    seed = _photos(cairn_env, [(NOTES, "modified")])
    with _make_client(cairn_env, seed) as client:
        reasons = {
            typed: _refusal(_rule_page(client, pattern=typed))
            for typed in ("", "/abs", "a/../b", "a**b", "**", "é" * 257)
        }
    assert all(reasons.values())
    assert len(set(reasons.values())) == len(reasons), reasons


@pytest.mark.parametrize("typed", ["**", "*", "**/*", "*/**", "?*", " ** "])
def test_a_match_all_pattern_is_refused_with_a_pointer_to_the_policy(cairn_env, typed):
    """Scenario "A match-all pattern is refused with a pointer to the policy"."""
    seed = _photos(cairn_env, [(NOTES, "modified")])
    with _make_client(cairn_env, seed) as client:
        checked = _rule_page(client, pattern=typed)
        resp = _post(client, "/collection/1/rules", csrf_token=_csrf(client), pattern=typed)
        assert resp.status_code == 303
        submitted = _get(client, resp.headers["location"], "rule_new.html")[0]
        assert _rules_json() == "[]"

    for body in (checked, submitted):
        reason = _refusal(body)
        assert "A rule cannot cover every file" in reason
        assert "policy" in reason
        refusal_html = body[body.index('id="rule-refusal"'):].split("</div>")[0]
        assert "/collection/1/edit" in [a.get("href") for a in _tags(refusal_html, "a")]
        assert _cards(body)["custom"]["form"] is None
        assert _typed(body) == typed.strip()


def test_a_duplicate_of_an_existing_rule_is_refused(cairn_env):
    seed = _with_rules(_photos(cairn_env, [(NOTES, "modified")]), "NOTES.md", "exports/")
    with _make_client(cairn_env, seed) as client:
        stored = _rules_json()
        for typed in ("NOTES.md", "  NOTES.md\t", "exports/"):
            checked = _rule_page(client, pattern=typed)
            resp = _post(client, "/collection/1/rules", csrf_token=_csrf(client), pattern=typed)
            assert resp.status_code == 303
            assert urlsplit(resp.headers["location"]).path == "/collection/1/rules/new"
            submitted = _get(client, resp.headers["location"], "rule_new.html")[0]
            for body in (checked, submitted):
                reason = _refusal(body)
                assert reason.startswith("Not added.") and "already has" in reason
                assert _cards(body)["custom"]["form"] is None
                assert _typed(body) == typed.strip()
            assert _rules_json() == stored
        # A proposal that is already a rule says so instead of offering to add it again.
        name = _cards(_rule_page(client, path=NOTES))["name"]
        assert name["form"] is None and "Already a rule" in name["text"]
        assert name["reach"] is not None


def test_a_hundred_and_first_rule_is_refused(cairn_env):
    seed = _photos(cairn_env, [(NOTES, "modified")])
    with _make_client(cairn_env, seed) as client:
        _write_rules_json([_rule(f"folder-{i}/") for i in range(100)])
        stored = _rules_json()
        checked = _rule_page(client, pattern="one-more.txt")
        resp = _post(
            client, "/collection/1/rules", csrf_token=_csrf(client), pattern="one-more.txt"
        )
        assert resp.status_code == 303
        submitted = _get(client, resp.headers["location"], "rule_new.html")[0]
        rules_page = _rules_page(client)
        assert _rules_json() == stored

    for body in (checked, submitted):
        reason = _refusal(body)
        assert reason.startswith("Not added.") and "100" in reason
        assert _cards(body)["custom"]["form"] is None
        assert _typed_value(body) == "one-more.txt"
    assert len(_rule_rows(rules_page)) == 100
    # The list is full, and the page says so in place of the add form.
    assert _get_forms(rules_page, "/collection/1/rules/new") == []
    assert "100 rules, the most it can hold" in _said(rules_page)


# =============================================================================================
# F. Creating a rule resolves nothing, and says so once
# =============================================================================================

CREATE_SEED = [
    (NOTES, "modified"),
    ("clients/acme/sub/deep.txt", "modified"),
    ("clients/acme/plan.docx", "ok"),
    ("clients/acme/gone.txt", "missing"),
    ("other/x.txt", "modified"),
    ("other/rot.bin", "modified", {"kind": "corrupted"}),
]


def test_creating_a_rule_changes_no_file_record_and_no_event(cairn_env):
    """Scenario "Creating a rule resolves nothing and says so" — the datastore half, which is the
    point of the whole change: across the POST (and the page it lands on) every column of every
    file row, event row and run row is what it was, and the only thing written is the rule."""
    other = cairn_env / "other-collection"
    other.mkdir()
    seed = _photos(cairn_env, CREATE_SEED)

    async def go():
        await seed()
        await _seed_events(1, [(NOTES, "modified", 12, None), (NOTES, "restored", 40, None)])
        await _seed_files(await seed_collection(other), [(NOTES, "modified"), ("k.txt", "ok")])

    with _make_client(cairn_env, go) as client:
        body, _r = _review(client)
        (href,) = [h for h in _rule_hrefs(body) if "NOTES" in h]
        folder = _cards(_get(client, href, "rule_new.html")[0])["folder"]
        assert folder["pattern"] == "clients/acme/" and folder["reach"][2] == 2

        before = _snapshot()
        assert len(before["files"]) == 8 and len(before["events"]) >= 8
        assert _rules_json() == "[]"

        started = _now().replace(microsecond=0)
        resp = client.post(
            folder["form"]["action"], data=folder["form"]["fields"], follow_redirects=False
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/collection/1/review"
        after_post = _snapshot()

        landed = client.get(resp.headers["location"])
        assert landed.status_code == 200
        after_render = _snapshot()

    assert after_post["files"] == before["files"]
    assert after_post["events"] == before["events"]
    assert after_post["runs"] == before["runs"]
    assert after_post["collections"] == before["collections"], "only rules_json was written"
    assert after_render == after_post, "rendering the confirmation writes nothing either"
    # The one thing written: the rule, on this collection only.
    (entry,) = _stored(1)
    assert set(entry) == {"pattern", "effect", "created_at"}
    assert (entry["pattern"], entry["effect"]) == ("clients/acme/", "edits")
    created = datetime.fromisoformat(entry["created_at"])
    assert started <= created <= _now() + timedelta(seconds=1)
    assert _rules_json(2) == "[]"


def test_creating_a_rule_returns_to_the_review_page_which_says_so_once(cairn_env):
    """Scenario "Creating a rule resolves nothing and says so" — the page half: both matching
    files are still listed with their accept actions, and the review view states ONCE that the
    rule was added, that it applies from the next scan, and that two listed changes match it."""
    with _make_client(cairn_env, _photos(cairn_env, CREATE_SEED)) as client:
        before_body, before = _review(client)
        assert _done_banner(before_body) is None
        folder = _cards(_rule_page(client, path=NOTES))["folder"]
        resp = client.post(
            folder["form"]["action"], data=folder["form"]["fields"], follow_redirects=False
        )
        assert (resp.status_code, resp.headers["location"]) == (303, "/collection/1/review")

        body, r = _review(client, resp.headers["location"])
        banner = _done_banner(body)
        assert banner is not None
        assert banner.startswith("Rule added.")
        assert "It applies from the next scan." in banner
        assert "2 changed files listed here match it and are still listed" in banner
        assert "a rule does not accept anything" in banner
        assert "changes nothing while" not in banner, "this collection is Locked"
        # It names no outcome it did not have.
        assert "Accepted" not in banner and "removed" not in banner
        assert body.count('id="done-banner"') == 1
        assert body.index('id="done-banner"') < body.index('class="review-row"')
        banner_html = body[body.index('id="done-banner"'):].split("</div>\n  </div>")[0]
        assert "/collection/1/rules" in [a.get("href") for a in _tags(banner_html, "a")]

        # Everything that was listed is still listed, in the same state, with the same controls —
        # and the same fingerprints, since no record the controls cover has changed.
        assert [(x["relpath"], x["kind"], x["fp"]) for x in r["rows"]] == [
            (x["relpath"], x["kind"], x["fp"]) for x in before["rows"]
        ]
        assert r["accept_changes"]["count"] == before["accept_changes"]["count"] == 4
        assert r["accept_changes"]["fp"] == before["accept_changes"]["fp"]
        by_path = {x["relpath"]: x for x in r["rows"]}
        for relpath in (NOTES, "clients/acme/sub/deep.txt"):
            block = _row_block(body, by_path[relpath]["id"])
            assert "Contents changed" in _text(block)
            (accept,) = _row_forms(block)
            assert accept["button"] == "Accept change"
            # The rule now covers it, so the row names the rule instead of offering another.
            assert _covered(block) == [(1, "clients/acme/")]
            assert _rule_links(block) == []
        # The changed file the rule does not match still offers one.
        assert _rule_links(body) == [(1, "other/x.txt")]

        # Once: the same address again carries no confirmation — nor does any other page that
        # can show one.
        assert _done_banner(client.get(resp.headers["location"]).text) is None
        assert _done_banner(client.get("/review").text) is None
        assert _done_banner(client.get("/collection/1/rules").text) is None

        # The accept the page still offers still works after the rule: a rule took nothing away.
        accepted = client.post(accept["action"], data=accept["fields"], follow_redirects=False)
        assert accepted.status_code == 303 and "stale" not in accepted.headers["location"]
    assert _patterns() == ["clients/acme/"]


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        (NOTES, "1 changed file listed here matches it and is still listed"),
        ("clients/acme/plan.docx", "No changed file listed here matches it."),
        ("nowhere/*.tmp", "No changed file listed here matches it."),
    ],
    ids=["one", "none (an unchanged file)", "none (matches nothing)"],
)
def test_the_confirmation_states_how_many_listed_changes_match(cairn_env, pattern, expected):
    with _make_client(cairn_env, _photos(cairn_env, CREATE_SEED)) as client:
        resp = _post(
            client, "/collection/1/rules", csrf_token=_csrf(client), pattern=pattern, path=NOTES
        )
        assert (resp.status_code, resp.headers["location"]) == (303, "/collection/1/review")
        body, r = _review(client, resp.headers["location"])

    banner = _done_banner(body)
    assert banner.startswith("Rule added. It applies from the next scan.")
    assert expected in banner
    if expected.startswith("No "):
        assert "still listed" not in banner
    assert len(r["rows"]) == 5, "nothing left the list"


def test_the_confirmation_counts_a_possibly_corrupted_file_as_still_listed_not_as_covered(
    cairn_env,
):
    """A rule never covers possible corruption. A pattern that matches such a file may count it
    among the listed files that "are still listed" — but must not suggest the rule deals with
    it."""
    with _make_client(cairn_env, _photos(cairn_env, CREATE_SEED)) as client:
        resp = _post(
            client, "/collection/1/rules", csrf_token=_csrf(client), pattern="other/",
            path="other/x.txt",
        )
        body, r = _review(client, resp.headers["location"])

    banner = _done_banner(body)
    assert "still listed" in banner
    by_path = {x["relpath"]: x for x in r["rows"]}
    assert by_path["other/rot.bin"]["kind"] == "corrupted"
    block = _row_block(body, by_path["other/rot.bin"]["id"])
    assert "Possible corruption" in _text(block) and _rule_links(block) == []
    # The rule matches its path, and still the row is not called covered: no rule covers this.
    assert _covered(block) == []
    assert _covered(_row_block(body, by_path["other/x.txt"]["id"])) == [(1, "other/")]


@pytest.mark.parametrize("token", [None, "", "not-a-token"], ids=["absent", "blank", "forged"])
def test_creating_a_rule_without_a_valid_csrf_token_is_refused(cairn_env, token):
    with _make_client(cairn_env, _photos(cairn_env, CREATE_SEED)) as client:
        _csrf(client)  # a session exists; the submission simply does not carry its token
        before = _snapshot()
        data = {"pattern": "clients/acme/", "path": NOTES}
        if token is not None:
            data["csrf_token"] = token
        resp = client.post("/collection/1/rules", data=data, follow_redirects=False)
        assert resp.status_code == 403
        assert _rules_json() == "[]" and _snapshot() == before
        assert _done_banner(client.get("/collection/1/review").text) is None


@pytest.mark.parametrize(
    "value",
    ["rule:abc", "rule:99999999999", "evil", "rule:", "rule:-1", "rule:1.5", "RULE:1", "rule:1:2",
     "rules:3", "rule:0x10"],
)
def test_a_forged_confirmation_cookie_renders_no_banner_and_no_error(cairn_env, value):
    with _make_client(cairn_env, _photos(cairn_env, CREATE_SEED)) as client:
        for url in ("/collection/1/review", "/review", "/collection/1/rules"):
            resp = client.get(url, headers={"cookie": f"cairn_done={value}"})
            assert resp.status_code == 200, url
            assert _done_banner(resp.text) is None, url
            assert "Rule added" not in resp.text
        assert _rules_json() == "[]"


# =============================================================================================
# G. The rules page and the collection page
# =============================================================================================

LISTED_FILES = [
    ("NOTES.md", "ok"),
    ("clients/acme/NOTES.md", "modified"),
    ("clients/beta/NOTES.md", "new"),
    ("archive/NOTES.md", "missing"),          # matches the name, is not tracked
    ("exports/2026/q1.csv", "ok"),
    ("exports/q2.csv", "ok"),
    ("exports/gone.csv", "missing"),          # in the folder, is not tracked
    ("readme.txt", "ok"),
    ("src/main.py", "ok"),
]


def test_the_rules_page_lists_each_rule_with_what_it_matches_now(cairn_env):
    """Scenarios "Rules are listed with what they match" and "A rule that matches nothing says
    so" — with several rules at once, each counted on its own."""
    seed = _with_rules(
        _photos(cairn_env, LISTED_FILES),
        "NOTES.md", "exports/", "nothing-here/*.tmp", "src/main.py",
    )
    with _make_client(cairn_env, seed) as client:
        before = _snapshot()
        body = _rules_page(client)
        assert _snapshot() == before

    rows = _rule_rows(body)
    assert [r["pattern"] for r in rows] == [
        "NOTES.md", "exports/", "nothing-here/*.tmp", "src/main.py",
    ]
    notes, exports, nothing, one = rows
    assert "matches 3 of 7 tracked files" in notes["meta"]
    assert "matches 2 of 7 tracked files" in exports["meta"]
    assert "matches 1 of 7 tracked files" in one["meta"]
    assert "Currently matches no files" in nothing["meta"]
    assert "of 7" not in nothing["meta"]
    for row in (notes, exports, one):
        assert "matches no files" not in row["meta"]
    # Each can be removed, after a confirmation, by its pattern.
    for row in rows:
        form = row["form"]
        assert form["action"] == "/collection/1/rules/delete"
        assert set(form["fields"]) == {"csrf_token", "pattern"}
        assert form["fields"]["pattern"] == row["pattern"]
        assert form["attrs"]["data-pattern"] == row["pattern"]
        assert form["attrs"]["onsubmit"].startswith("return confirm(")
        assert form["button"] == "Remove"
        # A visible control, not a borderless word at the end of the row.
        assert "btn" in form["button_class"].split()
        assert "btn--ghost" not in form["button_class"].split()
    # The same form for adding a rule directly: a check first, nothing stored by it.
    add = _get_forms(body, "/collection/1/rules/new")
    assert len(add) == 1 and set(add[0]["fields"]) == {"pattern"}


def test_a_collection_with_no_rules_says_so(cairn_env):
    with _make_client(cairn_env, _photos(cairn_env, LISTED_FILES)) as client:
        body = _rules_page(client)
    assert _rule_rows(body) == []
    assert "No rules" in _said(body)
    assert len(_get_forms(body, "/collection/1/rules/new")) == 1


def test_an_unrecognised_stored_entry_is_shown_as_ignored(cairn_env):
    """D1: an entry whose effect is not the one legal value is inert, and the rules page shows it
    as unrecognised — never as a rule that matches files."""
    with _make_client(cairn_env, _photos(cairn_env, LISTED_FILES)) as client:
        _write_rules_json([_rule("NOTES.md"), _rule("exports/", effect="other")])
        body = _rules_page(client)
        detail = client.get("/collection/1").text

    notes, other = _rule_rows(body)
    assert "matches 3 of 7 tracked files" in notes["meta"]
    assert other["pattern"] == "exports/"
    assert "ignored" in other["meta"] and "Not recognised" in other["meta"]
    assert "matches nothing" in other["meta"]
    assert "of 7" not in other["meta"] and "Changes expected" not in other["meta"]
    # It can still be removed, by its pattern.
    assert other["form"]["fields"]["pattern"] == "exports/"
    # The collection's own page counts the rules that are in force.
    assert _collection_rules_note(detail)[0].startswith("1 rule ")


def test_an_ignored_entry_is_not_presented_as_a_rule_on_the_rule_page(cairn_env):
    """D1: an entry with an unrecognised effect is inert. The page that proposes patterns must
    not tell the operator the pattern is "already a rule" when the scanner ignores it."""
    with _make_client(cairn_env, _photos(cairn_env, LISTED_FILES)) as client:
        _write_rules_json([_rule("NOTES.md", effect="other")])
        assert "ignored" in _rule_rows(_rules_page(client))[0]["meta"]
        name = _cards(_rule_page(client, path="clients/acme/NOTES.md"))["name"]
        typed = _rule_page(client, pattern="NOTES.md")

    for said in (name["text"], _text(typed[typed.index('id="rule-custom"'):].split("rule-own")[0])):
        claims_a_rule = "Already a rule" in said or "already has that rule" in said
        assert not claims_a_rule or "ignored" in said or "ecognised" in said, said


JUNK = [
    "not json",
    "{}",
    "null",
    '"NOTES.md"',
    '[1, "x", null, {"pattern": 5, "effect": "edits"}, {"effect": "edits"},'
    ' {"pattern": "**", "effect": "edits"}, {"pattern": " padded ", "effect": "edits"},'
    ' {"pattern": "readme.txt", "effect": "edits", "created_at": 7}]',
]


@pytest.mark.parametrize("raw", JUNK, ids=[j[:10] for j in JUNK])
def test_a_rule_list_that_is_not_what_the_panel_wrote_breaks_no_page(cairn_env, raw):
    """Whatever the column holds, every page that reads it renders, and only a well-formed entry
    is counted or described as matching anything."""
    with _make_client(cairn_env, _photos(cairn_env, LISTED_FILES)) as client:
        _write_rules_json(raw)
        before = _snapshot()
        pages = {
            url: client.get(url)
            for url in ("/collection/1/rules", "/collection/1/rules/new?path=readme.txt",
                        "/collection/1/rules/new?pattern=exports/", "/collection/1",
                        "/collection/1/review", "/review", "/", "/collections")
        }
        assert _rules_json() == raw, "reading a rule list never rewrites it"
        assert _snapshot() == before

    for url, resp in pages.items():
        assert resp.status_code == 200, url
    rows = _rule_rows(pages["/collection/1/rules"].text)
    in_force = [r for r in rows if "ignored" not in r["meta"]]
    note = _collection_rules_note(pages["/collection/1"].text)[0]
    if raw.startswith("["):
        assert [r["pattern"] for r in in_force] == ["readme.txt"]
        assert "matches 1 of 7 tracked files" in in_force[0]["meta"]
        assert len(rows) == 8
        assert note.startswith("1 rule ")
    else:
        assert rows == [] and note.startswith("No rules ")


def test_editing_the_collection_keeps_its_rules(cairn_env):
    """The policy lives on the same row as the rules; changing it (the very thing the rule pages
    point at) must not lose or rewrite the rule list."""
    seed = _with_rules(_photos(cairn_env, LISTED_FILES), *("NOTES.md", "exports/"))
    with _make_client(cairn_env, seed) as client:
        stored = _rules_json()
        for mode in ("churn", "worm"):
            resp = client.post(
                "/collection/1",
                data={
                    "csrf_token": _csrf(client), "name": "photos",
                    "root": str(cairn_env / "photos"), "mode": mode, "ots": "perfile",
                    "cadence": "86400", "verify_cadence": "604800", "excludes": "**/*.tmp",
                },
                follow_redirects=False,
            )
            assert resp.status_code == 303, resp.text[:300]
            assert _rules_json() == stored
            listed = [r["pattern"] for r in _rule_rows(_rules_page(client))]
            assert listed == ["NOTES.md", "exports/"]


def _collection_rules_note(detail: str) -> tuple[str, list[str]]:
    m = re.search(r'<div class="meta-cell__note" id="collection-rules">(.*?)</div>', detail, re.S)
    assert m, "the collection page has no statement of its rules"
    return _text(m.group(1)), [a.get("href", "") for a in _tags(m.group(1), "a")]


LOCKED_CLAIM = "Every change and every deletion is flagged."


@pytest.mark.parametrize(
    ("patterns", "starts"),
    [((), "No rules "), (("NOTES.md",), "1 rule "), (("NOTES.md", "exports/"), "2 rules ")],
    ids=["none", "one", "two"],
)
def test_the_collection_page_states_its_rule_count_and_links_to_the_list(
    cairn_env, patterns, starts
):
    """Scenario "The collection page states its rules" — and, with rules present, a Locked
    collection is no longer described as flagging every change."""
    seed = _with_rules(_photos(cairn_env, LISTED_FILES), *patterns)
    with _make_client(cairn_env, seed) as client:
        detail = client.get("/collection/1").text
        described = {
            url: client.get(url).text
            for url in ("/", "/collections", "/collection/1", "/collection/1/rules", "/review",
                        "/collection/1/rules/new")
        }

    said, hrefs = _collection_rules_note(detail)
    assert said.startswith(starts), said
    assert hrefs == ["/collection/1/rules"]
    if not patterns:
        assert LOCKED_CLAIM in _text(detail)
        return
    n = len(patterns)
    expected = f"except ordinary changes to the files its {n} rule{'' if n == 1 else 's'} cover"
    expected += "s." if n == 1 else "."
    assert expected in _text(detail)
    assert "Every deletion is flagged" in _text(detail)
    # Nowhere that describes THIS collection is the unqualified claim left standing — not in the
    # visible text and not in a tooltip.
    for url, page in described.items():
        assert LOCKED_CLAIM not in html_lib.unescape(page), url


def test_a_working_collections_policy_text_is_not_rewritten_by_its_rules(cairn_env):
    seed = _with_rules(_photos(cairn_env, LISTED_FILES, mode="churn"), "NOTES.md")
    with _make_client(cairn_env, seed) as client:
        detail = client.get("/collection/1").text
    said = _text(detail)
    assert "Ordinary edits are not flagged." in said
    assert "except ordinary changes" not in said
    assert _collection_rules_note(detail)[0].startswith("1 rule ")


# =============================================================================================
# H. Removing a rule
# =============================================================================================

THREE = ("NOTES.md", "exports/", "src/main.py")


def test_removing_a_rule_removes_only_that_rule_and_changes_no_record(cairn_env):
    """Scenario "Removing a rule": the second of three goes, the other two stay as they were and
    in order, and no file, event or run row is touched."""
    seed = _with_rules(_photos(cairn_env, LISTED_FILES), *THREE)
    with _make_client(cairn_env, seed) as client:
        stored = _stored()
        before = _snapshot()
        rows = _rule_rows(_rules_page(client))
        assert [r["pattern"] for r in rows] == list(THREE)

        resp = client.post(
            rows[1]["form"]["action"], data=rows[1]["form"]["fields"], follow_redirects=False
        )
        assert resp.status_code == 303
        assert urlsplit(resp.headers["location"]).path == "/collection/1/rules"
        landed = _get(client, resp.headers["location"], "collection_rules.html")[0]
        after = _snapshot()

    assert _stored() == [stored[0], stored[2]], "the others are byte-for-byte what they were"
    assert after == before
    assert [r["pattern"] for r in _rule_rows(landed)] == ["NOTES.md", "src/main.py"]
    said = _said(landed)
    assert "Rule removed." in said
    assert "No file record changed." in said
    assert "follow the collection's policy" in said


def test_a_stale_removal_removes_nothing_and_says_so(cairn_env):
    """Scenario "A stale removal removes nothing else"."""
    seed = _with_rules(_photos(cairn_env, LISTED_FILES), *THREE)
    with _make_client(cairn_env, seed) as client:
        token = _csrf(client)
        stored = _rules_json()
        before = _snapshot()
        # Gone already, never there, and near misses of rules that ARE there.
        for pattern in ("drafts/", "notes.md", "NOTES.md ", " NOTES.md", "NOTES.m", "NOTES.md/",
                        "exports", "exports/*", "*", "", "1", "2"):
            resp = _post(client, "/collection/1/rules/delete", csrf_token=token, pattern=pattern)
            assert resp.status_code == 303, pattern
            assert _rules_json() == stored, f"{pattern!r} removed something"
            landed = _get(client, resp.headers["location"], "collection_rules.html")[0]
            assert "Nothing was removed" in _said(landed), pattern
            assert "Rule removed" not in _said(landed), pattern
            assert [r["pattern"] for r in _rule_rows(landed)] == list(THREE)
        # No pattern submitted at all.
        resp = _post(client, "/collection/1/rules/delete", csrf_token=token)
        assert resp.status_code in (303, 422)
        assert _rules_json() == stored
        assert _snapshot() == before


def test_removal_names_the_rule_by_its_pattern_not_its_position(cairn_env):
    """A list that changed since the page was rendered cannot lose a different rule: the page's
    second control removes the pattern it was rendered for, wherever that now sits — and a control
    for a rule that is already gone removes nothing."""
    seed = _with_rules(_photos(cairn_env, LISTED_FILES), *THREE)
    with _make_client(cairn_env, seed) as client:
        stale = _rule_rows(_rules_page(client))
        # Behind the page's back: the first rule goes and a new one is put in front.
        stored = _stored()
        _write_rules_json([_rule("added-later/"), stored[1], stored[2]])

        second = client.post(
            stale[1]["form"]["action"], data=stale[1]["form"]["fields"], follow_redirects=False
        )
        assert second.status_code == 303
        assert _patterns() == ["added-later/", "src/main.py"], "exports/ went — nothing else"

        first = client.post(
            stale[0]["form"]["action"], data=stale[0]["form"]["fields"], follow_redirects=False
        )
        assert first.status_code == 303
        assert _patterns() == ["added-later/", "src/main.py"], "NOTES.md was already gone"
        assert "Nothing was removed" in _said(
            _get(client, first.headers["location"], "collection_rules.html")[0]
        )


@pytest.mark.parametrize("token", [None, "", "not-a-token"], ids=["absent", "blank", "forged"])
def test_removing_a_rule_without_a_valid_csrf_token_is_refused(cairn_env, token):
    seed = _with_rules(_photos(cairn_env, LISTED_FILES), *THREE)
    with _make_client(cairn_env, seed) as client:
        _csrf(client)
        stored = _rules_json()
        data = {"pattern": "exports/"}
        if token is not None:
            data["csrf_token"] = token
        resp = client.post("/collection/1/rules/delete", data=data, follow_redirects=False)
        assert resp.status_code == 403
        assert _rules_json() == stored


def test_a_rule_route_cannot_be_driven_by_get(cairn_env):
    """Nothing is stored or removed by following a link."""
    seed = _with_rules(_photos(cairn_env, LISTED_FILES), *THREE)
    with _make_client(cairn_env, seed) as client:
        stored = _rules_json()
        resp = client.get("/collection/1/rules/delete", params={"pattern": "exports/"})
        assert resp.status_code in (404, 405)
        client.get("/collection/1/rules", params={"pattern": "new-rule/", "gone": "1"})
        client.get("/collection/1/rules/new", params={"pattern": "new-rule/", "path": "a/b"})
        assert _rules_json() == stored


# =============================================================================================
# I. Scoping and hostile input
# =============================================================================================


def _rule_routes(client, cid: int, token: str):
    """The four rule routes (the rule page asked both ways), against collection ``cid``."""
    return {
        "rule page": client.get(f"/collection/{cid}/rules/new", params={"path": "bobs/edit.txt"}),
        "rule page (pattern)": client.get(f"/collection/{cid}/rules/new", params={"pattern": "x"}),
        "rules page": client.get(f"/collection/{cid}/rules"),
        "create": _post(client, f"/collection/{cid}/rules", csrf_token=token, pattern="bobs/"),
        "remove": _post(
            client, f"/collection/{cid}/rules/delete", csrf_token=token, pattern="bobs-rule/"
        ),
    }


def test_every_rule_route_answers_not_found_for_a_collection_that_does_not_exist(cairn_env):
    with _make_client(cairn_env, _photos(cairn_env, [(NOTES, "modified")])) as client:
        token = _csrf(client)
        for name, resp in _rule_routes(client, 4242, token).items():
            assert resp.status_code == 404, name
        assert _rules_json() == "[]"
        assert _done_banner(client.get("/collection/1/review").text) is None


def test_rule_routes_are_scoped_to_the_viewers_own_collections(cairn_env, monkeypatch):
    """Scenario "A rule page is scoped to the owner": another user's collection is not found — its
    existence, its files and its rules are not disclosed, and nothing is stored on or removed
    from it."""
    from src.config import get_settings

    mine, theirs = cairn_env / "mine", cairn_env / "theirs"
    mine.mkdir()
    theirs.mkdir()

    async def seed():
        cid = await seed_collection(mine)
        await _seed_files(cid, [("my.txt", "modified")])

        async def other(s):
            from src.models.db import User
            from src.services.collections import create_collection

            u = User(username="bob", is_admin=False)
            s.add(u)
            await s.commit()
            c = await create_collection(
                s, user_id=u.id, name="Bobs Files", root=str(theirs), ots_mode="none"
            )
            return c.id

        bob_cid = await _with_session(other)
        assert bob_cid == 2
        await _seed_files(bob_cid, [("bobs/edit.txt", "modified"), ("bobs/secret.txt", "ok")])

    with _make_client(cairn_env, seed) as client:
        _write_rules_json([_rule("bobs-rule/")], cid=2)
        monkeypatch.setenv("CAIRN_AUTH_MODE", "multi")
        monkeypatch.setenv("CAIRN_SECRET_KEY", "0" * 64)
        get_settings.cache_clear()
        try:
            token = _csrf(client)
            bobs = _rules_json(2)
            before = _snapshot()
            for name, resp in _rule_routes(client, 2, token).items():
                assert resp.status_code == 404, name
                for leak in ("Bobs Files", "secret.txt", "edit.txt", "bobs-rule"):
                    assert leak not in resp.text, (name, leak)
            assert _rules_json(2) == bobs
            assert _rules_json(1) == "[]"
            assert _snapshot() == before
            # The viewer's own pages work, and do not offer Bob's file a rule.
            body, _r = _review(client)
            assert _rule_links(body) == [(1, "my.txt")]
            assert _rule_links(client.get("/review").text) == [(1, "my.txt")]
        finally:
            get_settings.cache_clear()


# What is put in the address as the starting path, and a fragment of it that must never reach the
# page as markup. All of these are LEGAL relative paths (a file could be called this), so the
# page may show them — escaped.
MARKUP_PATHS = [
    ("<script>alert('zqx1')</script>", "<script>alert("),
    ("\"><img src=zqx onerror=alert(2)>", "<img src=zqx"),
    ("zqx's/o'clock \"quoted\".txt", "zqx's"),
    ("zqx/</div></form><h1>owned</h1>", "<h1>owned"),
    ("zqx/' + alert(1) + '", None),
    ("zqx/{{ 7*7 }}{% raw %}", None),
    # A line break can be part of a real file name; today the page drops such a path.
    ("zqxa\nzqxb<b>.txt", "zqxb<b>"),
    ("zqxa\r\nzqxb<b>.txt", "zqxb<b>"),
]


@pytest.mark.parametrize(
    ("value", "raw"), MARKUP_PATHS, ids=[repr(c[0])[:16] for c in MARKUP_PATHS]
)
def test_a_starting_path_containing_markup_is_only_ever_escaped_text(cairn_env, value, raw):
    """Scenario "A hostile starting path is only text" — the markup half."""
    with _make_client(cairn_env, _photos(cairn_env, [(NOTES, "modified")])) as client:
        resp = client.get("/collection/1/rules/new", params={"path": value})
        with_pattern = client.get(
            "/collection/1/rules/new", params={"path": value, "pattern": "a/b"}
        )
        assert _rules_json() == "[]"

    for page in (resp, with_pattern):
        assert page.status_code == 200
        body = page.text
        if raw is not None:
            assert raw not in body
        # Shown, if at all, as exactly the text it is.
        assert _starting_from(body) in (None, value)
        assert "49" not in _said(body), "the path is never evaluated as a template"
        free = _free_field(body)
        assert free["fields"].get("path", value) == value
        for card in _cards(body).values():
            if card["form"] is None:
                continue
            form = card["form"]
            # The pattern travels in attributes a browser decodes back to exactly the pattern,
            # and the confirm reads it from the data attribute — never from the script text.
            assert form["attrs"]["data-pattern"] == form["fields"]["pattern"]
            assert form["fields"]["path"] in ("", value)
            assert "zqx" not in form["attrs"]["onsubmit"]
            assert "alert" not in form["attrs"]["onsubmit"]
            assert "this.dataset.pattern" in form["attrs"]["onsubmit"]
        # Nothing the path carried became a tag of its own.
        assert not [i for i in _tags(body, "img") if "zqx" in i.get("src", "")]
        assert "owned" not in [_text(h) for h in re.findall(r"<h1>(.*?)</h1>", body, re.S)]


ILLEGAL_PATHS = [
    "zqxa\x00zqxb",
    "../../etc/zqxpasswd",
    "zqx/../../etc/passwd",
    "/zqxabs/path.txt",
    "//zqx.example/x",
    "zqxa//zqxb",
    "./zqx",
    "zqx/.",
    "zqx/",
    "zqx" + "A" * 5000,
    "<script>zqx</script>/../x",
]


@pytest.mark.parametrize("value", ILLEGAL_PATHS, ids=[repr(v)[:18] for v in ILLEGAL_PATHS])
def test_a_starting_path_that_is_not_a_legal_relative_path_is_not_rendered_at_all(
    cairn_env, value
):
    """Scenario "A hostile starting path is only text" — the "or not at all" half: nothing of the
    value reaches the page, no proposal is derived from it, and the free field is still there."""
    with _make_client(cairn_env, _photos(cairn_env, [(NOTES, "modified")])) as client:
        resp = client.get("/collection/1/rules/new", params={"path": value})
        assert _rules_json() == "[]"

    assert resp.status_code == 200
    body = resp.text
    assert "zqx" not in body
    assert "A" * 64 not in body
    assert _starting_from(body) is None
    assert _cards(body) == {}
    free = _free_field(body)
    assert "path" not in free["fields"] and free["fields"]["pattern"] == ""


def test_an_illegal_starting_path_submitted_with_a_refused_pattern_is_not_carried_back(cairn_env):
    with _make_client(cairn_env, _photos(cairn_env, [(NOTES, "modified")])) as client:
        token = _csrf(client)
        for path in ("//zqx.example/x", "../zqx", "zqx\x00a", "zqx" + "A" * 5000):
            resp = _post(client, "/collection/1/rules", csrf_token=token, pattern="**", path=path)
            assert resp.status_code == 303
            location = resp.headers["location"]
            assert location.startswith("/collection/1/rules/new?") and "zqx" not in location
        # A line break is legal in a file name, so such a path IS carried back — percent-encoded,
        # never as a raw CR/LF that could start another response header.
        resp = _post(client, "/collection/1/rules", csrf_token=token, pattern="**",
                     path="zqxa\r\nSet-Cookie: zqx=1")
        assert resp.status_code == 303
        location = resp.headers["location"]
        assert location.startswith("/collection/1/rules/new?") and "zqxa%0D%0ASet-Cookie" in location
        assert "\r" not in location and "\n" not in location
        assert not any(k.lower() == "set-cookie" and "zqx" in v for k, v in resp.headers.items())
        assert _rules_json() == "[]"


def test_a_collection_name_with_quotes_and_markup_is_escaped_on_both_rule_pages(cairn_env):
    root = cairn_env / "O'Brien \"zqx\" <b>files"
    root.mkdir()

    async def seed():
        from src.database import get_sessionmaker
        from src.models.db import Collection
        from src.services import rules

        cid = await seed_collection(root)
        await _seed_files(cid, [(NOTES, "modified")])
        async with get_sessionmaker()() as s:
            await rules.add_rule(s, await s.get(Collection, cid), "NOTES.md")

    with _make_client(cairn_env, seed) as client:
        pages = [
            client.get("/collection/1/rules/new", params={"path": NOTES, "pattern": "**"}),
            client.get("/collection/1/rules"),
            client.get("/collection/1/rules", params={"gone": "<script>zqx</script>"}),
        ]
    for page in pages:
        assert page.status_code == 200
        assert "<b>files" not in page.text and "<script>zqx" not in page.text
        assert "O'Brien \"zqx\" <b>files" in _said(page.text)
        for form in _forms(page.text):
            assert "zqx" not in form["attrs"].get("onsubmit", "")
    # An unknown `gone` value says nothing was done either way.
    assert "Rule removed" not in pages[2].text and "Nothing was removed" not in pages[2].text


# Patterns the grammar ACCEPTS (every character but `*`, `?` and `/` is literal), so each is
# displayed, offered for storing, stored, listed and removable — and must stay text throughout.
HOSTILE_PATTERNS = [
    "'\"><script>alert('zqx9')</script>",
    "zqx' + alert(1) + '",
    "zqx\");alert(1);(\"",
    "zqx/<img src=x onerror=alert(3)>/",
    "zqx&amp;&lt;b&gt;.txt",
    "zqx\\'; alert(4); '",
]


@pytest.mark.parametrize("pattern", HOSTILE_PATTERNS, ids=[p[:14] for p in HOSTILE_PATTERNS])
def test_a_hostile_pattern_stays_text_when_checked_stored_listed_and_removed(cairn_env, pattern):
    with _make_client(cairn_env, _photos(cairn_env, [(NOTES, "modified")])) as client:
        checked = client.get("/collection/1/rules/new", params={"pattern": pattern})
        assert checked.status_code == 200
        assert _rules_json() == "[]"
        own = _cards(checked.text)["custom"]
        assert own["pattern"] == pattern
        assert _typed_value(checked.text) == pattern
        add = own["form"]
        assert add["fields"]["pattern"] == pattern
        assert add["attrs"]["data-pattern"] == pattern

        created = client.post(add["action"], data=add["fields"], follow_redirects=False)
        assert (created.status_code, created.headers["location"]) == (303, "/collection/1/rules")
        assert _patterns() == [pattern], "stored exactly as typed"
        landed = client.get(created.headers["location"])
        assert _done_banner(landed.text).startswith("Rule added.")
        assert "zqx" not in _done_banner(landed.text), "the confirmation does not echo the pattern"
        review = client.get("/collection/1/review")
        listed = client.get("/collection/1/rules")
        (row,) = _rule_rows(listed.text)
        assert row["pattern"] == pattern
        remove = row["form"]
        assert remove["fields"]["pattern"] == pattern
        assert remove["attrs"]["data-pattern"] == pattern

        for page in (checked, landed, review, listed, client.get("/collection/1")):
            assert page.status_code == 200
            for raw in ("<script>alert('zqx9')", "<img src=x onerror"):
                assert raw not in page.text
            assert not [i for i in _tags(page.text, "img") if i.get("onerror")]
        for form in (add, remove):
            # No pattern text is interpolated into the inline confirm: it reads the data attribute.
            script = form["attrs"]["onsubmit"]
            assert "zqx" not in script and "alert" not in script
            assert "this.dataset.pattern" in script
        # Entities in a pattern are not decoded into something else on the way round.
        if "&amp;" in pattern:
            assert html_lib.escape(pattern, quote=False) in listed.text

        gone = client.post(remove["action"], data=remove["fields"], follow_redirects=False)
        assert gone.status_code == 303
        assert _rules_json() == "[]"


REFUSED_HOSTILE = [
    "zqx\n<script>alert(7)</script>",
    "zqx\r\nSet-Cookie: zqx=1",
    "zqx\x00<b>",
    "/<script>alert('zqx')</script>",
    "<script>zqx</script>/../x",
    "zqx" * 400,
]


@pytest.mark.parametrize("pattern", REFUSED_HOSTILE, ids=[repr(p)[:18] for p in REFUSED_HOSTILE])
def test_a_hostile_pattern_the_grammar_refuses_is_reported_escaped_and_not_stored(
    cairn_env, pattern
):
    with _make_client(cairn_env, _photos(cairn_env, [(NOTES, "modified")])) as client:
        checked = client.get("/collection/1/rules/new", params={"pattern": pattern})
        resp = _post(client, "/collection/1/rules", csrf_token=_csrf(client), pattern=pattern)
        assert resp.status_code == 303
        location = resp.headers["location"]
        assert location.startswith("/collection/1/rules/new?")
        assert "\r" not in location and "\n" not in location and "<" not in location
        assert not any(k.lower() == "set-cookie" and "zqx" in v for k, v in resp.headers.items())
        submitted = client.get(location)
        assert _rules_json() == "[]"

    for page in (checked, submitted):
        assert page.status_code == 200
        body = page.text
        assert _refusal(body).startswith("Not added.")
        assert _cards(body)["custom"]["form"] is None
        for raw in ("<script>alert(", "<script>zqx", "<b>"):
            assert raw not in _content(body)
        # Nothing the refusal says is built from the pattern.
        assert "zqx" not in _refusal(body)


# =============================================================================================
# J. A file a rule already covers says so instead
# =============================================================================================

COVER_SEED = [
    (NOTES, "modified"),
    ("clients/acme/plan.txt", "modified"),
    ("other/x.txt", "modified"),
    ("other/gone.txt", "missing"),
    ("clients/acme/rot.bin", "modified", {"kind": "corrupted"}),
    ("clients/acme/back.txt", "modified", {"kind": "restored_changed"}),
    ("clients/acme/lost.txt", "missing"),
]


def test_a_changed_file_a_rule_covers_names_the_rule_instead_of_offering_another(cairn_env):
    """Scenario "A file a rule already covers says so instead" — on the collection's page and the
    fleet page; the rule named is the first in the list that matches; and only an ORDINARY change
    is ever called covered (no rule covers a deletion, corruption or a changed return)."""
    seed = _with_rules(_photos(cairn_env, COVER_SEED), "NOTES.md", "clients/acme/")
    with _make_client(cairn_env, seed) as client:
        before = _snapshot()
        body, r = _review(client)
        fleet = client.get("/review").text
        rules_page = client.get("/collection/1/rules")
        by_path = {row["relpath"]: row for row in r["rows"]}

        def block(relpath, page=None):
            return _row_block(page or body, by_path[relpath]["id"])

        # Both rules match NOTES.md; the row names the first of them.
        assert _covered(block(NOTES)) == [(1, "NOTES.md")]
        assert _covered(block("clients/acme/plan.txt")) == [(1, "clients/acme/")]
        for relpath in (NOTES, "clients/acme/plan.txt"):
            assert _rule_links(block(relpath)) == [] and "rules/new" not in block(relpath)
            # It still has to be accepted, and can be.
            (accept,) = _row_forms(block(relpath))
            assert accept["button"] == "Accept change"
            assert "Contents changed" in _text(block(relpath))
        # A change no rule matches is still offered one.
        assert _rule_links(block("other/x.txt")) == [(1, "other/x.txt")]
        assert _covered(block("other/x.txt")) == []
        # Findings no rule covers say neither — even though `clients/acme/` matches their paths.
        for relpath in ("clients/acme/rot.bin", "clients/acme/back.txt", "clients/acme/lost.txt",
                        "other/gone.txt"):
            assert _covered(block(relpath)) == [] and _rule_links(block(relpath)) == [], relpath
        for page in (body, fleet):
            assert sorted(_covered(page)) == [(1, "NOTES.md"), (1, "clients/acme/")]
            assert _rule_links(page) == [(1, "other/x.txt")]
        # The marker leads to the list the rule is in.
        assert rules_page.status_code == 200
        assert [x["pattern"] for x in _rule_rows(rules_page.text)] == ["NOTES.md", "clients/acme/"]

        # The other way round, the other rule is the first that matches.
        stored = _stored()
        _write_rules_json([stored[1], stored[0]])
        again, _r = _review(client)
        assert _covered(block(NOTES, again)) == [(1, "clients/acme/")]
        assert sorted(_covered(client.get("/review").text)) == [
            (1, "clients/acme/"), (1, "clients/acme/"),
        ]
        # Rendering any of it changed no record.
        assert _snapshot() == before


def test_each_collection_on_the_fleet_page_is_judged_by_its_own_rules(cairn_env):
    a, b = cairn_env / "alpha", cairn_env / "beta"
    a.mkdir()
    b.mkdir()

    async def seed():
        from src.database import get_sessionmaker
        from src.models.db import Collection
        from src.services import rules

        await _seed_files(await seed_collection(a), [("docs/a.txt", "modified")])
        cid = await seed_collection(b)
        await _seed_files(cid, [("docs/a.txt", "modified"), ("misc/b.txt", "modified")])
        async with get_sessionmaker()() as s:
            await rules.add_rule(s, await s.get(Collection, cid), "docs/")

    with _make_client(cairn_env, seed) as client:
        fleet = client.get("/review").text
        own = [_review(client, f"/collection/{cid}/review")[0] for cid in (1, 2)]

    assert _covered(fleet) == [(2, "docs/")]
    assert sorted(_rule_links(fleet)) == [(1, "docs/a.txt"), (2, "misc/b.txt")]
    assert (_covered(own[0]), _rule_links(own[0])) == ([], [(1, "docs/a.txt")])
    assert (_covered(own[1]), _rule_links(own[1])) == ([(2, "docs/")], [(2, "misc/b.txt")])


def test_an_entry_the_scanner_ignores_does_not_cover_a_file(cairn_env):
    """D1: an entry with an unrecognised effect is inert. A row must not be told a rule covers it
    when its next change will be flagged like any other."""
    with _make_client(cairn_env, _photos(cairn_env, COVER_SEED)) as client:
        _write_rules_json([
            _rule("NOTES.md", effect="other"),
            {"pattern": "clients/acme/", "effect": "ignore"},
            {"pattern": 5, "effect": "edits"},
            "clients/acme/plan.txt",
        ])
        body, _r = _review(client)
        fleet = client.get("/review").text

    for page in (body, fleet):
        assert _covered(page) == []
        assert sorted(_rule_links(page)) == [
            (1, NOTES), (1, "clients/acme/plan.txt"), (1, "other/x.txt"),
        ]


@pytest.mark.parametrize("patterns", [(), ("NOTES.md", "other/")], ids=["no rules", "two rules"])
def test_no_row_offers_or_names_a_rule_in_a_working_collection(cairn_env, patterns):
    """"This applies in a collection whose policy flags ordinary changes": under Working a rule
    does nothing, so a changed row neither invites one nor credits one."""
    seed = _with_rules(_photos(cairn_env, COVER_SEED, mode="churn"), *patterns)
    with _make_client(cairn_env, seed) as client:
        body, r = _review(client)
        scoped, _r = _review(client, "/collection/1/review?dir=clients")
        fleet = client.get("/review").text

    kinds = {row["relpath"]: row["kind"] for row in r["rows"]}
    assert kinds[NOTES] == kinds["other/x.txt"] == "changed", "the rows ARE ordinary changes"
    for page in (body, scoped, fleet):
        assert _rule_links(page) == []
        assert _covered(page) == []
        assert "rules/new" not in _content(page)


def test_a_covering_rules_pattern_is_only_text_in_the_row(cairn_env):
    name = "zqx<img src=x onerror=alert(1)> \"q\" 'r' &amp;.txt"
    pattern = "zqx<img src=x onerror=alert(1)> \"q\" 'r' &amp;*"
    seed = _with_rules(_photos(cairn_env, [(name, "modified"), ("plain.txt", "ok")]), pattern)
    with _make_client(cairn_env, seed) as client:
        pages = [_review(client)[0], client.get("/review").text]

    for page in pages:
        assert _covered(page) == [(1, pattern)]
        assert _rule_links(page) == []
        assert "<img src=x" not in page
        assert not [i for i in _tags(page, "img") if i.get("onerror")]


# =============================================================================================
# K. Where a new rule returns to, and what the page it returns to says — once
# =============================================================================================


def _banner_links(body: str) -> list[str]:
    m = re.search(r'<div[^>]*id="done-banner"[^>]*>.*?</div>\s*</div>', body, re.S)
    return [a.get("href", "") for a in _tags(m.group(0), "a")] if m else []


def test_a_rule_typed_on_the_rules_page_returns_there_and_is_confirmed_once(cairn_env):
    """Scenario "A rule typed on the rules page returns there" — through the forms the pages
    render: the rules page's field, the check it leads to, and that page's add control."""
    with _make_client(cairn_env, _photos(cairn_env, CREATE_SEED)) as client:
        (field,) = _get_forms(_rules_page(client), "/collection/1/rules/new")
        assert set(field["fields"]) == {"pattern"}, "no starting file travels with it"
        checked = _rule_page(client, pattern="clients/acme/")
        own = _cards(checked)["custom"]
        assert own["reach"] == (3, 5, 2, 2)
        assert own["form"]["fields"]["path"] == ""

        before = _snapshot()
        add = own["form"]
        resp = client.post(add["action"], data=add["fields"], follow_redirects=False)
        assert (resp.status_code, resp.headers["location"]) == (303, "/collection/1/rules")
        after_post = _snapshot()
        landed = _rules_page(client)
        assert _snapshot() == after_post == before, "no file, event, run or other column changed"
        assert _patterns() == ["clients/acme/"]

        banner = _done_banner(landed)
        assert banner.startswith("Rule added. It applies from the next scan.")
        assert "2 changed files listed for review match it and are still listed" in banner
        assert "a rule does not accept anything" in banner
        assert "listed here" not in banner, "nothing is listed for review on the rules page"
        assert landed.count('id="done-banner"') == 1
        assert _banner_links(landed) == ["/collection/1/review"]
        assert landed.index('id="done-banner"') < landed.index('id="rule-list"')
        assert [r["pattern"] for r in _rule_rows(landed)] == ["clients/acme/"]

        # Once — here, and on the review page it points to (where the two files are still listed,
        # now named as covered).
        assert _done_banner(_rules_page(client)) is None
        review, r = _review(client)
        assert _done_banner(review) is None
        assert sorted(_covered(review)) == [(1, "clients/acme/")] * 2
        assert r["accept_changes"]["count"] == 4


@pytest.mark.parametrize(
    ("pattern", "expected", "links"),
    [
        (NOTES, "1 changed file listed for review matches it and is still listed",
         ["/collection/1/review"]),
        ("clients/acme/plan.docx", "No changed file listed for review matches it.", []),
    ],
    ids=["one", "none"],
)
def test_the_rules_page_confirmation_states_how_many_listed_changes_match(
    cairn_env, pattern, expected, links
):
    with _make_client(cairn_env, _photos(cairn_env, CREATE_SEED)) as client:
        resp = _post(client, "/collection/1/rules", csrf_token=_csrf(client), pattern=pattern)
        assert (resp.status_code, resp.headers["location"]) == (303, "/collection/1/rules")
        landed = _rules_page(client)

    banner = _done_banner(landed)
    assert banner.startswith("Rule added. It applies from the next scan.")
    assert expected in banner
    # It points at the review page only when there is something there to accept.
    assert _banner_links(landed) == links


@pytest.mark.parametrize(
    "path", [NOTES, None], ids=["from a listed file", "typed on the rules page"]
)
def test_in_a_working_collection_the_confirmation_says_the_rule_changes_nothing(cairn_env, path):
    """"…(or, where the collection's policy already expects edits, that the rule changes nothing
    while that policy stands)" — on whichever page the operator is returned to."""
    seed = _photos(cairn_env, CREATE_SEED, mode="churn")
    with _make_client(cairn_env, seed) as client:
        own = _cards(_rule_page(client, path=path, pattern="clients/acme/*.md"))["custom"]
        # The confirm says so before the click, too.
        assert "This collection is Working, so the rule has no effect" in (
            own["form"]["attrs"]["onsubmit"]
        )
        before = _snapshot()
        add = own["form"]
        resp = client.post(add["action"], data=add["fields"], follow_redirects=False)
        target = "/collection/1/review" if path else "/collection/1/rules"
        assert (resp.status_code, resp.headers["location"]) == (303, target)
        landed = client.get(target).text
        assert _snapshot() == before
        assert _done_banner(client.get(target).text) is None

    banner = _done_banner(landed)
    assert banner.startswith("Rule added.")
    assert "policy is Working" in banner
    assert "the rule changes nothing while that stands" in banner
    assert "applies from the next scan" not in banner
    assert "will not be flagged" not in banner, "nothing about the rule taking effect"
    # What is listed is still stated as listed, and still to be accepted.
    where = "here" if path else "for review"
    assert f"1 changed file listed {where} matches it and is still listed" in banner
    assert "a rule does not accept anything" in banner
    assert _patterns() == ["clients/acme/*.md"]


def test_the_rules_page_neither_shows_nor_uses_up_an_accept_confirmation(cairn_env):
    """The rules page shows the "Rule added" confirmation only. One left by an accept belongs to
    the review page: passing through the rules page must not show it or lose it."""
    seed = _with_rules(_photos(cairn_env, [("a.txt", "modified"), ("b.txt", "missing")]), "x/")
    with _make_client(cairn_env, seed) as client:
        accepts = (("a.txt", "Accepted 1 change."), ("b.txt", "Accepted 1 deletion"))
        for relpath, expected in accepts:
            body, r = _review(client)
            (row,) = [x for x in r["rows"] if x["relpath"] == relpath]
            (accept,) = _row_forms(_row_block(body, row["id"]))
            resp = client.post(accept["action"], data=accept["fields"], follow_redirects=False)
            assert resp.status_code == 303 and "stale" not in resp.headers["location"]

            for url in ("/collection/1/rules", "/collection/1/rules/new?pattern=x/"):
                passing = client.get(url)
                assert passing.status_code == 200
                assert _done_banner(passing.text) is None, url
                assert "Accepted" not in _said(passing.text), url
                assert "cairn_done" not in passing.headers.get("set-cookie", ""), url
            landed = client.get(resp.headers["location"]).text
            assert _done_banner(landed).startswith(expected)

        # A hand-made cookie of either kind gets the same treatment.
        for value in ("changes:3", "deletions:2"):
            resp = client.get("/collection/1/rules", headers={"cookie": f"cairn_done={value}"})
            assert resp.status_code == 200 and _done_banner(resp.text) is None
            assert "cairn_done" not in resp.headers.get("set-cookie", "")


RETURNS = [
    (NOTES, "review"),
    ("not/a/tracked/file.txt", "review"),
    ("", "rules"),
    (None, "rules"),
    ("//zqx.example/x", "rules"),
    ("../zqx", "rules"),
    ("/zqx/abs.txt", "rules"),
    ("zqx\x00b", "rules"),
    ("https://zqx.example/", "rules"),
]


def test_a_new_rule_returns_to_one_of_two_fixed_pages(cairn_env):
    """The submitted starting path only chooses between the review page and the rules page; it is
    never part of the address the operator is sent to."""
    with _make_client(cairn_env, _photos(cairn_env, CREATE_SEED)) as client:
        token = _csrf(client)
        before = _snapshot()
        for i, (path, target) in enumerate(RETURNS):
            data = {"csrf_token": token, "pattern": f"returns-{i}/"}
            if path is not None:
                data["path"] = path
            resp = client.post("/collection/1/rules", data=data, follow_redirects=False)
            assert resp.status_code == 303, path
            assert resp.headers["location"] == f"/collection/1/{target}", path
            assert not any("zqx" in v for v in resp.headers.values()), path
            landed = client.get(resp.headers["location"])
            assert _done_banner(landed.text).startswith("Rule added."), path
        assert _patterns() == [f"returns-{i}/" for i in range(len(RETURNS))]
        assert _snapshot() == before


# =============================================================================================
# L. Which files a pattern matches — on request, and in agreement with the count
# =============================================================================================

SAMPLE_FILES = (
    [(f"logs/f{i:02d}.txt", "ok") for i in range(25)]
    + [
        ("logs/Zeta.txt", "modified"),             # sorts before every lower-case name
        ("logs/sub/deep.txt", "new"),
        ("logs/Aaa.txt", "missing"),               # would be listed first, were it tracked
        ("NOTES.md", "ok"),
        ("Zeta/NOTES.md", "ok"),
        ("alpha/NOTES.md", "modified"),
        ("gone/NOTES.md", "missing"),
        ("only/one.bin", "ok"),
    ]
)
SAMPLE_TRACKED = sorted(p for p, status, *_ in SAMPLE_FILES if status != "missing")


def _expect_sample(pattern: str, tracked: list[str]) -> dict:
    """What a list for ``pattern`` must say, derived independently: the first twenty matching
    tracked paths in path order, and how many more there are."""
    hits = sorted(_matching(pattern, tracked))
    n = len(hits)
    if n == 1:
        summary = "Show the file it matches"
    elif n <= 20:
        summary = f"Show the {n} files it matches"
    else:
        summary = f"Show the first 20 of {n:,} files it matches"
    return {"summary": summary, "paths": hits[:20], "more": max(n - 20, 0)}


def _same_sample(got: dict | None, pattern: str, tracked: list[str]) -> None:
    want = _expect_sample(pattern, tracked)
    if not want["paths"]:
        assert got is None, "a pattern that matches nothing lists nothing"
        return
    assert got is not None, pattern
    assert {k: got[k] for k in want} == want, pattern


def test_the_rule_page_lists_which_files_each_pattern_matches(cairn_env):
    """"…how many of the collection's tracked files it matches — and, on request, which (the
    first of them, with how many more there are)"."""
    with _make_client(cairn_env, _photos(cairn_env, SAMPLE_FILES)) as client:
        pages = {
            typed: _rule_page(client, path="logs/f03.txt", pattern=typed)
            for typed in ("logs/", "NOTES.md", "only/one.bin", "nothing/", "logs/f??.txt", "*.txt")
        }
        assert _rules_json() == "[]"

    for typed, body in pages.items():
        cards = _cards(body)
        assert set(cards) >= {"custom", "file", "name"}
        for card in cards.values():
            pattern = card["pattern"]
            got = _sample(card["html"])
            _same_sample(got, pattern, SAMPLE_TRACKED)
            if got is not None:
                # The list and the count are the same statement.
                assert len(got["paths"]) + got["more"] == card["reach"][0], pattern
        # On request: the lists are there, closed.
        assert "<details" in body and not re.search(r"<details[^>]*\bopen\b", body)

    logs = _sample(_cards(pages["logs/"])["custom"]["html"])
    assert logs["summary"] == "Show the first 20 of 27 files it matches"
    assert logs["paths"][:3] == ["logs/Zeta.txt", "logs/f00.txt", "logs/f01.txt"]
    assert logs["paths"][-1] == "logs/f18.txt" and logs["more"] == 7
    notes = _sample(_cards(pages["NOTES.md"])["custom"]["html"])
    assert notes == {**notes, "summary": "Show the 3 files it matches", "more": 0,
                     "paths": ["NOTES.md", "Zeta/NOTES.md", "alpha/NOTES.md"]}
    one = _sample(_cards(pages["only/one.bin"])["custom"]["html"])
    assert (one["summary"], one["paths"]) == ("Show the file it matches", ["only/one.bin"])
    # A file that is missing is not tracked: it is in no list, whatever matches its path.
    for body in pages.values():
        assert "logs/Aaa.txt" not in body and "gone/NOTES.md" not in body


@pytest.mark.parametrize("n", [1, 2, 20, 21, 45])
def test_the_list_stops_at_twenty_and_says_how_many_more(cairn_env, n):
    files = [(f"d/f{i:03d}.txt", "ok") for i in range(n)] + [("other/bystander.txt", "ok")]
    files += [(f"pad/p{i:03d}.txt", "ok") for i in range(n)]  # keeps `d/` under half
    tracked = sorted(p for p, _s in files)
    seed = _with_rules(_photos(cairn_env, files), "d/")
    with _make_client(cairn_env, seed) as client:
        own = _cards(_rule_page(client, pattern="d/*.txt"))["custom"]
        (row,) = _rule_rows(_rules_page(client))

    for got, pattern in ((_sample(own["html"]), "d/*.txt"), (_sample(row["html"]), "d/")):
        _same_sample(got, pattern, tracked)
        assert len(got["paths"]) == min(n, 20) and got["more"] == max(n - 20, 0)
        assert got["paths"] == [f"d/f{i:03d}.txt" for i in range(min(n, 20))]
    assert own["reach"][:2] == (n, 2 * n + 1)
    assert f"matches {n} of {2 * n + 1} tracked files" in row["meta"]


def test_the_rules_page_lists_which_files_each_rule_matches(cairn_env):
    seed = _with_rules(_photos(cairn_env, SAMPLE_FILES), "logs/", "NOTES.md", "nothing/", "*.bin")
    with _make_client(cairn_env, seed) as client:
        body = _rules_page(client)
        _write_rules_json([_rule("logs/", effect="other")])
        ignored = _rules_page(client)

    rows = _rule_rows(body)
    assert [r["pattern"] for r in rows] == ["logs/", "NOTES.md", "nothing/", "*.bin"]
    for row in rows:
        _same_sample(_sample(row["html"]), row["pattern"], SAMPLE_TRACKED)
    assert "matches 27 of 31 tracked files" in rows[0]["meta"]
    assert _sample(rows[0]["html"])["more"] == 7
    assert "Currently matches no files" in rows[2]["meta"] and _sample(rows[2]["html"]) is None
    assert not re.search(r"<details[^>]*\bopen\b", body)
    # An entry that is ignored matches nothing, and lists nothing.
    (row,) = _rule_rows(ignored)
    assert "ignored" in row["meta"] and _sample(row["html"]) is None


HOSTILE_NAMES = [
    "d/<img src=zqx onerror=alert(1)>.txt",
    "d/</li></ul></details><script>alert('zqx')</script>",
    "d/a&amp;b &lt;i&gt;.txt",
    "d/quote \" and ' .txt",
    "d/line\nbreak  two spaces.txt",
    "d/{{ 7*7 }}.txt",
]


def test_a_hostile_file_name_is_only_text_in_the_list_of_matching_files(cairn_env):
    files = [(p, "ok") for p in HOSTILE_NAMES] + [("pad/a", "ok"), ("pad/b", "ok")]
    tracked = sorted(p for p, _s in files)
    seed = _with_rules(_photos(cairn_env, files), "d/")
    with _make_client(cairn_env, seed) as client:
        rule_page = _rule_page(client, pattern="d/**")
        rules_page = _rules_page(client)

    lists = [
        (_sample(_cards(rule_page)["custom"]["html"]), "d/**"),
        (_sample(_rule_rows(rules_page)[0]["html"]), "d/"),
    ]
    for got, pattern in lists:
        # Every name comes back exactly — which it could not if any of it had been read as markup
        # or if an entity in a name had been passed through undoubled.
        _same_sample(got, pattern, tracked)
        assert sorted(got["paths"]) == sorted(_matching(pattern, tracked))
        assert len(got["paths"]) == len(HOSTILE_NAMES)
    for page in (rule_page, rules_page):
        assert "<img src=zqx" not in page and "<script>alert('zqx')" not in page
        assert not [i for i in _tags(page, "img") if i.get("onerror")]
        assert "d/49.txt" not in page


# =============================================================================================
# M. A pattern that matches nothing says so first
# =============================================================================================


def test_a_typed_pattern_that_matches_nothing_says_so_before_offering_to_add_it(cairn_env):
    """Scenario "A pattern that matches nothing says so first"."""
    with _make_client(cairn_env, _photos(cairn_env, LISTED_FILES)) as client:
        body = _rule_page(client, pattern="nothing-here/*.tmp")
        matching = _rule_page(client, pattern="exports/")
        own = _cards(body)["custom"]
        assert own["reach"] == (0, 7, 0, 0)
        assert _sample(own["html"]) is None

        warning = _nomatch(body)
        said = _text(warning)
        assert "It matches no file in this collection today" in said
        assert "it would do nothing" in said
        # What usually went wrong, in general terms — and no guess at a folder that is not there.
        assert "Check the spelling and the capitals" in said
        assert "a folder needs a trailing slash" in said
        assert _typed_links(warning) == []
        # Said BEFORE the control, and the control is not the page's primary action.
        assert warning.index("matches no file") < warning.index("<form")
        assert body.index('id="rule-nomatch"') < body.index("Add it anyway")
        # ...the only add control the card has.
        (anyway,) = _forms(body[body.index('id="rule-custom"'):body.index('id="rule-own"')])
        assert own["form"] == anyway
        assert anyway["button"] == "Add it anyway"
        assert "btn--primary" not in anyway["button_class"].split()
        assert "Add this rule" not in own["text"]
        assert "although it matches no file today" in anyway["attrs"]["onsubmit"]
        assert "this.dataset.pattern" in anyway["attrs"]["onsubmit"]
        assert anyway["attrs"]["data-pattern"] == "nothing-here/*.tmp"

        # A pattern that does match is offered the ordinary way, with no such warning.
        ok = _cards(matching)["custom"]
        assert _nomatch(matching) is None
        assert ok["form"]["button"] == "Add this rule"
        assert "btn--primary" in ok["form"]["button_class"].split()

        # "Anyway" stores exactly what was typed, and nothing else changes.
        before = _snapshot()
        resp = client.post(anyway["action"], data=anyway["fields"], follow_redirects=False)
        assert (resp.status_code, resp.headers["location"]) == (303, "/collection/1/rules")
        assert _snapshot() == before
        assert _patterns() == ["nothing-here/*.tmp"]
        landed = _rules_page(client)
        assert "No changed file listed for review matches it." in _done_banner(landed)
        assert "Currently matches no files" in _rule_rows(landed)[0]["meta"]


def _typed_links(fragment: str) -> list[dict[str, str]]:
    """Links in ``fragment`` that open the rule page to check a pattern: ``{pattern, path}``."""
    out = []
    for a in _tags(fragment, "a"):
        parts = urlsplit(a.get("href", ""))
        if not RULE_PAGE.match(parts.path):
            continue
        query = parse_qs(parts.query, keep_blank_values=True, strict_parsing=True)
        assert set(query) <= {"pattern", "path"} and "pattern" in query, a["href"]
        out.append({
            "href": a["href"],
            "pattern": query["pattern"][0],
            "path": query.get("path", [None])[0],
        })
    return out


# (typed, the folder variant the page should offer, how many tracked files that matches)
FOLDER_HINTS = [
    ("exports", "exports/", 2),             # two tracked files; the third is missing
    ("src", "src/", 1),
    ("clients/acme", "clients/acme/", 1),
    ("clients/*", "clients/*/", 2),
    ("  exports  ", "exports/", 2),
    ("readme", None, 0),                    # readme/ matches nothing either
    ("archive", None, 0),                   # the only file beneath archive/ is missing
    ("Exports", None, 0),                   # case matters
    ("nothing-here/", None, 0),             # already a folder
    ("exports/2026/q1", None, 0),
]


@pytest.mark.parametrize(
    ("typed", "folder", "count"), FOLDER_HINTS, ids=[repr(c[0]) for c in FOLDER_HINTS]
)
def test_the_trailing_slash_is_suggested_only_when_the_folder_matches(
    cairn_env, typed, folder, count
):
    with _make_client(cairn_env, _photos(cairn_env, LISTED_FILES)) as client:
        body = _rule_page(client, path="readme.txt", pattern=typed)
        warning = _nomatch(body)
        assert warning is not None and _cards(body)["custom"]["reach"][0] == 0
        said = _text(warning)
        assert "It matches no file in this collection today" in said
        links = _typed_links(warning)
        if folder is None:
            assert links == []
            assert "With a trailing slash" not in said
            assert "Check the spelling" in said
        else:
            (link,) = links
            # The suggestion is a check, not an add: it opens the rule page for that pattern,
            # still starting from the same file.
            assert (link["pattern"], link["path"]) == (folder, "readme.txt")
            assert f"With a trailing slash it is a folder: {folder} matches {count} file" in said
            opened = _get(client, link["href"], "rule_new.html")[0]
            suggested = _cards(opened)["custom"]
            assert suggested["pattern"] == folder
            assert suggested["reach"][0] == count == len(_sample(suggested["html"])["paths"])
            assert _nomatch(opened) is None
            assert _starting_from(opened) == "readme.txt"
        # Either way "anyway" stores what was typed (without the spaces around it) — never the
        # suggestion — and returns to the review page the starting file came from.
        anyway = _cards(body)["custom"]["form"]
        assert anyway["button"] == "Add it anyway"
        before = _snapshot()
        resp = client.post(anyway["action"], data=anyway["fields"], follow_redirects=False)
        assert (resp.status_code, resp.headers["location"]) == (303, "/collection/1/review")
        assert _patterns() == [typed.strip(" ")]
        assert _snapshot() == before


def test_the_trailing_slash_link_survives_reserved_characters(cairn_env):
    folder = "a&b #1 'q' \"d\" +%41 é<i>"
    files = [(f"{folder}/one.txt", "ok"), (f"{folder}/sub/two.txt", "modified"), ("x.txt", "ok"),
             ("pad/1", "ok"), ("pad/2", "ok")]
    with _make_client(cairn_env, _photos(cairn_env, files)) as client:
        for start in (None, f"{folder}/one.txt"):
            body = _rule_page(client, path=start, pattern=folder)
            warning = _nomatch(body)
            (link,) = _typed_links(warning)
            assert (link["pattern"], link["path"]) == (folder + "/", start)
            assert "<i>" not in warning
            opened = _get(client, link["href"], "rule_new.html")[0]
            assert _cards(opened)["custom"]["pattern"] == folder + "/"
            assert _cards(opened)["custom"]["reach"] == (2, 5, 1, 1)
            assert _starting_from(opened) == start
        assert _rules_json() == "[]"


def test_a_proposal_for_a_file_that_is_not_tracked_says_it_matches_nothing(cairn_env):
    """A starting path can be typed into the address, or outlive its file: the proposals derived
    from it then match nothing, and each says so beside its add control."""
    with _make_client(cairn_env, _photos(cairn_env, LISTED_FILES)) as client:
        cards = _cards(_rule_page(client, path="not/tracked/file.bin"))

    assert set(cards) == {"file", "folder", "name"}
    for card in cards.values():
        assert card["reach"] == (0, 7, 0, 0)
        assert "It matches no file in this collection today" in card["text"]
        assert _sample(card["html"]) is None


# =============================================================================================
# N. A pattern that takes in half the collection or more says how much
# =============================================================================================

SHARES = [
    (5, 7, "71.4%"),
    (7, 7, "all"),
    (1, 1, "all"),
    (1, 2, "50.0%"),
    (2, 3, "66.7%"),
    (6, 7, "85.7%"),
    (3, 7, None),      # 42.9% — under half: no warning
    (1, 3, None),
    (0, 4, None),
]


@pytest.mark.parametrize(
    ("hit", "total", "expected"), SHARES, ids=[f"{h}/{t}" for h, t, _ in SHARES]
)
def test_a_broad_pattern_states_its_share_of_the_collection(cairn_env, hit, total, expected):
    files = [(f"hit/h{i}.txt", "ok") for i in range(hit)]
    files += [(f"miss/m{i}.txt", "ok") for i in range(total - hit)]
    files += [("hit/gone.txt", "missing"), ("miss/gone.txt", "missing")]  # never in either figure
    with _make_client(cairn_env, _photos(cairn_env, files)) as client:
        typed = _cards(_rule_page(client, pattern="hit/"))["custom"]
        proposed = _cards(_rule_page(client, path="hit/h0.txt"))["folder"]

    for card in (typed, proposed):
        assert card["reach"][:2] == (hit, total)
        assert _share(card["html"]) == expected
        if expected is None:
            assert "of this collection" not in card["text"]
            continue
        # It points at the policy — the tool for "most of the collection" — with a link to it.
        assert "in effect the Working policy" in card["text"]
        warn = card["html"][card["html"].index("That is <strong>"):]
        assert "/collection/1/edit" in [a.get("href") for a in _tags(warn.split("</div>")[0], "a")]


def test_a_share_just_under_the_whole_collection_is_not_rounded_up_to_all_of_it(cairn_env):
    """1,999 of 2,000 files is not the whole collection, and the one file it leaves out is the
    one that is still flagged. The share must not read as 100%."""
    files = [(f"hit/h{i:04d}.txt", "ok") for i in range(1999)] + [("kept/locked.pdf", "ok")]
    with _make_client(cairn_env, _photos(cairn_env, files)) as client:
        card = _cards(_rule_page(client, pattern="hit/"))["custom"]

    assert card["reach"][:2] == (1999, 2000)
    share = _share(card["html"])
    assert share is not None and share != "all"
    assert not share.startswith("100"), share


# =============================================================================================
# O. The mass-change ceiling is described as what it is
# =============================================================================================


def _ceiling(monkeypatch, *, percent: int, min_files: int) -> None:
    from src.config import get_settings

    monkeypatch.setenv("CAIRN_MASS_CHANGE_PERCENT", str(percent))
    monkeypatch.setenv("CAIRN_MASS_CHANGE_MIN_FILES", str(min_files))
    get_settings.cache_clear()


def _facts(body: str) -> str:
    return _text(_content(body)[_content(body).index('id="rule-facts"'):])


@pytest.mark.parametrize(
    ("percent", "min_files", "thresholds"),
    [
        (None, None, "200 files and 25% of the collection"),      # the defaults
        (10, 50, "50 files and 10% of the collection"),
        (40, 1000, "1,000 files and 40% of the collection"),
        (100, 1, "1 files and 100% of the collection"),
    ],
    ids=["defaults", "50 and 10%", "1,000 and 40%", "1 and 100%"],
)
def test_the_ceiling_is_stated_with_the_thresholds_in_force(
    cairn_env, monkeypatch, percent, min_files, thresholds
):
    """"…an unusually large change in one scan — stated as what it is, a ceiling across the
    collection with its two thresholds, not a check on any one file"."""
    from src.config import get_settings

    seed = _with_rules(_photos(cairn_env, [(NOTES, "modified")]), "NOTES.md")
    try:
        if percent is not None:
            _ceiling(monkeypatch, percent=percent, min_files=min_files)
        with _make_client(cairn_env, seed) as client:
            pages = [_rule_page(client, path=NOTES), _rules_page(client)]
    finally:
        get_settings.cache_clear()

    for body in pages:
        facts = _facts(body)
        assert "An unusually large change in one scan" in facts
        assert "across the collection, not to one file" in facts
        assert f"reach both {thresholds}" in facts
        assert "all of them are flagged instead" in facts
        # ...and what that leaves: one file's change is not flagged for being large.
        assert "however large the edit — is not flagged" in facts


def test_a_ceiling_that_is_switched_off_is_not_described(cairn_env, monkeypatch):
    from src.config import get_settings

    seed = _with_rules(_photos(cairn_env, [(NOTES, "modified")]), "NOTES.md")
    try:
        _ceiling(monkeypatch, percent=0, min_files=200)
        with _make_client(cairn_env, seed) as client:
            pages = [_rule_page(client, path=NOTES), _rules_page(client)]
            confirm = _cards(pages[0])["folder"]["form"]["attrs"]["onsubmit"]
    finally:
        get_settings.cache_clear()

    for body in pages:
        facts = _facts(body)
        assert "unusually large" not in facts and "200 files" not in facts
        assert "0%" not in facts
        # The rest of what a rule never covers is still said.
        for kept in ("Deletions.", "Possible corruption", "comes back different",
                     "however large the edit — is not flagged"):
            assert kept in facts
    assert "unusually large" not in confirm


def test_the_ceiling_is_not_said_to_be_out_of_reach_of_a_small_rule(cairn_env, monkeypatch):
    """The spec asks for the ceiling to be stated "as what it is, a ceiling across the
    collection". A sentence that makes it a property of one rule's size is the opposite claim, and
    the scanner disproves it here before the pages are read."""
    from src.config import get_settings
    from tests.test_scan_safety_nets import _edit, _scan

    root = cairn_env / "two-rules"
    root.mkdir()
    names = ["a0.txt", "a1.txt", "b0.txt", "b1.txt"]
    for name in names:
        (root / name).write_text(f"first {name}")
    outcome = {}

    async def seed():
        from src.database import get_sessionmaker
        from src.models.db import Collection
        from src.services import rules

        cid = await seed_collection(root, ots_mode="none")
        await _scan(cid)
        async with get_sessionmaker()() as s:
            c = await s.get(Collection, cid)
            await rules.add_rule(s, c, "a?.txt")
            await rules.add_rule(s, c, "b?.txt")
        for name in names:
            _edit(root / name, f"second {name}")
        outcome["summary"] = await _scan(cid)

    try:
        _ceiling(monkeypatch, percent=50, min_files=3)
        with _make_client(cairn_env, seed) as client:
            pages = [_rule_page(client, path="a0.txt"), _rules_page(client)]
            reach = {r["pattern"]: r["meta"] for r in _rule_rows(pages[1])}
    finally:
        get_settings.cache_clear()

    # The premise, checked without `assert` so that only the claim below can be the expected
    # failure: two rules of two files each, and one scan that hit the ceiling of three.
    summary = outcome["summary"]
    if summary.mass_change != 4 or summary.modified != 4:
        raise RuntimeError(f"premise not met: {summary}")
    if not all("matches 2 of 4 tracked files" in meta for meta in reach.values()):
        raise RuntimeError(f"premise not met: {reach}")

    for body in pages:
        facts = _facts(body)
        if "reach both 3 files and 50% of the collection" not in facts:
            raise RuntimeError("the pages do not state the thresholds in force")
        assert "never reaches it" not in facts


# =============================================================================================
# P. The pages around a rule: titles, ways back, the covered notice, the links to the list
# =============================================================================================


def _back_link(body: str) -> tuple[str, str]:
    (a,) = [a for a in _tags(_content(body), "a") if "back-link" in a.get("class", "").split()]
    m = re.search(r'<a class="back-link"[^>]*>(.*?)</a>', body, re.S)
    return a["href"], _text(m.group(1))


def test_the_rule_page_is_titled_for_what_a_rule_does_and_leads_back_to_where_it_came_from(
    cairn_env,
):
    with _make_client(cairn_env, _photos(cairn_env, [(NOTES, "modified")])) as client:
        from_file = _rule_page(client, path=NOTES)
        typed = _rule_page(client, pattern="NOTES.md")
        bare = _rule_page(client)
        illegal = _rule_page(client, path="../x")

    for body in (from_file, typed, bare, illegal):
        (h1,) = re.findall(r"<h1>(.*?)</h1>", _content(body), re.S)
        assert _text(h1) == "New rule: don't flag ordinary changes"
    assert _back_link(from_file) == ("/collection/1/review", "What changed in photos")
    for body in (typed, bare, illegal):
        assert _back_link(body) == ("/collection/1/rules", "Rules for photos")


def test_the_rule_page_says_when_a_rule_already_covers_the_starting_file(cairn_env):
    """Section (g): the page that would create a rule for a file says, before any pattern, that
    one of the collection's rules already covers it — and which."""
    seed = _with_rules(
        _photos(cairn_env, [(NOTES, "modified"), ("other/x.txt", "modified")]),
        "clients/*/NOTES.md", "NOTES.md",
    )

    def notice(body: str) -> str | None:
        m = re.search(r'id="rule-covered".*?</div>\s*</div>', body, re.S)
        return _text(m.group(0).split(">", 1)[1]) if m else None

    with _make_client(cairn_env, seed) as client:
        covered = _rule_page(client, path=NOTES)
        other = _rule_page(client, path="other/x.txt")
        no_start = _rule_page(client, pattern="NOTES.md")
        _write_rules_json([_rule("NOTES.md", effect="other")])
        inert = _rule_page(client, path=NOTES)

    said = notice(covered)
    assert said is not None
    # The first rule in the list that matches — the one the review row names, too.
    assert "This file is already covered by the rule clients/*/NOTES.md." in said
    assert "it only needs its change accepted" in said
    assert "Another rule for it would add nothing." in said
    assert covered.index('id="rule-covered"') < covered.index('id="rule-proposal-file"')
    # Its name-in-any-folder proposal is the second rule: marked as existing, not offered again.
    assert _cards(covered)["name"]["form"] is None
    for body in (other, no_start, inert):
        assert notice(body) is None and "already covered" not in _said(body)


@pytest.mark.parametrize(
    ("patterns", "label"),
    [((), "Rules"), (("NOTES.md",), "Rules (1)"), (("NOTES.md", "exports/"), "Rules (2)")],
    ids=["none", "one", "two"],
)
def test_the_collection_page_and_the_review_toolbar_link_to_the_rules_with_their_count(
    cairn_env, patterns, label
):
    seed = _with_rules(_photos(cairn_env, LISTED_FILES), *patterns)
    with _make_client(cairn_env, seed) as client:
        detail = client.get("/collection/1").text
        review, _r = _review(client)

    for page, element in ((detail, "collection-rules-btn"), (review, "review-rules-link")):
        (a,) = [a for a in _tags(page, "a") if a.get("id") == element]
        assert a["href"] == "/collection/1/rules"
        m = re.search(rf'<a[^>]*id="{element}"[^>]*>(.*?)</a>', page, re.S)
        assert _text(m.group(1)) == label


def test_the_learn_page_describes_a_rule_without_the_ceiling(cairn_env):
    with _make_client(cairn_env, _photos(cairn_env, [(NOTES, "modified")])) as client:
        learn = _text(client.get("/learn").text)

    assert "Don’t flag future changes…" in learn
    assert (
        "ordinary changes to them are no longer flagged, while their deletion, possible "
        "corruption and a file that comes back different still are."
    ) in learn
    assert "unusually large changes still are" not in learn
