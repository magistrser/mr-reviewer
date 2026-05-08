# Inline Comment Template

Render this template into the `body` field of a positioned MR discussion.
Replace every `<…>` placeholder. Drop the finding if any required placeholder
cannot be filled.

```
**[<SEVERITY>] <SHORT_TITLE>**  _(rule: <RULE_IDS>, confidence: <CONFIDENCE>)_

**What:** <one-sentence description of the issue at this line>

**Why it matters:** <one or two sentences linking to the rule's "Why">

**Suggested change:**
```<LANGUAGE>
<minimal code suggestion or a unified-diff style snippet>
```

<optional: 1–2 sentences of extra context, alternatives, or links to other
findings; omit the line entirely if not used>
```

## Field rules

- `<SEVERITY>` — exactly one of `Critical`, `Major`, `Minor`, `Suggestion`.
- `<SHORT_TITLE>` — under 70 characters, no trailing period.
- `<RULE_IDS>` — one or more `CS-*` IDs separated by `, `. Required.
- `<CONFIDENCE>` — `High`, `Medium`, or `Low`. Critical/Major findings MUST
  be at least `Medium`; otherwise demote per `severity-levels.md`.
- `<LANGUAGE>` — fenced-block language tag matching the file
  (`rust`, `python`, `ts`, `go`, `sql`, …). Use `text` if unsure.
- Code suggestion SHOULD compile or apply cleanly. If only an outline is
  possible, prefix it with `// pseudo:` (or the language equivalent).
- Do NOT include screenshots, images, or links to external systems other
  than the project's own docs.

## Position payload (sent alongside the body)

```
{
  "base_sha":   "<diff_refs.base_sha>",
  "start_sha":  "<diff_refs.start_sha>",
  "head_sha":   "<diff_refs.head_sha>",
  "old_path":   "<file old path or same as new_path>",
  "new_path":   "<file new path>",
  "new_line":   <integer>,        // for added/modified lines
  "old_line":   <integer or null>,// for removed lines (set new_line=null)
  "position_type": "text"
}
```

## Worked example

```
**[Major] Swallowed error on retry path**  _(rule: CS-CORE-006, confidence: High)_

**What:** The `Err` arm here returns without logging or wrapping the original error.

**Why it matters:** Silent failures in the retry loop will mask upstream outages and make incidents hard to diagnose.

**Suggested change:**
```rust
Err(err) => {
    warn!(error = %err, attempt, "subzone sync attempt failed; retrying");
    return Err(err.context("subzone sync attempt failed"));
}
```
```
