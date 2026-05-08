## CS-CORE — Core engineering

### [CS-CORE-001] Prefer readability over cleverness
- **Rule:** Code SHOULD be optimized for the next reader, not for line count
  or perceived cleverness.
- **Why:** Dense code hides bugs and slows review and maintenance.
- **Trigger:** A changed line uses a dense one-liner or non-idiomatic trick
  where a straightforward version would be clearer.

### [CS-CORE-002] Comments explain why, not what
- **Rule:** Comments SHOULD explain rationale, invariants, tradeoffs, or
  constraints. Comments SHOULD NOT restate the obvious behavior of the code.
- **Why:** Restating comments rot quickly; rationale comments stay useful.
- **Trigger:** A changed comment paraphrases the code, or a non-obvious
  workaround is introduced without explanation.

### [CS-CORE-003] Explicit control flow
- **Rule:** Control flow SHOULD be explicit. Side effects MUST NOT be hidden
  inside getters, constructors, or coercion.
- **Why:** Hidden effects break local reasoning and make failures surprising.
- **Trigger:** A simple-looking access or construction performs IO, mutates
  shared state, or hides failure-prone work.

### [CS-CORE-004] Cohesive modules
- **Rule:** Modules, classes, and functions SHOULD have one clear purpose.
- **Why:** Mixed responsibilities create unclear ownership and fragile change
  boundaries.
- **Trigger:** A changed file or symbol takes on an unrelated second role.

### [CS-CORE-005] Composition over inheritance
- **Rule:** Prefer composition over inheritance unless the framework or
  language idiom clearly expects inheritance.
- **Why:** Composition keeps dependencies explicit and avoids fragile class
  hierarchies.
- **Trigger:** New inheritance is added only to share helpers or avoid a
  small amount of duplication.

### [CS-CORE-006] Never silently swallow errors
- **Rule:** Code MUST NOT discard errors without logging, wrapping, or
  intentional documented suppression.
- **Why:** Silent failure breaks diagnosis and usually hides real defects.
- **Trigger:** A changed path catches or ignores an error with no further
  handling or explanation.

### [CS-CORE-008] Avoid premature abstraction
- **Rule:** Interfaces, abstract bases, factories, and heavy generics SHOULD
  NOT be introduced for a single concrete implementation or call site.
- **Why:** Indirection without a real substitution need makes the codebase
  harder to change.
- **Trigger:** A new abstraction has one implementation and no real boundary
  need in the changed code.

## CS-NAMING — Naming and literals

### [CS-NAMING-001] Descriptive names
- **Rule:** Identifiers MUST describe their role. Single-letter names are
  allowed only for small local counters or well-known math variables.
- **Why:** Names are part of the interface readers use to understand code.
- **Trigger:** A new variable, function, type, or file uses a vague name
  like `data`, `tmp`, `obj`, or `helper` at a meaningful scope.

### [CS-NAMING-002] Verbs for functions
- **Rule:** Function names SHOULD start with an action verb.
- **Why:** Verb-based names communicate behavior faster than noun-shaped
  helpers.
- **Trigger:** A new function is named like a noun or a boolean-returning
  helper lacks an `is`/`has`/`can`/`should` prefix.

### [CS-NAMING-003] No magic literals
- **Rule:** Non-obvious domain values SHOULD be named constants or domain
  types.
- **Why:** Named values make invariants explicit and reduce accidental drift.
- **Trigger:** A changed line introduces a literal whose meaning is not clear
  from local context.

## CS-FUNC — Function design

### [CS-FUNC-001] One responsibility per function
- **Rule:** A function SHOULD do one thing.
- **Why:** Single-purpose functions are easier to reason about and test.
- **Trigger:** A changed function mixes validation, IO, formatting, and
  orchestration in one path.

### [CS-FUNC-002] Extract complex inline logic
- **Rule:** Complex inline logic SHOULD be extracted to named helpers when it
  improves readability or testability.
- **Why:** Named helpers make intent and failure modes easier to inspect.
- **Trigger:** A changed block adds deep nesting or a long inline branch that
  obscures the main path.
