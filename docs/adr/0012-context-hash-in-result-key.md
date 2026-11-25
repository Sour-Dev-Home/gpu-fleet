# 0012. Context hash in the result key; add-only schema migrations

**Status:** Accepted (maintainer, 2026-11-25)
**Date:** 2026-11-25
**Extends:** [0003](0003-content-addressed-results.md) (closes its "proposed for step 3b" consequence)

## Context
A result's key covers the input, the model and the prompt version. That is enough for Metaphor's pass 1, which
describes one shot on its own. Pass 2 writes each description in story order, using the story bible and the
descriptions before it ([#1013](https://github.com/Sour-Dev-Home/metaphor-accessibility/issues/1013)). Same shot,
same model, same prompt, but a corrected bible should give a different description. With the old key the cache
would serve the description written for the old bible, without any error.

ADR 0003 also warns that changing the key scheme makes every cached result look missing.

## Decision
- Jobs get an optional `context_sha256`: the SHA-256 of all other text the prompt includes. The job's creator
  computes it. The worker never sees or needs the context text itself in this step.
- `result_key` hashes `[input, model, prompt_version, context]` when a context is given, and exactly the old
  `[input, model, prompt_version]` when it isn't. Old keys stay valid, so no version prefix is needed. The two
  forms can't collide, because JSON arrays of different lengths are different strings.
- The API accepts `context_sha256` as 64 lowercase hex characters or leaves it out (ADR 0011's rules).
- **Schema changes are add-only.** The store keeps a `MIGRATIONS` list of nullable columns and adds any that are
  missing with `ALTER TABLE ADD COLUMN` when it opens. Existing rows read the new column as NULL, meaning "no
  context". Columns are never renamed or dropped in place (the expand step of expand/contract).

## Consequences
- Pass 1 results cached before this change are still found. A test pins the old key formula.
- Changing any part of the context (bible, earlier descriptions) forces a fresh run, as intended. The cost: an
  edit early in the story invalidates every later pass 2 result. That's correct, and pass 2 is text-only and cheap.
- Older workers ignore the new field and would store a pass 2 result under the wrong key. Upgrade workers before
  queueing pass 2 jobs. All workers belong to the maintainer until phase 10.
- `MIGRATIONS` is enough for one SQLite file. The networked store in phase 4 will need a real migration tool
  (for example Alembic), which will get its own ADR.

## Alternatives considered
- **Fold the context into `prompt_version`:** no schema change, but it mixes "which template" with "which story
  state", and the version labels become meaningless hashes.
- **Always include the context, as an empty string for pass 1:** simpler code, but it changes every existing key
  and needs the version prefix that ADR 0003 calls for.
- **A migration framework now:** too heavy for one nullable column in a local SQLite file.
