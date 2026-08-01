"""Assembles `known_hosts` at runtime instead of pinning it in a secret.

A host's SSH public key is not a secret — it's the thing a client publishes so others can verify
they're really talking to it. Storing it in a secret manager bought nothing and cost real
maintenance: GitHub rotates its host keys (it replaced its RSA key in 2023 after a leak), and a
pinned file goes stale silently until an operator notices every push failing and edits the secret
by hand.

**`StrictHostKeyChecking` stays at `yes`.** `accept-new` is not a weaker-but-acceptable substitute
here: every run is a fresh container with an empty `known_hosts`, so "accept whatever the server
presents on first contact" would run on *every* connection, not just a genuine first one — that's
trust-on-first-use with no way to ever graduate to "verified," i.e. no verification at all. Real
verification requires the expected keys to already be known before the SSH connection opens, which
is exactly what this module assembles.

**GitHub's current host keys come from `https://api.github.com/meta`** (the `ssh_keys` field) —
TLS-verified HTTPS straight to GitHub, a better trust root than a value pinned into a secret
months ago by whoever last remembered to update it. Other hosts (e.g. the NAS) publish their key
nowhere fetchable, so those come from operator-supplied configuration instead
(`GIT_SSH_KNOWN_HOSTS_EXTRA` — see `obsidian_tools/config.py`) and are appended verbatim: plain
config, not a secret, same as the GitHub keys are once fetched.

The fetch is deliberately narrow: a short timeout, no retries, and skipped entirely when no
configured remote is `github.com` (see `any_remote_is_github`) — this component's core job doesn't
need GitHub to be reachable when every remote it actually pushes to is reachable another way. When
the fetch *is* needed, a failure raises `KnownHostsError` rather than falling back to an unverified
connection — the caller must fail the run, not push over an unknown host key (see
`obsidian_tools/commands/commit.py::run`).
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

GITHUB_META_URL = "https://api.github.com/meta"
GITHUB_HOST = "github.com"

# Short and un-retried on purpose (module docstring): this fetch must never become a hard
# dependency on GitHub's availability for a run whose remotes are all reachable another way.
FETCH_TIMEOUT_SECONDS = 5.0


class KnownHostsError(RuntimeError):
    """Host keys could not be established. The caller must fail the run rather than proceed with
    an incomplete or unverified `known_hosts` file."""


def remote_host(url: str) -> str | None:
    """The SSH host named by a git remote URL, or `None` if `url` doesn't name one (e.g. a local
    filesystem path, as used throughout this codebase's own tests). Handles both forms git accepts
    for an SSH remote: `ssh://[user@]host[:port]/path` and the scp-like shorthand
    `user@host:path`. Never raises -- an unrecognized remote simply never counts toward "does this
    run touch github.com" (`any_remote_is_github`, below).
    """
    if "://" in url:
        return urlsplit(url).hostname
    if "@" in url:
        after_at = url.split("@", 1)[1]
        if ":" in after_at:
            return after_at.split(":", 1)[0]
    return None


def any_remote_is_github(urls: Sequence[str]) -> bool:
    """True if any of `urls` names `github.com` -- the signal for whether this run needs GitHub's
    host keys at all (module docstring: the fetch is skipped entirely otherwise)."""
    return any((remote_host(url) or "").lower() == GITHUB_HOST for url in urls)


def fetch_github_host_keys(*, timeout: float = FETCH_TIMEOUT_SECONDS) -> list[str]:
    """Fetch GitHub's current SSH host public keys from `GITHUB_META_URL`'s `ssh_keys` field.
    Raises `KnownHostsError` on any failure -- a network error, a timeout, an unparsable response,
    or a response with no usable keys -- so the caller never mistakes "the fetch didn't happen" for
    "GitHub has no host keys."
    """
    request = urllib.request.Request(GITHUB_META_URL, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw_body: bytes = response.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise KnownHostsError(f"failed to fetch GitHub host keys from {GITHUB_META_URL}: {exc}") from exc

    try:
        payload: object = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise KnownHostsError(f"GitHub host key response from {GITHUB_META_URL} was not valid JSON: {exc}") from exc

    keys_field: object = cast("dict[str, object]", payload).get("ssh_keys") if isinstance(payload, dict) else None
    if not isinstance(keys_field, list) or not keys_field:
        raise KnownHostsError(f"GitHub host key response from {GITHUB_META_URL} had no usable 'ssh_keys' entry")

    keys: list[str] = []
    for entry in cast("list[object]", keys_field):
        if not isinstance(entry, str):
            raise KnownHostsError(
                f"GitHub host key response from {GITHUB_META_URL} had a non-string 'ssh_keys' entry: {entry!r}"
            )
        keys.append(entry)
    return keys


def format_known_hosts(*, github_keys: Sequence[str] = (), extra_lines: str = "") -> str:
    """Pure: render fetched GitHub host keys (each formatted as `github.com <key>`) followed by
    operator-supplied extra lines, verbatim -- one line per non-blank line of `extra_lines`, in the
    order given. Blank/whitespace-only lines in `extra_lines` are dropped rather than preserved as
    empty `known_hosts` lines; every line is right-stripped since a trailing space is invisible in
    an editor but changes the line's meaning to `ssh`. Returns `""` for no input at all -- an empty
    `known_hosts` file, not an error; a fetch failure when one *is* required is `KnownHostsError`,
    raised by `fetch_github_host_keys` before this function ever runs.
    """
    lines = [f"{GITHUB_HOST} {key.strip()}" for key in github_keys]
    lines.extend(stripped for raw in extra_lines.splitlines() if (stripped := raw.strip()))
    return "\n".join(lines) + ("\n" if lines else "")


def assemble_known_hosts(
    *,
    remote_urls: Sequence[str],
    extra_lines: str,
    destination: Path,
    timeout: float = FETCH_TIMEOUT_SECONDS,
) -> Path:
    """Fetch GitHub's host keys if -- and only if -- `remote_urls` needs them, combine them with
    `extra_lines`, and write the result to `destination` (creating its parent directory if needed).
    Logs which host keys were assembled and from where, at INFO: with alerting off on this
    platform (`docs/DESIGN.md`), this log line is the only diagnostic a failed host-key
    verification has to point back to.
    """
    github_keys: list[str] = []
    if any_remote_is_github(remote_urls):
        github_keys = fetch_github_host_keys(timeout=timeout)
        logger.info(
            "fetched github.com host keys",
            extra={"event": "known_hosts_github_fetched", "source": GITHUB_META_URL, "count": len(github_keys)},
        )
    else:
        logger.info(
            "no github.com remote configured; skipped the GitHub host key fetch",
            extra={"event": "known_hosts_github_fetch_skipped"},
        )

    content = format_known_hosts(github_keys=github_keys, extra_lines=extra_lines)

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")
    destination.chmod(0o644)  # public information -- see module docstring; no reason to lock it down

    extra_line_count = content.count("\n") - len(github_keys) if content else 0
    logger.info(
        "assembled known_hosts",
        extra={
            "event": "known_hosts_assembled",
            "path": str(destination),
            "github_key_count": len(github_keys),
            "extra_line_count": extra_line_count,
        },
    )
    return destination
