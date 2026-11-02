#!/usr/bin/env python3
"""
RankLedger - keep a ledger of where your name (or brand) ranks in search.

The loop:  WATCH -> DETECT -> COUNTER-MOVE (proposed) -> REPEAT.

What it does:
  * Sweeps public search-result pages for the queries you configure in
    targets.json and records, in a local SQLite ledger, where every BAD
    page you want buried (data-broker profiles, stale listings) and every
    GOOD property you own (your site, your profiles) ranks per query -
    out to the top 100 where the source allows pagination (Bing, Brave,
    Yahoo, Yandex; DuckDuckGo stays a single page; Google is never
    scraped - see the honest-limits section of README.md).
  * Diffs each sweep against the previous one and raises alerts: a bad
    item newly appearing or climbing inside the top 30, reappearing after
    removal (a previously-removed URL alerts from anywhere in the top
    100), or a good property slipping off page 1. Deep-window movement
    (rank 31-100) is recorded on the status board, not alerted.
  * Maps every alert to a suggested counter-move from playbook.json and
    appends it to ALERTS.md. RankLedger PROPOSES. It never submits,
    posts, clicks, or files anything - acting on an alert is always a
    human decision.

Window honesty: every sweep records how deep it looked (its "window").
A target "appearing" below the previous sweep's window is NOT treated as
new - it may always have been sitting there, unseen. The diff only calls
something new/gone when the rank in question falls inside BOTH sweeps'
windows.

What it cannot do: it cannot see other people searching, and it cannot
reach into a search engine or anyone's browser to move a result.
Rankings live in the engines' indexes and are identical in every
browser. This tool watches the weather and tells you which lever to
pull next; the levers themselves are removal/opt-out at the source and
fresh owned content. See README.md.

Stdlib only. Written from scratch.

Usage:
  python3 rankledger.py init                 # create ledger + check data files
  python3 rankledger.py sweep                # live sweep, all sources
  python3 rankledger.py sweep --source bing  # one source only
  python3 rankledger.py report               # status board (full depth)
  python3 rankledger.py alerts               # show recent alerts
"""

import argparse
import base64
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "rankledger.db")
TARGETS_PATH = os.path.join(HERE, "targets.json")
PLAYBOOK_PATH = os.path.join(HERE, "playbook.json")
ALERTS_PATH = os.path.join(HERE, "ALERTS.md")

USER_AGENT = "RankLedger/1.0 (personal search-presence monitor; low-rate, public pages only)"
CONFIDENCE_NOTE = ("indicative only - ranks vary by geography, personalization "
                   "and data center; treat as a weather reading, not a record")
PAGE_SIZE = 10          # positions 1..10 = page 1
CLIMB_THRESHOLD = 3     # bad item climbing this many positions = alert
MAX_RESULTS = 100       # v2 deep window: top 100 per query/source
LEGACY_WINDOW = 40      # v1 sweeps were capped here (backfilled on migrate)
ALERT_ZONE = 30         # bad-item movement at/inside this rank can alert;
                        # deeper movement is board-only (still recorded)
PAGE_DELAY = 2.0        # seconds between pages of one paginated query

# ---------------------------------------------------------------- data files

def load_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)

def load_targets():
    data = load_json(TARGETS_PATH)
    for t in data["targets"]:
        t["_norm_host"] = (t.get("match_host") or "").lower().lstrip("www.")
    return data

def load_playbook():
    return load_json(PLAYBOOK_PATH).get("moves", {})

# ---------------------------------------------------------------- url utils

def norm_url(raw):
    """Canonical form for matching: lowercase host (no www.), path, no
    trailing slash, no query/fragment. Unwraps DuckDuckGo, Bing and Yahoo
    redirect links to their real destinations."""
    if not raw:
        return ""
    u = raw.strip()
    if u.startswith("//"):
        u = "https:" + u
    parsed = urllib.parse.urlparse(u)
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host in ("duckduckgo.com", "html.duckduckgo.com"):
        qs = urllib.parse.parse_qs(parsed.query)
        if "uddg" in qs:
            return norm_url(qs["uddg"][0])
    if host == "bing.com" and (parsed.path or "").rstrip("/") == "/ck/a":
        # Bing wraps results in a click-tracking redirect; the destination
        # rides in the 'u' parameter ("a1" + base64url). Unwrap to the real
        # destination; if it cannot be decoded, drop the wrapper entirely.
        qs = urllib.parse.parse_qs(parsed.query)
        u = (qs.get("u") or [""])[0]
        if u.startswith("a1"):
            try:
                padded = u[2:] + "=" * (-len(u[2:]) % 4)
                dest = base64.urlsafe_b64decode(padded.encode()).decode("utf-8")
                return norm_url(dest)
            except Exception:
                return ""
        return ""
    if host == "r.search.yahoo.com":
        # Yahoo wraps results in a redirect whose destination rides in the
        # /RU=<percent-encoded url>/ path segment.
        m = re.search(r"/RU=([^/]+)", u)
        if m:
            return norm_url(urllib.parse.unquote(m.group(1)))
        return ""
    path = urllib.parse.unquote(parsed.path or "")
    path = re.sub(r"/+$", "", path)
    return host + path

def target_matches(target, normed):
    host = target["_norm_host"]
    if not host:
        return False
    if not normed.startswith(host):
        return False
    frag = target.get("match_path_contains")
    if frag is None:
        return False  # explicitly unmeasurable until a rule exists
    if frag == "":
        return True
    rest = normed[len(host):].lower()
    return frag.lower() in rest

def rank_targets(ordered_normed, targets):
    """Return {target_id: (rank, page)} for targets found in the window."""
    found = {}
    for idx, nu in enumerate(ordered_normed, start=1):
        for t in targets:
            if t["id"] in found:
                continue
            if target_matches(t, nu):
                found[t["id"]] = (idx, (idx - 1) // PAGE_SIZE + 1)
    return found

# ---------------------------------------------------------------- html parse

class DDGParser(HTMLParser):
    """DuckDuckGo html endpoint: result links are <a class='result__a'>."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        d = dict(attrs)
        if "result__a" in (d.get("class") or ""):
            href = d.get("href")
            if href:
                self.links.append(href)


class BingParser(HTMLParser):
    """Bing: organic results live in <li class='b_algo'>, link in <h2><a>."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self._in_algo = 0
        self._in_h2 = False

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "li":
            if self._in_algo:
                self._in_algo += 1  # nested list inside a result block
            elif "b_algo" in (d.get("class") or ""):
                self._in_algo = 1
        elif tag == "h2" and self._in_algo:
            self._in_h2 = True
        elif tag == "a" and self._in_algo and self._in_h2:
            href = d.get("href")
            if href and href.startswith("http"):
                self.links.append(href)

    def handle_endtag(self, tag):
        if tag == "li" and self._in_algo:
            self._in_algo -= 1
        elif tag == "h2":
            self._in_h2 = False


class BraveParser(HTMLParser):
    """Brave: web results live in <div class='snippet ...'> blocks; the
    result link is an <a> inside. Collect every http href in the block in
    order — engine self-links are filtered during assembly."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "div":
            if self._depth:
                self._depth += 1
            elif "snippet" in (d.get("class") or ""):
                self._depth = 1
        elif tag == "a" and self._depth:
            href = d.get("href")
            if href and href.startswith("http"):
                self.links.append(href)

    def handle_endtag(self, tag):
        if tag == "div" and self._depth:
            self._depth -= 1


class YahooParser(HTMLParser):
    """Yahoo: organic results live in <div class='dd algo ...'>, title link
    in <h3 class='title'><a> (usually an r.search.yahoo.com redirect that
    norm_url unwraps)."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self._depth = 0
        self._in_h3 = False

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "div":
            if self._depth:
                self._depth += 1
            elif "algo" in (d.get("class") or "").split():
                self._depth = 1
        elif tag == "h3" and self._depth:
            self._in_h3 = True
        elif tag == "a" and self._depth and self._in_h3:
            href = d.get("href")
            if href and href.startswith("http"):
                self.links.append(href)

    def handle_endtag(self, tag):
        if tag == "div" and self._depth:
            self._depth -= 1
        elif tag == "h3":
            self._in_h3 = False


class YandexParser(HTMLParser):
    """Yandex: organic results live in <li class='serp-item ...'> blocks.
    Collect every http href in the block in order — yandex.* self-links are
    filtered during assembly."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "li":
            if self._depth:
                self._depth += 1
            elif "serp-item" in (d.get("class") or ""):
                self._depth = 1
        elif tag == "a" and self._depth:
            href = d.get("href")
            if href and href.startswith("http"):
                self.links.append(href)

    def handle_endtag(self, tag):
        if tag == "li" and self._depth:
            self._depth -= 1


PARSERS = {
    "ddg": DDGParser,
    "bing": BingParser,
    "brave": BraveParser,
    "yahoo": YahooParser,
    "yandex": YandexParser,
}

# Hosts that belong to the engine itself — never real results. (Bing's
# ck/a wrappers and Yahoo's r.search redirects are unwrapped by norm_url
# before this filter sees them.)
SELF_HOSTS = {
    "brave": ("brave.com",),
    "yahoo": ("yahoo.com",),
    "yandex": ("yandex.com", "yandex.ru", "ya.ru"),
}

def assemble_links(source, raw_links, cap=MAX_RESULTS):
    """Normalize, drop engine self-links, dedupe, cap — in page order."""
    out, seen = [], set()
    self_hosts = SELF_HOSTS.get(source, ())
    for link in raw_links:
        nu = norm_url(link)
        if not nu:
            continue
        host = nu.split("/", 1)[0]
        if any(host == sh or host.endswith("." + sh) for sh in self_hosts):
            continue
        if nu in seen:
            continue
        seen.add(nu)
        out.append(nu)
        if len(out) >= cap:
            break
    return out

def parse_results(source, html, cap=MAX_RESULTS):
    """Parse ONE result page into an ordered, deduped list of result URLs."""
    if source not in PARSERS:
        raise ValueError("unknown source: %s" % source)
    p = PARSERS[source]()
    p.feed(html)
    return assemble_links(source, p.links, cap)

# ---------------------------------------------------------------- fetching

def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", errors="replace")


def _page_url(source, q, page_idx):
    if source == "bing":
        return "https://www.bing.com/search?q=%s&count=10&first=%d" % (q, page_idx * 10 + 1)
    if source == "brave":
        return "https://search.brave.com/search?q=%s&source=web&offset=%d" % (q, page_idx)
    if source == "yahoo":
        return "https://search.yahoo.com/search?p=%s&n=10&b=%d" % (q, page_idx * 10 + 1)
    if source == "yandex":
        return "https://yandex.com/search/?text=%s&p=%d" % (q, page_idx)
    raise ValueError("no pagination for source: %s" % source)

# pages = how many result pages to walk; per_page = positions per page.
PAGINATED = {
    "bing":   {"pages": 10, "per_page": 10, "markers": ()},
    "brave":  {"pages": 5,  "per_page": 20, "markers": ("captcha",)},
    "yahoo":  {"pages": 10, "per_page": 10, "markers": ("captcha",)},
    "yandex": {"pages": 10, "per_page": 10,
               "markers": ("showcaptcha", "not a robot", "captcha")},
}

def _looks_blocked(source, status, html):
    if status != 200:
        return True
    low = html.lower()
    return any(m in low for m in PAGINATED[source]["markers"])

def sweep_query(source, query, fetcher=fetch, page_delay=PAGE_DELAY):
    """One query against one source.
    Returns (status, ordered_normed_urls, window).
    status: ok | blocked | error. Blocked is recorded, never evaded.
    window: how many positions were genuinely examined (for ddg, the
    single page's result count; for paginated sources, MAX_RESULTS when
    the full walk completed, else the unique results actually seen)."""
    q = urllib.parse.quote_plus(query)
    if source == "ddg":
        try:
            status, html = fetcher("https://html.duckduckgo.com/html/?q=" + q)
        except urllib.error.HTTPError as exc:
            return ("blocked" if exc.code in (403, 429, 503) else "error"), [], 0
        except Exception:
            return "error", [], 0
        low = html.lower()
        if status != 200 or "anomaly" in low or "captcha" in low:
            return "blocked", [], 0
        results = parse_results(source, html)
        if not results:
            return "blocked", [], 0
        return "ok", results, len(results)
    if source not in PAGINATED:
        raise ValueError("unknown source: %s" % source)
    spec = PAGINATED[source]
    ordered, seen = [], set()
    for page_idx in range(spec["pages"]):
        if page_idx and page_delay:
            time.sleep(page_delay)
        try:
            status, html = fetcher(_page_url(source, q, page_idx))
        except urllib.error.HTTPError as exc:
            if not ordered:
                return ("blocked" if exc.code in (403, 429, 503) else "error"), [], 0
            break  # partial walk: keep what we honestly saw
        except Exception:
            if not ordered:
                return "error", [], 0
            break
        if _looks_blocked(source, status, html):
            if not ordered:
                return "blocked", [], 0
            break
        page_links = parse_results(source, html, cap=MAX_RESULTS)
        new_links = [u for u in page_links if u not in seen]
        if not new_links:
            # Engine ran out of results (or repeats page 1): the window is
            # exactly what we have seen.
            return "ok", ordered, len(ordered)
        for u in new_links:
            seen.add(u)
            ordered.append(u)
            if len(ordered) >= MAX_RESULTS:
                break
        if len(ordered) >= MAX_RESULTS:
            break
    if not ordered:
        return "blocked", [], 0
    completed = len(ordered) >= min(MAX_RESULTS, spec["pages"] * spec["per_page"])
    window = MAX_RESULTS if completed else len(ordered)
    return "ok", ordered, window

# ---------------------------------------------------------------- ledger

SCHEMA = """
CREATE TABLE IF NOT EXISTS sweeps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    source TEXT NOT NULL,
    query TEXT NOT NULL,
    status TEXT NOT NULL,
    result_count INTEGER NOT NULL DEFAULT 0,
    window INTEGER NOT NULL DEFAULT 40
);
CREATE TABLE IF NOT EXISTS results (
    sweep_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    norm_url TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS target_ranks (
    sweep_id INTEGER NOT NULL,
    target_id TEXT NOT NULL,
    rank INTEGER,
    page INTEGER
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    sweep_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    target_id TEXT NOT NULL,
    query TEXT NOT NULL,
    source TEXT NOT NULL,
    detail TEXT NOT NULL,
    playbook_key TEXT NOT NULL
);
"""

def _migrate(conn):
    """v1 -> v2: sweeps gain a window column. Existing rows keep the
    DEFAULT 40 backfill — v1 never looked deeper than 40, so that is the
    honest window for every legacy sweep."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(sweeps)")]
    if "window" not in cols:
        conn.execute("ALTER TABLE sweeps ADD COLUMN window INTEGER NOT NULL DEFAULT %d"
                     % LEGACY_WINDOW)
        conn.commit()

def connect(db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn

def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

def previous_sweep_id(conn, source, query, before_id):
    row = conn.execute(
        "SELECT id FROM sweeps WHERE source=? AND query=? AND id<? AND status='ok' "
        "ORDER BY id DESC LIMIT 1", (source, query, before_id)).fetchone()
    return row[0] if row else None

def sweep_window(conn, sweep_id):
    row = conn.execute("SELECT window FROM sweeps WHERE id=?", (sweep_id,)).fetchone()
    return row[0] if row and row[0] else LEGACY_WINDOW

def ranks_for_sweep(conn, sweep_id):
    return {tid: (rk, pg) for tid, rk, pg in conn.execute(
        "SELECT target_id, rank, page FROM target_ranks WHERE sweep_id=?",
        (sweep_id,))}

def ever_seen_before(conn, source, query, target_id, before_id):
    row = conn.execute(
        "SELECT COUNT(*) FROM target_ranks tr JOIN sweeps s ON s.id = tr.sweep_id "
        "WHERE s.source=? AND s.query=? AND tr.sweep_id<? AND tr.target_id=? "
        "AND tr.rank IS NOT NULL", (source, query, before_id, target_id)).fetchone()
    return row[0] > 0

# ---------------------------------------------------------------- diff/alert

def playbook_key_for(kind, target):
    if kind == "good_slip":
        return "good_slip"
    if kind == "bad_reappear":
        return "bad_reappear"
    suffix = target.get("kind", "")
    key = "%s_%s" % (kind, suffix)
    return key

def diff_sweep(conn, sweep_id, source, query, targets_by_id):
    """Compare this sweep to the previous ok sweep for the same source+query.
    Returns a list of alert dicts (also written to the alerts table).

    v2 alert rules (window-aware):
      * bad_reappear — a target marked removed, visible again anywhere in
        the window; or a previously-seen bad url back inside the alert zone
        after a genuine absence (absence only counts when the rank falls
        inside the previous sweep's window too).
      * bad_new — never seen before, now inside the alert zone AND inside
        the previous sweep's window (so the absence last time was real).
        First sightings deeper than that are board-only.
      * bad_climb — both ranks known, climb >= threshold, landing inside
        the alert zone (includes entering the zone from deep).
      * good_slip — was on page 1, now off it or genuinely absent (current
        window must cover page 1 for an absence to count).
    """
    prev_id = previous_sweep_id(conn, source, query, sweep_id)
    cur = ranks_for_sweep(conn, sweep_id)
    ts = now_iso()
    alerts = []
    if prev_id is None:
        return alerts  # baseline sweep: record, don't alert
    prev = ranks_for_sweep(conn, prev_id)
    prev_window = sweep_window(conn, prev_id)
    cur_window = sweep_window(conn, sweep_id)

    def emit(kind, target, detail):
        key = playbook_key_for(kind, target)
        conn.execute(
            "INSERT INTO alerts (ts, sweep_id, kind, target_id, query, source, detail, playbook_key) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (ts, sweep_id, kind, target["id"], query, source, detail, key))
        alerts.append({"kind": kind, "target": target, "query": query,
                       "source": source, "detail": detail, "playbook_key": key})

    for tid, target in targets_by_id.items():
        cur_rank = cur.get(tid, (None, None))[0]
        prev_rank = prev.get(tid, (None, None))[0]
        if target["side"] == "bad":
            if cur_rank is not None and prev_rank is None:
                # Absence last sweep is only proven for ranks the previous
                # sweep actually examined.
                absence_proven = cur_rank <= prev_window
                if target.get("status") == "removed":
                    emit("bad_reappear", target,
                         "%s was marked REMOVED but is visible again at #%d "
                         "for '%s' (%s)." % (target["label"], cur_rank, query, source))
                elif not absence_proven:
                    pass  # first sighting below the old window: board-only
                elif ever_seen_before(conn, source, query, tid, prev_id):
                    if cur_rank <= ALERT_ZONE:
                        emit("bad_reappear", target,
                             "%s is visible again at #%d for '%s' (%s) after being gone."
                             % (target["label"], cur_rank, query, source))
                elif cur_rank <= ALERT_ZONE:
                    emit("bad_new", target,
                         "%s newly appeared at #%d for '%s' (%s)."
                         % (target["label"], cur_rank, query, source))
            elif cur_rank is not None and prev_rank is not None:
                if prev_rank - cur_rank >= CLIMB_THRESHOLD and cur_rank <= ALERT_ZONE:
                    emit("bad_climb", target,
                         "%s climbed #%d -> #%d for '%s' (%s)."
                         % (target["label"], prev_rank, cur_rank, query, source))
        else:  # good
            if target.get("status") != "active":
                continue  # pending properties can't slip yet
            if prev_rank is not None and prev_rank <= PAGE_SIZE:
                if cur_rank is not None and cur_rank > PAGE_SIZE:
                    emit("good_slip", target,
                         "%s slipped off page 1 for '%s' (%s): was #%d, now #%d."
                         % (target["label"], query, source, prev_rank, cur_rank))
                elif cur_rank is None and cur_window >= PAGE_SIZE:
                    emit("good_slip", target,
                         "%s slipped off page 1 for '%s' (%s): was #%d, now "
                         "absent from the top %d."
                         % (target["label"], query, source, prev_rank, cur_window))
    conn.commit()
    return alerts

def append_alerts_md(alerts, playbook):
    if not alerts:
        return
    fresh = not os.path.exists(ALERTS_PATH)
    with open(ALERTS_PATH, "a", encoding="utf-8") as fh:
        if fresh:
            fh.write("# RankLedger — Alerts\n\n"
                     "Every alert maps to a suggested counter-move from playbook.json. "
                     "RankLedger proposes; you decide; nothing here submits itself.\n")
        for a in alerts:
            move = playbook.get(a["playbook_key"]) or playbook.get("default", {})
            fh.write("\n## %s — %s\n\n" % (now_iso(), a["kind"]))
            fh.write("- **What:** %s\n" % a["detail"])
            fh.write("- **Counter-move (%s):** %s\n"
                     % (move.get("title", a["playbook_key"]), move.get("action", "")))

# ---------------------------------------------------------------- commands

def record_sweep(conn, source, query, status, ordered, targets, window=None):
    if window is None:
        window = len(ordered) if status == "ok" else 0
    cur = conn.execute(
        "INSERT INTO sweeps (ts, source, query, status, result_count, window) "
        "VALUES (?,?,?,?,?,?)",
        (now_iso(), source, query, status, len(ordered), window))
    sweep_id = cur.lastrowid
    if status == "ok":
        conn.executemany(
            "INSERT INTO results (sweep_id, position, norm_url) VALUES (?,?,?)",
            [(sweep_id, i + 1, u) for i, u in enumerate(ordered)])
        found = rank_targets(ordered, targets)
        conn.executemany(
            "INSERT INTO target_ranks (sweep_id, target_id, rank, page) VALUES (?,?,?,?)",
            [(sweep_id, t["id"], found.get(t["id"], (None, None))[0],
              found.get(t["id"], (None, None))[1]) for t in targets])
    conn.commit()
    return sweep_id

def cmd_init(_args):
    conn = connect()
    data = load_targets()
    load_playbook()
    n_bad = sum(1 for t in data["targets"] if t["side"] == "bad")
    n_good = sum(1 for t in data["targets"] if t["side"] == "good")
    print("Ledger ready: %s" % DB_PATH)
    print("Targets: %d bad, %d good | Queries: %d"
          % (n_bad, n_good, len(data["queries"])))
    conn.close()
    return 0

ALL_SOURCES = ["ddg", "bing", "brave", "yahoo", "yandex"]

def cmd_sweep(args):
    data = load_targets()
    targets = data["targets"]
    targets_by_id = {t["id"]: t for t in targets}
    playbook = load_playbook()
    conn = connect()
    sources = ALL_SOURCES if args.source == "all" else [args.source]
    all_alerts = []
    for source in sources:
        for query in data["queries"]:
            status, ordered, window = sweep_query(source, query,
                                                  page_delay=args.page_delay)
            sweep_id = record_sweep(conn, source, query, status, ordered,
                                    targets, window=window)
            note = ""
            if status == "ok":
                new_alerts = diff_sweep(conn, sweep_id, source, query, targets_by_id)
                all_alerts.extend(new_alerts)
                note = "%d results (window %d), %d new alert(s)" % (
                    len(ordered), window, len(new_alerts))
            else:
                note = "source %s - recorded, not retried" % status
            print("[%s] %-28s %s" % (source, "'%s'" % query, note))
            time.sleep(args.delay)
    append_alerts_md(all_alerts, playbook)
    print("Sweep complete (%s). Confidence: %s."
          % (now_iso(), CONFIDENCE_NOTE))
    if all_alerts:
        print("%d alert(s) written to ALERTS.md." % len(all_alerts))
    conn.close()
    return 0

def _fmt_rank(rank, window):
    if rank:
        return "#%d" % rank
    return ">%d" % window if window else "absent"

def cmd_report(_args):
    data = load_targets()
    targets = data["targets"]
    conn = connect()
    print("=" * 72)
    print("RANKLEDGER — STATUS BOARD (full depth, window up to %d)"
          % MAX_RESULTS)
    print("=" * 72)
    sources = [r[0] for r in conn.execute(
        "SELECT DISTINCT source FROM sweeps ORDER BY source")]
    if not sources:
        print("No sweeps yet. Run: python3 rankledger.py sweep")
        conn.close()
        return 0
    for source in sources:
        print("\n## Source: %s" % source)
        for query in data["queries"]:
            row = conn.execute(
                "SELECT id, ts, status, result_count, window FROM sweeps "
                "WHERE source=? AND query=? ORDER BY id DESC LIMIT 1",
                (source, query)).fetchone()
            if not row:
                continue
            sweep_id, ts, status, count, window = row
            print("\n  Query: '%s'  (last sweep %s, %s, %d results, window %d)"
                  % (query, ts, status, count, window or 0))
            if status != "ok":
                continue
            cur = ranks_for_sweep(conn, sweep_id)
            prev_id = previous_sweep_id(conn, source, query, sweep_id)
            prev = ranks_for_sweep(conn, prev_id) if prev_id else {}
            for side, title in (("bad", "BAD - push down"), ("good", "GOOD - push up")):
                print("    %s:" % title)
                for t in targets:
                    if t["side"] != side:
                        continue
                    rk = cur.get(t["id"], (None, None))[0]
                    prk = prev.get(t["id"], (None, None))[0]
                    move = ""
                    if rk and prk:
                        d = prk - rk
                        move = " (up %d)" % d if d > 0 else (" (down %d)" % -d if d < 0 else " (flat)")
                    elif rk and prev_id:
                        move = " (new in window)"
                    tag = "" if t["status"] == "active" else " [%s]" % t["status"]
                    print("      %-6s %s%s%s" % (_fmt_rank(rk, window), t["label"], tag, move))
    n_alerts = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
    print("\nTotal alerts on record: %d (see ALERTS.md)" % n_alerts)
    print("Confidence: %s." % CONFIDENCE_NOTE)
    conn.close()
    return 0

def cmd_alerts(args):
    conn = connect()
    rows = conn.execute(
        "SELECT ts, kind, query, source, detail FROM alerts ORDER BY id DESC LIMIT ?",
        (args.limit,)).fetchall()
    if not rows:
        print("No alerts recorded.")
    for ts, kind, query, source, detail in rows:
        print("%s [%s] (%s / %s) %s" % (ts, kind, query, source, detail))
    conn.close()
    return 0

def main(argv=None):
    ap = argparse.ArgumentParser(description="RankLedger v2")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    sw = sub.add_parser("sweep")
    sw.add_argument("--source", choices=ALL_SOURCES + ["all"], default="all")
    sw.add_argument("--delay", type=float, default=6.0,
                    help="seconds between query sweeps (polite rate; default 6)")
    sw.add_argument("--page-delay", type=float, default=PAGE_DELAY,
                    help="seconds between pages of one query (default %.1f)" % PAGE_DELAY)
    sub.add_parser("report")
    al = sub.add_parser("alerts")
    al.add_argument("--limit", type=int, default=20)
    args = ap.parse_args(argv)
    if args.cmd == "init":
        return cmd_init(args)
    if args.cmd == "sweep":
        return cmd_sweep(args)
    if args.cmd == "report":
        return cmd_report(args)
    if args.cmd == "alerts":
        return cmd_alerts(args)
    return 2

if __name__ == "__main__":
    sys.exit(main())
