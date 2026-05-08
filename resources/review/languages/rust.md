## CS-RUST — Rust

### [CS-RUST-001] No `unwrap()` or `expect()` in production code
- **Rule:** Production Rust MUST NOT use `.unwrap()` or `.expect()` on
  fallible values.
- **Why:** Panics turn recoverable failures into crashes.
- **Trigger:** A changed non-test path introduces `.unwrap()` or `.expect()`.

### [CS-RUST-002] Add context to propagated errors
- **Rule:** Errors crossing a meaningful boundary SHOULD carry operation
  context when propagated.
- **Why:** Bare propagation loses the step that failed.
- **Trigger:** A changed `?` crosses a significant layer boundary with no
  added context.

### [CS-RUST-003] Avoid `serde_json::Value` for stable domain data
- **Rule:** Stable domain or storage shapes SHOULD use concrete Rust types,
  not `serde_json::Value`.
- **Why:** Untyped JSON values hide schema drift and invalid states.
- **Trigger:** A changed stable schema field or struct uses
  `serde_json::Value` where a concrete type is known.

### [CS-RUST-004] No blocking calls inside `async fn`
- **Rule:** Blocking IO or CPU-heavy work MUST NOT run directly inside
  `async fn`; offload it first.
- **Why:** Blocking starves the async executor and degrades the whole service.
- **Trigger:** A changed `async fn` directly performs blocking file, thread,
  sleep, or synchronous external work.
