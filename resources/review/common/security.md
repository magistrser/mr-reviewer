## CS-SEC — Security

### [CS-SEC-001] Authn/authz at every boundary
- **Rule:** Authentication and authorization MUST be enforced at every
  externally reachable boundary.
- **Why:** Missing checks at one edge invalidate stronger checks elsewhere.
- **Trigger:** A changed externally reachable path lacks an auth check or
  relies only on obscurity or caller discipline.

### [CS-SEC-002] No hardcoded secrets
- **Rule:** Secrets MUST come from environment variables or a secret manager.
- **Why:** Hardcoded secrets leak into version control and are expensive to
  rotate safely.
- **Trigger:** A changed file contains a literal that looks like a password,
  token, key, or credential-bearing connection string.

### [CS-SEC-003] Parameterized queries and contextual escaping
- **Rule:** SQL MUST use parameterized queries. Output to HTML, JS, shell, or
  similar contexts MUST use contextual escaping.
- **Why:** Context-sensitive injection flaws are high-impact and easy to miss
  once they ship.
- **Trigger:** A changed path concatenates untrusted data into SQL or renders
  raw unescaped output into a sensitive sink.

### [CS-SEC-004] No internal-error leakage
- **Rule:** External error responses MUST NOT include stack traces, file
  paths, or library internals.
- **Why:** Internal details help attackers and confuse callers.
- **Trigger:** A changed error path serializes raw exception details or
  privileged operational context to an external caller.
