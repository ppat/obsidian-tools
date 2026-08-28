# local-replicator: install and uninstall

An operator runbook for the one component in this whole design that runs outside the cluster
(`DESIGN.md` "Architecture", item 10; `docs/DESIGN.md` §2 item 10, §4 Plane B) — a launchd job on
the operator's Mac, installed by hand on one machine, not applied by Flux. This file is operational,
not design: it belongs beside [`gui-access.md`](./gui-access.md) in this directory's two-tier split
(design of record in `DESIGN.md`; one-time and ongoing operator procedures here) — see
[`README.md`](./README.md) for that split. It carries no authority over `docs/DESIGN.md`.

Everything here assumes `obsidian-tools` is already installed as a console script (`pip install`,
`pipx install`, or `uv tool install` from this repository, or from a built wheel/artifact) and
runnable as `obsidian-tools replicate`. Packaging that installation itself is out of scope for this
file — see the repository root [`README.md`](../README.md) for building/installing the package.

## Prerequisites this component depends on and cannot enforce

Nothing in `obsidian-tools replicate` checks any of these. Getting one wrong produces a job that
runs, appears to succeed, and quietly corrupts or stalls the device replica — there is no error
message pointing back here.

1. **Turn off "Optimize Mac Storage"** — System Settings → Apple ID → iCloud → iCloud Drive →
   "Optimize Mac Storage" (the exact path varies slightly by macOS version; search Settings for
   "Optimize Mac Storage" if it isn't where expected). With it **on**, iCloud evicts file content
   it judges cold and leaves a dataless `.icloud` placeholder stub in its place — a real file, on
   disk, that is not the file. This process's overlay step (`obsidian_tools/local_replicator/rsync_ops.py`)
   and the `git diff` that follows it both read whatever currently sits at every path in the iCloud
   vault directory — a read against a dataless stub can hang rather than fail cleanly, which the
   retry/backoff machinery this codebase uses elsewhere for the in-cluster NFS mount does not paper
   over here, because a hang is not the same failure shape as an error. This is also one of the
   design's own three deliberately unresearched residual questions (`docs/DESIGN.md` §4 Plane B):
   whether "Optimize Mac Storage" off is *sufficient* on its own has never been confirmed by
   anything but running it — treat this prerequisite as necessary, not as a guarantee.

2. **Locate the iCloud vault path.** The vault both Obsidian apps actually open lives at
   `iCloud Drive/Obsidian/<Vault Name>`, which resolves on disk to:

   ```text
   ~/Library/Mobile Documents/com~apple~CloudDocs/Obsidian/<Vault Name>
   ```

   `<Vault Name>` is whatever the vault was named when it was first opened in Obsidian on this
   Mac — there is no way to derive it from anything this codebase knows, which is why
   `ICLOUD_VAULT_DIR` has no default (`obsidian_tools/config.py`'s `ReplicateConfig`) and the
   plist template below has a placeholder for it rather than a guess. If the directory doesn't
   exist yet, open Obsidian on the Mac first and create/open the vault at that iCloud location —
   this process publishes *into* that directory, it does not create the vault identity itself.

3. **A read-only SSH deploy key for the vault's git remote.** Generate a dedicated key — never
   reuse the in-cluster git committer's read-write key here:

   ```sh
   ssh-keygen -t ed25519 -f ~/.ssh/obsidian_vault_readonly -N "" -C "local-replicator (read-only)"
   ```

   Register the **public** key as a read-only deploy key on the vault's GitHub repository (repo
   Settings → Deploy keys → Add deploy key; leave "Allow write access" unchecked). This process
   only ever fetches — it never pushes (`obsidian_tools/local_replicator/clone.py` has no push
   call anywhere) — so a write-capable key here would be unused privilege, not a convenience.

4. **`~/.ssh/known_hosts` must already trust `github.com`.** `build_ssh_command`
   (`obsidian_tools/vault_git/ssh.py`) sets `StrictHostKeyChecking=yes` and `BatchMode=yes` against
   whatever's already in that file — unlike the in-cluster git committer, `replicate` never
   assembles its own `known_hosts` (`obsidian_tools/vault_git/known_hosts.py`'s
   `assemble_known_hosts` runs only from `commands/commit.py`); this process trusts the file as it
   finds it, and `BatchMode=yes` means it can never fall back to an interactive prompt to fix that
   itself. Populate it once, ahead of the install step below, verifying the fingerprint against
   [GitHub's own published SSH key fingerprints](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints)
   rather than trusting it blind:

   ```sh
   ssh-keyscan github.com >> ~/.ssh/known_hosts
   ```

   Getting this wrong doesn't fail loudly. The first fetch exhausts its retries
   (`obsidian_tools/retry.py`) and raises `RetryExhaustedError`, which is not one of the exceptions
   `commands/replicate.py`'s `run()` catches (`_CYCLE_FAILURES` is `(GitCommandError, RsyncError)`
   only) — so it surfaces as an uncaught Python traceback in `local-replicator.err.log`, not as a
   `"cycle_failed"` line in the structured JSON log. `local-replicator.log` looks like nothing ran
   at all; the actual failure is only in the `.err.log` file beside it.

5. **`obsidian-tools` installed from a released tag, and on `PATH` (or its full absolute path known
   ahead of the install step below)** — launchd does not run inside a login shell, so it does not
   read `~/.zshrc`/`~/.zprofile` and will not find a `uv tool`/`pipx`-installed script unless the
   plist's `PATH` or `ProgramArguments` names it explicitly.

   Any of `pip install`, `pipx install`, or `uv tool install` against a released tag of this
   repository works — this Mac is running the tool, not developing it, so a tagged release rather
   than an editable checkout. With [`mise`](https://mise.jdx.dev/)'s `pipx` backend (shells out to
   `uv` when the operator's global `mise` config sets `[settings.pipx] uvx = true`, to `pipx`
   itself otherwise):

   ```sh
   mise use pipx:ppat/obsidian-tools@v0.4.0
   ```

   Whichever method installs it, the resulting binary path is **version-scoped** — mise's `pipx`
   backend, for example, resolves to
   `~/.local/share/mise/installs/pipx-ppat-obsidian-tools/<version>/bin/obsidian-tools`, not a
   path that stays stable across an upgrade. That matters for the plist below: `ProgramArguments`
   takes a literal absolute path, so upgrading `obsidian-tools` later means updating both plists
   with the new version's path, not just re-running the install command. Find the path actually in
   use with:

   ```sh
   command -v obsidian-tools
   ```

6. **Set the Tasks plugin's task format, by hand, on every device this vault reaches** — Obsidian
   Settings → Tasks → Task format = **Dataview** (`taskFormat: "dataview"`), on this Mac and on
   every iPhone/iPad that opens the vault. This one cannot ride in on the `.obsidian/` baseline the
   way the vault's other locked settings do: the Tasks plugin stores it in
   `.obsidian/plugins/obsidian-tasks-plugin/data.json`, and the baseline allowlist withholds *every*
   plugin's `data.json` categorically, because that filename is also where a plugin keeps its
   credentials — the Local REST API bearer token included
   (`obsidian_tools/vault_git/baseline_selector.py`, `ppat/obsidian-tools#3`). Carving out one
   plugin's `data.json` would turn that categorical rule into a per-plugin judgement call, which is
   the mistake the rule exists to prevent — so this setting is set by hand per device instead, and a
   freshly seeded device does not arrive with it.

   Left unset the plugin defaults to the emoji format, while the vault mandates the bracket
   inline-field form (the vault's own `CLAUDE.md` section 11; `docs/settings-lock.md` "Tasks", which
   also records the minimum Obsidian version every device needs for that format to render). Nothing
   reports the mismatch: a task written on that device carries the wrong format, this component's
   drift capture publishes the line into the vault as ordinary human-authored content, and mixed
   formats are terminal — there is no converter, upstream closed mixed-format support
   `not_planned`, and some tasks in a mixed vault simply stop parsing. Set it on every device or on
   none.

## Install

This installs **two** LaunchAgents: the replication cycle itself (`replicate`), and a separate one
for the spool drainer (`drain`) — deliberately decoupled from the cycle's own schedule
(`docs/DESIGN.md` §2 item 10, §4 Plane B). In Phase 2 the drainer only prevents the spool directory
from growing without bound (it discards what it drains — `ppat/obsidian-tools#4` is where that
changes); it is still real, running code, not something you can skip installing.

1. Confirm every item in "Prerequisites" above.

2. Copy both plist templates out of this repository and fill in the placeholders. There is no
   installer script — the substitution is a handful of values per file, and a copy-then-edit is
   harder to get subtly wrong than a script silently defaulting one of them:

   ```sh
   mkdir -p ~/Library/LaunchAgents
   cp packaging/launchd/com.homelab-ops.obsidian-tools.local-replicator.plist.template \
     ~/Library/LaunchAgents/com.homelab-ops.obsidian-tools.local-replicator.plist
   cp packaging/launchd/com.homelab-ops.obsidian-tools.local-replicator-drain.plist.template \
     ~/Library/LaunchAgents/com.homelab-ops.obsidian-tools.local-replicator-drain.plist
   ```

   Open both copies in an editor and replace every `__PLACEHOLDER__` token:

   | Token | Replace with | Which file(s) |
   | --- | --- | --- |
   | `__HOME_DIR__` | Absolute path to your home directory (`echo $HOME`) — plists cannot expand `$HOME` or `~` themselves; every occurrence must be the literal path. | both |
   | `__OBSIDIAN_TOOLS_BIN__` | Absolute path from `command -v obsidian-tools` (Prerequisites, item 5). | both |
   | `__ICLOUD_VAULT_DIR__` | The full path from Prerequisites item 2, e.g. `/Users/<you>/Library/Mobile Documents/com~apple~CloudDocs/Obsidian/BRAIN`. | `local-replicator.plist` only |
   | `__GIT_REMOTE_ORIGIN_URL__` | The vault's git SSH remote, e.g. `git@github.com:ppat/obsidian-vault.git`. | `local-replicator.plist` only |

   `LOCAL_REPLICATOR_SPOOL_DIR` has a sensible default
   (`~/Library/Application Support/obsidian-tools/local-replicator/spool`) and is set explicitly to
   the same value in both files rather than left to each process's own default — the two jobs share
   one spool directory by construction, and an operator who ever needs to relocate it only has to
   get it right in one place if both files already agree.

3. Create the log directory both plists write to (launchd does not create parent directories for
   `StandardOutPath`/`StandardErrorPath` — a missing directory silently drops every log line
   rather than erroring):

   ```sh
   mkdir -p ~/Library/Logs/obsidian-tools
   ```

4. Load both:

   ```sh
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.homelab-ops.obsidian-tools.local-replicator.plist
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.homelab-ops.obsidian-tools.local-replicator-drain.plist
   ```

   `RunAtLoad` is set on both, so this also triggers an immediate first run of each rather than
   waiting a full `StartInterval`.

5. Confirm they're running and check the first run's logs:

   ```sh
   launchctl print gui/$(id -u)/com.homelab-ops.obsidian-tools.local-replicator
   launchctl print gui/$(id -u)/com.homelab-ops.obsidian-tools.local-replicator-drain
   tail -f ~/Library/Logs/obsidian-tools/local-replicator.log ~/Library/Logs/obsidian-tools/local-replicator-drain.log
   ```

   Each line is a JSON object (`obsidian_tools/logging_config.py`). For the replication cycle, look
   for `"event": "cycle_complete"` and confirm `"checkout"` is non-null after the first successful
   run. A first cycle publishes the whole vault unconditionally (there is no prior baseline to
   compare against yet — `docs/DESIGN.md` §4 Plane B, "Losing the Mac clone loses the baseline"
   describes the same re-baselining behaviour for a lost cache), so expect it to take longer than
   steady-state cycles. For the drainer, look for `"event": "drain_complete"`; in steady state with no
   human edits, `"drained"` should be **zero or close to it**.

   A persistently non-zero count *is* worth investigating rather than shrugging at. `.obsidian/`
   churn — plugin caches, index state — does not reach the spool, despite the device-side detector
   being deliberately unfiltered (`docs/DESIGN.md` §1.5 R2): the vault repository carries a tracked
   `.gitignore` listing `.obsidian/`, and the cycle stages with `git add -A`, which consults ignore
   rules for untracked paths. So only the handful of `.obsidian/` files the bootstrap commit
   actually tracked can ever show as drift, and only when genuinely changed — which is exactly the
   signal wanted, a human having altered a setting on a device.

## Running a cycle by hand

`obsidian-tools replicate` and `obsidian-tools drain` are ordinary commands — running either
directly from a shell (to force a cycle ahead of `StartInterval`, or while debugging) works exactly
like the launchd-triggered run, with one difference worth knowing about: an interactive shell has
an environment, and launchd's doesn't.

If this operator's shell exports its own `GIT_SSH_COMMAND` — say, one built around a personal,
read-write SSH key for other git work — a manually-run cycle inherits it. That is **not** a
problem here: `GitRunner.run` (`obsidian_tools/vault_git/runner.py:281`) sets `GIT_SSH_COMMAND`
into the subprocess environment unconditionally whenever the caller passed one, which `replicate`
always does (`build_ssh_command` in `cycle.py`), so this component's own read-only deploy key wins
regardless of what the shell had exported. Worth knowing anyway, because a green cycle looks
identical either way — there is no log line that names which key was actually used — and because
launchd inherits none of this: a LaunchAgent's `EnvironmentVariables` dict is the entire
environment its job sees, so this hazard is specific to running the command by hand and never
affects the scheduled job.

## Troubleshooting

### `"event": "drift_uncaptured"` — a drift patch that carries no content

Logged at `ERROR`, once per cycle it occurs in, whenever `git diff` produced a patch that doesn't
actually carry what changed for one or more drifted paths
(`obsidian_tools/local_replicator/drift.py`'s `captures_content`) — in practice, a binary landing
in the vault on the device: a picture pasted into a note, a PDF, anything outside the vault's
markdown-only contract (`docs/DESIGN.md` Sec 5, Sec 8b G5). `git diff --cached` emits
`Binary files ... differ` for it — a patch asserting that something changed while carrying none of
it — so this component withholds the path from the spool rather than risk the publish rsync's
`--delete` destroying the only copy of those bytes.

Withholding it also withholds the **whole cycle's** publish and tag advance, exactly like a spool
write failure, not just the offending path (`docs/DESIGN.md` §4 Plane B, "Why the gate moved").
The `cycle_complete` line that follows names the count in `uncaptured`, and `"tag_advanced": false`
confirms nothing was published that cycle; the `cycle_tag_not_advanced` line right before it names
the cause in plain text, distinguishing this from an actual spool write failure.

**Remedy:** delete the offending file from the device's iCloud vault directory (or move it out of
the vault entirely) and let the next scheduled cycle run — there's nothing to fix in this codebase,
since an attachment reaching the vault at all is out-of-contract input, not a bug. Two shapes of
that remedy behave differently, and it matters which one applies: a **deletion** of a file already
tracked in git always resolves cleanly on its own, because the pre-deletion bytes are still in git
at `LAST_CHECKOUT` and the next cycle simply spools the deletion. A **modification** to an
already-tracked binary is not the same — its new bytes exist only on the device, so deleting the
file discards them, not just the drift; there is no way to recover those bytes through this
component, since they were never captured anywhere.

### `"event": "drift_matches_upstream"` — drift whose content the upstream revision already holds

Logged at `INFO`, once per cycle it occurs in. It is **not** a fault, nothing is gated by it, and the
paths it names were spooled like any other drift. What it records is that this cycle found drift
whose content is byte-identical to the upstream revision the clone already knew, and that each of
those spool entries carries that observation (`matches_upstream`, alongside `baseline_sha` and
`upstream_sha` — `obsidian_tools/local_replicator/drift.py`'s `SpoolEntry`).

**It is an observation, not a diagnosis, and it must not be read as one.** At least two quite
different situations produce it, and this component cannot tell them apart:

- A crash in the window between step 6 and step 7 (`ppat/obsidian-tools#36`) — the publish placed
  the fresh tree in iCloud, the process died before `LAST_CHECKOUT` advanced, so the next cycle
  reads everything that commit touched as device-side drift. This is genuinely republished upstream
  content.
- **A wholly ordinary gated cycle.** Step 5 fetches unconditionally while step 6 is gated, so any
  withheld cycle — including the everyday `drift_uncaptured` case above — leaves the
  remote-tracking ref ahead of anything iCloud has seen, and further ahead every cycle the gate
  persists. A human edit that happens to match that ref is then reported here even though it is a
  perfectly real device-side edit.

`baseline_sha != upstream_sha` does **not** separate the two: a gated cycle produces it as readily
as a crash, and gated cycles are far more common. The trap is sharpest for **deletions and
renames**, where no content coincidence is needed at all — a human deleting a note that an agent
independently deleted or renamed upstream is enough.

**Remedy: usually none.** In the common case the cycle self-heals: it publishes and advances the tag
as normal, and the next cycle sees no drift. Two exceptions:

- If this line appears **alongside `drift_uncaptured`**, the cycle is *not* self-healing — it is
  gated, and it will keep re-spooling the same drift until you apply that section's remedy. Act on
  `drift_uncaptured`; this line is a symptom of the same pause.
- If the upstream commit that was republished carried a **binary**, the cycle is wedged rather than
  paused: the binary reads as a device-side creation, `captures_content` refuses it, and every
  subsequent cycle repeats identically. Remove the file from the iCloud vault directory and let the
  next cycle run.

The device does not suppress these entries, because deciding a drifted path is not a human's edit is
a judgement reserved for the server (`docs/DESIGN.md` §1.5 R2), and because a human edit reproducing
upstream is genuinely indistinguishable from residue here. Until `drift-processor` exists (Phase 5)
the drainer discards everything it reads, so the only cost today is a few spool entries.

### A paused cycle re-spools the same drift every cycle until the pause clears

Once a cycle is gated (a spool write failure, or `drift_uncaptured` above), the drift itself is not
remembered between cycles: the next cycle's overlay-and-diff reproduces it from scratch against the
same, still-unmoved `LAST_CHECKOUT` (`docs/DESIGN.md` §4 Plane B). At the default 900-second
interval, a pause left unattended for a day produces on the order of 96 duplicate spool entries for
the same drift, and the drainer picks up every one on its own schedule — there is no deduplication
anywhere in this pipeline. This is bounded, not unbounded: an unresolved *deletion* used to wedge a
cycle permanently (fixed — deletions are now captured, so they resolve on the first retry); what
remains is an ordinary paused cycle's retry cadence, not a runaway.

**Remedy:** resolve whatever is gating the cycle as soon as it's noticed — delete the offending file,
fix the disk that failed a spool write — rather than leaving a pause unattended. Deduplicating the
spool itself is deliberately not built here: there is no consumer of the spool until Phase 5's
`drift-processor` exists (in Phase 2 the drainer discards what it reads, `drainer.py`'s
`discard_sink`), and *where* deduplication would belong — at the spool write, in the drainer, or in
`drift-processor` on receipt — is a decision that belongs with that consumer, not guessed at ahead
of it.

### A device-edited `.obsidian/` file redrifts every cycle and never resolves on its own

"Install" step 5 above already rules out plugin-cache/index-state `.obsidian/` churn as a source of
drift, because it's untracked and `.gitignore`d. A genuine edit to one of the handful of
`.obsidian/` files the bootstrap commit *did* track (a locked setting — see the vault's own
`CLAUDE.md` section 11, `docs/settings-lock.md`) is different, and not in the direction this
component's other drift handling would suggest. `overlay` (`rsync_ops.py`) does not exclude
`.obsidian/` from the device-side comparison — only `.git/` and the narrow
`SHARED_EXCLUDE_LIST` (`exclude.py`) are excluded there — so the edit is picked up, diffed, and
spooled like any other drift. But `publish` (the same module) *does* exclude `.obsidian/` from what
it writes back to the device, once `device_baseline.py` considers the device baselined — that
exclusion is what lets a device keep its own configuration indefinitely (Prerequisites, item 6, and
"Resetting a device" → "The `.obsidian/` baseline only" below both depend on it). Publish therefore never
reconciles the device's copy with the parked baseline, so the same unchanged content is read as
fresh drift again on the very next cycle, and every cycle after that — at the default 900-second
interval, roughly 96 identical spool entries a day, indefinitely, not just for one unlucky run.

Both halves are correct by design on their own terms — `overlay` staying unfiltered is the
device-side detector's deliberate breadth (`docs/DESIGN.md` §1.5 R2), and `publish` excluding
`.obsidian/` is what makes a device's settings sticky once seeded. The pair's asymmetry is tracked
as [`ppat/obsidian-tools#46`](https://github.com/ppat/obsidian-tools/issues/46); it is not something
to work around by hand-editing either function.

**Symptom:** `cycle_complete`'s `"drifted"` count stays non-zero across consecutive cycles for the
same path(s) under `.obsidian/`, with neither `drift_uncaptured` nor `drift_matches_upstream`
alongside it — check the `"drifted"` list itself, not just the count, since a real, changing vault
note drifting on every cycle would look identical from the count alone.

**Remedy:** this is a locked setting, not one the device owns — restore it from the parked
baseline rather than accepting the device's edit. Quit Obsidian on the device first (same hazard as
the `.obsidian/` reset below: Obsidian can write its in-memory state back over a file replaced out
from under it), then copy the file back from `$OBSIDIAN_CACHE_CLONE_DIR`, which stays checked out
at the baseline `publish` last used:

```sh
cp "$OBSIDIAN_CACHE_CLONE_DIR/.obsidian/<path>" "$ICLOUD_VAULT_DIR/.obsidian/<path>"
```

The next cycle reports `drifted=0` for that path once the copy matches again.

## Uninstall

```sh
launchctl bootout gui/$(id -u)/com.homelab-ops.obsidian-tools.local-replicator
launchctl bootout gui/$(id -u)/com.homelab-ops.obsidian-tools.local-replicator-drain
rm ~/Library/LaunchAgents/com.homelab-ops.obsidian-tools.local-replicator.plist
rm ~/Library/LaunchAgents/com.homelab-ops.obsidian-tools.local-replicator-drain.plist
```

This stops both schedules only. It does not touch:

- **The iCloud vault directory itself** (`ICLOUD_VAULT_DIR`) — it is the actual vault the Obsidian
  apps open; nothing about uninstalling the sync job should delete vault content.
- **The parked cache clone** (`OBSIDIAN_CACHE_CLONE_DIR`, default `~/.cache/obsidian-vault`) — a
  disposable replication artifact, not vault content, but left alone on uninstall on the same
  principle: removal is a separate, explicit decision (`rm -rf ~/.cache/obsidian-vault`), not a
  side effect of stopping the schedule.
- **The spool directory** (`LOCAL_REPLICATOR_SPOOL_DIR`, default
  `~/Library/Application Support/obsidian-tools/local-replicator/spool`) — with the drainer no
  longer running, anything left there is simply undrained, not lost; remove it by hand
  (`rm -rf ~/Library/Application\ Support/obsidian-tools/local-replicator/spool`) only once you're
  sure nothing in it still needs draining.
- **The read-only deploy key** — revoke it separately from the GitHub repository's Deploy keys
  settings if it's no longer needed, and delete the local key files
  (`~/.ssh/obsidian_vault_readonly*`) by hand.

## Resetting a device

### The `.obsidian/` baseline only

`.obsidian/` is copied once, then left alone permanently — a setting changed later at the cluster
GUI does not reach an already-seeded device (`docs/DESIGN.md` §8a D3). The only reset path is
deleting `.obsidian/` from the device's iCloud vault directory by hand; the next cycle re-seeds it
from the frozen baseline.

**Quit Obsidian on this device before deleting `.obsidian/`.** Obsidian holds workspace and plugin
state in memory and can write it back out on quit, silently undoing the reset if the app is still
open when the directory is removed or recreated underneath it — the same hazard
[`gui-access.md`](./gui-access.md) documents for the cluster-side GUI, and it applies here for the
same reason: the app, not this process, owns when it flushes in-memory state to disk.

```sh
rm -rf "$ICLOUD_VAULT_DIR/.obsidian"
```

The next scheduled (or manually triggered, `obsidian-tools replicate`) cycle detects the absent
completion marker and reseeds from whatever `.obsidian/` baseline the parked clone currently holds
(`obsidian_tools/local_replicator/device_baseline.py`).

### The whole vault

Wiping `.obsidian/` only resets locked settings. Reprovisioning a device from scratch, or
discarding a device copy that's diverged too far to trust, means clearing the whole vault directory
— and that needs one more step than the `.obsidian/`-only reset above, or the vault simply comes
back exactly as it was.

**Clearing `$ICLOUD_VAULT_DIR` alone does not reset anything — it looks like it worked, and then
undoes itself on the next cycle.** `LAST_CHECKOUT` (the parked clone's own tag,
`obsidian_tools/local_replicator/tag.py`) survives independently of whatever's on disk in iCloud,
and `run_cycle` (`obsidian_tools/local_replicator/cycle.py`) still finds it set on the next cycle:
step 1 parks the clone back at that tag; step 2's `overlay` then rsyncs the now-empty
`$ICLOUD_VAULT_DIR` onto it **with `--delete`**, so every file the baseline held reads as deleted;
`git diff` captures that as ordinary, real drift — the whole vault, spooled as a device-side
deletion — and step 5's `git reset --hard` immediately restores the working tree from git,
unaffected by any of it. The very next `publish` rsyncs that intact tree straight back into the
now-empty `$ICLOUD_VAULT_DIR`. The device gets its files back within one cycle, `.obsidian/`'s
completion marker was never touched, and nothing was actually reset — harmless in Phase 2 only
because the drainer discards what it read (`drainer.py`'s `discard_sink`) rather than acting on a
vault's worth of deletion entries that were never really deletions.

**Quit Obsidian first** — same reason as the `.obsidian/`-only reset above.

```sh
rm -rf "$ICLOUD_VAULT_DIR"
rm -rf "$OBSIDIAN_CACHE_CLONE_DIR"
```

Clearing `$OBSIDIAN_CACHE_CLONE_DIR` (default `~/.cache/obsidian-vault`, the same directory
"Uninstall" above already treats as disposable) takes `LAST_CHECKOUT` with it — the tag lives
inside that clone, not anywhere else — so the next cycle finds no previous checkout at all and
takes the same no-baseline branch a first-ever run does: no overlay, no diff, no spool, straight to
a fresh clone and an unconditional republish, `.obsidian/` reseeded from the frozen baseline
included (`docs/DESIGN.md` §4 Plane B, "Losing the Mac clone loses the baseline" describes the same
branch). Deleting the tag itself (`git -C "$OBSIDIAN_CACHE_CLONE_DIR" tag -d LAST_CHECKOUT`) without
removing the rest of the clone reaches the same branch and works just as well, if keeping the
clone's other state around is useful for some reason — either way, the tag is what has to go, not
just the iCloud directory.

## What isn't verified here

Three questions the design deliberately leaves for the operator to answer by running this, not by
reading about it (`docs/DESIGN.md` §4 Plane B, "Three residual questions"):

- Whether iCloud reliably propagates a file written into its folder by an external process (this
  one) rather than by the Finder or Obsidian itself.
- Whether Obsidian on iOS is content with rsync-written files, or expects something about how they
  arrived that rsync doesn't provide.
- Whether "Optimize Mac Storage" off is *sufficient* on its own, rather than merely necessary
  (Prerequisites, item 1).

A negative answer to any of these changes this document or the plist, not the architecture
(`docs/DESIGN.md` §4 Plane B).
