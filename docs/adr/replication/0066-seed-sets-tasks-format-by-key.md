# 0066. The device seed sets the Tasks plugin's task format by key, without carrying any plugin's `data.json`

**Status:** Proposed (adds one key-level write to the seed in [ADR-0028](./0028-settings-baseline-seed.md); supersedes none of it) ·
**Pillar:** [The device loop is non-destructive by ordering](../../../DESIGN.md#the-device-loop-is-non-destructive-by-ordering-not-by-hope) ·
**Serves:** [R1](../../../USE_CASES.md#axis-3--readers-connected) ·
**Unit:** [B8](../../../ROADMAP.md#group-b--connection-work)

## Context

The vault mandates the Tasks plugin's bracket format for task metadata
([ADR-0016](../content-model/0016-task-metadata-bracket-format.md)). Left unset, the plugin
defaults to the emoji format. That mismatch is silent, and it cannot be undone once tasks are
written.

The setting lives in the plugin's `.obsidian/plugins/obsidian-tasks-plugin/data.json`. The settings
baseline withholds every plugin's `data.json` categorically, because that filename is also where
plugins keep credentials, including the REST API's bearer token
([ADR-0028](./0028-settings-baseline-seed.md)). So today the operator sets the format by hand on each
device. The owner does no manual steps.

## Decision

**The seed writes one key.** When `local-replicator` seeds a device's `.obsidian/`, it also sets
`taskFormat: "dataview"` in the Tasks plugin's `data.json` on the device. It creates the file if it
is missing, and it leaves every other key as it finds it. It never copies a `data.json` from the
baseline or from the instance, so the allowlist's categorical rule stands unchanged.

The iCloud vault directory is the one both devices open, so one seed covers the Mac and every iPhone
or iPad that opens the vault — provided no device overrides its configuration folder [inferred]. A device that does is outside
the seed altogether, as it already is for the settings baseline.

## Alternatives considered

- **Setting it by hand on each device** — a manual step, and one that fails silently.
- **An allowlist exception for one plugin's `data.json`** — it turns a categorical rule into a
  judgement call per plugin, which is the mistake the rule exists to prevent.

## Consequences

- **A freshly seeded device arrives with the right format.** The seed stays a seed: after it, the
  device owns its configuration ([ADR-0028](./0028-settings-baseline-seed.md)).
- **The runbook's hand step for this setting retires** once B8 builds the seed step.
