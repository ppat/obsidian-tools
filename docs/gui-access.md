# GUI access

An operator runbook for reaching the headless Obsidian instance's own GUI in the
cluster. This is **P8** (`DESIGN.md` section 1.3): a declared design exception, not a
normal access path.

**P8 has no server-side gate, no validation, and no record.** A write made here carries
none of the MCP path's controls - no `source:`/`authority:`/`trigger:` stamping, no
schema check, not even a log entry until the nightly lint notices it after the fact. Use
this only for configuration that is unreachable any other way, or for repair. Never for
routine authoring or browsing - that traffic belongs on the MCP path, through an agent,
where it is validated and recorded.

Everything in this file was proven by actually running it against the live instance, not
written from the design ahead of time. Where a step names a namespace or deployment, it
is this project's cluster instance (`obsidian-vault` / `deploy/obsidian`); adjust if
you're pointed at a different one.

## Connecting

1. Start x11vnc **detached**, so a dropped `kubectl exec` stream does not kill it:

   ```sh
   kubectl exec -n obsidian-vault deploy/obsidian -c obsidian -- sh -c \
     'setsid nohup x11vnc -display :99 -localhost -rfbport 5900 -passwd <throwaway> \
      -shared -forever >/tmp/x11vnc.log 2>&1 </dev/null &'
   ```

2. Port-forward to it:

   ```sh
   kubectl port-forward -n obsidian-vault deploy/obsidian 5900:5900
   ```

3. Open a VNC viewer at `vnc://localhost:5900`. macOS has one built in:

   ```sh
   open vnc://localhost:5900
   ```

4. Teardown, when the session ends: Ctrl-C the port-forward, then kill x11vnc explicitly
   - it was started detached, so it does **not** die with your terminal or with the
   port-forward:

   ```sh
   kubectl exec -n obsidian-vault deploy/obsidian -- pkill x11vnc
   ```

## Use `-passwd`, not `-nopw`

Pass a throwaway password with `-passwd`, even though `-localhost` already makes the
port unreachable from outside the pod's network namespace and no-auth would be fine on
the merits. Use `-passwd` anyway, because the failure mode of `-nopw` is misleading
rather than merely inconvenient: macOS Screen Sharing negotiates **RFB protocol 3.3**,
where the *server* dictates the security type rather than the client choosing one, and
Apple's client will not proceed against a server that offers no authentication. It
prompts for a password it can never satisfy **while the connection is still establishing
at the socket level**, so the symptom reads as a networking fault - a dead port-forward,
a wrong port - and not as a protocol negotiation refusal, which is what it actually is.
This cost real time to diagnose once; it should not cost it again.

`-localhost` remains the actual security boundary here, not the password. The throwaway
password only exists to satisfy the client's negotiation; it is not protecting anything
`-localhost` doesn't already protect. TigerVNC-based clients handle a no-auth server
correctly, so `-nopw` is fine if your viewer isn't macOS Screen Sharing.

## Hazards

Every one of these was hit during the first real session. Read all five before you start.

1. **No clipboard.** Paste is greyed out and keyboard shortcuts do nothing through the
   x11vnc + macOS Screen Sharing path. Deliver any content by writing the file from
   outside instead of typing it in:

   ```sh
   kubectl exec -i -n obsidian-vault deploy/obsidian -- sh -c \
     'umask 0027 && cat > /vault/brain/<file>'
   ```

   For repo content, pipe it straight from git rather than retyping it:

   ```sh
   git show origin/<branch>:<file> | kubectl exec -i -n obsidian-vault deploy/obsidian -- \
     sh -c 'umask 0027 && cat > /vault/brain/<file>'
   ```

   The `umask 0027` matters and is not incidental: vault files are `0640`/`0750`, so a
   sidecar can read and commit them but not author them. That permission split is how the
   single-writer invariant is enforced at the filesystem level, and delivering content
   without it would quietly weaken that invariant.

2. **The Templates core plugin inserts into the currently active file, not a new one.**
   Create and open a new note *first* - right-clicking the target folder and creating
   the note there works - before invoking *Insert template*. Running it with `log.md`
   open overwrote `log.md`, the append-only audit log, repeatedly, because it was the
   active file at the time.

3. **Close a file in Obsidian before restoring it from outside.** Obsidian holds the
   editor buffer in memory and will write the stale in-memory version back over a file
   you just restored from outside the app - you fix it, and watch it un-fix itself on
   the next autosave.

4. **The pod can vanish mid-session.** It was evicted three times in about an hour by
   the descheduler for `LowNodeUtilization`, once relocating to another node. This
   instance now carries an annotation to stop that, but any session can still end
   abruptly for other reasons, and x11vnc dies with the pod when it does.

5. **Pasting multi-line shell blocks is hazardous.** Leading indentation breaks
   heredocs - an indented `EOF` terminator never matches, and the resulting error points
   somewhere unrelated to the real cause. Rewriting as one long line is not safer either:
   terminal line-wrapping can insert real newlines mid-token on a long pasted line.
   Prefer short-lined, heredoc-free commands, as used throughout this file.

## Backup

There is no backup mechanism for this path yet, and this file does not invent one. Both
accidental overwrites hit during the first session (`TODO.md` lost its `# TODO` header;
`log.md` was clobbered per hazard 2 above) were recoverable only because git happened to
already hold those files from a manual seed an hour earlier - that was luck from timing,
not a safety net. Do not treat git as a backup for GUI work until it is actually wired up
as one.

The full procedure lands in a later phase, once the committer exists (see `DESIGN.md`
Phase 2 and the Commit section of `settings-lock.md`): at that point the instruction
becomes "force a commit before opening the GUI." Until then, there is no general recovery
path for a mistake made here beyond whatever git already happens to hold.

## The settings checklist's transient copy

`settings-lock.md` is copied to the vault root for the duration of a settings session and
must be deleted from the vault when the session ends - see that file for the copy/delete
steps; it is not repeated here. While the copy is present, `TODO.md`'s task queries
exclude it (`ppat/obsidian-vault#7`), so it does not flood the task view with checklist
lines for as long as it sits at the vault root.
