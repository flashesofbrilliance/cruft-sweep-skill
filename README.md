# cruft-sweep-skill

Survey, classify and safely reclaim stale git worktrees in a many-worktree repo, and spread shared files across them. Reversible by construction: every prune writes a verified `git bundle` first, and a salvage preflight flags claim-shaped lines that exist nowhere else before anything is removed.

## Install
```
/plugin marketplace add flashesofbrilliance/arcs-skills
/plugin install cruft-sweep@arcs
```
Then ask to "clean up worktrees", or run the engine directly:
```
python3 scripts/cruft_sweep.py survey --protect main
python3 scripts/cruft_sweep.py prune <worktree> --dry-run
python3 scripts/cruft_sweep.py --self-test
```
Stdlib Python 3 and git only. Optional environment: `CRUFT_SWEEP_MEMORY_DIR` (a notes folder the salvage check also greps), `CRUFT_SWEEP_REGENERABLE` (comma list of paths a hook keeps re-dirtying). Never removes worktrees without an explicit go per batch. MIT.
