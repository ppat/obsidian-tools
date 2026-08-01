# Settings lock checklist

A one-time procedure, executed by a human at the headless Obsidian instance's GUI before
any vault content exists. It is documentation in this repository, not a note in the vault:
it is executed once and then it is history, whereas the vault holds durable, regenerable
content. `_ops/` in the vault is defined for machine-written output, and `status: evergreen`
is a note-maturity tier, so a hand-written checklist run once belongs to neither.

**Do this once, at the first launch, before any content exists.** Every setting below is
cheap to fix now and expensive or impossible to fix later: adding a required property once
notes exist means backfilling every note, and `authority:` is the worst case, because the
information needed to answer it correctly is gone by then.

**Do this in the GUI, at the instance, by hand.** Do not hand-craft `.obsidian/*.json`.
That format is undocumented and is easy to get subtly wrong on precisely the settings that
have to be right first. The GUI is reachable by `kubectl port-forward` from the Mac; there
is no ingress.

This file is the checklist, not a record of state. When a line is done, tick it in your
working copy and append one line to the vault's `log.md`.

## Working through this inside Obsidian: the transient copy

Every line below is executed in Obsidian's own Settings panes, so the checklist has to be
readable on screen at the same time. Obsidian can only display files inside the vault, and
this file is not a vault file. The other projects following this convention have an *agent*
execute their checklist, so nothing ever needs to be on screen; here a human does, so the
gap is real and is closed by a transient copy rather than by moving the file back.

1. Copy this file to the **vault root** as `settings-lock.md`, at the start of the session.
2. Work through it in Obsidian's own editor, ticking lines as you go. Treat the copy as
   scratch: it is not vault content, nothing links to it, and no agent may write to it.
3. Append your notes and any recorded observations to the vault's `log.md`, not to the copy.
4. **Delete the copy from the vault root** when the session ends.

The vault's `.gitignore` carries `/settings-lock.md`, so a scheduled commit during the
session cannot capture it. That is a backstop, not permission to leave it there: the vault's
root file list is a closed contract and this file is not on it.

Keep this file ASCII-only. The vault forbids non-ASCII characters and has a mechanical
banned-character check, so a copy carrying an em dash or a curly quote would be flagged the
moment it lands.

## Core plugins

- [ ] Enable: Properties, Bases, Templates, Daily Notes, Backlinks, Outgoing Links.
- [ ] Turn **off**: **Sync** and **Publish**. Both are gated behind an Obsidian account
      and neither account is logged in, but **Sync was found enabled** as a core
      plugin regardless - a logged-out account does not stop it being on. The design
      rejected Sync partly on principle, not just on cost: it means handing vault
      content to a third party who may train on it, and the vault holds
      `10-areas/finance/`.
- [ ] Turn **off**: **Unique note creator**. It timestamps new filenames, which
      violates the `slug(title)` filename rule.
- [ ] **Zettelkasten prefixer was not present** in this Obsidian version. Do not spend
      time looking for it to disable.
- [ ] The remaining core plugins Obsidian enables by default - Bookmarks, Canvas,
      Command palette, Search, Graph view, Page preview, Word count - are harmless UI
      with no vault-write path. Leave them; they need no action here.

## Community plugins

- [ ] Verify three community plugins, and only those three, are present and enabled:
      **Local REST API with MCP**, Tasks, Dataview. Do not install them by hand: all
      three arrive baked into the image and are enabled automatically the first time
      each one's plugin directory is seeded, which is exactly this first launch, so
      this step is a check, not an installation. If any is missing or disabled, that
      means the image did not seed correctly; fix that rather than installing the
      plugin from the GUI, which would leave the vault out of step with what the
      image bakes in on every rebuild. That is the whole set.
      The REST API plugin is the load-bearing one: it is the only route from outside
      the container into the vault, and its `/mcp` endpoint cannot be disabled from
      the GUI - the design cites that undisableable endpoint as the reason
      NetworkPolicy, not application configuration, has to be the boundary (see
      [`DESIGN.md`](./DESIGN.md)). The ratified *additional* plugin set (Tasks,
      Dataview) is easy to confuse with the complete set; this line names all three
      because the earlier wording did not.
- [ ] Do **not** install Templater or QuickAdd. Both declare a minimum application
      version above the stable release the image pins, so neither would load. QuickAdd
      is hotkey-driven and no automated writer needs it. Templater is a real loss:
      see "Templates and daily notes" below for what it would have bought and what
      now covers that gap instead.
- [ ] Do **not** install Kanban. It is excluded on maintenance and post-1.9 breakage
      grounds. Use a Bases board view instead.
- [ ] Do **not** install Linter or Frontmatter Date Manager. Frontmatter normalisation
      belongs to the maintenance pass, which runs outside the application. Keeping it
      there makes the one-authority rule structural rather than dependent on getting a
      plugin's on-save setting right, and the date plugin has no vault-wide command at
      all, so a scheduled pass could not have driven it anyway.
- [ ] Do **not** install an in-app frontmatter validation plugin. The only viable
      candidate cannot express enums or the lowercase-tag rule, precisely the parts of
      the schema most likely to be violated, so it would be a second, lossy source of
      truth competing with the Python validator that runs outside the application.
- [ ] Keep the set small. A plugin that is not on this list does not go on.

## Updates

- [ ] Turn **off** automatic update checks (General settings). The image pins the
      Obsidian version and Renovate drives version bumps, not the application, and
      the pod log shows an update check hitting GitHub on every start regardless of
      whether anything is installed. An in-container update could not apply even if
      one were found - `/opt/obsidian` is root-owned and the root filesystem is
      read-only - so the check is pure egress noise with no possible effect.

## Files and links

- [ ] Default location for new notes: **`00-inbox/`**.
- [ ] Default location for new attachments: **In the folder specified below**, set to
      **`_attachments/`**. The default behaviour drops attachments beside the note and
      becomes unfixable at scale. The folder is committed and stays empty; the vault is
      markdown only.
- [ ] Use `[[Wikilinks]]`: on.
- [ ] New link format: shortest path when possible.
- [ ] Deleted files: **Move to Obsidian trash (`.trash/`)**. Set here, this session - it
      is not already the effective default. The GUI showed **System trash** in effect,
      and this session changed it. See below for what is, and is not, established about
      what that setting actually governs.

This setting decides how destructive a *GUI* delete is. Whether it also governs an *MCP*
delete is unknown - see the last two paragraphs below - and that unknown is live, not
academic: there is no move or rename tool in the MCP surface, so every relocation - a
rolled-up source into `90-archive/`, a slug correction - is a write to the new path
followed by a delete at the old one.

`.trash/` was chosen over the other two values on their own merits, independent of what
the GUI happened to be set to. **System trash** is undefined here: the image ships no
desktop trash implementation, so the call may fail or fall back without saying so, and
undefined is worse than either alternative because it is unknown rather than chosen.
**Permanent delete** leaves git history as the only recovery. `.trash/` at least names
its behaviour and is inspectable.

Deleting a note through the MCP was observed landing in `/vault/.trash/` with its
original mtime intact - a rename, not a truncation - while the GUI setting was still at
System trash. That observation establishes what an MCP delete does; it does not establish
what governs it, since the setting in effect at the time was the opposite of what the
delete actually did. Two explanations are undistinguished: the Local REST API plugin may
call the local-trash path directly, ignoring the app setting entirely; or Electron's
system-trash call may simply fail in a container with no desktop trash implementation,
and Obsidian falls back to `.trash/` regardless of what the setting says. An earlier
version of this entry treated the observation as confirming the setting, and called
leaving it alone a decision to "pin" the current behaviour rather than change it - that
was false: the setting was System trash, and this session changed it.

The experiment that would distinguish the two explanations has not been run: set
*Deleted files* to permanent delete, delete a throwaway note through the MCP, and see
whether it still lands in `.trash/`. Worth running before the ingestor is granted delete
in anger - if the app setting does not govern MCP deletes at all, choosing a "safer"
value here buys nothing on the write path that actually matters.

Note `.trash/` is gitignored, so it is a local-volume-only net: it never reaches git and is
lost with the volume. There are three recovery surfaces and they fail independently -
`.trash/`, git history, and the lagging macOS replica. The first and last hold the *file*, so
recovery is copying it back; git holds the *history*, so recovery means knowing what to look
for and when.

## Templates and daily notes

- [ ] Templates (core): template folder location = **`_templates/`**. This is the core
      plugin, which is explicitly invoked; it does not fire on file creation.
- [ ] Expect **five** files in `_templates/`, not one per note type: `note`, `task`,
      `meeting`, `devlog`, and `daily-note`. Template files exist only for the types a
      human plausibly creates by hand at the keyboard. Every other type's frontmatter is
      defined in `CLAUDE.md` section 3.1, as one delimited YAML block per type, because
      agents write from the schema document and never from a template file.
- [ ] Templater is not installed, so folder templates and trigger-on-new-file-creation
      are unavailable. Worth recording what that costs, because it is a real gap rather
      than a non-decision: those would have made a note created by hand at the GUI born
      with correct frontmatter, which was the cheapest available control on the one
      write path that has no server-side gate. Nothing replaces it at creation time.
      What covers the gap instead is the nightly maintenance pass, which fills missing
      required fields without overwriting existing values, so a GUI-created note is
      briefly non-conforming rather than permanently so, and shows up in the pass's
      report either way.
      The counter-argument that made this less painful than it looks: the trigger fires
      on *every* file creation event, including notes an agent creates through the REST
      interface, where a template would be interference rather than help, since agents
      write complete notes.
- [ ] Daily notes: date format = **`YYYY-MM-DD`** exactly. Not a locale format.
- [ ] Daily notes: new file location = **`40-journal/`**.
- [ ] Daily notes: template file = **`_templates/daily-note.md`**.

## Property types

Declare every property's type before any note exists. There are **16** properties below;
finishing this section means 16 ticks, not a count done from memory. A field that holds a
date in one note and a string in another silently breaks every view over it, with no
error - not hypothetical: during this checklist's first live run, `salience` was
auto-recorded as **Text** on creation and had to be corrected to **Number** by hand,
exactly the failure this section exists to prevent, caught only because the type was
checked.

**Assign each one by hand.** Obsidian records only the types you select manually; every
other property's type is inferred per note, which is exactly the drift this list exists
to prevent. Tick a line only after selecting the type in the Properties view.

- [ ] `created` - **Date**
- [ ] `updated` - **Date**
- [ ] `reviewed` - **Date**
- [ ] `consolidated` - **Date**
- [ ] `salience` - **Number**
- [ ] `tags` - **List** (tags)
- [ ] `aliases` - **Aliases** (native type; not List, and cannot be changed to List -
      confirmed in the GUI). Needs no manual assignment; tick this as a check that it
      is already correct, not as an action.
- [ ] `related` - **List**
- [ ] `refs` - **List**
- [ ] `type` - **Text**
- [ ] `title` - **Text**
- [ ] `source` - **Text**
- [ ] `authority` - **Text**
- [ ] `trigger` - **Text**
- [ ] `status` - **Text**
- [ ] `confidence` - **Text**

`authority` and `trigger` did not exist in earlier drafts of this schema. They are the
whole reason this checklist is done on day one.

Every note the vault actually holds carries exactly 13 of these 16 fields at creation:
`consolidated` and `salience` are absent until the consolidation pass sets them
(`CLAUDE.md` section 12), and `aliases` is absent from any note that has no alias. A
property no note carries is not offered in the Properties view at all, so declaring its
type needs a note that carries it. Rather than work out case by case which of the 16
need this, carry all 16 on one scratch note and type them in one pass:

```yaml
---
created: 2026-01-01
updated: 2026-01-01
reviewed:
consolidated: 2026-01-01
salience: 5
tags: [scratch]
aliases: [scratch-alias]
related: [scratch]
refs: [scratch]
type: note
title: scratch
source: scratch
authority: human
trigger: manual
status: draft
confidence: high
---
```

Use non-empty values for every list field so the List type is unambiguous in the
Properties view, and leave `reviewed` empty rather than `null` - `null` is a value, not
the absence of one, and would declare the wrong thing.

- [ ] Create the scratch note in `_ops/agent/` by `kubectl exec`, not by typing it in at
      the GUI. There is no working clipboard through this path (see
      [`gui-access.md`](./gui-access.md)), and hand-typing sixteen fields invites exactly
      the kind of error this section exists to catch. Use the same delivery mechanism
      already used to get this checklist itself onto the vault root.
- [ ] Assign each property's type by hand in the Properties view, tick the 16 lines
      above, then delete the scratch note.
- [ ] Confirm the declarations survived deleting the note. Obsidian persists
      manually-assigned property types in `.obsidian/types.json`: `cat` it before
      deleting the note, delete the note, `cat` it again, and compare. All 16
      declarations survived deletion of the only note carrying them when this was run -
      declaring a type is not tied to a note continuing to exist. Expect roughly 22
      `TQ_*` entries in the same file, registered by the Tasks plugin for its own query
      properties; that is expected and is not schema drift.

Two vocabulary changes are easy to miss when checking values against an older draft:

- **`confidence` has three values, not four: `high`, `medium`, `speculation`.** `stated`
  was removed. It was never a confidence level - it meant "the source says this", which is
  provenance, and provenance is `authority`. See `CLAUDE.md` section 4.
- **There is no `person` type.** It folded into `entity`. There is no kind tag and no closed
  kind vocabulary to go with it - `tags:` stays free-form. The `type` vocabulary is closed
  and has eleven values: `note`, `source`, `entity`, `concept`, `project`, `task`,
  `decision`, `devlog`, `meeting`, `research`, `moc`.

Frontmatter normalisation (key order, ISO dates, lowercase tags, banned characters,
inserting missing required fields, stamping `created`/`updated`) is owned entirely by
the Python maintenance pass that runs outside the application, not by a plugin. No
Linter or date-stamping plugin is installed for this job; see `CLAUDE.md` section 5.

## Tasks

- [ ] Set the task format explicitly: **`taskFormat: "dataview"`**, the bracket
      inline-field format ratified in `CLAUDE.md` section 11. The plugin defaults to
      the emoji format, so leaving this alone *is* a choice, and a device where it is
      unset writes the other format silently. Mixed-format vaults cannot be read back
      and there is no converter, so set it on every device or none.
- [ ] Record the Obsidian version on every device that opens this vault, including iOS.
      **1.11.4 or newer is a hard prerequisite** on all of them: below it, alternating
      bracket fields render underlined or invisible. This is a reading-integrity
      failure on the exact platforms the vault replicates to.
- [ ] Keep vault markdown out of any generic formatter that normalises intra-line
      whitespace (Prettier, markdownlint autofix, editor format-on-save). The bracket
      format's two-space field separator does not survive one; see `CLAUDE.md`
      section 11.

## Verification, before declaring this done

- [ ] Open `TODO.md` and confirm both `base` blocks render. They were authored without a
      running instance and have never been rendered. Fix the syntax in place if they do
      not, and note it in `log.md`.
- [ ] Create one note from each of the five templates in `_templates/` and confirm the
      frontmatter lands with the declared types intact.
- [ ] Confirm the Properties panel shows each field with the type declared above, not
      as inferred text.
- [ ] Create one note per `type` from the section 3.1 blocks in `CLAUDE.md` - including
      the seven types with no template file - and confirm each block parses as valid YAML
      in the Properties panel. These blocks are now the only definition of those types'
      frontmatter, so a defect in one is not caught anywhere else.
- [ ] Confirm the filename rule holds end to end: create a note titled
      `"Two   Spaces / Test"` and confirm the file is `two-spaces-test.md`. Obsidian will
      not do this for you - the rule is enforced by writers and by the lint, not by the
      application - so this step is confirming that a hand-created file is named
      correctly, and that a wrongly-named one is what the lint later flags.
- [ ] Type a wrong-typed value into a declared property on purpose (text into a Date
      field) and record what happens: refused outright, or accepted with an advisory
      mismatch indicator. This is not documented either way, it takes a minute, and the
      answer decides how much of the ungated GUI path is already covered. Note the
      result in `log.md`; it is an input to the validation-plugin decision.

## Commit

**This section does not run during the GUI session.** Everything above it is GUI work and
completes in one sitting. Everything below needs a git working tree on the volume and a
`git update-index` run against the committer's own index - and the committer does not exist
until Phase 2, which is deliberately sequenced *after* this checklist so that its first push
baselines a fully configured `.obsidian/` rather than a half-finished one. So these steps are
committer *provisioning*, and belong to that work. Do not stall here looking for
preconditions that are not there yet.

`.gitignore` carries `.obsidian/` wholesale. The directory is a bootstrap artefact whose
job is a common config compatible with the minimum plugin and feature set the cluster
instance supports, not a mirror of that instance's evolving state. So the baseline needs a
forced add — but **the pathspec must be an allowlist, never a denylist naming only the two
workspace files.** A denylist here would commit
`.obsidian/plugins/obsidian-local-rest-api/data.json` — the file holding the vault's Local
REST API bearer token (`DESIGN.md` §2 item 5) — into permanent history on both remotes,
pulled to the Mac clone and published into iCloud and onto the phone (caught by review
before this ever ran; see `ppat/obsidian-tools#3`). `data.json` is the conventional
filename for *every* Obsidian plugin's settings, so a denylist would have to enumerate
every current and future secret-bearing file to stay safe; the next plugin that stores a
credential there would reintroduce the leak silently. Capture only what a device baseline
actually needs — shared application config, and a plugin's *code*, never its *state*:

**The same mistake recurs one level down if `snippets/`/`themes/` are named bare.** An
earlier version of this command did exactly that, and a bare directory prefix inside an
allowlist admits every file of any name at any depth underneath it, sight unseen — themes
are third-party code installed through the ungated GUI path, so "nothing secret would ever
land there" is not a claim this checklist gets to make. A probe against that version staged
`.obsidian/themes/Minimal/data.json`, `.obsidian/themes/deep/nested/inner/data.json` and
`.obsidian/snippets/sub/dir/creds.json`. Narrowed to file globs below, which costs a device
baseline nothing: `*.css` under both, plus a theme's own `manifest.json`.

```sh
git add --force -- \
  .obsidian/app.json \
  .obsidian/appearance.json \
  .obsidian/core-plugins.json \
  .obsidian/community-plugins.json \
  .obsidian/daily-notes.json \
  .obsidian/templates.json \
  .obsidian/hotkeys.json \
  .obsidian/types.json \
  .obsidian/snippets/*.css \
  .obsidian/themes/*/*.css \
  .obsidian/themes/*/manifest.json \
  .obsidian/plugins/*/manifest.json \
  .obsidian/plugins/*/main.js \
  .obsidian/plugins/*/styles.css
```

Only pass a given path if it actually exists — `git add` fails outright on a pathspec that
matches nothing, and not every device baseline needs every line above (a plugin that ships
with no stylesheet has no `styles.css` to add, for instance). `obsidian_tools/vault_git/baseline.py`
does this programmatically; if running this by hand, drop any line that doesn't apply here
rather than passing it as-is.

- [ ] Run the command above (with any inapplicable lines dropped), confirm with
      `git diff --cached --name-only` that no `data.json` and neither workspace file is
      staged, and commit. This is the only time `.obsidian/` is committed.
- [ ] Run `git update-index --skip-worktree` on every file committed in that baseline, on
      the committer's own working tree. `.gitignore` alone does not close this: git
      consults ignore rules only for untracked files, so a later change to a baselined
      file would still be staged by `git add -A`. Verified behaviour, not an assumption.
- [ ] Confirm no `.obsidian/` change is staged afterwards: touch a settings file, run
      `git add -A`, and check that `git status --short` reports nothing.

Accepted consequence, and it is deliberate: a setting changed at this GUI afterwards does
not reach the devices. Re-baselining is a manual act - change it, force-add `.obsidian/`
again, let the replication cycle carry it. Nothing does this on a schedule.

## Out of scope here

Mac-side settings ("Optimize Mac Storage" off, the iCloud vault location, the rsync
exclude list) belong to the read-replica work, not to this checklist.
