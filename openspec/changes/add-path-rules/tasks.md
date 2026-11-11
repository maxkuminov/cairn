## 1. Rules module and storage

- [ ] 1.1 Migration `0014`: `collections.rules_json` TEXT NOT NULL DEFAULT '[]'; ORM column
- [ ] 1.2 `src/services/rules.py`: `validate_pattern`, `compile`/matcher (`effect_for(relpath)`), `load(collection)`, `add_rule`, `remove_rule`; the 100-rule and duplicate limits
- [ ] 1.3 `tests/test_rules.py`: every grammar scenario, every refusal, first-match-wins; `tests/test_migration_0014.py`

## 2. Scanner

- [ ] 2.1 Hash-compare branch: a rule makes an ordinary change quiet in `worm`; corruption, return and the unresolved-change rule unchanged and first
- [ ] 2.2 Missing sweep: quiet removal under a comes-and-goes rule (after move reconciliation), by the statements `accept_files` uses; the quiet-removal ceiling
- [ ] 2.3 Mass-change ceiling counts rule-covered quiet changes, in `worm` too
- [ ] 2.4 `rules.apply_to_open_issues` (D4 predicate; via `accept_files`); `rules.preview`
- [ ] 2.5 `tests/test_scan_rules.py`: one test per scenario of both scanner requirements and the alerting scenarios

## 3. Panel

- [ ] 3.1 Routes: `GET /collection/{id}/rules/new`, `POST /collection/{id}/rules`, `GET /collection/{id}/rules`, `POST /collection/{id}/rules/delete` — owner-scoped, CSRF, refused under an operation in flight
- [ ] 3.2 Templates `rule_new.html`, `collection_rules.html`; "Always expect this…" on review rows and group headers; rule count on the collection page; one sentence in the review guidance and the learn page
- [ ] 3.3 One-shot confirmation after creating a rule
- [ ] 3.4 `tests/test_rules_panel.py`: every web-panel scenario, plus hostile `path` / pattern values reflected safely

## 4. Verify and document

- [ ] 4.1 Full suite; `openspec validate add-path-rules --strict`
- [ ] 4.2 Sandbox on a copy of production data: preview numbers for real noisy patterns; create a rule; screenshots
- [ ] 4.3 `README.md`, `DESIGN.md` §3 / §5, `CLAUDE.md`
