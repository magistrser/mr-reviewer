# mr-review

`mr-review` reviews GitLab merge requests with an OpenAI-compatible model, then posts useful inline comments back to GitLab.

It is built for teams that want automated review without giving the model broad, uncontrolled access to the repository. Python prepares the workspace, gathers deterministic context, runs focused review passes, validates and deduplicates findings, optionally translates them, and publishes only the final review output.

## What You Get

- Inline GitLab comments for concrete issues found in a merge request.
- An optional generated merge-request summary note.
- Focused review passes for correctness, security, async/concurrency, API boundaries, data integrity, architecture, and supported languages.
- Deduplication against nearby generated findings and existing GitLab discussions.
- Optional translation before publishing.
- A preview mode that lets you edit or unpublish findings before they reach GitLab.
- Resumable local workspaces for interrupted runs.
- A FastAPI mode for background review jobs.

## Requirements

- Python 3.14.3 or newer
- `uv`
- `git`
- GitLab API access token
- An OpenAI-compatible chat-completions endpoint, such as a local model server

The project is tuned for local models around 30B with a limited context window. Better results usually come from better deterministic context and project rules, not from giving the model unlimited repository access.

## Quick Start

Install dependencies from the repository root:

```bash
uv sync
```

Create a `.env` file:

```env
GITLAB_TOKEN=glpat-your-token
GITLAB_API_URL=https://gitlab.example.com/api/v4
OPENAI_API_KEY=your-agent-key
```

Create `settings.yml` to make the first run explicit:

```yaml
security:
  auth:
    baseUrl: http://localhost:11434/v1

model:
  generationConfig:
    timeout: 300000

review:
  translation:
    language: ENG
```

Run a review:

```bash
uv run mr-review "https://gitlab.example.com/group/project/-/merge_requests/42"
```

Use a specific model when your endpoint serves more than one:

```bash
uv run mr-review \
  "https://gitlab.example.com/group/project/-/merge_requests/42" \
  --model your-model-id
```

## Configuration

By default, the CLI reads:

- credentials from `<review-root>/.env`
- settings from `<review-root>/settings.yml`
- `settings.dev.yml` from this repository when `<review-root>/settings.yml` does not exist

The default `review-root` is the directory where you run the command.

Common settings:

```yaml
security:
  auth:
    baseUrl: http://localhost:11434/v1

model:
  generationConfig:
    timeout: 300000

review:
  parallelReviews: 2
  parallelClusterReviews: 1
  httpTimeoutSeconds: 60
  translation:
    language: ENG
    parallelReviews: 1
  dedup:
    lineWindow: 3
    parallelReviews: 1
```

Useful notes:

- `OPENAI_BASE_URL` or `AGENT_BASE_URL` can replace `security.auth.baseUrl`.
- `OPENAI_API_KEY` or `AGENT_API_KEY` should be used for secrets.
- If a local endpoint ignores API keys, still set a placeholder key because the app requires one.
- `review.translation.language: ENG` disables translation.
- `review.parallelReviews` controls concurrent file-review model calls.
- `review.parallelClusterReviews` controls concurrent cross-file review calls.
- `review.dedup.lineWindow` controls how nearby findings are grouped before publishing.
- Deduplication groups by target file, focus area, and nearby line window. Exact `dedup_key` and `rule_ids` are kept as metadata, but they are not trusted as cross-model identity.

For the full tuning surface, use `settings.dev.yml` as a reference. It shows pass selection, repo indexing, retrieval policies, context budgets, translation, and deduplication options.

## CLI Usage

Preview findings before publishing:

```bash
uv run mr-review \
  "https://gitlab.example.com/group/project/-/merge_requests/42" \
  --preview-mode
```

Continue the most recent interrupted review:

```bash
uv run mr-review --continue
```

Continue a specific workspace:

```bash
uv run mr-review --continue project-mr-42-2026-05-08_16-40-12
```

Use custom paths:

```bash
uv run mr-review \
  "https://gitlab.example.com/group/project/-/merge_requests/42" \
  --review-root /path/to/workdir \
  --env-path /path/to/.env \
  --settings-path /path/to/settings.yml
```

Useful flags:

- `--model`: use a specific model instead of the first model returned by `/models`
- `--preview-mode`: review, edit, or unpublish findings before GitLab publish
- `--continue [workspace]`: resume the latest workspace or a named workspace
- `--review-root`: choose where workspaces and default config files live
- `--env-path`: choose a credentials file
- `--settings-path`: choose a YAML settings file
- `--resources-dir`: override packaged review resources
- `--agents-dir`: override packaged agent prompts

`--preview-mode` requires an interactive terminal. In non-interactive shells, the CLI fails before starting MR work.

## What Happens During a Review

For each merge request, `mr-review`:

1. Resolves the GitLab MR and checks out the MR head SHA.
2. Collects changed files, unresolved discussions, and existing positioned comments.
3. Generates an optional MR summary.
4. Selects eligible files and builds deterministic file context.
5. Plans bounded cross-file cluster checks.
6. Runs focused file review passes.
7. Runs cross-file cluster review passes.
8. Validates, consolidates, and deduplicates findings.
9. Translates publishable findings when configured.
10. Optionally opens preview mode.
11. Publishes inline comments and a regenerated summary note to GitLab.

The CLI prints numbered steps and live progress. When output is not a TTY, it falls back to plain log lines.

## Preview Mode

Preview mode lets you inspect each publishable finding before GitLab sees it.

Available controls:

- `Left`, `Right`, `p`, `n`: move between findings
- `Up`, `Down`, `j`, `k`: scroll
- `Space`: toggle publish on or off
- `e`: edit the title and body
- `Tab`, `Shift-Tab`: switch edit field
- `Ctrl-S`: save edits
- `Esc`: discard unsaved edits
- `q`: finish preview and publish remaining findings
- `Ctrl-C`: cancel before publishing

## FastAPI Mode

Start the development server:

```bash
uv run fastapi dev main.py
```

Endpoints:

- `GET /health` returns `Ok`
- `GET /metrics` exposes Prometheus metrics
- `POST /api/v1/reviews` starts a review job
- `GET /api/v1/reviews/{job_id}` returns job status and result details

Example request:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/reviews \
  -H "Content-Type: application/json" \
  -d '{"mr_url":"https://gitlab.example.com/group/project/-/merge_requests/42","model":"your-model-id"}'
```

FastAPI mode is non-interactive and always publishes with preview disabled. Job state is stored under `<review-root>/.mr-review-api-jobs/`.

## Project-Specific Review Knowledge

Add `review-profile.md` to the chosen review root when a repository needs custom review guidance.

Use it for:

- architecture rules
- domain assumptions
- risky areas
- known bad patterns
- team conventions

The profile is included in every review context, so keep it direct and practical.

## Workspaces and Artifacts

Each run creates a workspace:

```text
<review-root>/.pr-review-workspaces/<repo-slug>-mr-<iid>-YYYY-MM-DD_HH-MM-SS/
```

Important artifacts:

- `progress.json`: resumable stage state
- `meta.json`: MR identity and SHAs
- `changed-files.json`: changed files and gating metadata
- `file-analysis.json`: deterministic file analysis
- `cluster-plan.json`: cross-file review plan
- `repo-catalog.json`: indexed source metadata used for bounded retrieval
- `pr-summary.md`: generated MR summary
- `findings/*.json`: raw file-pass findings
- `cluster-findings/*.json`: raw cluster-pass findings
- `dedup-report.json`: kept and dropped finding indexes
- `all-findings.json`: final findings before translation or preview
- `translated-publication.json`: translated publish payload
- `preview-publication.json`: publish payload after preview edits
- `quality-report.json`: run summary and validation details
- `publish-result.json`: GitLab publish result

Completed workspaces are not published again when resumed. This protects against duplicate GitLab comments.

## Development

Run the local checks:

```bash
uv sync
uv run flake8
uv run mypy .
uv run pytest -s --color=yes --junitxml=report.xml \
  --cov=application --cov=domain --cov=infrastructure \
  --cov-config=.coverage_conf --cov-fail-under 70
```

CLI help:

```bash
uv run mr-review --help
```

## Architecture For Contributors

The source follows a Clean Architecture split:

- `application/`: async use cases, orchestration, ports, DTOs, and job status models
- `domain/`: typed entities and deterministic review logic
- `infrastructure/`: GitLab, model runtime, CLI, FastAPI, metrics, and workspace adapters
- `runtime/`: neutral async filesystem and subprocess helpers
- `resources/agents/`: packaged prompts
- `resources/review/`: review plans, standards, templates, and severity policy
- `main.py`: FastAPI entrypoint
- `infrastructure/cli/main.py`: CLI entrypoint

Two front doors share the same `ReviewMergeRequestUseCase`: the CLI and the FastAPI background job runner.

Important contributor rules:

- Keep orchestration in `application`.
- Keep deterministic review logic in `domain`.
- Keep external systems in `infrastructure` or `runtime`.
- Keep agents constrained to prepared inputs and generated contexts.
- Improve review quality through deterministic context and repo knowledge before expanding model autonomy.

## Troubleshooting

If the CLI waits at the first step, read the latest detail line:

- `Model: resolving default model from /models` means the agent endpoint is being queried. Pass `--model <id>` or check the endpoint.
- `GitLab MR: loading ...` means GitLab metadata is being fetched. Check `GITLAB_TOKEN` and `GITLAB_API_URL`.
- `Workspace: cloning ...` means repository checkout is running. Check Git access and repository size.

Common failures:

- `GITLAB_TOKEN not set`: add it to `.env` or export it.
- `GITLAB_API_URL not set`: add the GitLab API v4 URL.
- `Agent API key not set`: set `OPENAI_API_KEY` or `AGENT_API_KEY`.
- `security.auth.baseUrl must be set`: add `security.auth.baseUrl` to settings or export `OPENAI_BASE_URL`.
- `--preview-mode requires interactive stdin and stdout TTYs`: run from an interactive terminal.
