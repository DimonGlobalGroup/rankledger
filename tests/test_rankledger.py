#!/usr/bin/env python3
"""
Self-test for RankLedger. NO live network calls.

Runs against the EXAMPLE targets.json that ships with the repo
(placeholders: "Your Name", example.com URLs) and proves:

  1. Smoke: the ledger DB initializes in a temp dir with the full schema.
  2. DuckDuckGo fixture parsing extracts the ordered result URLs
     (redirect wrappers unwrapped), and target ranking finds the seeded
     good/bad example URLs at the right positions. The 'pending' example
     target never matches.
  3. The diff engine fires exactly the right alerts across three sweeps:
       - baseline sweep  -> no alerts
       - sweep B         -> bad_climb (broker profile #6 -> #1)
                            + good_slip (your website #3 -> #15)
       - sweep C         -> bad_reappear (a listing marked 'removed'
                            reappears at #40 - any depth alerts)
  4. The report renders from the fixture data in the ledger.
  5. Alerts render into ALERTS.md with their playbook counter-moves.

Run:  python3 tests/test_rankledger.py   (exit 0 = pass)
"""

import contextlib
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import rankledger  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FAILURES = []


def check(name, cond, extra=""):
    print("%s %s %s" % ("PASS" if cond else "FAIL", name, extra))
    if not cond:
        FAILURES.append(name)


def fillers(n, start=1):
    return ["example.com/filler-%d" % i for i in range(start, start + n)]


def run():
    data = rankledger.load_targets()
    targets = data["targets"]
    by_id = {t["id"]: t for t in targets}
    playbook = rankledger.load_playbook()
    tmp = tempfile.mkdtemp(prefix="rankledger-test-")
    db_path = os.path.join(tmp, "test.db")

    # ---- 1. smoke: ledger initializes in a temp dir --------------------
    conn = rankledger.connect(db_path)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    check("smoke: ledger schema created",
          {"sweeps", "results", "target_ranks", "alerts"} <= tables,
          str(sorted(tables)))

    # ---- 2. parsing + ranking on the example fixture --------------------
    with open(os.path.join(HERE, "fixtures", "ddg_example.html")) as fh:
        urls = rankledger.parse_results("ddg", fh.read())
    check("ddg parse order", urls == [
        "example.com/filler-article",
        "social.example.com/your-name",
        "example.com/your-website",
        "example.com/filler-two",
        "directory.example.com/someone-else",
        "people-search.example.com/your-name"], "got %s" % urls)

    ranks = rankledger.rank_targets(urls, targets)
    check("rank my-profile #2", ranks.get("my-profile") == (2, 1),
          str(ranks.get("my-profile")))
    check("rank my-website #3", ranks.get("my-website") == (3, 1),
          str(ranks.get("my-website")))
    check("rank broker-profile #6", ranks.get("broker-profile") == (6, 1),
          str(ranks.get("broker-profile")))
    check("pending target never matches", "my-new-page" not in ranks)
    check("removed listing absent in fixture", "stale-listing" not in ranks)

    # ---- 3. diff across three sweeps ------------------------------------
    src, query = "fixture", "Your Name"

    sid_a = rankledger.record_sweep(conn, src, query, "ok", urls, targets)
    alerts_a = rankledger.diff_sweep(conn, sid_a, src, query, by_id)
    check("baseline: no alerts", alerts_a == [], str(alerts_a))

    # Sweep B: broker climbs #6 -> #1; website slips #3 -> #15; profile holds.
    sweep_b = (["people-search.example.com/your-name",
                "social.example.com/your-name"]
               + fillers(12, 21)
               + ["example.com/your-website"]
               + fillers(5, 41))
    sid_b = rankledger.record_sweep(conn, src, query, "ok", sweep_b, targets)
    alerts_b = rankledger.diff_sweep(conn, sid_b, src, query, by_id)
    kinds_b = sorted(a["kind"] + ":" + a["target"]["id"] for a in alerts_b)
    check("sweep B alerts",
          kinds_b == ["bad_climb:broker-profile", "good_slip:my-website"],
          str(kinds_b))
    check("sweep B playbook keys",
          {a["playbook_key"] for a in alerts_b} == {"bad_climb_broker", "good_slip"},
          str({a["playbook_key"] for a in alerts_b}))

    # Sweep C: the 'removed' listing reappears at #40; nothing else moves.
    sweep_c = (sweep_b + fillers(19, 61)
               + ["directory.example.com/your-name"]
               + fillers(3, 81))
    sid_c = rankledger.record_sweep(conn, src, query, "ok", sweep_c, targets)
    alerts_c = rankledger.diff_sweep(conn, sid_c, src, query, by_id)
    kinds_c = [a["kind"] + ":" + a["target"]["id"] for a in alerts_c]
    check("sweep C alerts", kinds_c == ["bad_reappear:stale-listing"],
          str(kinds_c))
    conn.close()

    # ---- 4. report renders from the fixture data -------------------------
    orig_connect = rankledger.connect
    rankledger.connect = lambda dp=None: orig_connect(dp or db_path)
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rankledger.cmd_report(None)
        out = buf.getvalue()
    finally:
        rankledger.connect = orig_connect
    check("report header", "RANKLEDGER" in out and "STATUS BOARD" in out)
    check("report shows example targets",
          "Your website" in out and "Your social profile" in out)
    check("report counts alerts", "Total alerts on record: 3" in out)

    # ---- 5. ALERTS.md rendering ------------------------------------------
    rankledger.ALERTS_PATH = os.path.join(tmp, "ALERTS.md")
    rankledger.append_alerts_md(alerts_b + alerts_c, playbook)
    with open(rankledger.ALERTS_PATH) as fh:
        md = fh.read()
    check("alerts.md has counter-moves",
          "Counter-move" in md and "opt-out" in md.lower() and "Re-file" in md,
          "")

    print("\n%s" % ("ALL TESTS PASSED" if not FAILURES else
                    "FAILURES: %s" % ", ".join(FAILURES)))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(run())
