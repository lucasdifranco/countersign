Parts of this skill and of the `auditor-*.md` agents are adapted from:

Elephant-Goldfish Model — https://github.com/google/elephant-goldfish-model
Copyright 2026 Google LLC. Licensed under the Apache License, Version 2.0
(https://www.apache.org/licenses/LICENSE-2.0). Version used: commit 94d948d (2026-11-23).

Source files: skills/egm-review, skills/egm-review-loop, skills/egm-resolve,
agents/comprehension.md, agents/design_critic.md, agents/implementation_readiness.md,
agents/codebase_details.md, agents/synthesizer.md.

Changes: translation and rewrite in English; code audit (diff/commit) in addition to document;
direct dispatch of the auditors by the main session; independent verifier per finding;
evidence rules (file:line, two paths) and task folder rules; removal of presets, brainstorm,
draft and single-question gates. `auditor-verifier.md` is original.

In this repository the five `auditor-*.md` files are the ones used by `audit.py`, which appends an adaptation
text to each one: on the server the roles have no tools, they receive the target and the reading list in the
prompt itself, and synthesis and verifier answer in JSON.
