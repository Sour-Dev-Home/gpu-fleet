# 0001. Record architecture decisions as ADRs

**Status:** Accepted
**Date:** 2026-11-25

## Context
Decisions were spread across issue comments (Metaphor #1013, #1025), PR bodies, commit messages and chat.
None of these answers "why is it like this?" six months later, or for a reviewer seeing the repo cold.

## Decision
Every decision about architecture, protocols, storage, security posture, cost or tooling gets an ADR in
`docs/adr/`, in the same PR as the change it describes. Proposals that the maintainer hasn't approved yet are
marked **Proposed** until they are.

## Consequences
- A few minutes per decision.
- Reviews can check code against the recorded intent.
- Superseding is explicit instead of silent.
