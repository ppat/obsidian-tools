"""The replication cycle: compare, capture, pull, publish, advance — in that order, because the
order is load-bearing (docs/DESIGN.md §2 item 10, §4 Plane B; ppat/obsidian-tools#3).

**Why comparison and capture precede the pull.** Whatever currently sits in iCloud was placed
there by the last successful publish — exactly `LAST_CHECKOUT`. The human's edits were made
against *that*. The diff is only meaningful while it is still the live comparison: pulling first
would bring new upstream content in, and the subsequent rsync would then be comparing iCloud
against a baseline it was never actually checked out against, mixing upstream change and human
drift into one diff with no way to separate them. Pulling first also does work that gets thrown
away whenever capture fails and that cycle's publish has to skip a path.

**The one-parked-clone simplification.** A previous design sketch used a second local checkout
pinned at `LAST_CHECKOUT` purely to hold the comparison baseline, separate from the clone being
pulled forward. That component doesn't need to exist: if the single cache clone simply *stays
parked at LAST_CHECKOUT between cycles*, it already *is* the baseline — compare iCloud against it,
capture, pull it forward, publish, advance the tag, and it's parked at the new `LAST_CHECKOUT`,
ready for the next cycle. `clone.py` and `tag.py` exist to make exactly that true.

**What "parked between cycles" has to mean when a cycle is only partly successful.** `fetch_origin`
always runs (cheap, and it's how new history becomes available for a future cycle to retry
against), but `checkout_forward` — moving the parked working tree past `LAST_CHECKOUT` — only
happens once this cycle's publish has actually placed the fetched commit's content in iCloud for
*every* drifted path. If even one path's capture failed, that path's iCloud content does not match
the fetched commit, so the tree as a whole is no longer byte-identical to any single commit the
`LAST_CHECKOUT` tag could name — advancing the tag (or leaving the tree checked out ahead of it)
would make the next cycle's comparison run against a baseline iCloud was never actually brought to,
reporting the *next* unrelated upstream change on that path as human drift rather than as the
retry it actually is. So a cycle with any capture failure checks the tree back out to the previous
`LAST_CHECKOUT` before finishing — the fetched commit stays available in the git-dir for a future
cycle to retry against, but the parked tree itself does not move until a cycle clears every path.
Accepted cost, stated plainly: a stuck path holds back *every* path's publish, not just its own,
until it resolves. That trade favours the project's standing rule over throughput — no merge
engine, nothing risked, ever, in exchange for staleness that is cheap here specifically because the
whole vault is small (docs/DESIGN.md §4 Plane B, "Payload").
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from obsidian_tools.config import ReplicateConfig
from obsidian_tools.local_replicator.capture import CaptureSink, capture_path, discard_sink
from obsidian_tools.local_replicator.clone import checkout_forward, ensure_cache_clone, fetch_origin
from obsidian_tools.local_replicator.device_baseline import is_baselined, seed_baseline
from obsidian_tools.local_replicator.rsync_ops import DriftedPath, compare_dry_run, publish
from obsidian_tools.local_replicator.tag import advance_last_checkout, read_last_checkout
from obsidian_tools.vault_git.runner import GitRunner
from obsidian_tools.vault_git.ssh import build_ssh_command

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CycleResult:
    drifted: tuple[str, ...]
    captured: tuple[str, ...]
    capture_failed: tuple[str, ...]
    obsidian_seed_attempted: bool
    tag_advanced: bool
    checkout: str | None


def run_cycle(config: ReplicateConfig, *, capture_sink: CaptureSink = discard_sink) -> CycleResult:
    cache_clone_dir = Path(config.cache_clone_dir)
    icloud_vault_dir = Path(config.icloud_vault_dir)
    icloud_vault_dir.mkdir(parents=True, exist_ok=True)

    ssh_command = build_ssh_command(config.ssh_key_path, config.ssh_known_hosts_path)
    runner = GitRunner(cache_clone_dir / ".git", cache_clone_dir, ssh_command=ssh_command)
    ensure_cache_clone(runner, branch=config.branch, origin_url=config.origin_url)

    previous_checkout = read_last_checkout(runner)

    # Step 1: compare — against the clone exactly as it sits on disk right now, before anything
    # below touches it. A missing baseline (first run, or a lost/re-provisioned cache) means
    # there is nothing meaningful to diff against; re-baselining publishes everything once instead
    # of reporting the whole vault as drifted (docs/DESIGN.md §4 Plane B, "Losing the Mac clone").
    drifted: list[DriftedPath] = compare_dry_run(cache_clone_dir, icloud_vault_dir) if previous_checkout else []

    # Step 2: capture — attempted for every drifted path, before the pull or the publish.
    captured: list[str] = []
    failed: list[str] = []
    for item in drifted:
        if capture_path(icloud_vault_dir, item, sink=capture_sink):
            captured.append(item.path)
        else:
            failed.append(item.path)

    # Step 3: pull. Fetch always runs; the working tree only advances once we know below whether
    # this cycle's publish can leave it advanced (see module docstring).
    fetched_sha = fetch_origin(runner, branch=config.branch)
    if fetched_sha is None:
        logger.info(
            "origin has no history for this branch yet; nothing to publish this cycle",
            extra={"event": "cycle_no_origin_history"},
        )
        return CycleResult(
            drifted=tuple(item.path for item in drifted),
            captured=tuple(captured),
            capture_failed=tuple(failed),
            obsidian_seed_attempted=False,
            tag_advanced=False,
            checkout=previous_checkout,
        )
    checkout_forward(runner, branch=config.branch, sha=fetched_sha)

    # Step 4: publish, gated per-path on capture. `.obsidian/` gets its own one-time seed copy
    # (device_baseline.py) rather than going through the ordinary drift/capture path at all, and is
    # always excluded from this main sync — either because it was just seeded, or because it was
    # already present and this is exactly the "exclude when present" rule.
    obsidian_seed_attempted = False
    exclude_paths = list(failed)
    if is_baselined(icloud_vault_dir):
        exclude_paths.append(".obsidian/")
    else:
        obsidian_seed_attempted = True
        seed_baseline(cache_clone_dir, icloud_vault_dir)
        exclude_paths.append(".obsidian/")

    publish(cache_clone_dir, icloud_vault_dir, extra_excludes=exclude_paths)

    # Step 5: advance LAST_CHECKOUT only if every drifted path published this cycle — otherwise
    # revert the parked tree to the previous checkout rather than leave it half-advanced.
    if failed:
        if previous_checkout is not None:
            checkout_forward(runner, branch=config.branch, sha=previous_checkout)
        tag_advanced = False
        checkout = previous_checkout
        logger.info(
            "capture failed for at least one drifted path; LAST_CHECKOUT not advanced",
            extra={"event": "cycle_tag_not_advanced", "failed_paths": failed},
        )
    else:
        advance_last_checkout(runner, fetched_sha)
        tag_advanced = True
        checkout = fetched_sha
        logger.info(
            "cycle complete, LAST_CHECKOUT advanced",
            extra={"event": "cycle_tag_advanced", "checkout": fetched_sha},
        )

    return CycleResult(
        drifted=tuple(item.path for item in drifted),
        captured=tuple(captured),
        capture_failed=tuple(failed),
        obsidian_seed_attempted=obsidian_seed_attempted,
        tag_advanced=tag_advanced,
        checkout=checkout,
    )
