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

4. **`obsidian-tools` on `PATH` for a non-interactive launchd job**, or its full absolute path
   known ahead of the install step below — launchd does not run inside a login shell, so it does
   not read `~/.zshrc`/`~/.zprofile` and will not find a `uv tool`/`pipx`-installed script unless
   the plist's `PATH` or `ProgramArguments` names it explicitly. Find it once with:

   ```sh
   command -v obsidian-tools
   ```

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
   cp packaging/launchd/com.ppat.obsidian-tools.local-replicator.plist.template \
     ~/Library/LaunchAgents/com.ppat.obsidian-tools.local-replicator.plist
   cp packaging/launchd/com.ppat.obsidian-tools.local-replicator-drain.plist.template \
     ~/Library/LaunchAgents/com.ppat.obsidian-tools.local-replicator-drain.plist
   ```

   Open both copies in an editor and replace every `__PLACEHOLDER__` token:

   | Token | Replace with | Which file(s) |
   | --- | --- | --- |
   | `__HOME_DIR__` | Absolute path to your home directory (`echo $HOME`) — plists cannot expand `$HOME` or `~` themselves; every occurrence must be the literal path. | both |
   | `__OBSIDIAN_TOOLS_BIN__` | Absolute path from `command -v obsidian-tools` (Prerequisites, item 4). | both |
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
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ppat.obsidian-tools.local-replicator.plist
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ppat.obsidian-tools.local-replicator-drain.plist
   ```

   `RunAtLoad` is set on both, so this also triggers an immediate first run of each rather than
   waiting a full `StartInterval`.

5. Confirm they're running and check the first run's logs:

   ```sh
   launchctl print gui/$(id -u)/com.ppat.obsidian-tools.local-replicator
   launchctl print gui/$(id -u)/com.ppat.obsidian-tools.local-replicator-drain
   tail -f ~/Library/Logs/obsidian-tools/local-replicator.log ~/Library/Logs/obsidian-tools/local-replicator-drain.log
   ```

   Each line is a JSON object (`obsidian_tools/logging_config.py`). For the replication cycle, look
   for `"event": "cycle_complete"` and confirm `"checkout"` is non-null after the first successful
   run. A first cycle publishes the whole vault unconditionally (there is no prior baseline to
   compare against yet — `docs/DESIGN.md` §4 Plane B, "Losing the Mac clone loses the baseline"
   describes the same re-baselining behaviour for a lost cache), so expect it to take longer than
   steady-state cycles. For the drainer, look for `"event": "drain_complete"`; `"drained"` will
   often be non-zero even with no human edits at all, since the device-side detector submits every
   `.obsidian/` change it sees unfiltered (`docs/DESIGN.md` §1.5 R2) and Obsidian's own plugins
   churn state there continuously — that noise is discarded here by design, not a sign of anything
   wrong.

## Uninstall

```sh
launchctl bootout gui/$(id -u)/com.ppat.obsidian-tools.local-replicator
launchctl bootout gui/$(id -u)/com.ppat.obsidian-tools.local-replicator-drain
rm ~/Library/LaunchAgents/com.ppat.obsidian-tools.local-replicator.plist
rm ~/Library/LaunchAgents/com.ppat.obsidian-tools.local-replicator-drain.plist
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

## Resetting the device's `.obsidian/` baseline

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
