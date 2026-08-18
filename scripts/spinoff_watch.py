#!/usr/bin/env python3
"""
spinoff_watch.py — EDGAR Form 10 spin-off discovery + fact extraction.

WHAT THIS AUTOMATES (the drudgery, zero alpha):
  1. Finds every new Form 10 / 10-12B / 10-12G registration (how a US spin-off
     registers its shares) in a date window.
  2. Pulls the structured metadata: company, CIK, filing date, accession, doc URLs.
  3. Where XBRL exists, pulls hard numbers (shares out, debt, revenue, equity).
  4. Emits a CSV + a per-situation markdown stub with the FORCED-SELLER CHECKLIST
     you fill in by reading. That reading is the part that is not automatable.

USAGE
  python spinoff_watch.py --email you@example.com --days 120
  python spinoff_watch.py --email you@example.com --start 2026-01-01 --end 2026-08-18
  python spinoff_watch.py --self-test          # offline logic test, no network

SEC RULES (enforced here, don't remove):
  * A descriptive User-Agent with a real email is REQUIRED. No email -> SEC blocks you.
  * Max 10 requests/second. This script throttles to ~7/s and backs off on 429.
  Ref: https://www.sec.gov/os/webmaster-faq#developers
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from urllib.parse import urlencode

try:
    import requests
except ImportError:
    sys.exit("pip install requests")

SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
COMPANYFACTS = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
FULL_INDEX = "https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{qtr}/form.idx"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/"
FILING_IDX = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{acc}-index.htm"

# 10-12B: registration tied to a DISTRIBUTION — this is the spin-off form.
SPINOFF_FORMS = {"10-12B", "10-12B/A"}
# 10-12G: general Section 12(g) registration. Mostly OTC companies, funds and
# SPACs registering a share class — NOT demergers, so no structural forced
# seller exists. Off by default; --include-12g to widen the net.
REGISTRATION_FORMS = {"10-12G", "10-12G/A"}

# XBRL older than this is stale enough to mislead (e.g. a company that stopped
# filing years ago still returns its last figures as "latest").
STALE_MONTHS = 18

MIN_INTERVAL = 1.0 / 7.0  # ~7 req/s, under SEC's 10/s ceiling


class Fetcher:
    """Throttled, retrying SEC client with a compliant User-Agent."""

    def __init__(self, email, verbose=True):
        if not email or "@" not in email:
            raise ValueError("SEC requires a real email in the User-Agent header.")
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": f"spinoff-research/1.0 ({email})",
            "Accept-Encoding": "gzip, deflate",
        })
        self._last = 0.0
        self.verbose = verbose

    def get(self, url, tries=4, expect="text"):
        for attempt in range(tries):
            gap = time.time() - self._last
            if gap < MIN_INTERVAL:
                time.sleep(MIN_INTERVAL - gap)
            try:
                r = self.s.get(url, timeout=30)
                self._last = time.time()
                if r.status_code == 200:
                    return r.json() if expect == "json" else r.text
                if r.status_code in (429, 403):
                    wait = 2 ** attempt
                    if self.verbose:
                        print(f"  throttled ({r.status_code}), sleeping {wait}s", file=sys.stderr)
                    time.sleep(wait)
                    continue
                if r.status_code == 404:
                    return None
            except requests.RequestException as e:
                if self.verbose:
                    print(f"  net error: {e}", file=sys.stderr)
                time.sleep(2 ** attempt)
        return None


# ---------------------------------------------------------------- discovery

def quarters_in_range(start: date, end: date):
    """Yield (year, quarter) pairs covering the range."""
    out = []
    y, q = start.year, (start.month - 1) // 3 + 1
    while (y, q) <= (end.year, (end.month - 1) // 3 + 1):
        out.append((y, q))
        q += 1
        if q > 4:
            y, q = y + 1, 1
    return out


def parse_form_idx(text: str, start: date, end: date, forms=None):
    """
    Parse EDGAR's fixed-width form.idx and return matching Form 10 rows.
    Columns: Form Type | Company Name | CIK | Date Filed | File Name
    """
    forms = SPINOFF_FORMS if forms is None else forms
    rows = []
    for line in text.splitlines():
        if not line.strip() or line.startswith(("Form Type", "-", " ")) and not line[:12].strip():
            pass
        parts = re.split(r"\s{2,}", line.strip())
        if len(parts) < 5:
            continue
        form = parts[0].strip().upper()
        if form not in forms:
            continue
        company, cik_s, dt_s, path = parts[1], parts[2], parts[3], parts[4]
        try:
            cik = int(cik_s)
            filed = datetime.strptime(dt_s.strip(), "%Y-%m-%d").date()
        except ValueError:
            continue
        if not (start <= filed <= end):
            continue
        acc = path.strip().split("/")[-1].replace(".txt", "")
        rows.append({
            "form": form,
            "company": company.strip(),
            "cik": cik,
            "filed": filed.isoformat(),
            "accession": acc,
            "filing_index": FILING_IDX.format(
                cik=cik, acc_nodash=acc.replace("-", ""), acc=acc),
        })
    return rows


def discover(f: Fetcher, start: date, end: date, forms=None):
    """Pull Form 10 registrations from the quarterly full indexes."""
    forms = SPINOFF_FORMS if forms is None else forms
    seen, out = set(), []
    for (y, q) in quarters_in_range(start, end):
        url = FULL_INDEX.format(year=y, qtr=q)
        print(f"[discover] {y} Q{q} ...", file=sys.stderr)
        txt = f.get(url)
        if not txt:
            print(f"  no index for {y} Q{q}", file=sys.stderr)
            continue
        for row in parse_form_idx(txt, start, end, forms=forms):
            key = (row["cik"], row["accession"])
            if key not in seen:
                seen.add(key)
                out.append(row)
    out.sort(key=lambda r: r["filed"], reverse=True)
    return out


# ---------------------------------------------------------------- enrichment

def _latest_units(facts, tag, taxonomy="us-gaap", unit_pref=("USD", "shares")):
    node = facts.get("facts", {}).get(taxonomy, {}).get(tag)
    if not node:
        return None
    for unit in unit_pref:
        series = node.get("units", {}).get(unit)
        if not series:
            continue
        dated = [x for x in series if x.get("end")]
        if not dated:
            continue
        best = max(dated, key=lambda x: x["end"])
        return {"tag": tag, "value": best.get("val"), "unit": unit, "as_of": best.get("end")}
    return None


def enrich(f: Fetcher, row: dict):
    """Attach XBRL facts + SIC/industry where available. Many spincos have no XBRL yet."""
    cik = row["cik"]
    sub = f.get(SUBMISSIONS.format(cik=cik), expect="json")
    if sub:
        row["sic"] = sub.get("sic", "")
        row["sic_desc"] = sub.get("sicDescription", "")
        row["exchange"] = ",".join(filter(None, sub.get("exchanges", []) or []))
        row["ticker"] = ",".join(filter(None, sub.get("tickers", []) or []))
        row["state"] = sub.get("stateOfIncorporation", "")

    cf = f.get(COMPANYFACTS.format(cik=cik), expect="json")
    if not cf:
        row["xbrl"] = "none-yet"
        return row
    wanted = [
        ("shares_out", "CommonStockSharesOutstanding", ("shares",)),
        ("shares_dei", "EntityCommonStockSharesOutstanding", ("shares",)),
        ("revenue", "Revenues", ("USD",)),
        ("revenue_alt", "RevenueFromContractWithCustomerExcludingAssessedTax", ("USD",)),
        ("total_debt", "LongTermDebtNoncurrent", ("USD",)),
        ("debt_current", "LongTermDebtCurrent", ("USD",)),
        ("equity", "StockholdersEquity", ("USD",)),
        ("op_income", "OperatingIncomeLoss", ("USD",)),
        ("net_income", "NetIncomeLoss", ("USD",)),
        ("cash", "CashAndCashEquivalentsAtCarryingValue", ("USD",)),
    ]
    got = {}
    for label, tag, units in wanted:
        tax = "dei" if tag.startswith("Entity") else "us-gaap"
        hit = _latest_units(cf, tag, taxonomy=tax, unit_pref=units)
        if hit:
            got[label] = hit
    row["xbrl"] = "yes" if got else "none-yet"
    for label, hit in got.items():
        row[label] = hit["value"]
        row[f"{label}_asof"] = hit["as_of"]

    # Staleness: EDGAR happily returns a defunct filer's last-ever numbers as
    # "latest". Flag anything older than STALE_MONTHS so it can't be read as current.
    dates = [h["as_of"] for h in got.values() if h.get("as_of")]
    if dates:
        newest = max(dates)
        row["xbrl_latest_period"] = newest
        try:
            d = datetime.strptime(newest, "%Y-%m-%d").date()
            months = (date.today() - d).days / 30.44
            row["xbrl_age_months"] = round(months, 1)
            if months > STALE_MONTHS:
                row["xbrl_stale"] = f"STALE ({months/12:.1f}y old)"
                row["xbrl"] = "stale"
            else:
                row["xbrl_stale"] = ""
        except ValueError:
            row["xbrl_stale"] = "unparseable date"
    return row


# ---------------------------------------------------------------- output

CHECKLIST = """# {company}  (CIK {cik})

* Form: {form}   Filed: {filed}
* Filing index: {filing_index}
* Ticker/exchange (if assigned): {ticker} {exchange}
* Industry: {sic_desc} (SIC {sic})
* XBRL available: {xbrl}

## Machine-pulled facts (verify against the Form 10 — do not trust blindly)
{facts_block}

## THE TEST: who is forced to sell, and is the reason unrelated to value?

- [ ] **Forced seller identified?** Who must dump this regardless of price?
      (parent's index funds ineligible to hold spinco / too small for institutional
      mandates / wrong sector / holders receive odd-lot fractions)
- [ ] **Size mismatch** — spinco market cap as % of parent. The smaller, the
      more mechanical the selling. Note it: ____%
- [ ] **Index treatment** — will it be added to an index, or excluded? Excluded
      is the interesting case.
- [ ] **When does the forced selling exhaust?** Distribution date + weeks.
- [ ] Structural ugliness (mandate/liquidity) vs fundamental ugliness (bad
      business the parent was right to dump). WHICH IS IT, and why?

## Management incentives (read the Form 10's compensation + ownership items)
- [ ] Who is going to the spinco vs staying at the parent? Do the good people move?
- [ ] Equity grants struck at/near the when-issued price? Sizeable?
- [ ] Insider open-market buying after distribution?
- [ ] Was this division starved of capital inside the parent?

## Valuation
- [ ] Segment financials from the PARENT's old 10-Ks (pre-spin history)
- [ ] Pro-forma debt loaded onto the spinco — how much, what covenants, what rate?
- [ ] Normalised earnings power; comps
- [ ] What is it worth, and what would have to be true for me to be wrong?

## Verdict
Thesis in 3 sentences:
Disconfirming evidence I looked for:
Pass / Watch / Paper position (size, date, price):
"""


def facts_block(row):
    keys = ["shares_out", "shares_dei", "revenue", "revenue_alt", "op_income",
            "net_income", "total_debt", "debt_current", "equity", "cash"]
    lines = []
    if row.get("xbrl_stale"):
        lines.append(
            f"> **WARNING — {row['xbrl_stale']}.** Latest XBRL period is "
            f"{row.get('xbrl_latest_period','?')}. These numbers are NOT current "
            f"and may belong to a filer that went dark. Ignore them and read the filing.\n")
    for k in keys:
        if k in row and row[k] is not None:
            v = row[k]
            v = f"{v:,.0f}" if isinstance(v, (int, float)) else v
            lines.append(f"* {k}: {v}  (as of {row.get(k + '_asof','?')})")
    return "\n".join(lines) if lines else "* none — no XBRL filed yet. Read the Form 10 by hand."


COLUMNS = ["filed", "form", "company", "cik", "ticker", "exchange", "sic_desc",
           "xbrl", "xbrl_stale", "xbrl_latest_period", "xbrl_age_months",
           "shares_out", "shares_dei", "revenue", "revenue_alt",
           "op_income", "net_income", "total_debt", "debt_current", "equity",
           "cash", "state", "filing_index"]


def write_outputs(rows, outdir):
    os.makedirs(outdir, exist_ok=True)
    csv_path = os.path.join(outdir, "spinoffs.csv")
    with open(csv_path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)

    notes_dir = os.path.join(outdir, "notes")
    os.makedirs(notes_dir, exist_ok=True)
    made = []
    for r in rows:
        slug = re.sub(r"[^a-z0-9]+", "-", r["company"].lower()).strip("-")[:60]
        p = os.path.join(notes_dir, f"{r['filed']}_{slug}.md")
        if os.path.exists(p):
            continue
        with open(p, "w") as fh:
            fh.write(CHECKLIST.format(
                company=r.get("company", ""), cik=r.get("cik", ""),
                form=r.get("form", ""), filed=r.get("filed", ""),
                filing_index=r.get("filing_index", ""),
                ticker=r.get("ticker", "") or "n/a",
                exchange=r.get("exchange", "") or "",
                sic_desc=r.get("sic_desc", "") or "unknown",
                sic=r.get("sic", "") or "?",
                xbrl=r.get("xbrl", "?"),
                facts_block=facts_block(r)))
        made.append(p)
    return csv_path, notes_dir, made


# ---------------------------------------------------------------- self-test

FIXTURE = """Form Type                             Company Name                            CIK        Date Filed  File Name
---------------------------------------------------------------------------------------------------------------------
10-12B                                Amentum Holdings, Inc.                  2011286    2026-03-14  edgar/data/2011286/0001234567-26-000123.txt
10-K                                  Some Other Co                           99999      2026-03-14  edgar/data/99999/0000000000-26-000001.txt
10-12G                                Tiny Spinco LLC                         2022999    2026-05-02  edgar/data/2022999/0001111111-26-000045.txt
10-12B/A                              Amentum Holdings, Inc.                  2011286    2026-04-01  edgar/data/2011286/0001234567-26-000200.txt
10-12B                                Stale Oldco                             123456     2019-01-04  edgar/data/123456/0009999999-19-000001.txt
8-K                                   Noise Corp                              55555      2026-03-15  edgar/data/55555/0005555555-26-000010.txt
"""


def self_test():
    ok = True

    # Default: 10-12B only. The 10-12G row must NOT appear.
    rows = parse_form_idx(FIXTURE, date(2026, 1, 1), date(2026, 8, 18))
    forms = sorted(r["form"] for r in rows)
    assert forms == ["10-12B", "10-12B/A"], forms
    assert not any("Tiny Spinco" in r["company"] for r in rows), "10-12G leaked in by default"
    print(f"PASS  default forms: {len(rows)} rows, 10-12G excluded, 10-K/8-K/out-of-range dropped")

    # Widened: 10-12G included on request.
    wide = parse_form_idx(FIXTURE, date(2026, 1, 1), date(2026, 8, 18),
                          forms=SPINOFF_FORMS | REGISTRATION_FORMS)
    assert sorted(r["form"] for r in wide) == ["10-12B", "10-12B/A", "10-12G"]
    print(f"PASS  --include-12g widens to {len(wide)} rows")

    assert all(r["cik"] > 0 for r in rows)
    a = [r for r in rows if r["company"].startswith("Amentum")][0]
    assert a["accession"] == "0001234567-26-000123", a["accession"]
    assert "2011286" in a["filing_index"] and "000123456726000123" in a["filing_index"].replace("-", "")
    print(f"PASS  accession + URL build: {a['filing_index']}")

    qs = quarters_in_range(date(2025, 11, 5), date(2026, 8, 18))
    assert qs == [(2025, 4), (2026, 1), (2026, 2), (2026, 3)], qs
    print(f"PASS  quarter spanning: {qs}")

    fake_cf = {"facts": {
        "us-gaap": {
            "Revenues": {"units": {"USD": [
                {"end": "2025-12-31", "val": 1000},
                {"end": "2026-06-30", "val": 1250}]}},
            "LongTermDebtNoncurrent": {"units": {"USD": [{"end": "2026-06-30", "val": 800000}]}}},
        "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
            {"end": "2026-06-30", "val": 42000000}]}}}}}
    rev = _latest_units(fake_cf, "Revenues")
    assert rev["value"] == 1250 and rev["as_of"] == "2026-06-30", rev
    sh = _latest_units(fake_cf, "EntityCommonStockSharesOutstanding", taxonomy="dei", unit_pref=("shares",))
    assert sh["value"] == 42000000
    assert _latest_units(fake_cf, "NotATag") is None
    print("PASS  XBRL: picks most recent period, right units, missing tag -> None")

    # Staleness regression — modelled on the real B-Scada case, where EDGAR
    # returned 2016 figures as "latest" and they read as current.
    def stale_check(as_of):
        r = {}
        got = {"revenue": {"value": 784540, "as_of": as_of}}
        dates = [h["as_of"] for h in got.values()]
        newest = max(dates)
        d = datetime.strptime(newest, "%Y-%m-%d").date()
        months = (date.today() - d).days / 30.44
        r["xbrl_stale"] = f"STALE ({months/12:.1f}y old)" if months > STALE_MONTHS else ""
        r["xbrl_latest_period"] = newest
        return r

    old = stale_check("2016-04-30")
    assert old["xbrl_stale"].startswith("STALE"), old
    fresh = stale_check(date.today().isoformat())
    assert fresh["xbrl_stale"] == "", fresh
    print(f"PASS  staleness: 2016 data -> '{old['xbrl_stale']}', current data -> clean")

    warned = facts_block({"revenue": 784540, "revenue_asof": "2016-04-30",
                          "xbrl_stale": "STALE (10.3y old)",
                          "xbrl_latest_period": "2016-04-30"})
    assert "WARNING" in warned and "NOT current" in warned
    print("PASS  stale warning renders at top of the note's facts block")

    tmp = "/tmp/spinoff_selftest"
    os.system(f"rm -rf {tmp}")
    enriched = dict(rows[0]); enriched.update({
        "sic_desc": "Services-Engineering", "sic": "8711", "ticker": "AMTM",
        "exchange": "NYSE", "xbrl": "yes", "shares_dei": 42000000,
        "shares_dei_asof": "2026-06-30", "total_debt": 800000,
        "total_debt_asof": "2026-06-30"})
    csv_path, notes_dir, made = write_outputs([enriched], tmp)
    assert os.path.exists(csv_path)
    with open(csv_path) as fh:
        head = fh.readline().strip().split(",")
        assert head == COLUMNS
        body = fh.readline()
        assert "Amentum" in body and "42000000" in body
    assert len(made) == 1
    note = open(made[0]).read()
    assert "forced to sell" in note and "42,000,000" in note and "8711" in note
    _, _, again = write_outputs([enriched], tmp)
    assert again == [], "should not clobber existing notes"
    print(f"PASS  outputs: csv cols ok, note written w/ formatted facts, idempotent")

    try:
        Fetcher("not-an-email")
        ok = False
        print("FAIL  should reject bad email")
    except ValueError:
        print("PASS  refuses to run without a valid SEC User-Agent email")

    print("\nAll self-tests passed." if ok else "\nFAILURES above.")
    return 0 if ok else 1


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="EDGAR Form 10 spin-off watcher")
    ap.add_argument("--email", help="your email — SEC requires it in User-Agent")
    ap.add_argument("--days", type=int, default=180, help="lookback window (default 180)")
    ap.add_argument("--start", help="YYYY-MM-DD (overrides --days)")
    ap.add_argument("--end", help="YYYY-MM-DD (default today)")
    ap.add_argument("--out", default="./spinoff_out", help="output dir")
    ap.add_argument("--no-enrich", action="store_true", help="skip XBRL lookups (faster)")
    ap.add_argument("--include-12g", action="store_true",
                    help="also capture 10-12G registrations (mostly NOT spin-offs: "
                         "OTC listings, funds, SPACs). Adds noise; off by default.")
    ap.add_argument("--self-test", action="store_true", help="offline logic test")
    a = ap.parse_args()

    if a.self_test:
        return self_test()
    if not a.email:
        ap.error("--email is required (SEC blocks anonymous scrapers)")

    end = datetime.strptime(a.end, "%Y-%m-%d").date() if a.end else date.today()
    start = (datetime.strptime(a.start, "%Y-%m-%d").date() if a.start
             else end - timedelta(days=a.days))
    print(f"Scanning EDGAR Form 10 registrations {start} .. {end}", file=sys.stderr)

    forms = set(SPINOFF_FORMS)
    if a.include_12g:
        forms |= REGISTRATION_FORMS
    print(f"Forms: {', '.join(sorted(forms))}", file=sys.stderr)

    f = Fetcher(a.email)
    rows = discover(f, start, end, forms=forms)
    print(f"[discover] {len(rows)} Form 10 filings found", file=sys.stderr)

    if not a.no_enrich:
        for i, r in enumerate(rows, 1):
            print(f"[enrich {i}/{len(rows)}] {r['company'][:50]}", file=sys.stderr)
            enrich(f, r)

    csv_path, notes_dir, made = write_outputs(rows, a.out)
    print(f"\nCSV:   {csv_path}")
    print(f"Notes: {notes_dir}  ({len(made)} new checklist stubs)")
    print("\nNext: open each note, fill the forced-seller checklist by READING the Form 10.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
