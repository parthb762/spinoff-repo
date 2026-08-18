# spinoff-watch

Weekly EDGAR scan for US spin-off registrations (Form 10 / 10-12B / 10-12G).

Discovery and fact-extraction are automated. **Analysis is not, by design** —
the edge is in reading the filings, so this repo hands you filings, not verdicts.

## Setup

1. Push this repo to GitHub (private is fine — Actions works on private repos).
2. Settings → Secrets and variables → Actions → New repository secret:
   - Name: `SEC_EMAIL`
   - Value: your real email (the SEC blocks anonymous scrapers)
3. Settings → Actions → General → Workflow permissions → **Read and write**.
4. Actions tab → "Weekly spinoff scan" → **Run workflow** to test it now.

Runs automatically 08:00 Monday Brisbane time. Adjust the cron in
`.github/workflows/weekly-scan.yml` if you want a different slot.

## Weekly routine

1. Check the run summary for the filing count.
2. Apply the size filter — discard anything over ~$500M market cap **unread**.
   Specialists have priced it; you have no edge there. The zone is sub-$150M.
3. For survivors, open the Form 10 and answer the only question that matters:
   **who is forced to sell, why, and is that reason unrelated to value?**
   If you can't name a specific forced seller, cut it.
4. If it survives, fill a thesis note before taking a paper position.

## Why commit the notes

Git timestamps are tamper-evident. A thesis committed before the outcome is
known is real evidence; one reconstructed afterwards is not. Commit each thesis
when you write it, not when you score it.

## Local run

    pip install requests
    python scripts/spinoff_watch.py --email you@example.com --days 30 --out ./spinoff_out
