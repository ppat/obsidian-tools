# Changelog

## [0.4.0](https://github.com/ppat/obsidian-tools/compare/v0.3.1...v0.4.0) (2026-08-01)


### 🛠 Improvements

* **dev-tools:** stop the Hypothesis profiles inheriting database=None inside GitHub Actions ([#40](https://github.com/ppat/obsidian-tools/issues/40)) ([b6dcdc5](https://github.com/ppat/obsidian-tools/commit/b6dcdc58d6d9474b04805f9ff3b1c776aedda6d0))


### ✨ Features

* **replication:** implement local-replicator (read-replica publication) ([#23](https://github.com/ppat/obsidian-tools/issues/23)) ([5ab1bd3](https://github.com/ppat/obsidian-tools/commit/5ab1bd35724433d13536eabe5e7d5946d9ca22af))


### 🚀 Enhancements + Bug Fixes

* **committer:** baseline the daily-notes and templates settings the Phase 1 lock set ([#43](https://github.com/ppat/obsidian-tools/issues/43)) ([babfd30](https://github.com/ppat/obsidian-tools/commit/babfd30c21141ee1a5d912583af0c85f43577938))
* **committer:** refuse a partial .obsidian/ baseline, and log what the allowlist drops ([#41](https://github.com/ppat/obsidian-tools/issues/41)) ([9504253](https://github.com/ppat/obsidian-tools/commit/9504253ca83e2e627398f615f1ed85e2c2dd41f6))
* **replication:** record the upstream-identity observation on every drift patch ([#42](https://github.com/ppat/obsidian-tools/issues/42)) ([bba5d99](https://github.com/ppat/obsidian-tools/commit/bba5d995f044def1149ffe71e18dbf6641f6905d))
* **replication:** stat each entry explicitly so an unreadable file withholds the device marker ([#44](https://github.com/ppat/obsidian-tools/issues/44)) ([a7aff96](https://github.com/ppat/obsidian-tools/commit/a7aff96420a6314bbe3d8c7d3400759d3d199d5f))

## [0.3.1](https://github.com/ppat/obsidian-tools/compare/v0.3.0...v0.3.1) (2026-08-01)


### 🚀 Enhancements + Bug Fixes

* **committer:** build the venv at its final path so the entrypoint can exec ([#33](https://github.com/ppat/obsidian-tools/issues/33)) ([66ad3eb](https://github.com/ppat/obsidian-tools/commit/66ad3eb9d999e79b8dfe4d5a766c434f813826c6))

## [0.3.0](https://github.com/ppat/obsidian-tools/compare/v0.2.1...v0.3.0) (2026-08-01)


### ✨ Features

* **committer:** assemble known_hosts at runtime; make the NAS remote optional ([#30](https://github.com/ppat/obsidian-tools/issues/30)) ([a5bd9df](https://github.com/ppat/obsidian-tools/commit/a5bd9df8845db3fd9cf1956c3242b128182c40d1))

## [0.2.1](https://github.com/ppat/obsidian-tools/compare/v0.2.0...v0.2.1) (2026-07-31)


### 🚀 Enhancements + Bug Fixes

* **committer:** extract six decisions into pure functions ([#27](https://github.com/ppat/obsidian-tools/issues/27)) ([5fda02b](https://github.com/ppat/obsidian-tools/commit/5fda02b8da17c77bda429aab240515d4cad14aaf))

## [0.2.0](https://github.com/ppat/obsidian-tools/compare/v0.1.0...v0.2.0) (2026-07-31)


### ✨ Features

* **committer:** add CLI entry point and the commit subcommand ([#22](https://github.com/ppat/obsidian-tools/issues/22)) ([d6887cf](https://github.com/ppat/obsidian-tools/commit/d6887cf1d24f1b09aeb8eb1282f78bb12a2062af))

## [0.1.0](https://github.com/ppat/obsidian-tools/compare/v0.0.2...v0.1.0) (2026-07-31)


### ✨ Features

* redesign the write path around a work queue with three streams ([#20](https://github.com/ppat/obsidian-tools/issues/20)) ([903be90](https://github.com/ppat/obsidian-tools/commit/903be90a1e28d00593cbdd0b5107f5d2a3b1de81))


### 🚀 Enhancements + Bug Fixes

* correct seven consistency defects from the write-path redesign review ([#21](https://github.com/ppat/obsidian-tools/issues/21)) ([1db8559](https://github.com/ppat/obsidian-tools/commit/1db85595b57d1052b841b00f31c6b42f5506d0b7))
* reconcile DESIGN.md with Phase 0/1 implementation reality ([#18](https://github.com/ppat/obsidian-tools/issues/18)) ([4d1b121](https://github.com/ppat/obsidian-tools/commit/4d1b121e47e809a790fd3da9ac6b08d0ba2690d1))

## [0.0.2](https://github.com/ppat/obsidian-tools/compare/v0.0.1...v0.0.2) (2026-07-30)


### 🛠 Improvements

* commit the research and design canon ([72e42b8](https://github.com/ppat/obsidian-tools/commit/72e42b85bb034bf088385ce277751a3401443117))
* commit the research and design canon ([a4c2b66](https://github.com/ppat/obsidian-tools/commit/a4c2b6617c0ab71281ea6dc0a6adea942aba8e0a))
* move the settings-lock checklist into this repository ([#13](https://github.com/ppat/obsidian-tools/issues/13)) ([839a993](https://github.com/ppat/obsidian-tools/commit/839a9939437c96ffe78ef895470f4b8e621e5db5))
* settings-lock corrections and a gui-access runbook, from a live GUI walkthrough ([#15](https://github.com/ppat/obsidian-tools/issues/15)) ([77be627](https://github.com/ppat/obsidian-tools/commit/77be627d1323c6ef76dee4008ca398a7318c4c77))


### 🚀 Enhancements + Bug Fixes

* drop the kind vocabulary from the entity note in the checklist ([#14](https://github.com/ppat/obsidian-tools/issues/14)) ([e65025c](https://github.com/ppat/obsidian-tools/commit/e65025cee9e919545c8acdb9c257c40caf88978d))
