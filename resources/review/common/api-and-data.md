## CS-API — API and data

### [CS-API-001] Validate external input at the boundary
- **Rule:** Input from outside the process MUST be validated before it enters
  the domain.
- **Why:** Boundary validation prevents invalid states from leaking across
  layers and turning into deeper bugs.
- **Trigger:** A changed boundary passes raw request, file, queue, or RPC
  data directly into domain logic without validation.

### [CS-API-002] Structured error responses
- **Rule:** API error responses MUST use structured bodies and correct status
  codes.
- **Why:** Inconsistent error shapes break callers and often leak internal
  behavior.
- **Trigger:** A changed endpoint returns plain strings, the wrong status
  class, or a success status with an error payload.

### [CS-API-003] Atomicity for multi-step state changes
- **Rule:** Multi-step state changes MUST be atomic when partial failure would
  break invariants.
- **Why:** Partial writes corrupt state in ways that are hard to repair.
- **Trigger:** A changed path performs multiple writes for one logical change
  without a transaction or equivalent guard.

### [CS-API-004] No N+1 access patterns
- **Rule:** Code MUST NOT issue per-item external access when a reasonable
  batched form exists.
- **Why:** Loop-driven IO causes latency spikes and scalability problems.
- **Trigger:** A changed loop issues one query, RPC, or similar external call
  per element.

### [CS-API-005] Safe, reversible migrations
- **Rule:** Migrations SHOULD be safe under live traffic and reversible where
  practical.
- **Why:** Schema changes fail hardest during rollouts, when rollback is most
  valuable.
- **Trigger:** A changed migration assumes an unsafe rollout order, drops live
  columns too early, or rewrites data without a safe path.
