# mr-review

`mr-review` is the Python rewrite of the legacy GitLab MR review flow.

The guiding design is:

- Python owns orchestration, workspace state, GitLab calls, gating, context assembly, consolidation, deduplication, optional translation, and publishing.
- The LLM is used only where judgment helps:
  - summarize the MR
  - review focused file or cluster tasks
- The whole runtime path is async for I/O-heavy work.

The runtime code now uses top-level `application`, `domain`, `infrastructure`, and `runtime` packages.
The product and CLI name remain `mr-review`.

## What it does

Given a GitLab merge request URL, `mr-review` will:

1. Resolve the MR and prepare a local workspace at the MR head SHA.
2. Collect changed files, unresolved existing discussions, and positioned PR comments.
3. Generate an optional PR summary.
4. Gate files, enrich deterministic file analysis, and build a cross-file cluster plan.
5. Run focused file review passes:
   - correctness
   - risk passes such as security or async when the file shape suggests them
   - language-specific pass when supported
6. Run bounded cluster review passes for cross-file mismatches.
7. Validate, consolidate, and deduplicate findings.
8. Translate publishable review text when `review.translation.language` is not `ENG`.
9. Optionally preview translated findings in an interactive terminal before publish.
10. Post reviewed inline comments and one regenerated summary note back to GitLab.

The file-review, cluster-review, dedup, and translation stages can each run with bounded parallelism from YAML settings.

## Local-model design

This project is tuned for local models around 30B with a 32k context window.

Quality comes from better deterministic context, not from giving the model unlimited repo access:

- file review context is assembled in Python and capped before the model sees it
- cluster review is bounded to a small set of related files
- review agents are restricted to a tiny read budget
- findings are validated locally before publish

Context budgets are defined in `domain/review/context.py`, loaded through `ConfigLoader`, and can be overridden from YAML settings.

- `review.context.scale` multiplies every context limit after overrides are applied
- `review.context.maxContextChars` and `review.context.maxClusterContextChars` cap the full assembled file and cluster contexts
- `review.context.sectionChars` lets you tune individual sections like standards, excerpts, symbols, or the review plan
- count-style limits such as `maxSymbolBlocks`, `maxSymbolLines`, `clusterExcerptRadius`, and `clusterMaxSegments` are scaled too

## Architecture

The project follows a Clean Architecture split:

- `application/`
  - async use-case orchestration and ports
  - `ReviewMergeRequestUseCase` is the main entry
  - `ReviewWorkspaceService` manages review workspace state transitions
- `domain/`
  - typed entities and deterministic review logic
  - `domain/review/` contains gating, context building, planning, validation, consolidation, and benchmark scoring
- `infrastructure/`
  - concrete adapters for GitLab, model runtime, workspace setup, and async filesystem or subprocess helpers
- `resources/`
  - packaged agent prompts and review standards
- `main.py`
  - FastAPI application entrypoint with health, metrics, and review job endpoints
- `infrastructure/cli/main.py`
  - async composition root and CLI entrypoint

Inside canonical modules, behavior lives on structures instead of loose module functions. Important structures include:

- `ReviewMergeRequestUseCase`
- `ReviewWorkspaceService`
- `ReviewPublisher`
- `ReviewCommentDeduplicator`
- `ReviewScopePlanner`
- `ReviewContextBuilder`
- `CommentDedupPlanner`
- `FindingValidator`
- `FindingsConsolidator`
- `ReviewBenchmarkScorer`
- `GitLabCompactor`

## Project layout

- `infrastructure/cli/main.py`: CLI entrypoint and composition root
- `main.py`: FastAPI application entrypoint
- `application/`: use case layer
- `domain/`: domain layer
- `infrastructure/`: concrete adapters
- `resources/agents/`: packaged prompts
- `resources/review/`: review plan, shared standards, language standards, severity levels, and comment template

## Requirements

- Python 3.14.3+
- `git`
- GitLab API access
- an OpenAI-compatible chat-completions endpoint

Use `uv sync` to create a Python 3.14 virtual environment from the template configuration.

## Configuration

By default the CLI reads:

- GitLab credentials from `<review-root>/.env`
- model endpoint settings from `<review-root>/settings.yml` when present, otherwise from root `settings.dev.yml`

Expected `.env` keys:

```env
GITLAB_TOKEN=glpat-...
GITLAB_API_URL=https://gitlab.example.com/api/v4
OPENAI_API_KEY=...
```

`OPENAI_BASE_URL` or `security.auth.baseUrl` supplies the OpenAI-compatible endpoint. `OPENAI_API_KEY`
or `AGENT_API_KEY` is preferred for secrets; legacy local `security.auth.apiKey` is still accepted for
backward-compatible private settings files.

Expected YAML settings shape:

```yaml
security:
  auth:
    baseUrl: https://your-openai-compatible-endpoint/v1
model:
  generationConfig:
    timeout: 300000
review:
  parallelReviews: 2
  parallelClusterReviews: 1
  dedup:
    lineWindow: 3
    parallelReviews: 2
  translation:
    language: ENG
    parallelReviews: 2
  indexing:
    enabled: true
    maxCatalogFileBytes: 131072
    maxRetrievedArtifactsPerTask: 3
    maxRetrievedCharsPerTask: 2500
    maxRetrievedLinesPerArtifact: 80
  passes:
    file:
      enabled:
        - correctness
        - security
        - async
        - python
    cluster:
      enabled:
        - security
        - api_boundary
  context:
    scale: 1.0
    maxContextChars: 28000
    maxClusterContextChars: 30000
```

Notes:

- `review.parallelReviews` controls how many file-review agent calls can run at the same time
- `review.parallelClusterReviews` controls how many cluster-review agent calls can run at the same time
- the built-in default is `1`
- if `review.parallelClusterReviews` is omitted, it falls back to `review.parallelReviews`
- both camelCase and snake_case keys are accepted for `review.parallelReviews`
- both camelCase and snake_case keys are accepted for `review.parallelClusterReviews`
- all values inside `review.dedup` are optional
- `review.dedup.lineWindow` controls the nearby-line grouping window used before publish
- `review.dedup.parallelReviews` controls how many dedup-agent group checks can run at the same time
- `review.dedup.parallelReviews` defaults to `1`, so dedup stays sequential unless you opt in
- both camelCase and snake_case keys are accepted for `review.dedup`
- all values inside `review.translation` are optional
- `review.translation.language` selects the target publish language
- if `review.translation.language` resolves to `ENG`, translation is skipped and English findings are published directly
- `review.translation.parallelReviews` controls how many translation jobs can run at the same time
- `review.translation.parallelReviews` defaults to `1`
- translation happens after deduplication and before GitLab publication
- translation failures are fatal for the run so nothing is posted in a partially translated state
- both camelCase and snake_case keys are accepted for `review.translation`
- all values inside `review.indexing` are optional
- `review.indexing.enabled` controls whether repo-wide indexing and retrieval run at all
- `review.indexing.maxCatalogFileBytes` limits which tracked source files are indexed into `repo-catalog.json`
- `review.indexing.maxRetrievedArtifactsPerTask`, `maxRetrievedCharsPerTask`, and `maxRetrievedLinesPerArtifact` define the top-level retrieval defaults that per-pass policies inherit unless they override them
- `review.indexing.filePolicies.<pass-id>` configures file-pass retrieval with `enabled`, `allowedKinds`, `hintSelection`, `minScore`, `requireUniqueTop`, `maxArtifacts`, `maxChars`, and `maxLinesPerArtifact`
- `review.indexing.clusterPolicies.<focus-area>` configures cluster retrieval with the same policy fields
- `hintSelection: "exactly_one"` skips retrieval unless exactly one request remains after policy filtering
- `hintSelection: "first_n"` keeps request order and attempts retrievals until the artifact or char budget is exhausted
- symbol-aware retrieval uses pre-indexed `symbol_spans` from `repo-catalog.json` and falls back to file snippets when no exact symbol span matches
- both camelCase and snake_case keys are accepted for `review.indexing`, `review.indexing.filePolicies`, and `review.indexing.clusterPolicies`
- all values inside `review.passes` are optional
- `review.passes.file.enabled` limits file review to the listed file pass ids
- `review.passes.cluster.enabled` limits cluster review to the listed cluster pass ids
- available file pass ids currently include `correctness`, `security`, `async`, `python`, `rust`, `typescript`, and `javascript`
- available cluster pass ids currently include `security`, `api_boundary`, `data_integrity`, `async_concurrency`, and `architecture`
- if `review.passes.file.enabled` is omitted, all packaged file passes remain enabled
- if `review.passes.cluster.enabled` is omitted, all packaged cluster passes remain enabled
- an empty enabled list is valid and disables that side of review entirely
- all values inside `review.context` are optional
- if omitted, the current built-in defaults are used
- `scale` is applied after individual overrides, so `scale: 0.75` shrinks every budget and count limit by 25%
- both camelCase and snake_case keys are accepted for `review.context`
- both camelCase and snake_case keys are accepted for `review.dedup`
- both camelCase and snake_case keys are accepted for `review.passes.file.enabled` and `review.passes.cluster.enabled`

Optional repo-specific review knowledge:

- place `review-profile.md` in the chosen review root
- it is injected into every review context
- use it for project conventions, architecture rules, risky areas, and known bad patterns

## Install

From the repo root:

```bash
uv sync
```

The CLI entrypoint is installed into the synced environment:

```bash
.venv/bin/mr-review \
  "https://gitlab.example.com/group/subgroup/project/-/merge_requests/42"
```

The FastAPI application uses the template entrypoint:

```bash
.venv/bin/fastapi dev
```

## Usage

Basic usage:

```bash
mr-review "https://gitlab.example.com/group/subgroup/project/-/merge_requests/42"
```

Pick a specific model:

```bash
mr-review \
  "https://gitlab.example.com/group/subgroup/project/-/merge_requests/42" \
  --model your-model-id
```

Preview findings before publish:

```bash
mr-review \
  "https://gitlab.example.com/group/subgroup/project/-/merge_requests/42" \
  --preview-mode
```

Continue an interrupted review:

```bash
mr-review --continue
mr-review --continue project-mr-42-2026-05-08_16-40-12
```

Override config paths:

```bash
mr-review \
  "https://gitlab.example.com/group/subgroup/project/-/merge_requests/42" \
  --review-root /path/to/workdir \
  --env-path /path/to/.env \
  --settings-path /path/to/settings.yml
```

Useful flags:

- `--model`: force a model instead of taking the first entry returned by `/models`
- `--preview-mode`: review one translated finding at a time in an interactive terminal before GitLab publish
- `--continue [workspace]`: resume the latest workspace, or a specific workspace directory name
- `--review-root`: where `.pr-review-workspaces` is created and where default root config is read from
- `--env-path`: override GitLab credentials file
- `--settings-path`: override YAML settings file
- `--resources-dir`: override packaged review resources
- `--agents-dir`: override packaged agent prompts

`--preview-mode` requires interactive `stdin` and `stdout`. If either stream is not a TTY, the CLI fails fast before it starts MR work.

## FastAPI mode

FastAPI mode is non-interactive and publishes like the CLI with preview disabled.

- `GET /health` returns plain `Ok`
- `GET /metrics` exposes Prometheus metrics
- `POST /api/v1/reviews` accepts `{"mr_url": "...", "model": "optional-model"}`
- `GET /api/v1/reviews/{job_id}` returns `queued`, `running`, `succeeded`, or `failed`

Review jobs are persisted under `<review-root>/.mr-review-api-jobs/`. API composition uses the same
`ReviewMergeRequestUseCase` as the CLI, but with API output and job-safe workspace provisioning.

## CLI output

The CLI prints numbered steps, live pass progress, and a final summary.
Interactive progress rendering is powered by `rich`.
Interactive preview mode is powered by `prompt_toolkit`.

When stdout is not a TTY, progress is emitted as plain log lines. Expect output like:

```text
[1/10] Resolve merge request and prepare workspace
  Model             local-model
  MR                group/project!42

[5/10] Run review passes
  Parallel reviews  2
  Review pass       [1/9] started src/app.py (python)
  Progress          0 done, 1 active of 2, 8 pending
  In progress       src/app.py (python)
  Review pass       [2/9] started src/api.py (security)
  Progress          0 done, 2 active of 2, 7 pending
  In progress       src/app.py (python), src/api.py (security)
  Review pass       [1/9] finished src/app.py (python)
  Progress          1 done, 1 active of 2, 7 pending
  In progress       src/api.py (security)

[6/10] Run cluster review passes
  Parallel cluster reviews  1
  Cluster pass      [1/2] started Api Boundary changes in src/api (api_boundary)

[7/10] Validate, consolidate, and deduplicate findings
  Nearby groups     6
  Dedup window      +/- 3 lines
  Parallel dedup reviews 2
  Dedup group       [1/6] started src/app.py (group_001 lines 10-12)
  Progress          0 done, 1 active of 2, 5 pending
  In progress       src/app.py (group_001 lines 10-12)

[8/10] Translate publishable review output
  Translation       translated
  Translation language RUS
  Parallel translations 2

[9/10] Preview translated review findings
  Preview           reviewed
  Preview items     4
  Edited            1
  Unpublished       1

Review complete
```

When stdout is a TTY, `ConsoleReviewOutput` uses `rich.Live` to redraw the current progress panel in place so the terminal behaves more like a small CLI UI instead of a growing log.
When `--preview-mode` is enabled, the preview screen uses framed, color-accented `prompt_toolkit` panels for context, finding content, editor fields, and controls.

When `--preview-mode` is enabled, the preview UI shows one finding at a time and supports:

- previous or next finding: `Left`, `Right`, `p`, `n`
- scroll current finding: `Up`, `Down`, `j`, `k`
- toggle publish on or off: `Space`
- edit current `short_title` and `body`: `e`
- move the caret inside title or body while editing: `Left`, `Right`, `Up`, `Down`, `Home`, `End`
- switch edit field: `Tab`, `Shift-Tab`
- save edits: `Ctrl-S`
- discard unsaved edits: `Esc`
- finish preview and continue to publish: `q`
- cancel the whole run before publish: `Ctrl-C`

Browse navigation keys are active only outside edit mode.

The final summary includes:

- scope counts
- file and cluster pass counts
- translation status and language
- preview status and preview edits
- kept, dropped, and invalid findings
- publish result
- artifact paths

## Workspace artifacts

Each run creates a fresh workspace with a human-readable local timestamp under:

```text
<review-root>/.pr-review-workspaces/<repo-slug>-mr-<iid>-YYYY-MM-DD_HH-MM-SS/
```

The CLI preserves older workspaces so interrupted reviews can be resumed. `mr-review --continue` resumes the most
recent resumable workspace. `mr-review --continue <workspace-name>` resumes a specific directory name. If the
selected workspace is already complete, the CLI reports that and exits without publishing again.

Important files inside the workspace:

- `meta.json`: MR identity and SHAs
- `progress.json`: resumable stage state, selected model, preview mode, warnings, and per-stage summaries
- `changed-files.json`: compact changed-file list plus gating metadata
- `file-analysis.json`: deterministic per-file analysis used for focused passes
- `cluster-plan.json`: bounded cluster review plan
- `repo-catalog.json`: tracked source-file catalog with import metadata, symbol names, and `symbol_spans` used for bounded repo retrieval
- `existing-discussions.json`: unresolved inline notes captured for reporting and review context
- `existing-comments.json`: positioned PR comments used for nearby-line dedup before publish
- `pr-summary.md`: optional summary written by the summarize agent
- `findings/*.json`: raw file-pass outputs
- `cluster-findings/*.json`: raw cluster-pass outputs
- `retrieval-plans/*.json`: per-task retrieval diagnostics including the applied retrieval policy, candidate requests, and returned artifacts
- `retrieval-report.json`: run-level retrieval totals split by file and cluster stages
- `raw-findings.json`: validated consolidated findings before final capping
- `dedup-inputs/*.json`: per-group inputs for the deduplication agent
- `dedup-results/*.json`: per-group outputs from the deduplication agent
- `dedup-report.json`: kept and dropped comment indexes plus dedup anomalies
- `all-findings.json`: final findings that will be published
- `translation-inputs/*.json`: per-job inputs for the translation agent
- `translation-results/*.json`: per-job translation outputs
- `translation-report.json`: translation job status and artifact summary
- `translated-publication.json`: publish-ready translated payload used by the publisher
- `preview-publication.json`: publish-ready payload after preview edits or unpublished findings are applied
- `preview-report.json`: preview status, edited count, unpublished count, and artifact summary
- `quality-report.json`: validation, translation, preview, final-findings, publish, and warning summary
- `publish-result.json`: publish status and note IDs

FastAPI jobs also write `progress.json` in their workspaces, but the API does not currently expose a continue
endpoint or request option.

The CLI also writes a copy of the publish result to `/tmp/pr-review-publish-result-<project>-<iid>.json`.

## Review resources

Review resources now use one structured plan plus shared rule files:

- `resources/review/review-plan.toml`: the single pass-definition manifest
- `resources/review/common/`: standards shared across multiple passes
- `resources/review/languages/`: language-specific standards
- `resources/review/severity-levels.md`: severity policy
- `resources/review/templates/inline-comment.md`: comment rendering template

Review behavior is now:

- the review plan defines when each pass runs, which standard files it loads, and what to check
- shared standards used by multiple passes live under `resources/review/common/`
- language-specific standards live under `resources/review/languages/`
- each pass loads an explicit bundle of resource files instead of filtering sections out of a monolithic standards file

Agent prompts live in `resources/agents/`:

- `summarize-pr-agent.md`
- `review-agent.md`
- `cluster-review-agent.md`
- `deduplicate-comments-agent.md`
- `translate-review-agent.md`

## Evaluation harness

The deterministic scoring helper lives in `domain/review/benchmark.py`.

Use `ReviewBenchmarkScorer` to compare actual findings against expected findings for known MRs. This is the intended base for a real precision or recall benchmark set as you collect examples from your repository.

## Development

Run the template checks:

```bash
uv sync
flake8
mypy .
pytest -s --color=yes --junitxml=report.xml \
  --cov=application --cov=domain --cov=infrastructure \
  --cov-config=.coverage_conf --cov-fail-under 70
```

CLI help:

```bash
.venv/bin/mr-review --help
```

FastAPI development server:

```bash
.venv/bin/fastapi dev
```

## Notes

- The default config files are `.env` and `settings.yml` in the chosen review root.
- Review quality should be improved by extending deterministic context and repo knowledge first, not by letting agents browse the whole repository.
- Review pass failures remain non-fatal: a missing findings file is stubbed on the next loop so the run can continue.
- New code should import from `application`, `domain`, and `infrastructure`.
- In canonical source, add behavior to structures instead of new module-level functions.
