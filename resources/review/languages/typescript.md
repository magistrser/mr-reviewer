## CS-TS — TypeScript and JavaScript

### [CS-TS-001] No `any` at public boundaries
- **Rule:** Exported functions and types MUST NOT use `any` for parameters or
  return values.
- **Why:** `any` disables type checking exactly where modules need the
  strongest contracts.
- **Trigger:** A changed exported boundary uses `any` or a boundary-crossing
  `as any` cast.

### [CS-TS-002] No unhandled Promise rejections
- **Rule:** Every async call result MUST be awaited, returned, or handled.
- **Why:** Dropped promises hide failures and can terminate services or leave
  state inconsistent.
- **Trigger:** A changed async call result is discarded without `await`,
  `return`, or explicit rejection handling.

### [CS-TS-003] No non-null assertion operator in production code
- **Rule:** The non-null assertion operator (`!`) MUST NOT be used when null
  is a real runtime possibility.
- **Why:** `!` hides nullability problems until they fail later and farther
  from the source.
- **Trigger:** A changed non-test path uses `value!` or `value!.field`.
