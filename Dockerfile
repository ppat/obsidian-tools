# =======================================================================================================================================
# Builder stage. Runs on the BUILD platform, not the target one: obsidian_tools has zero runtime
# dependencies today (see pyproject.toml) and is pure Python throughout, so nothing built here is
# architecture-specific. Pinning `--platform=$BUILDPLATFORM` means this stage runs once, natively,
# regardless of how many target platforms are requested — installing the package doesn't get run a
# second time under arm64 QEMU emulation just to produce byte-identical output. If a future
# subcommand (e.g. `replicate`) ever adds a dependency with compiled extensions, this stage is the
# one that would need revisiting; nothing else in this file assumes pure Python.
FROM --platform=$BUILDPLATFORM python:3.14.7-slim-bookworm@sha256:23c59390fc717bf09f9336908199a0ae75d9c4264bf296123f94ad772fea3b52 AS builder
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ARG TARGETARCH
WORKDIR /build

# uv's own distributed image, not `pip install uv` — a static binary copy is faster and avoids
# resolving uv's own dependencies through pip. Pinned to the same version this repo's own
# contributors use (see mise.toml) rather than a version chosen independently for the image.
COPY --from=ghcr.io/astral-sh/uv:0.12.0@sha256:606e70c71c852d03f611b1e56a195d08648507018a7057fab82c4974c4eae105 /uv /usr/local/bin/uv

# The build context is the whole repository root (see .github/workflows/build-image.yaml's own
# comment on why) so that this stage can install the repository's own package locally — no
# cross-repo fetch, no published wheel, no private-repo token. Only what's actually needed for
# `uv sync` to install the package is copied; .dockerignore keeps the rest (tests/, docs/, .git/)
# out of the build context entirely rather than relying on COPY selectivity alone.
COPY pyproject.toml uv.lock README.md ./
COPY obsidian_tools ./obsidian_tools

# Build the virtualenv at the exact absolute path it will occupy in the final stage, NOT at
# ${WORKDIR}/.venv. This is load-bearing, not tidiness: a virtualenv's console scripts carry an
# absolute shebang naming their interpreter, so a venv created at /build/.venv gets
# `#!/build/.venv/bin/python` baked into bin/obsidian-tools. Copying that venv to a different
# path leaves the shebang pointing at an interpreter that does not exist in the final image, and
# the container dies at startup with
#
#     exec /opt/obsidian-tools/.venv/bin/obsidian-tools failed: No such file or directory
#
# which names the *script* — the file that does exist — rather than the missing interpreter, and
# so reads as though the entrypoint were never installed. Building at the destination path means
# the shebangs are already correct and nothing has to be rewritten afterwards.
ENV UV_PROJECT_ENVIRONMENT=/opt/obsidian-tools/.venv

# `--frozen` refuses to silently re-resolve against a stale uv.lock (a mismatch fails the build
# instead of shipping different versions than CI validated). `--no-dev` excludes the dev
# dependency group (pytest, ruff, pyright, hypothesis) — none of it belongs in a runtime image.
# `--no-editable` installs the package as a normal site-packages copy rather than a symlink back
# to /build/obsidian_tools, so the final stage only needs to copy .venv, not the source tree too.
RUN --mount=type=cache,target=/root/.cache/uv,id=obsidian-tools-cache-uv-${TARGETARCH} \
    uv sync --frozen --no-dev --no-editable

# =======================================================================================================================================
# Final stage. Runs once per requested target platform (the default; no --platform override here).
FROM python:3.14.7-slim-bookworm@sha256:23c59390fc717bf09f9336908199a0ae75d9c4264bf296123f94ad772fea3b52
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ARG TARGETARCH
WORKDIR /

# uid/gid 1000 deliberately, not an arbitrary system uid: this matches the vault Deployment's
# fsGroup (ADR-0040, ppat/homelab-ops-kubernetes-apps#3443) so the committer's
# group-read access to vault content and the group ownership `fsGroupChangePolicy: OnRootMismatch`
# preserves line up without a separate SecurityContext override at the point of use.
ARG OBSIDIAN_TOOLS_UID=1000
ARG OBSIDIAN_TOOLS_GID=1000

RUN rm -f /etc/apt/apt.conf.d/docker-clean && \
    echo 'Binary::apt::APT::Keep-Downloaded-Packages "true";' > /etc/apt/apt.conf.d/keep-cache

# git: every operation this image runs shells out to the real git binary rather than a Python git
#   library (see obsidian_tools/vault_git/runner.py — the detached --git-dir/--work-tree
#   invocation this whole component is built around is a git CLI concept, not a library one).
# openssh-client: provides `ssh`, invoked via GIT_SSH_COMMAND (obsidian_tools/vault_git/ssh.py)
#   for both remotes, over one mounted key — see that module for why host key checking is never
#   disabled here.
# tini: this image owns PID 1 directly (no s6-overlay, no init supervisor, no start-as-root-then-
#   drop dance); tini reaps and forwards signals cleanly for the short git child processes this
#   CLI spawns, which matters even for a one-shot CronJob pod that can be terminated mid-run.
# Cache mount ids carry ${TARGETARCH}: buildx builds this stage once per target platform, and
# cache mounts default to sharing=shared, so a single id would put two concurrent apt runs on the
# same /var/lib/apt and one of them would die on the lock.
RUN --mount=type=cache,target=/var/cache/apt,id=obsidian-tools-cache-apt-${TARGETARCH} \
    --mount=type=cache,target=/var/cache/debconf,id=obsidian-tools-cache-debconf-${TARGETARCH} \
    --mount=type=cache,target=/var/lib/apt,id=obsidian-tools-lib-apt-${TARGETARCH} \
    apt-get update && \
    DEBIAN_FRONTEND="noninteractive" apt-get install -yq --no-install-recommends \
        ca-certificates \
        git \
        openssh-client \
        tini

RUN groupadd -g "${OBSIDIAN_TOOLS_GID}" -r obsidian-tools && \
    useradd -u "${OBSIDIAN_TOOLS_UID}" \
        -g obsidian-tools \
        --system \
        --create-home \
        --home-dir /home/obsidian-tools \
        --no-log-init \
        --shell /usr/sbin/nologin \
        obsidian-tools

# Source and destination paths must be identical — see the builder stage's UV_PROJECT_ENVIRONMENT
# comment. Changing either side alone reintroduces the broken-shebang failure.
COPY --from=builder --chown=root:root /opt/obsidian-tools/.venv /opt/obsidian-tools/.venv

ENV PATH="/opt/obsidian-tools/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER obsidian-tools
WORKDIR /home/obsidian-tools

# No CMD: the Kubernetes CronJob supplies the subcommand as container args (e.g. `["commit"]`) —
# see ppat/homelab-ops-kubernetes-apps#3443 for the manifest that does so.
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/obsidian-tools/.venv/bin/obsidian-tools"]
