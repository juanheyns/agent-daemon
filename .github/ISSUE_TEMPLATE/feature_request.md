---
name: Feature request
about: Suggest a change to the daemon, client, or protocol
title: ""
labels: enhancement
---

## Problem
<!-- What are you trying to do? What's blocking you today? -->

## Proposal
<!-- Concrete change you have in mind. Wire-protocol impact, if any. -->

## Alternatives considered
<!-- Other approaches and why they're worse. Optional. -->

## Out of scope

`blemeesd` is pass-through by design — it brokers `claude -p` sessions,
it does not add tool protocols, system prompts, event filtering, or
multi-user / system-daemon modes. Requests in those directions will
likely be closed. See [the spec](../../README.md#2-goals-and-non-goals).
