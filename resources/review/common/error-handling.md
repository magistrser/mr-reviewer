## CS-ERR — Error handling

### [CS-ERR-001] Structured, idiomatic errors
- **Rule:** Use the language or framework's idiomatic error pattern
  consistently within a module.
- **Why:** Mixed error styles make control flow and recovery rules harder to
  follow.
- **Trigger:** A changed file mixes incompatible error-handling styles at the
  same layer.

### [CS-ERR-002] Errors carry context
- **Rule:** Errors crossing a layer boundary MUST include enough context to
  identify the failed operation without leaking secrets.
- **Why:** Boundary crossings are where debugging context is most often lost.
- **Trigger:** A changed boundary returns or rethrows a bare error with no
  extra operation context.

### [CS-ERR-003] No exceptions for normal control flow
- **Rule:** Exceptions MUST NOT be used as ordinary control flow unless that
  is the platform convention.
- **Why:** Error channels used as branching logic hide expected behavior in
  failure semantics.
- **Trigger:** A changed path uses exceptions where a normal optional or
  result-based branch would fit the local style.

## CS-RES — Resource lifecycle

### [CS-RES-001] Release every acquired resource
- **Rule:** Files, sockets, connections, locks, and streams MUST be released
  with the language's safe lifecycle pattern.
- **Why:** Resource leaks tend to appear only on edge paths and are expensive
  to debug in production.
- **Trigger:** A changed path acquires a resource without a guaranteed close,
  release, or rollback on every exit path.
