# Changelog

All notable changes to RankLedger are recorded here.

## [1.0.0] — 2026-11-01

First public edition.

- Local SQLite ledger of search-result positions for the queries and
  good/bad URLs you configure in `targets.json`.
- Sweeps across five public result sources — Bing, Brave, Yahoo and
  Yandex paginated out to the top 100, DuckDuckGo as a single page —
  at a polite rate, with blocked sources recorded honestly, never evaded.
- Window-aware diffing between sweeps: a target is only called "new" or
  "gone" when the rank in question falls inside both sweeps' windows.
- Alerts for the movement that matters: a bad page newly appearing or
  climbing inside the top 30, a removed page reappearing anywhere in the
  top 100, and a good property slipping off page 1.
- Every alert is written to `ALERTS.md` with a suggested counter-move
  from `playbook.json`. RankLedger proposes; it never submits, posts,
  or files anything itself.
- Full-depth status board (`report`) and alert history (`alerts`).
- Ships with an example `targets.json`; replace the placeholders with
  your own name, brand, and pages.
- Stdlib only. No accounts, no API keys, no telemetry — the ledger
  never leaves your machine.
