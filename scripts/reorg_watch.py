#!/usr/bin/env python3
"""
reorg_watch.py — EDGAR scan for Chapter 11 filings and emergences.

Two signals, both from 8-K filings via EDGAR full-text search:

  EMERGENCE  — company has emerged from Chapter 11 and issued NEW equity,
               usually to former creditors who never wanted it. This is the
               candidate list. The forced sellers are the creditors.
  BANKRUPTCY — 8-K Item 1.03 (Bankruptcy or Receivership) with no emergence
               language. WATCHLIST ONLY. The old equity is almost always
               cancelled and worthless — never buy it. Track for emergence.

Like spinoff_watch.py, this automates discovery and fact-gathering only.
It does not rank, summarise, or judge. Reading the plan is the user's job.

USAGE
  python reorg_watch.py --email you@example.com --days 10
  python reorg_watch.py --self-test
  python reorg_watch.py --email you@example.com --start 2025-01-01 --end 2025-12-31 \
      --no-enrich --no-notes --out ./backtest_out      # back-test a past window

Every run also writes reorg_dropped.csv: emergence-phrase hits that the
item filter threw away. Skim it now and then to check the filter is not
discarding real emergences.

Uses EDGAR full-text search (efts.sec.gov) — the backend of SEC's own search
page. Same User-Agent and rate-limit rules as the rest of EDGAR apply.
"""

import argparse
import csv
import os
import re
import sys
from datetime import date, datetime, timedelta
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from spinoff_watch import Fetcher, enrich, facts_block  # noqa: E402

FTS = "https://efts.sec.gov/LATEST/search-index"
FILING_IDX = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{acc}-index.htm"

# Phrases that indicate a completed emergence, not just a filing.
EMERGENCE_PHRASES = [
    '"emerged from chapter 11"',
    '"emergence from chapter 11"',
    '"emerged from bankruptcy"',
    '"plan of reorganization became effective"',
]
# "effective date of the plan" was dropped: it also matches equity-incentive
# plans and merger plans (TechPrecision, Aureus Greenway false positives, Oct 2026).

# An emergence 8-K reports the new capital structure: new securities (3.02),
# changed holder rights (3.03), change in control (5.01) or a new charter (5.03).
# A filing-day 8-K (Item 1.03 + DIP loan 1.01/2.03 + delisting notice 3.01)
# has none of these, even when the text talks about "the plan".
EMERGENCE_ITEMS = {"3.02", "3.03", "5.01", "5.03"}
BANKRUPTCY_QUERY = '"chapter 11"'
PAGE = 100
MAX_PAGES = 5


# ---------------------------------------------------------------- search

def fts_url(query, start, end, offset=0):
    return FTS + "?" + urlencode({
        "q": query, "forms": "8-K",
        "dateRange": "custom", "category": "custom",
        "startdt": start.isoformat(), "enddt": end.isoformat(),
        "from": offset,
    })


def parse_hits(payload):
    """Normalise an EDGAR full-text-search JSON page into flat rows.
    Defensive: skips anything missing a CIK or accession."""
    rows = []
    hits = (payload or {}).get("hits", {}).get("hits", []) or []
    for h in hits:
        s = h.get("_source", {}) or {}
        ciks = s.get("ciks") or []
        adsh = s.get("adsh") or (h.get("_id", "").split(":")[0] or None)
        if not ciks or not adsh:
            continue
        try:
            cik = int(str(ciks[0]).lstrip("0") or 0)
        except ValueError:
            continue
        names = s.get("display_names") or [""]
        # display_names look like "Acme Corp  (ACME)  (CIK 0001234567)"
        raw = names[0]
        name = re.sub(r"\s*\(CIK[^)]*\)", "", raw).strip()
        tick = re.findall(r"\(([A-Z.\-]{1,6})\)", name)
        name = re.sub(r"\s*\([A-Z.\-]{1,6}\)", "", name).strip()
        rows.append({
            "cik": cik,
            "company": name,
            "ticker": tick[0] if tick else "",
            "filed": s.get("file_date", ""),
            "form": s.get("form", "8-K"),
            "accession": adsh,
            "items": ",".join(s.get("items") or []),
            "filing_index": FILING_IDX.format(
                cik=cik, acc_nodash=adsh.replace("-", ""), acc=adsh),
        })
    return rows


def total_hits(payload):
    t = (payload or {}).get("hits", {}).get("total", {})
    return t.get("value", 0) if isinstance(t, dict) else int(t or 0)


def search(f, query, start, end, max_pages=None):
    out = []
    for p in range(max_pages or MAX_PAGES):
        data = f.get(fts_url(query, start, end, p * PAGE), expect="json")
        if not data:
            break
        page = parse_hits(data)
        out.extend(page)
        if len(page) < PAGE or (p + 1) * PAGE >= total_hits(data):
            break
    return out


# ---------------------------------------------------------------- classify

def classify(emergence_rows, bankruptcy_rows):
    """
    One row per company. Emergence beats bankruptcy — a company that filed
    and emerged inside the window is a candidate, not a watch item.
    Bankruptcy rows only count if the 8-K actually carries Item 1.03.
    Latest filing per company wins.
    """
    best = {}

    def keep(row, status):
        row = dict(row, status=status)
        cur = best.get(row["cik"])
        rank = {"EMERGENCE": 2, "BANKRUPTCY": 1}
        if (cur is None or rank[status] > rank[cur["status"]]
                or (rank[status] == rank[cur["status"]] and row["filed"] > cur["filed"])):
            best[row["cik"]] = row

    for r in emergence_rows:
        items = set((r.get("items") or "").split(","))
        if items & EMERGENCE_ITEMS:
            keep(r, "EMERGENCE")
        elif "1.03" in items:
            keep(r, "BANKRUPTCY")      # e.g. a first-day filing describing its plan
        # otherwise: phrase matched but no capital-structure change -> noise
    for r in bankruptcy_rows:
        if "1.03" in (r.get("items") or "").split(","):
            keep(r, "BANKRUPTCY")
    return sorted(best.values(), key=lambda r: (r["status"] != "EMERGENCE", r["filed"]), reverse=False)


def dropped(emergence_rows, kept):
    """Emergence-phrase hits discarded by the item filter, one per company,
    excluding companies that were kept anyway. For auditing the filter."""
    kept_ciks = {r["cik"] for r in kept}
    out = {}
    for r in emergence_rows:
        items = set((r.get("items") or "").split(","))
        if items & EMERGENCE_ITEMS or "1.03" in items or r["cik"] in kept_ciks:
            continue
        cur = out.get(r["cik"])
        if cur is None or r["filed"] > cur["filed"]:
            out[r["cik"]] = dict(r, status="DROPPED")
    return sorted(out.values(), key=lambda r: r["filed"])


# ---------------------------------------------------------------- output

EMERGENCE_NOTE = """# {company}  (CIK {cik})  — POST-REORG CANDIDATE

* Emergence 8-K filed: {filed}   Items: {items}
* Filing index: {filing_index}
* Ticker (if assigned): {ticker}
* Industry: {sic_desc}

## Machine-pulled facts — READ THE WARNING
> **Pre-reorganisation data.** XBRL describes the OLD capital structure. Old
> shares are usually cancelled at emergence, so share counts, equity and debt
> below are likely meaningless. Get the NEW share count and debt from the
> emergence 8-K and the plan. Revenue is still a fair guide to business size.

{facts_block}

## Size filter first
- [ ] Plan equity value (from the disclosure statement's valuation) : $____
- [ ] Over ~$500M? **Discard unread.** Distressed specialists own that space.

## Venue and disclosure — discard gates
- [ ] Where does the new equity trade? Exchange / OTCQX / OTCQB / Pink
- [ ] Will the company keep filing with the SEC? A Form 15 deregistration
      means no ongoing disclosure → **discard.**
- [ ] Pink with no current information → **discard.**

## THE TEST: who is forced to sell?
- [ ] Which creditor classes received the new equity?
- [ ] Are they mandate-bound sellers? (CLOs and many bond funds cannot hold
      equity; distressed-debt funds often exit after emergence)
- [ ] Is the selling still happening, or finished?

## Capital structure — the post-reorg specifics
- [ ] New share count, and how much went to each class
- [ ] Warrants issued to old equity or junior creditors (dilution overhang)
- [ ] Management incentive plan size (% of equity) and strike — incentive signal
- [ ] Rights offering? Who backstopped it, at what discount?
- [ ] Exit financing: new debt load, maturities, covenants
- [ ] Fresh-start accounting — the balance sheet has been reset; old ratios mean nothing

## Valuation
- [ ] Plan's valuation range for the reorganised equity vs current price
- [ ] Normalised earnings power post-restructuring
- [ ] Catalysts: uplisting, index inclusion, first analyst coverage, refinancing
- [ ] What would have to be true for me to be wrong?

## Verdict
Thesis in 3 sentences:
Disconfirming evidence I looked for:
Pass / Watch / Paper position (size, date, price):
"""

BANKRUPTCY_NOTE = """# {company}  (CIK {cik})  — IN CHAPTER 11 (WATCH ONLY)

* Item 1.03 8-K filed: {filed}
* Filing index: {filing_index}

**Do not buy the existing equity.** Old shares are almost always cancelled
when a plan is confirmed. The opportunity, if any, is the NEW equity after
emergence — this company will reappear as an EMERGENCE row if that happens.

- [ ] Prepackaged / pre-negotiated (weeks to months) or free-fall (6–18+ months)?
- [ ] Who is expected to own the reorganised company?
- [ ] Rough business size (revenue) — would it pass the size filter later?
"""


COLUMNS = ["status", "filed", "form", "company", "ticker", "cik", "items",
           "sic_desc", "xbrl", "xbrl_stale", "revenue", "revenue_alt",
           "filing_index"]


def write_dropped(rows, outdir):
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, "reorg_dropped.csv")
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["status", "filed", "form", "company",
                                           "ticker", "cik", "items", "filing_index"],
                           extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return path


def write_outputs(rows, outdir, notes=True):
    os.makedirs(outdir, exist_ok=True)
    csv_path = os.path.join(outdir, "reorg.csv")
    with open(csv_path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)

    made = []
    for r in (rows if notes else []):
        sub = "reorg" if r["status"] == "EMERGENCE" else "reorg-watch"
        d = os.path.join(outdir, "notes", sub)
        os.makedirs(d, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "-", r["company"].lower()).strip("-")[:60] or str(r["cik"])
        p = os.path.join(d, f"{r['filed']}_{slug}.md")
        if os.path.exists(p):
            continue
        tpl = EMERGENCE_NOTE if r["status"] == "EMERGENCE" else BANKRUPTCY_NOTE
        with open(p, "w") as fh:
            fh.write(tpl.format(
                company=r.get("company", ""), cik=r.get("cik", ""),
                filed=r.get("filed", ""), items=r.get("items", "") or "?",
                filing_index=r.get("filing_index", ""),
                ticker=r.get("ticker", "") or "n/a",
                sic_desc=r.get("sic_desc", "") or "unknown",
                facts_block=facts_block(r)))
        made.append(p)
    return csv_path, made


# ---------------------------------------------------------------- self-test

def _fixture(hits, total=None):
    return {"hits": {"total": {"value": total if total is not None else len(hits)},
                     "hits": hits}}


def _hit(cik, name, date_, adsh, items):
    return {"_id": f"{adsh}:x.htm", "_source": {
        "ciks": [f"{cik:010d}"], "display_names": [name],
        "file_date": date_, "form": "8-K", "adsh": adsh, "items": items}}


def self_test():
    em = parse_hits(_fixture([
        _hit(1111, "Phoenix Holdings Inc  (PHNX)  (CIK 0000001111)", "2026-09-10",
             "0000001111-26-000050", ["1.03", "3.03", "5.02"]),
    ]))
    assert em[0]["company"] == "Phoenix Holdings Inc" and em[0]["ticker"] == "PHNX", em
    assert em[0]["cik"] == 1111
    assert "000000111126000050" in em[0]["filing_index"].replace("-", "")
    print("PASS  parse: name, ticker, CIK and filing URL extracted from display_names")

    bk = parse_hits(_fixture([
        _hit(1111, "Phoenix Holdings Inc  (CIK 0000001111)", "2026-09-02",
             "0000001111-26-000040", ["1.03"]),                 # same co, earlier filing
        _hit(2222, "Sinking Retail Corp  (CIK 0000002222)", "2026-09-12",
             "0000002222-26-000010", ["1.03", "9.01"]),         # genuine new filing
        _hit(3333, "Mentions Only LLC  (CIK 0000003333)", "2026-09-11",
             "0000003333-26-000009", ["8.01"]),                 # says "chapter 11", not Item 1.03
        {"_id": "junk", "_source": {}},                           # malformed hit
    ]))
    assert len(bk) == 3, "malformed hit should be skipped"
    print("PASS  parse: malformed hits skipped without crashing")

    noisy = parse_hits(_fixture([
        _hit(4444, "Leslie's, Inc.  (LESL)  (CIK 0001821806)", "2026-10-05",
             "0001193125-26-414287", ["1.01", "1.03", "2.03", "3.01", "9.01"]),  # filing day
        _hit(5555, "TechPrecision Corp  (TPCS)  (CIK 0001328792)", "2026-09-29",
             "0001104659-26-111987", ["5.02", "5.07", "9.01"]),                  # equity plan vote
    ]))
    rows = classify(em + noisy, bk)
    by = {r["cik"]: r for r in rows}
    assert by[4444]["status"] == "BANKRUPTCY", "filing-day 8-K is not an emergence"
    assert 5555 not in by, "phrase hit without capital-structure items is noise"
    print("PASS  classify: filing-day 8-K demoted to watch; equity-plan noise dropped")
    dr = dropped(em + noisy, rows)
    assert [r["cik"] for r in dr] == [5555], dr
    assert dr[0]["status"] == "DROPPED"
    print("PASS  dropped: filtered phrase hits are kept for audit, kept companies excluded")
    assert by[1111]["status"] == "EMERGENCE", "emergence must beat an earlier filing"
    assert by[2222]["status"] == "BANKRUPTCY"
    assert 3333 not in by, "a passing mention of chapter 11 without Item 1.03 is noise"
    assert rows[0]["status"] == "EMERGENCE", "candidates listed first"
    print("PASS  classify: emergence beats filing, Item 1.03 required, candidates first")

    assert total_hits(_fixture([], total=240)) == 240
    print("PASS  pagination total read")

    tmp = "/tmp/reorg_selftest"
    os.system(f"rm -rf {tmp}")
    csv_path, made = write_outputs(rows, tmp)
    head = open(csv_path).readline().strip().split(",")
    assert head == COLUMNS
    em_note = [p for p in made if "/reorg/" in p][0]
    bk_note = [p for p in made if "/reorg-watch/" in p][0]
    t = open(em_note).read()
    assert "Pre-reorganisation data" in t and "forced to sell" in t and "Form 15" in t
    b = open(bk_note).read()
    assert "Do not buy the existing equity" in b
    _, again = write_outputs(rows, tmp)
    assert again == [], "must not clobber existing notes"
    print("PASS  outputs: candidate and watch notes routed to separate folders, "
          "warnings present, idempotent")

    print("\nAll self-tests passed.")
    return 0


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="EDGAR Chapter 11 / emergence watcher")
    ap.add_argument("--email")
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--out", default="./spinoff_out")
    ap.add_argument("--no-enrich", action="store_true")
    ap.add_argument("--max-pages", type=int, default=MAX_PAGES,
                    help="100 hits per page per query (raise for long back-tests)")
    ap.add_argument("--no-notes", action="store_true", help="CSV only (back-tests)")
    ap.add_argument("--start", help="YYYY-MM-DD; overrides --days")
    ap.add_argument("--end", help="YYYY-MM-DD; default today")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if not a.email:
        ap.error("--email is required (SEC blocks anonymous scrapers)")

    end = date.fromisoformat(a.end) if a.end else date.today()
    start = date.fromisoformat(a.start) if a.start else end - timedelta(days=a.days)
    f = Fetcher(a.email)
    print(f"Scanning 8-Ks for Chapter 11 activity {start} .. {end}", file=sys.stderr)

    em = []
    for q in EMERGENCE_PHRASES:
        em.extend(search(f, q, start, end, a.max_pages))
    bk = search(f, BANKRUPTCY_QUERY, start, end, a.max_pages)
    rows = classify(em, bk)

    if not a.no_enrich:
        for r in rows:
            enrich(f, r)

    csv_path, made = write_outputs(rows, a.out, notes=not a.no_notes)
    dr = dropped(em, rows)
    write_dropped(dr, a.out)
    n_em = sum(r["status"] == "EMERGENCE" for r in rows)
    n_bk = sum(r["status"] == "BANKRUPTCY" for r in rows)
    print(f"\nEmergence candidates: {n_em}   Chapter 11 watch: {n_bk}   "
          f"Dropped by item filter: {len(dr)}")
    print(f"CSV: {csv_path}   ({len(made)} new notes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())