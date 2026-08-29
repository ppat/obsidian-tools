"""obsidian-tools: code for BRAIN, a git-backed Obsidian vault written to by a human and multiple AI agents.

Ships as a single console entry point, `obsidian-tools`, with one subcommand per component (see
`obsidian_tools/cli.py`). `commit` — the in-cluster git committer — is implemented; further
subcommands land as their own tickets are implemented. See ../DESIGN.md for the design of record,
../docs/adr/README.md for the decision records it rests on, and ../ROADMAP.md for sequencing.
"""

__version__ = "0.1.0"
