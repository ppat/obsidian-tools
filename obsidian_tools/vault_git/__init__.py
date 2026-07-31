"""Git helpers shared by every obsidian-tools subcommand that produces or consumes vault history.

`commit` (the in-cluster git committer) is the first consumer. `replicate` (the Mac-side
`local-replicator`, tracked separately at ppat/obsidian-tools#3) is expected to reuse
`GitRunner` for its own pull-only clone rather than growing a second git wrapper.
"""
