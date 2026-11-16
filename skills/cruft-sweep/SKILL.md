---
name: cruft-sweep
description: Survey, classify, and safely reclaim stale git worktrees in a many-worktree repo, and spread shared files across them. Use when worktrees accumulate ("clean up worktrees", "prune stale branches", "spread this to other worktrees"), before a big refactor, or when `git worktree list` is unmanageable. Reversible by construction - bundles every branch before removing anything.
---

# /cruft-sweep — reclaim stale worktrees without losing work

> Extracted 2026-08-29 from a live 68-worktree sweep. The engine is
> `${CLAUDE_PLUGIN_ROOT}/scripts/cruft_sweep.py`; this file is the judgment layer. Reference:
> [[safe-multiworktree-git-primitives]].

## The core danger
Worktrees share ONE object store and ONE ref namespace. Deleting a worktree can destroy the
only copy of work, and the obvious signals LIE (see the 8 tuition rules in the script's
docstring: `ahead` counts are rewrite-inflated; renames fake "unique work"; a background hook
races merges; base may have deleted a file ON PURPOSE for security).

**Non-negotiable: bundle before remove.** Every prune writes a verified `git bundle` first,
so any misclassification is recoverable.

## Sequence

**1. Survey (read-only, always first)**
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/cruft_sweep.py survey --protect <branch1>,<branch2>
```
Verdicts: `SAFE` (no uncommitted work, no branch-unique content) · `REVIEW` (human decides) ·
`PROTECT` (never touched). Pass `--json` to drive further steps.

**2. Inspect anything nontrivial before deciding**
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/cruft_sweep.py inspect <worktree>
```
Classifies each uncommitted file: `UNIQUE` (nothing like it on base) · `ON-BASE-DIFFERS`
(base has a newer canonical version - usually superseded) · `DUP-IDENTICAL` (safe to drop).
**Judgment gate:** UNIQUE files that relate to LIVE work mean KEEP THE WORKTREE and commit
the work; UNIQUE files from finished explorations mean capture-and-archive.

**3. Spread shared files to every worktree (optional)**
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/cruft_sweep.py spread --files .claude/skills/x/SKILL.md,scripts/y.py
```
Uses immutable blob reads, so it cannot lose the hook race and never partially aborts.
Prefer this over merging base into many branches: deliver the pollen, not the whole tree
(a full merge drags hundreds of unrelated commits into a focused branch).

**4. Prune (destructive, gated)**
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/cruft_sweep.py prune <wt>... --dry-run      # preview first
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/cruft_sweep.py prune <wt>...                # SAFE only
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/cruft_sweep.py prune <wt> --capture-dirty   # WIP-commits dirt first
```
`--capture-dirty` makes a WIP commit (captures untracked too, unlike `git stash create`)
so nothing is lost, then bundles, verifies, and removes. Branch refs are kept as a second
backup. Restore: `git worktree add <path> <branch>` or `git clone <bundle>`.

**Salvage preflight (rule 12, runs inside every prune)**
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/cruft_sweep.py salvage <wt>...                # report-only
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/cruft_sweep.py prune <wt> --force-salvage-ok  # after filing what it found
```
A bundle is a backup nobody reads. Before bundling, prune finds the branch's commits not on
origin/main by patch-id (`git cherry`), pulls claim-shaped lines (decisions, rules, numbers,
TODOs) added to `.md`/`.json` files in them, and greps up to 4 key terms per claim across
origin/main and the memory dir (`--memory DIR`, or set `CRUFT_SWEEP_MEMORY_DIR`; with neither, only origin is grepped). A claim whose
terms never co-occur in one file is UNIQUE, and the worktree is `SKIP-SALVAGE`d with the
claims printed. Capped (300 commits, 60 claims, stops at 8 finds), deterministic, no LLM, so
it over-flags prose (cover letters read as unique): judge the list, then file or force.
Uncommitted files stay governed by `--capture-dirty`.

**Filing the finds.** The script has no tracker or artifact access, so there is no
`--file-tracker` flag. It prints a `--- SALVAGE-ROWS BEGIN (jsonl) ---` ... `END` block, one
JSON row per claim (`worktree, branch, sha, file, claim, terms`). The calling session files
those rows (tech-debt tracker via ArtifactData, memory, or backlog), then reruns prune with
`--force-salvage-ok`.

## Guardrails
- **Human authorization per prune batch.** Survey and inspect freely; never remove worktrees
  without an explicit go. NOTOMATION: the sweep induces the decision, it does not make it.
- **Always `--protect`** branches holding the only copy of unmerged work.
- **Verify, never trust exit codes** (rule 7). After a spread, confirm with `git ls-files`.
- Skip any worktree with a live session mid-task; check `/tasks` and running agents first.
- Report honestly: say how many were skipped and why, not just how many were reclaimed.
