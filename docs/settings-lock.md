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

## Community plugins

- [ ] Verify Tasks and Dataview, and only those two, are present and enabled. Do not
      install them by hand: both arrive baked into the image and are enabled
      automatically the first time each one's plugin directory is seeded, which is
      exactly this first launch, so this step is a check, not an installation. If
      either is missing or disabled, that means the image did not seed correctly;
      fix that rather than installing the plugin from the GUI, which would leave the
      vault out of step with what the image bakes in on every rebuild. That is the
      whole set.
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

## Files and links

- [ ] Default location for new notes: **`00-inbox/`**.
- [ ] Default location for new attachments: **In the folder specified below**, set to
      **`_attachments/`**. The default behaviour drops attachments beside the note and
      becomes unfixable at scale. The folder is committed and stays empty; the vault is
      markdown only.
- [ ] Use `[[Wikilinks]]`: on.
- [ ] New link format: shortest path when possible.

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

Declare every property's type before any note exists. A field that holds a date in one
note and a string in another silently breaks every view over it, with no error.

**Assign each one by hand.** Obsidian records only the types you select manually; every
other property's type is inferred per note, which is exactly the drift this list exists
to prevent. Tick a line only after selecting the type in the Properties view.

- [ ] `created` - **Date**
- [ ] `updated` - **Date**
- [ ] `reviewed` - **Date**
- [ ] `consolidated` - **Date**
- [ ] `salience` - **Number**
- [ ] `tags` - **List** (tags)
- [ ] `aliases` - **List**
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

`consolidated` and `salience` are the two properties that no note carries at creation:
both are absent until the consolidation pass sets them (`CLAUDE.md` section 12). A property
no note carries may not be listed in the Properties view at all, so to declare its type,
create a throwaway note in `_ops/agent/` carrying `consolidated: 2026-01-01` and
`salience: 5`, assign both types by hand, then delete the note.

- [ ] After deleting the throwaway note, confirm both type declarations survived it. If
      Obsidian dropped either one, keep a single scratch note in `_ops/agent/` carrying
      both fields and record that in `log.md`, so the next reader knows why it exists.

Two vocabulary changes are easy to miss when checking values against an older draft:

- **`confidence` has three values, not four: `high`, `medium`, `speculation`.** `stated`
  was removed. It was never a confidence level - it meant "the source says this", which is
  provenance, and provenance is `authority`. See `CLAUDE.md` section 4.
- **There is no `person` type.** It folded into `entity`, distinguished by a required kind
  tag (`person`, `org`, `tool`, `place`). The `type` vocabulary is closed and has eleven
  values: `note`, `source`, `entity`, `concept`, `project`, `task`, `decision`, `devlog`,
  `meeting`, `research`, `moc`.

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

`.gitignore` carries `.obsidian/` wholesale. The directory is a bootstrap artefact whose
job is a common config compatible with the minimum plugin and feature set the cluster
instance supports, not a mirror of that instance's evolving state. So the baseline needs a
forced add, and the add must exclude the two per-instance workspace-state files the vendor
documents as ones to ignore. `--force` overrides every ignore rule, a specific one
included, which is why the exclusion has to live in the pathspec:

```sh
git add --force -- .obsidian/ \
  ':!.obsidian/workspace.json' ':!.obsidian/workspaces.json'
```

- [ ] Run the command above, confirm with `git diff --cached --name-only` that neither
      workspace file is staged, and commit. This is the only time `.obsidian/` is committed.
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
