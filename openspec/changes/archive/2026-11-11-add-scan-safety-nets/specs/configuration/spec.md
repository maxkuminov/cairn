## ADDED Requirements

### Requirement: The mass-change ceiling is configurable

The configuration SHALL expose two settings governing when a scan stops absorbing changes in a
`churn` collection: the minimum number of changed files in one pass (`CAIRN_MASS_CHANGE_MIN_FILES`,
default 200) and the share of the collection's tracked files, in percent
(`CAIRN_MASS_CHANGE_PERCENT`, default 25). Both conditions SHALL have to hold for the rule to
apply, and a share of zero SHALL disable it. A value that is negative or, for the share, above 100
SHALL be rejected at load like any other invalid setting.

#### Scenario: Defaults

- **WHEN** neither setting is supplied
- **THEN** the minimum SHALL be 200 files and the share 25 percent

#### Scenario: Disabled

- **WHEN** the share is set to zero
- **THEN** the mass-change rule SHALL never apply
