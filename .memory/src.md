# mr-review project memory

Last revised: 2026-05-08.

## Purpose

`mr-review` is a deterministic GitLab merge-request review orchestrator.

Python owns orchestration, workspace state, GitLab API calls, file gating, deterministic context assembly,
finding validation, consolidation, deduplication, optional translation, preview, and publishing. LLM calls are
restricted to bounded judgment tasks: MR summary, focused file review passes, bounded cluster review passes,
deduplication decisions, and translation.

The project is tuned for local OpenAI-compatible models around 30B with limited context. Review quality should
come from better deterministic planning/context and repo-specific knowledge, not broad model autonomy.

## Current architecture

The project follows the bundled `python_template` layout:

- `application/`: use cases, orchestration services, ports, DTOs, review job status models.
- `domain/`: typed domain models and deterministic review rules. It must not import `application` or `infrastructure`.
- `infrastructure/`: concrete adapters for GitLab, OpenAI-compatible agents, CLI, FastAPI, metrics, workspace setup, and job execution.
- `runtime/`: neutral async filesystem/subprocess helpers shared by layers.
- `resources/`: packaged review standards and agent prompts.
- `main.py`: FastAPI application entrypoint.
- `infrastructure/cli/main.py`: CLI application entrypoint.
- `settings.py`: Pydantic app settings plus backward-compatible review `ConfigLoader`.

Two front doors share the same `ReviewMergeRequestUseCase`:

- CLI mode keeps the `mr-review` command and all existing options, including interactive `--preview-mode`, plus
  resumable `--continue [workspace-name]`.
- FastAPI mode is non-interactive, disables preview, and publishes results through background jobs.

`infrastructure/composition.py` is the canonical composition root used by both modes. Use it instead of wiring
GitLab, agent, settings, and resources separately in new entrypoints.

## FastAPI API

FastAPI mode exposes:

- `GET /health` -> plain `Ok`
- `GET /metrics` -> Prometheus metrics
- `POST /api/v1/reviews` -> starts a background review job and returns `202` with `job_id`, `status`, `status_url`
- `GET /api/v1/reviews/{job_id}` -> returns `queued`, `running`, `succeeded`, or `failed` plus progress, warnings, result, or structured error

Jobs are file-backed under `<review-root>/.mr-review-api-jobs/`.

API mode uses `WorkspaceBuilder.setup_workspace_job_safe`, which does not delete existing MR workspaces. CLI mode
also preserves existing MR workspaces so interrupted reviews can be resumed.

## Resumable workspaces

Workspaces are created under `<review-root>/.pr-review-workspaces/` with names like
`project-mr-42-2026-05-08_16-40-12`. The timestamp is intentionally human-readable and local-time based. If a name
collides within the same second, the workspace builder appends a numeric suffix.

`progress.json` is the top-level resume contract owned by `ReviewMergeRequestUseCase`. It stores schema version,
MR URL, project path, MR IID, workspace path, selected model, preview mode, run status, current stage, completed
stage data, warnings, timestamps, and final result data. The use case marks each of the ten major stages started,
completed, or failed; on resume it skips completed stages and loads their saved summaries/artifacts.

`mr-review --continue` selects the most recently modified resumable workspace. `mr-review --continue <name>` selects
that exact workspace directory. Completed workspaces are reported without calling GitLab or publishing again.
Legacy workspaces without `progress.json` are not resumable unless they already contain both `publish-result.json`
and `quality-report.json`, in which case they are treated as done for reporting.

Resume behavior deliberately avoids duplicate publishing: if progress marks the publish stage complete, or if
`publish-result.json` already exists and parses as a complete publish result, the publisher is not called again.
File and cluster passes still use finding files as subtask completion markers. Deduplication and translation also
reuse valid per-job result JSON files so a failed run continues from remaining subjobs.

## Configuration

Template app settings are read from `settings.dev.yml` for development/test or `settings.yml` for production based
on `ENVIRONMENT`.

The review config loader accepts YAML only. CLI default resolution:

- `<review-root>/settings.yml` if it exists
- otherwise root `settings.dev.yml`

`settings.dev.yml` is the committed development/template config. It intentionally mirrors the full local
`settings.yml` review tuning shape so new environments can see every supported knob, but it must not contain real
agent credentials or private endpoint details. Keep `security.auth.baseUrl` on a localhost/example endpoint and
`security.auth.apiKey` empty; real values should come from `.env` or process environment.

Secrets should not be committed. Use `.env` or environment variables:

- `GITLAB_TOKEN`
- `GITLAB_API_URL`
- `OPENAI_API_KEY` or `AGENT_API_KEY`
- `OPENAI_BASE_URL` or `AGENT_BASE_URL` when not provided as non-secret settings

`review.httpTimeoutSeconds` controls GitLab API request timeouts and defaults to 60 seconds.

## Review resources

Review resources live in `resources/review/`.

- `review-plan.toml` is the source of truth for file and cluster passes.
- `common/` contains shared standards.
- `languages/` contains language-specific standards.
- `severity-levels.md` and `templates/inline-comment.md` support validation/publishing context.

Agent prompts live in `resources/agents/`.

`OpenAIAgentRunner` must execute tool calls whenever a response message includes them, regardless of the
OpenAI-compatible server's `finish_reason`. Some local servers return `finish_reason: "stop"` alongside valid
tool calls; treating `stop` as terminal before executing those calls causes missing artifact files such as
translation result JSON.

## Guardrails

- Keep orchestration in `application`, not CLI/API adapters.
- Keep deterministic review logic in `domain`.
- Keep external HTTP, filesystem, subprocess, metrics, terminal UI, and FastAPI concerns in `infrastructure` or `runtime`.
- Keep review agents constrained to provided inputs and generated contexts.
- Do not reintroduce the old `src` package or `src.*` imports.
- Keep tests named `*_tests.py`; `pytest.ini` intentionally ignores `python_template/`.
- Tests must derive repository paths from `Path(__file__).resolve().parents[1]`; do not hard-code developer
  workstation checkout paths because GitHub Actions checks out the repo elsewhere.
- Tests for Rich live TTY output should assert `ConsoleReviewOutput` live state/sealing behavior rather than
  exact captured stdout emptiness; Rich versions differ on whether fake TTY streams receive escape-rendered frames.
- Use `PYTHONPYCACHEPREFIX=/tmp/mr-review-pyc` during local verification to avoid writing caches into source packages.

## CLI troubleshooting

Step 1 of the CLI intentionally prints sub-status details before workspace setup completes:

- `Model: resolving default model from /models` means no `--model` was passed and the agent endpoint is being queried for the default model. This metadata request is clamped to 30 seconds even when generation timeout is higher. If it waits or fails here, pass `--model <id>` or check the configured agent endpoint.
- `GitLab MR: loading ...` means GitLab metadata is being fetched.
- `Workspace: cloning ...` means repository checkout is in progress.

The CLI should not silently sit on `[1/10] Resolve merge request and prepare workspace`; if it does, inspect the last
detail line to identify whether the agent endpoint, GitLab API, or git workspace setup is blocking.

## Verification

Verified on 2026-05-08 with `uv sync` and Python 3.14.4:

- `flake8`
- `mypy .`
- `pytest -s --color=yes --junitxml=report.xml --cov=application --cov=domain --cov=infrastructure --cov-config=.coverage_conf --cov-fail-under 70`

After the resumable workspace implementation and OpenAI-compatible tool-call finish-reason fix, quick verification
passed with 114 tests.

Note: `mypy.ini` excludes tests and quarantines several older dynamic modules with `ignore_errors`. That preserves
the template gate while leaving a clear future cleanup target for deeper TypedDict/UI hardening.

## GitHub publishing and CI

The repository is intended to be published to `https://github.com/magistrser/mr-reviewer` with `master` as the
protected integration branch.

GitHub Actions mirrors the local template gate rather than the private GitLab Docker pipeline. The workflow in
`.github/workflows/checks.yml` runs on pull requests targeting `master` and pushes to `master`, installs Python
`3.14` plus `uv`, syncs locked dependencies with all groups, then runs:

- `uv run flake8`
- `uv run mypy .`
- `uv run pytest -s --color=yes --junitxml=report.xml --cov=application --cov=domain --cov=infrastructure --cov-config=.coverage_conf --cov-fail-under 70`

The GitLab CI file is kept because it still captures the private Docker build and publish pipeline. GitHub CI is
limited to source checks because the private registries and image-publish credentials from GitLab are not available
in GitHub Actions by default.
