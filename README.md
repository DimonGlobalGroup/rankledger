# RankLedger

**Your name, your search results, watched on a schedule.**

Search your name or your brand today and you'll find two kinds of pages:
the ones you own and want people to find — and the ones you don't.
Data-broker profiles, stale listings, pages you already got removed once
that quietly came back. They move. Brokers re-list on cycles, rankings
shift, and nobody tells you when a page you buried climbs back toward
page 1.

RankLedger keeps the ledger. It sweeps public search results for your
queries on a schedule, records where every page you track — good and
bad — actually ranks, sweep after sweep, in a local SQLite database,
and alerts you when something moves in a way that matters: a bad page
climbing, a removed page reappearing, a good property slipping off
page 1. Each alert comes with a suggested counter-move. RankLedger
proposes; **you** act. It never submits, posts, or files anything itself.

By Dimon Global Group.

## Why a ledger, not a one-off search

A single search tells you where things stand *right now, from here*.
RankLedger's value is the diff: every sweep is compared against the
previous one, with **window honesty** — each sweep records how deep it
genuinely looked (its window), and a page is only called "new" or "gone"
when the rank in question falls inside *both* sweeps' windows. A bad
page first spotted at #50 after a shallower sweep isn't "new" — it may
have been sitting there all along. It goes on the board, watched, and
only alerts if it climbs toward the light.

## What it does

- **WATCH** — sweeps five public result sources (Bing, Brave, Yahoo and
  Yandex paginated out to the **top 100**; DuckDuckGo as a single page)
  at a polite rate with an honest user-agent. A source that blocks the
  sweep is recorded as *blocked* and left alone — never evaded.
- **DETECT** — every sweep lands in the ledger: the full ordered result
  list, the rank of every tracked target, and the sweep's window.
- **ALERT** — diffs raise alerts on page-1-relevant movement:
  | Alert | Fires when |
  |---|---|
  | `bad_new` | a bad URL appears inside the top 30, provably absent before |
  | `bad_climb` | a bad URL climbs ≥ 3 positions and lands inside the top 30 |
  | `bad_reappear` | a bad URL marked **removed** is visible again *anywhere* in the top 100 — or a known bad URL returns to the top 30 after a proven absence |
  | `good_slip` | a good property falls off page 1 |
  
  Movement deeper than the top 30 is written to the status board, not
  alerted — a bad page sitting at #40 is already buried for any human
  purpose; what matters is whether it starts moving.
- **COUNTER-MOVE** — each alert is appended to `ALERTS.md` with the
  suggested next step from `playbook.json` (file the opt-out, re-file
  the removal, refresh the property). Yours to approve and execute.

## Quickstart

Requires Python 3.9+. Standard library only — no installs, no accounts,
no API keys.

```bash
python3 rankledger.py init     # create the ledger, check the data files
python3 rankledger.py sweep    # live sweep, all sources
python3 rankledger.py report   # status board: every target, rank + movement
python3 rankledger.py alerts   # recent alerts
```

One source only: `python3 rankledger.py sweep --source bing`

Run it on a schedule so the ledger stays a ledger — e.g. cron:

```cron
15 8 * * *  cd /path/to/rankledger && python3 rankledger.py sweep
```

## Configure it: `targets.json`

The repo ships with an **example** `targets.json` full of placeholders
(`Your Name`, `example.com` URLs). Replace them with your own:

- `queries` — the searches to watch: your name, name + city, your brand.
- `targets` — every page to track, each with:
  - `side`: `"bad"` (push down) or `"good"` (push up)
  - `match_host` + `match_path_contains`: how to recognize the page in
    result lists (set `match_path_contains` to `null` for a placeholder
    that shouldn't match anything yet)
  - `kind`: free-form category (`broker`, `listing`, `site`, `social`…)
    — used to pick the playbook counter-move
  - `status`: `active` · `pending` (not watched for slips yet) ·
    `removed` (a bad page you got taken down — RankLedger watches for
    its return)

Edit `playbook.json` to make the counter-moves match *your* playbook.

## Honest limits

- **It samples public search results; it cannot see other people's
  searches.** Rankings live on the engines' servers. There is no signal
  that reports who searched for you, and no way to reach into an engine
  or a browser and move a result. Anything claiming otherwise doesn't
  exist.
- **Ranks are indicative.** They vary by geography, personalization, and
  data center. Treat a rank as a weather reading, not a court record —
  every sweep is labeled with its source and timestamp.
- **Google is not scraped** (blocks + terms of service). The five
  sources here are the zero-budget automated layer; check Google
  positions by hand when they matter, or plug in a paid SERP API.
- **No ranking manipulation.** RankLedger never clicks results, never
  bots traffic. The levers that actually bury a page are removal at the
  source and owned content that outranks it — this tool tells you which
  lever to pull, and when.
- **AI answer engines drink from the same wells** — the same indexes and
  broker databases. Remove the bad data at the source and there is
  nothing left for an AI to find.

## Privacy

Everything runs locally. The ledger (`rankledger.db`) and `ALERTS.md`
stay on your machine; there is no telemetry and nothing phones home.
The only network traffic is the sweeps themselves, fetching public
result pages at a polite rate.

## Files

| File | What |
|---|---|
| `rankledger.py` | the tool (stdlib only, from scratch) |
| `targets.json` | your queries + tracked good/bad URLs (example data ships) |
| `playbook.json` | alert → counter-move mapping |
| `rankledger.db` | SQLite ledger: sweeps (with window depth), results, target ranks, alerts (created by `init`) |
| `ALERTS.md` | human-readable alert log with counter-moves (created on first alert) |
| `tests/` | self-test, no live calls: `python3 tests/test_rankledger.py` |

## License

MIT — see [LICENSE](LICENSE). © 2026 Dimon Global Group.
