## CS-PY — Python

### [CS-PY-001] No mutable default arguments
- **Rule:** Function parameters MUST NOT use mutable values such as `[]`,
  `{}`, or `set()` as defaults.
- **Why:** Python evaluates default arguments once at definition time, so
  mutable defaults leak state across calls.
- **Trigger:** A changed function definition uses a mutable default value.

### [CS-PY-002] No `assert` for runtime validation
- **Rule:** `assert` MUST NOT be used for runtime validation in production
  code.
- **Why:** Optimized Python can remove `assert`, silently removing the check.
- **Trigger:** A changed non-test path uses `assert` to validate external or
  user-controlled input.

### [CS-PY-003] No blocking calls inside `async def`
- **Rule:** Blocking IO or CPU-heavy work MUST NOT be called directly inside
  `async def`; offload it first.
- **Why:** Blocking inside coroutines starves the event loop and hurts every
  concurrent task.
- **Trigger:** A changed `async def` directly performs blocking file, sleep,
  or synchronous external work.
