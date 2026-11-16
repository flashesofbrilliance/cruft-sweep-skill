#!/usr/bin/env python3
"""cruft_sweep — survey, classify, and safely reclaim stale git worktrees.

Extracted 2026-08-29 from a live 68-worktree sweep. Every rule below is priced tuition:
a naive version of this script gets each one WRONG.

  1. `ahead` count is MEANINGLESS as a staleness signal. A history rewrite (filter-repo)
     inflates every pre-rewrite branch to ahead=700-1047 of pure ghost commits.
  2. Classify by BLOB CONTENT, not path. Renames (e.g. the ADR collision remap 024->058)
     make identical content look like "unique work" under a path-only diff.
  3. A background hook continuously re-dirties _INBOX/*.jsonl and _STATE/snapshot.json,
     so `git merge` (which checks tree cleanliness) loses a race and refuses to start.
     Those files are REGENERABLE - exclude them from every dirty calculation.
  4. `git checkout <ref> -- a b c` aborts the WHOLE checkout if ANY path is absent in
     that ref. Use `git show <ref>:<path>` (immutable blob read) to deliver files.
  5. NEVER `git stash push/pop` - the stash stack is shared across all worktrees.
     `git stash create` is safe BUT does not capture untracked files; to capture
     everything use a WIP commit (`git add -A && git commit`).
  6. Bundle BEFORE removing. `git bundle create <file> <branch>` (positional ref, not
     --branches=glob which refuses "empty bundle"). Restore: git clone <file>.
  7. NEVER trust a command's exit code as proof. Verify with `git ls-files <file>`.
  8. A path on the branch but absent from base is NOT automatically un-landed work: base may
     have DELETED it deliberately (e.g. `sec(warroom): migrate briefs behind admin auth`
     removed public/*.html internal briefs). Restoring those would UNDO a security fix.
     Check `git log <base> --diff-filter=D -- <path>`; if base deleted it, treat as ghost.
  9. Resolve each worktree's OWN head before bundling. `rev-parse --abbrev-ref HEAD` on a
     detached worktree returns the literal "HEAD", and `bundle create <file> HEAD` run in the
     main repo then bundles the MAIN checkout; a worktree whose .git file is gone falls
     through to the enclosing main checkout the same way. Prune now refuses (exit 2) unless
     the dir is a listed worktree with its own GIT_DIR and the bundled tip == its HEAD;
     detached heads are pinned under refs/cruft-archive/<name>-<sha8> (kept permanently so gc
     never drops the commit). `--self-test` reproduces both.
 10. Bundles are INCREMENTAL: `bundle create <f> <ref> ^origin/<base>`. Full-history bundles
     were ~150MB each and 183 of them (11GB) ate all the disk the pruning freed (2026-10-05).
     A tip already on origin/<base> needs no bundle: its sha goes to MANIFEST.tsv only.
 11. Compare against origin/<base>, not the local <base> branch, which lags. A stale local
     main made worktrees cut from origin/main show dozens of "unique" files (2026-10-05).
     `--base` still wins when given a remote-qualified ref; a bare name prefers origin/<name>.
 12. A bundle is a backup, not a salvage. Archiving a branch whose commits carry a decision,
     rule or number that exists nowhere on origin/<base> or in memory buries it where no
     session will look. Prune runs a SALVAGE PREFLIGHT first: commits not on base by
     patch-id (`git cherry`), claim-shaped lines added to .md/.json in them, key terms
     grepped across origin/<base> + the memory dir. Any uncovered claim -> SKIP-SALVAGE
     unless --force-salvage-ok. Deterministic, capped, no LLM; `salvage` runs it alone.

Verdicts:
  SAFE    - no uncommitted work AND no branch-unique content blobs -> bundle+prune ok
  REVIEW  - has uncommitted work or unique content -> human decides
  PROTECT - on the protect list -> never touched

Usage:
  cruft_sweep.py survey                     # read-only table (default)
  cruft_sweep.py survey --json              # machine-readable
  cruft_sweep.py inspect <worktree>         # per-file DUP/DIFFERS/UNIQUE for its dirt
  cruft_sweep.py spread --files a,b         # deliver files from main to all worktrees
  cruft_sweep.py prune <wt> [<wt>...]       # salvage->capture->bundle->verify->remove (SAFE
                                            #   only unless --capture-dirty is passed)
  cruft_sweep.py salvage <wt> [<wt>...]     # rule 12 preflight alone (report-only)
  cruft_sweep.py --self-test                # frustration gate for rule 9
Flags: --repo PATH --base main --bundles DIR --protect a,b --capture-dirty --dry-run
       --memory DIR --force-salvage-ok
"""
import argparse, json, os, re, subprocess, sys

# Paths a background hook keeps re-dirtying (rule 3); set CRUFT_SWEEP_REGENERABLE="a/,b.json" for yours.
REGENERABLE = tuple(x for x in os.environ.get("CRUFT_SWEEP_REGENERABLE", "").split(",") if x)
CONTENT_RE = ("src/", "docs/", "scripts/", "lib/", "app/")


# Inherited GIT_DIR / GIT_WORK_TREE (set inside hooks) override -C and would point every
# call at one repo regardless of the worktree we name. Never let them leak in.
GIT_ENV = {k: v for k, v in os.environ.items()
           if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR")}


def git(repo, *args, check=False):
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, env=GIT_ENV)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()


def is_regenerable(path):
    return any(path.startswith(p) or p in path for p in REGENERABLE)


def real_dirty(wt):
    """Uncommitted files, EXCLUDING regenerable hook state (rule 3)."""
    out = git(wt, "status", "--porcelain")
    return [l[3:] for l in out.splitlines() if l and not is_regenerable(l[3:])]


def deleted_from_base(repo, base, path):
    """True if base deliberately deleted this path (rule 8) - ghost, not lost work."""
    return bool(git(repo, "log", base, "--oneline", "-1", "--diff-filter=D", "--", path))


def content_blobs(repo, ref):
    """{blob_sha: [paths]} for deliverable content only (rule 2: compare by content)."""
    out = git(repo, "ls-tree", "-r", ref)
    blobs = {}
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4 and parts[1] == "blob":
            path = parts[3]
            if any(path.startswith(p) for p in CONTENT_RE):
                blobs.setdefault(parts[2], []).append(path)
    return blobs


def worktrees(repo):
    out, cur = [], {}
    for line in git(repo, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            cur = {"path": line.split(" ", 1)[1]}
        elif line.startswith("branch "):
            cur["branch"] = line.split(" ", 1)[1].replace("refs/heads/", "")
        elif line == "detached":
            cur["branch"] = ""  # survey must see detached worktrees too (rule 9)
        elif line == "" and cur.get("path"):
            if "branch" in cur:
                out.append(cur)
            cur = {}
    if "branch" in cur:
        out.append(cur)
    return out


def survey(a):
    base_map = content_blobs(a.repo, a.base)
    base_blobs = set(base_map)
    base_paths = {pth for paths in base_map.values() for pth in paths}
    protect = set(x for x in (a.protect or "").split(",") if x)
    rows = []
    for wt in worktrees(a.repo):
        path, br = wt["path"], wt.get("branch") or "(detached)"
        if os.path.realpath(path) == os.path.realpath(a.repo):
            continue
        name = os.path.basename(path)
        try:
            dirty = real_dirty(path)
            uniq_paths = []
            for sha, paths in content_blobs(path, "HEAD").items():
                if sha in base_blobs:
                    continue  # identical content exists on base (rule 2: renames)
                for pth in paths:
                    if pth in base_paths:
                        continue  # base has this path with different content = stale version
                    if deleted_from_base(a.repo, a.base, pth):
                        continue  # rule 8: base deliberately removed it = ghost
                    uniq_paths.append(pth)
            behind = git(path, "rev-list", "--count", f"HEAD..{a.base}")
        except Exception as e:
            rows.append({"name": name, "branch": br, "verdict": "ERROR", "note": str(e)[:60]})
            continue
        if br in protect or name in protect:
            v = "PROTECT"
        elif dirty or uniq_paths:
            v = "REVIEW"
        else:
            v = "SAFE"
        rows.append({"name": name, "branch": br, "verdict": v, "dirty": len(dirty),
                     "unique": len(uniq_paths), "behind": behind,
                     "unique_sample": uniq_paths[:3]})
    return rows


def cmd_survey(a):
    rows = survey(a)
    if a.json:
        print(json.dumps(rows, indent=2)); return
    order = {"SAFE": 0, "REVIEW": 1, "PROTECT": 2, "ERROR": 3}
    rows.sort(key=lambda r: (order.get(r["verdict"], 9), r["name"]))
    print(f"{'WORKTREE':<44}{'VERDICT':<9}{'dirty':<7}{'uniq':<6}{'behind':<8}unique-sample")
    for r in rows:
        s = ",".join(r.get("unique_sample", []))[:56]
        print(f"{r['name']:<44}{r['verdict']:<9}{r.get('dirty',''):<7}"
              f"{r.get('unique',''):<6}{r.get('behind',''):<8}{s}")
    tally = {}
    for r in rows:
        tally[r["verdict"]] = tally.get(r["verdict"], 0) + 1
    print("\n" + "  ".join(f"{k}={v}" for k, v in sorted(tally.items())))


def cmd_inspect(a):
    """Per-file keep-vs-archive classification of a worktree's uncommitted work."""
    wt = a.target if os.path.isdir(a.target) else os.path.join(a.repo, ".claude/worktrees", a.target)
    for f in real_dirty(wt):
        f = f.rstrip("/")
        on_base = subprocess.run(["git", "-C", a.repo, "cat-file", "-e", f"{a.base}:{f}"],
                                 capture_output=True).returncode == 0
        if not on_base:
            print(f"  UNIQUE          {f}")
        else:
            local = git(wt, "hash-object", os.path.join(wt, f)) if os.path.isfile(os.path.join(wt, f)) else ""
            base = git(a.repo, "rev-parse", f"{a.base}:{f}")
            print(f"  {'DUP-IDENTICAL' if local == base else 'ON-BASE-DIFFERS':<15} {f}")


def cmd_spread(a):
    """Deliver files from base to every worktree via immutable blob read (rules 4)."""
    files = [f for f in a.files.split(",") if f]
    for wt in worktrees(a.repo):
        path = wt["path"]
        if os.path.realpath(path) == os.path.realpath(a.repo):
            continue
        got = 0
        for f in files:
            blob = subprocess.run(["git", "-C", a.repo, "show", f"{a.base}:{f}"],
                                  capture_output=True)
            if blob.returncode != 0:
                continue
            dest = os.path.join(path, f)
            if a.dry_run:
                got += 1; continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as fh:
                fh.write(blob.stdout)
            git(path, "add", f)
            got += 1
        if got and not a.dry_run:
            subprocess.run(["git", "-C", path, "commit", "-q", "-o", *files,
                            "-m", "chore(doctrine): spread shared files from base [NCI:SKIP]"],
                           capture_output=True)
        # rule 7: verify, never trust exit codes
        ok = sum(1 for f in files if git(path, "ls-files", f))
        print(f"{os.path.basename(path):<44} delivered={got} verified={ok}/{len(files)}")


def resolve_base(repo, base):
    """Prefer the remote copy of base (rule 11). The local branch is whatever some worktree
    last pulled; on 2026-10-05 a stale local `main` made 46 files that origin/main already
    carried read as "unique" in every worktree cut from origin/main, so SAFE read as REVIEW."""
    if "/" not in base and git(repo, "rev-parse", "--verify", "-q", f"origin/{base}"):
        return f"origin/{base}"
    return base


def resolve_worktree(repo, wt):
    """Prove `wt` is a live linked worktree of `repo` and return its OWN head (rule 9).

    Returns (head_sha, branch_or_None, None) on success, (None, None, reason) on refusal.
    Every check guards the same failure: a git command run against a directory that is
    not (or no longer) a worktree silently resolves to the enclosing MAIN checkout, because
    .claude/worktrees/<name> lives inside it. That is how five 2026-10-05 prunes bundled
    the main checkout's HEAD instead of each worktree's commit.
    """
    gitfile = os.path.join(wt, ".git")
    if not os.path.isfile(gitfile):
        return None, None, "no .git file (worktree removed/archived?)"
    top = git(wt, "rev-parse", "--show-toplevel")
    if not top or os.path.realpath(top) != os.path.realpath(wt):
        return None, None, f"git resolves to {top or '?'} not to the worktree itself"
    gdir = git(wt, "rev-parse", "--absolute-git-dir")
    common = git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not gdir or os.path.dirname(os.path.realpath(gdir)) != os.path.realpath(os.path.join(common, "worktrees")):
        return None, None, f"GIT_DIR {gdir or '?'} is not under {common}/worktrees"
    listed = {os.path.realpath(l.split(" ", 1)[1])
              for l in git(repo, "worktree", "list", "--porcelain").splitlines()
              if l.startswith("worktree ")}
    if os.path.realpath(wt) not in listed:
        return None, None, "not in `git worktree list`"
    head = git(wt, "rev-parse", "--verify", "-q", "HEAD")
    if len(head) != 40:
        return None, None, "HEAD does not resolve"
    branch = git(wt, "symbolic-ref", "-q", "--short", "HEAD") or None
    return head, branch, None


MEMORY_DIR = os.environ.get("CRUFT_SWEEP_MEMORY_DIR", "")
CLAIM_RE = re.compile(r"\b(decid\w*|decision|rule|must|never|always|todo|fixme|adr-\d+|locked|"
                      r"verdict|threshold|budget)\b|\d{2,}", re.I)
STOP = set("""about above after again against because before being below between could doesn
during every first great having other their there these thing those through under until
where which while would should never always about using without within  into from that this
with will have been were what when your must rule decision decided""".split())
MAX_COMMITS, MAX_CLAIMS, MAX_REPORT = 300, 60, 8


def claim_terms(line):
    """Up to 4 distinctive key terms: multi-digit numbers first, then the longest words."""
    nums = sorted(set(re.findall(r"\b\d{2,}(?:\.\d+)?\b", line)))
    words = sorted({w for w in re.findall(r"[a-z][a-z0-9_-]{4,}", line.lower()) if w not in STOP},
                   key=lambda w: (-len(w), w))
    return (nums + words)[:4]


def salvage(repo, base, head, memdir):
    """Rule 12. Returns (findings, note). A finding is a claim on the branch whose key terms
    never co-occur in one file on `base` nor in one memory file."""
    cherry = git(repo, "cherry", base, head).splitlines()
    shas = [l.split()[1] for l in cherry if l.startswith("+ ")]
    note = f"{len(shas)} commit(s) not on {base} by patch-id"
    if len(shas) > MAX_COMMITS:
        note += f" (checked newest {MAX_COMMITS})"
        shas = shas[-MAX_COMMITS:]
    mem = []
    if memdir and os.path.isdir(memdir):
        for f in sorted(os.listdir(memdir)):
            if f.endswith(".md"):
                with open(os.path.join(memdir, f), errors="replace") as fh:
                    mem.append(fh.read().lower())
    seen, findings, checked = set(), [], 0
    for sha in reversed(shas):  # newest first
        files = [f for f in git(repo, "diff-tree", "--no-commit-id", "-r", "--name-only",
                                "--diff-filter=AM", sha).splitlines() if f.endswith((".md", ".json"))]
        for f in files:
            for l in git(repo, "show", "--format=", "-U0", sha, "--", f).splitlines():
                if not l.startswith("+") or l.startswith("+++"):
                    continue
                line = l[1:].strip()
                if len(line) < 20 or line in seen or not CLAIM_RE.search(line):
                    continue
                seen.add(line)
                terms = claim_terms(line)
                if len(terms) < 2:
                    continue
                checked += 1
                if checked > MAX_CLAIMS:
                    return findings, note + f"; claim cap {MAX_CLAIMS} reached"
                args = ["grep", "-q", "-i", "-F", "--all-match"]
                for t in terms:
                    args += ["-e", t]
                on_base = subprocess.run(["git", "-C", repo, *args, base], capture_output=True,
                                         env=GIT_ENV).returncode == 0
                if on_base or any(all(t in m for t in terms) for m in mem):
                    continue
                findings.append({"sha": sha[:8], "file": f, "claim": line[:200], "terms": terms})
                if len(findings) >= MAX_REPORT:  # enough to decide SKIP; stop paying for greps
                    return findings, note + f"; stopped at {MAX_REPORT} unique claims"
    return findings, note + f"; {checked} claim(s) checked"


def emit_salvage_rows(rows):
    """Machine-readable block for the calling session to file (the script has no tracker access)."""
    if not rows:
        return
    print("--- SALVAGE-ROWS BEGIN (jsonl) ---")
    for r in rows:
        print(json.dumps(r, sort_keys=True))
    print("--- SALVAGE-ROWS END ---")


def cmd_salvage(a):
    rows = []
    for target in a.targets:
        wt = os.path.abspath(target if os.path.isdir(target) else os.path.join(a.repo, ".claude/worktrees", target))
        name = os.path.basename(wt.rstrip("/"))
        head, br, why = resolve_worktree(a.repo, wt)
        if why:
            print(f"REFUSE       {name} ({why})"); continue
        found, note = salvage(a.repo, a.base, head, a.memory)
        dirty = real_dirty(wt)
        print(f"{'UNIQUE' if found else 'COVERED':12} {name} [{br or 'detached'} @ {head[:8]}] {note}"
              + (f"; {len(dirty)} uncommitted file(s)" if dirty else ""))
        for f in found[:MAX_REPORT]:
            print(f"    {f['sha']} {f['file']}: {f['claim']}  terms={','.join(f['terms'])}")
        rows += [{"worktree": name, "branch": br, **f} for f in found]
    emit_salvage_rows(rows)


def bundle_tips(repo, bundle):
    """{refname: sha} as recorded INSIDE the bundle file."""
    out = git(repo, "bundle", "list-heads", bundle)
    return {parts[1]: parts[0] for parts in (l.split() for l in out.splitlines()) if len(parts) == 2}


def cmd_prune(a):
    os.makedirs(a.bundles, exist_ok=True)
    manifest = os.path.join(a.bundles, "MANIFEST.tsv")
    refused = 0
    a.upstream = a.base  # already resolved to origin/<base> in main() (rule 11)
    salvage_rows = []
    for target in a.targets:
        wt = target if os.path.isdir(target) else os.path.join(a.repo, ".claude/worktrees", target)
        wt = os.path.abspath(wt)
        name = os.path.basename(wt.rstrip("/"))
        if not os.path.isdir(wt):
            print(f"SKIP-MISSING {name}"); continue
        head, br, why = resolve_worktree(a.repo, wt)
        if why:
            print(f"REFUSE       {name} (not a valid worktree: {why}) - NOT removed"); refused += 1; continue
        label = br or f"detached {head[:8]}"
        dirty = real_dirty(wt)
        if dirty and not a.capture_dirty:
            print(f"SKIP-DIRTY   {name} ({len(dirty)} files; pass --capture-dirty to WIP-commit)")
            continue
        found, note = salvage(a.repo, a.upstream, head, a.memory)  # rule 12
        if found and not a.force_salvage_ok:
            print(f"SKIP-SALVAGE {name} ({len(found)} unique claim(s); {note}; "
                  f"pass --force-salvage-ok after filing them)")
            for f in found[:MAX_REPORT]:
                print(f"    {f['sha']} {f['file']}: {f['claim']}")
            salvage_rows += [{"worktree": name, "branch": br, **f} for f in found]
            continue
        if a.dry_run:
            print(f"DRY-RUN      {name} [{label} @ {head[:8]}] dirty={len(dirty)}"); continue
        if dirty:  # rule 5: WIP commit captures untracked too, unlike stash create
            git(wt, "add", "-A")
            subprocess.run(["git", "-C", wt, "commit", "-q", "-m",
                            "wip(cruft-sweep): capture uncommitted work before archival [NCI:SKIP]"],
                           capture_output=True, env=GIT_ENV)
            head = git(wt, "rev-parse", "--verify", "-q", "HEAD")
        # Detached HEAD has no branch to bundle: pin the SHA under a permanent
        # refs/cruft-archive/<name>-<sha8> so the bundle carries THIS worktree's commit (never
        # whatever "HEAD" means in the main repo) and gc can never drop it after removal.
        ref = f"refs/heads/{br}" if br else f"refs/cruft-archive/{name}-{head[:8]}"
        landed = subprocess.run(["git", "-C", a.repo, "merge-base", "--is-ancestor", head, a.upstream],
                                capture_output=True, env=GIT_ENV).returncode == 0
        if not br and not landed:  # a landed commit is already kept reachable by upstream
            git(a.repo, "update-ref", ref, head)
        bundle = os.path.join(a.bundles, f"{name}.bundle")
        if landed:  # rule 10: already on upstream, nothing to back up
            bfile = f"on-{a.upstream}"
        else:
            subprocess.run(["git", "-C", a.repo, "bundle", "create", bundle, ref, f"^{a.upstream}"],
                           capture_output=True, env=GIT_ENV)
            verify = subprocess.run(["git", "-C", a.repo, "bundle", "verify", bundle], capture_output=True, env=GIT_ENV)
            if verify.returncode != 0:  # rule 6+7
                print(f"REFUSE       {name} (bundle failed verify) - NOT removed"); refused += 1; continue
            tip = bundle_tips(a.repo, bundle).get(ref)
            if tip != head:  # rule 9: the backup must hold the worktree's own commit
                print(f"REFUSE       {name} (bundled {ref} @ {(tip or 'none')[:8]} != worktree HEAD "
                      f"{head[:8]}) - NOT removed"); refused += 1; continue
            bfile = f"{name}.bundle"
        rm = subprocess.run(["git", "-C", a.repo, "worktree", "remove", "--force", wt],
                            capture_output=True, env=GIT_ENV)
        if rm.returncode == 0:
            with open(manifest, "a") as fh:
                # Only name a ref that exists: a landed detached head is never pinned.
                rcol = br or (ref if not landed else "(detached)")
                fh.write(f"{name}\t{rcol}\t{head[:8]}\t{bfile}\n")
            print(f"PRUNED       {name} [{label} @ {head[:8]}] -> {bfile} ({ref})")
        else:
            print(f"SKIP-REMFAIL {name} (bundle kept, worktree intact)")
    subprocess.run(["git", "-C", a.repo, "worktree", "prune"], capture_output=True, env=GIT_ENV)
    emit_salvage_rows(salvage_rows)
    if refused:
        print(f"{refused} worktree(s) REFUSED - see above", file=sys.stderr)
        sys.exit(2)


def self_test(script):
    """Frustration gate (AGENTS.md #15): reproduce the 2026-10-05 wrong-HEAD bundles.

    Builds a throwaway repo whose MAIN checkout sits on a different commit from three
    worktrees (branch / detached / .git removed) and runs `<script> prune` on them.
    Pass `--self-test-against OLD.py` to prove the assertions fail on the buggy version.
    """
    import shutil, tempfile
    tmp = tempfile.mkdtemp(prefix="cruft-selftest-")
    repo, bundles = os.path.join(tmp, "repo"), os.path.join(tmp, "bundles")
    fails = []

    def sh(cwd, *args):
        r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, env=GIT_ENV)
        if r.returncode:
            raise RuntimeError(f"git {' '.join(args)}: {r.stderr}")
        return r.stdout.strip()

    def commit(cwd, msg):
        with open(os.path.join(cwd, f"{msg}.txt"), "w") as fh:
            fh.write(msg)
        sh(cwd, "add", "-A"); sh(cwd, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", msg)
        return sh(cwd, "rev-parse", "HEAD")

    try:
        os.makedirs(repo); sh(repo, "init", "-q", "-b", "main"); commit(repo, "base")
        with open(os.path.join(repo, "history.bin"), "wb") as fh:
            fh.write(os.urandom(2 * 1024 * 1024))  # incompressible base history a full bundle drags in
        commit(repo, "heavy-history")
        with open(os.path.join(repo, ".git", "info", "exclude"), "a") as fh:
            fh.write(".claude/\n")  # main checkout stays clean around worktrees
        wts = os.path.join(repo, ".claude", "worktrees")
        cases = {}
        sh(repo, "worktree", "add", "-q", "-b", "br-landed", os.path.join(wts, "landed"))
        sh(repo, "worktree", "add", "-q", "--detach", os.path.join(wts, "landed-detached"))
        landed_head = sh(repo, "rev-parse", "HEAD")
        for name in ("good", "detached", "nogit"):
            sh(repo, "worktree", "add", "-q", "-b", f"br-{name}", os.path.join(wts, name))
            cases[name] = commit(os.path.join(wts, name), f"work-{name}")
        sh(os.path.join(wts, "detached"), "checkout", "-q", "--detach")
        sh(repo, "branch", "-q", "-D", "br-detached")  # only the detached HEAD holds it now
        os.remove(os.path.join(wts, "nogit", ".git"))
        main_head = commit(repo, "main-moves-on")
        # Rule 11 fixture: origin/main carries a file the local main lacks; a worktree cut from
        # origin/main must still survey SAFE, and a detached worktree must appear in the table.
        sh(repo, "checkout", "-q", "-b", "upstream-tmp")
        os.makedirs(os.path.join(repo, "src"), exist_ok=True)
        with open(os.path.join(repo, "src", "landed.ts"), "w") as fh:
            fh.write("export const landed = 1\n")
        os.makedirs(os.path.join(repo, "docs"), exist_ok=True)
        known = "Decision: the quokka ledger keeps 37 shards behind the marmalade gateway.\n"
        with open(os.path.join(repo, "docs", "known.md"), "w") as fh:
            fh.write(known)
        sh(repo, "add", "-A"); sh(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "upstream")
        sh(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        sh(repo, "checkout", "-q", "main"); sh(repo, "branch", "-q", "-D", "upstream-tmp")
        sh(repo, "worktree", "add", "-q", "-b", "br-fresh", os.path.join(wts, "fresh"), "origin/main")
        sh(repo, "worktree", "add", "-q", "--detach", os.path.join(wts, "loose"), "origin/main")
        # Rule 12 fixtures: one branch adds a decision found nowhere else, one restates a decision
        # origin/main already carries (different patch, so `git cherry` still marks it '+').
        memdir = os.path.join(tmp, "memory"); os.makedirs(memdir)
        for name, text in (("salv-unique", "Decision: route all kumquat telemetry through the zephyr relay by 2027.\n"),
                           ("salv-onmain", "Recap. " + known)):
            p = os.path.join(wts, name)
            sh(repo, "worktree", "add", "-q", "-b", f"br-{name}", p, "origin/main")
            with open(os.path.join(p, f"{name}.md"), "w") as fh:
                fh.write(text)
            sh(p, "add", "-A"); sh(p, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", name)
        sv = subprocess.run([sys.executable, script, "survey", "--json", "--repo", repo],
                            capture_output=True, text=True, env=GIT_ENV)
        try:
            rows = {r["name"]: r for r in json.loads(sv.stdout)}
        except ValueError:
            rows = {}

        r = subprocess.run([sys.executable, script, "prune", "good", "detached", "nogit", "landed", "landed-detached",
                            "--repo", repo, "--bundles", bundles, "--memory", memdir],
                           capture_output=True, text=True, env=GIT_ENV)
        out = r.stdout + r.stderr

        def check(ok, msg):
            print(("PASS " if ok else "FAIL ") + msg)
            if not ok:
                fails.append(msg)

        for name in ("good", "detached"):
            b = os.path.join(bundles, f"{name}.bundle")
            tips = bundle_tips(repo, b) if os.path.exists(b) else {}
            check(cases[name] in tips.values(), f"{name}: bundle holds worktree commit {cases[name][:8]}")
            check(main_head not in tips.values(), f"{name}: bundle does NOT hold main HEAD {main_head[:8]}")
            size = os.path.getsize(b) if os.path.exists(b) else 0
            check(0 < size < 256 * 1024, f"{name}: 1-commit bundle is incremental ({size} bytes < 256KB)")
        pins = sh(repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/cruft-archive/")
        check(f"refs/cruft-archive/detached-{cases['detached'][:8]} {cases['detached']}" in pins,
              "detached: commit pinned under refs/cruft-archive/<name>-<sha8> after prune")
        check(not os.path.exists(os.path.join(bundles, "landed.bundle")), "landed: tip on base, no bundle written")
        check("refs/cruft-archive/landed-detached-" not in pins, "landed-detached: no redundant pin for a commit on base")
        man = open(os.path.join(bundles, "MANIFEST.tsv")).read() if os.path.exists(os.path.join(bundles, "MANIFEST.tsv")) else ""
        check(f"landed\tbr-landed\t{landed_head[:8]}\ton-" in man, "landed: sha recorded in MANIFEST.tsv")
        rows_man = [l.split("\t") for l in man.splitlines() if l.count("\t") == 3]
        dangling = [r[1] for r in rows_man if r[1].startswith("refs/")
                    and not sh(repo, "for-each-ref", r[1])]
        check(not dangling, f"manifest names only refs that exist (dangling: {dangling})")
        check(any(r[0] == "landed-detached" and r[1] == "(detached)" for r in rows_man),
              "landed-detached: manifest ref column is (detached)")
        check(not os.path.exists(os.path.join(bundles, "nogit.bundle")), "nogit: no bundle written")
        check(os.path.isdir(os.path.join(wts, "nogit")), "nogit: directory left in place")
        check(r.returncode != 0, f"exit nonzero when a worktree is refused (got {r.returncode})")
        check(main_head[:7] not in out, "output never reports main HEAD as a worktree tip")
        check(rows.get("fresh", {}).get("verdict") == "SAFE",
              f"survey: worktree cut from origin/main is SAFE despite stale local main "
              f"(got {rows.get('fresh', {}).get('verdict')}, unique={rows.get('fresh', {}).get('unique')})")
        check("loose" in rows, "survey: detached worktree is listed")
        sr = subprocess.run([sys.executable, script, "prune", "salv-unique", "salv-onmain", "--repo", repo,
                             "--bundles", bundles, "--memory", memdir], capture_output=True, text=True, env=GIT_ENV)
        sout = sr.stdout + sr.stderr
        check("SKIP-SALVAGE salv-unique" in sout and "kumquat" in sout,
              "salvage: branch with a unique decision is skipped and the claim is named")
        check(os.path.isdir(os.path.join(wts, "salv-unique")), "salvage: unique branch's worktree left in place")
        check("SALVAGE-ROWS BEGIN" in sout and '"worktree": "salv-unique"' in sout,
              "salvage: machine-readable rows block emitted")
        check("PRUNED       salv-onmain" in sout, "salvage: branch whose content is already on main is pruned")
        rep = subprocess.run([sys.executable, script, "salvage", "salv-unique", "--repo", repo, "--memory", memdir],
                             capture_output=True, text=True, env=GIT_ENV).stdout
        check(rep.startswith("UNIQUE") and os.path.isdir(os.path.join(wts, "salv-unique")),
              "salvage subcommand: reports UNIQUE and removes nothing")
        fr = subprocess.run([sys.executable, script, "prune", "salv-unique", "--force-salvage-ok", "--repo", repo,
                             "--bundles", bundles, "--memory", memdir], capture_output=True, text=True, env=GIT_ENV)
        check("PRUNED       salv-unique" in fr.stdout, "salvage: --force-salvage-ok overrides the skip")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"self-test: {'FAIL' if fails else 'PASS'} ({len(fails)} failing)")
    return 1 if fails else 0


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("cmd", choices=["survey", "inspect", "spread", "prune", "salvage"], nargs="?", default="survey")
    p.add_argument("targets", nargs="*")
    p.add_argument("--repo", default=os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd()))
    p.add_argument("--base", default="main")
    p.add_argument("--bundles", default=os.path.expanduser("~/worktree-bundles"))
    p.add_argument("--protect", default="")
    p.add_argument("--files", default="")
    p.add_argument("--json", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--capture-dirty", action="store_true")
    p.add_argument("--memory", default=MEMORY_DIR, help="memory dir grepped by the salvage preflight")
    p.add_argument("--force-salvage-ok", action="store_true", help="prune even when salvage finds unique claims")
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--self-test-against", default="", help="run the self-test against another copy")
    a = p.parse_args()
    if a.self_test or a.self_test_against:
        sys.exit(self_test(os.path.abspath(a.self_test_against or __file__)))
    # repo must be the MAIN checkout for worktree listing
    a.repo = git(a.repo, "rev-parse", "--path-format=absolute", "--git-common-dir").replace("/.git", "") or a.repo
    a.base = resolve_base(a.repo, a.base)
    if a.cmd == "inspect":
        a.target = a.targets[0] if a.targets else sys.exit("inspect needs a worktree")
        cmd_inspect(a)
    elif a.cmd == "spread":
        if not a.files: sys.exit("spread needs --files a,b,c")
        cmd_spread(a)
    elif a.cmd == "salvage":
        if not a.targets: sys.exit("salvage needs worktree name(s)")
        cmd_salvage(a)
    elif a.cmd == "prune":
        if not a.targets: sys.exit("prune needs worktree name(s)")
        cmd_prune(a)
    else:
        cmd_survey(a)


if __name__ == "__main__":
    main()
