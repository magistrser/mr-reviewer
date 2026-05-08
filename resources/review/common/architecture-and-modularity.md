## CS-ARCH — Architecture

### [CS-ARCH-001] Separate transport, application, and infrastructure
- **Rule:** Transport, application or business logic, and infrastructure MUST
  stay in distinct layers.
- **Why:** Layer isolation keeps change impact understandable and testable.
- **Trigger:** A changed handler talks directly to low-level storage or a
  use-case constructs transport-layer responses.

### [CS-ARCH-002] Inject external dependencies
- **Rule:** External dependencies such as DB clients, HTTP clients, clocks, or
  RNGs MUST be injected instead of constructed inline.
- **Why:** Inline construction hides boundaries and makes code harder to test
  and evolve.
- **Trigger:** A changed class or function creates long-lived infrastructure
  dependencies directly inside business logic.

### [CS-ARCH-003] Prefer stateless services
- **Rule:** Service classes SHOULD remain stateless where practical.
- **Why:** Mutable service state is easy to couple accidentally across calls.
- **Trigger:** A changed service accumulates mutable instance state used across
  requests or operations.

## CS-MOD — Modularity

### [CS-MOD-001] Group by feature, not generic buckets
- **Rule:** New domain logic SHOULD live near the feature it serves, not in a
  generic `utils/`, `helpers/`, or `common/` bucket.
- **Why:** Feature-local code is easier to own and change safely.
- **Trigger:** A changed file introduces business logic under a generic bucket
  without an established project convention for that layout.

### [CS-MOD-002] Acyclic, shallow dependencies
- **Rule:** Dependencies between modules MUST remain acyclic.
- **Why:** Cycles make code harder to test, load, and refactor.
- **Trigger:** A changed import creates or strongly suggests a dependency
  cycle visible from the changed files.

### [CS-MOD-003] No god objects or god utility modules
- **Rule:** A class or module SHOULD NOT accumulate unrelated concerns.
- **Why:** Large mixed-responsibility modules become unstable change hubs.
- **Trigger:** A changed module adds another unrelated public responsibility
  to an already broad class or file.
