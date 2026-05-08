# Severity Levels

Assign exactly one severity per finding. Use the highest level that fits.

## Critical

- Definition: Will cause data loss, security breach, or production outage if
  merged.
- Triggers (any one):
  - Hardcoded secret in changed file (`CS-SEC-002`).
  - Missing authn/authz on a new externally reachable surface
    (`CS-SEC-001`).
  - SQL injection vector or unescaped HTML/JS sink (`CS-SEC-003`).
  - Migration that destroys data without backfill (`CS-API-005`).
  - Multi-step write without atomicity in a code path that corrupts state
    on partial failure (`CS-API-003`).
- Verdict implication: `request changes`.

## Major

- Definition: Will cause incorrect behaviour, severe maintainability
  damage, or operational pain. Does not breach security.
- Triggers (any one):
  - Swallowed error on a path that must propagate (`CS-CORE-006`).
  - N+1 IO inside a hot path (`CS-API-004`).
  - Resource leak on an error path (`CS-RES-001`).
  - Cross-layer leak: handler talks to DB driver, or use-case crafts an
    HTTP response (`CS-ARCH-001`).
  - External error response leaks internals (`CS-SEC-004`).
- Verdict implication: `request changes`.

## Minor

- Definition: Real issue but not blocking. Should be addressed before merge
  or in a follow-up.
- Triggers (any one):
  - Missing context on errors crossing a layer (`CS-ERR-002`).
  - Deep nesting / long inline block (`CS-FUNC-002`).
  - Domain logic placed under `utils/` (`CS-MOD-001`).
  - Magic literal in business logic (`CS-NAMING-003`).
  - Premature single-use abstraction (`CS-CORE-008`).
- Verdict implication: `comment` (block only when several Minors compound
  in the same module).

## Suggestion

- Definition: Style, taste, or future-proofing. Strictly optional.
- Triggers (any one):
  - Naming nitpicks where the existing module is consistent
    (`CS-NAMING-001`).
  - Composition-over-inheritance preference where inheritance is
    idiomatic (`CS-CORE-005`).
  - Comment phrasing.
- Verdict implication: `comment`.

## Confidence

Pair every severity with a confidence:

- **High** — the trigger is unambiguous in the diff.
- **Medium** — the trigger fires but depends on context not visible in the
  diff (e.g. caller behaviour).
- **Low** — you suspect a problem but cannot prove it from the diff. Demote
  to Suggestion or drop the finding.

Never post a Critical or Major finding at Low confidence. Demote it.

## Verdict aggregation

Compute the MR verdict at the end of Phase F:

| Findings present                            | Verdict          |
| ------------------------------------------- | ---------------- |
| Any Critical                                | request changes  |
| No Critical, any Major                      | request changes  |
| No Critical, no Major, any Minor/Suggestion | comment          |
| Zero findings                               | approve          |
