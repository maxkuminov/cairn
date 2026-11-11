## 1. Rules module and storage

- [x] 1.1 Migration `0014`: `collections.rules_json` TEXT NOT NULL DEFAULT '[]'; ORM column
- [x] 1.2 `src/services/rules.py`: `validate_pattern`, compiled matcher (`matches(relpath)`), `load(collection)`, `add_rule`, `remove_rule` (by pattern), limits; `reach(session, collection, pattern)`
- [x] 1.3 `tests/test_rules.py`: every grammar scenario, every refusal, limits; `tests/test_migration_0014.py`

## 2. Scanner

- [x] 2.1 Hash-compare branch: a rule makes an ordinary change quiet in `worm`, through the same path a `churn` re-baseline takes; the return check, the corruption check and the unresolved-change helper unchanged and first; rules compiled once at the start of the pass
- [x] 2.2 Mass-change ceiling counts every quiet re-baseline of the pass, in either mode
- [x] 2.3 `tests/test_scan_rules.py`: one test per scenario of the scanner requirement, the modified mass-change scenario, and the alerting scenarios

## 3. Panel

- [x] 3.1 Routes: `GET /collection/{id}/rules/new`, `POST /collection/{id}/rules`, `GET /collection/{id}/rules`, `POST /collection/{id}/rules/delete` — owner-scoped, CSRF on the POSTs
- [x] 3.2 Templates `rule_new.html`, `collection_rules.html`; the link on changed rows; rule count on the collection page; one sentence in the review guidance and the learn page
- [x] 3.3 One-shot confirmation after creating a rule
- [x] 3.4 `tests/test_rules_panel.py`: every web-panel scenario, plus hostile `path` / pattern values

## 4. Verify and document

- [x] 4.1 Full suite; `openspec validate add-path-rules --strict`
- [x] 4.2 Sandbox on a copy of production data: reach numbers for real noisy patterns; create a rule; screenshots
- [x] 4.3 `README.md`, `DESIGN.md` §5, `CLAUDE.md`
