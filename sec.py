"""Official figures for SEC-registered money market funds, from their monthly Form N-MFP3.

Writes official/<FUND>.json: one entry per report date with shares outstanding, net assets
and the daily gross subscriptions and redemptions the fund reported. Only new filings are fetched.
SEC asks automated clients to declare a contact: SEC_USER_AGENT in .env.
"""
import json
import os
import sys
import re
import time
import urllib.request
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent


def sec_get(url, agent):
    req = urllib.request.Request(url, headers={"User-Agent": agent})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode()


def filings(submissions, series_id=None):
    """Latest N-MFP3 (amendments win) per report date, newest first: [(report_date, accession, primary_doc)]."""
    recent = submissions["filings"]["recent"]
    best = {}
    for i, form in enumerate(recent["form"]):
        if not form.startswith("N-MFP3"):
            continue
        key = recent["reportDate"][i]
        if key not in best or recent["filingDate"][i] >= best[key][0]:
            best[key] = (recent["filingDate"][i], recent["accessionNumber"][i], recent["primaryDocument"][i].split("/")[-1])
    return [(day, acc, doc) for day, (_, acc, doc) in sorted(best.items(), reverse=True)]


def parse_nmfp3(xml, series_id):
    one = lambda tag: (re.findall(rf"<{tag}>([^<]*)</{tag}>", xml) or [None])[0]
    if one("seriesId") != series_id:
        raise ValueError(f"filing is for series {one('seriesId')}, not {series_id}")
    flows = re.findall(r"<dailyGrossSubscriptions>([^<]*)</dailyGrossSubscriptions>\s*"
                       r"<dailyGrossRedemptions>([^<]*)</dailyGrossRedemptions>\s*"
                       r"<dailyShareHolderFlowDate>([^<]*)</dailyShareHolderFlowDate>", xml)
    return {"report_date": one("reportDate"), "series_id": series_id,
            "shares_outstanding": one("numberOfSharesOutstanding"), "net_assets": one("netAssetOfSeries"),
            "daily_flows": [{"date": d, "subscriptions": s, "redemptions": r} for s, r, d in sorted(flows, key=lambda f: f[2])]}


def monthly_flows(entry):
    subs = sum(Decimal(f["subscriptions"]) for f in entry["daily_flows"])
    reds = sum(Decimal(f["redemptions"]) for f in entry["daily_flows"])
    return subs, reds


def update(fund, official, agent, keep=6, get=sec_get):
    """Refresh official/<fund>.json with the latest `keep` filings; returns the entries."""
    path = HERE / "official" / f"{fund}.json"
    cached = {e["report_date"]: e for e in json.loads(path.read_text())} if path.exists() else {}
    cik = official["cik"].lstrip("0")
    subs = json.loads(get(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json", agent))
    for day, accession, doc in filings(subs)[:keep]:
        url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/{doc}"
        if day in cached and cached[day].get("source") == url:
            continue
        time.sleep(0.5)  # SEC fair-access: well under 10 requests a second
        if day in cached:  # a later filing for a month already saved: an amendment replaces it, and says so
            print(f"sec: {fund} {day} amended, now {url} (was {cached[day].get('source')})")
        cached[day] = parse_nmfp3(get(url, agent), official["series_id"]) | {"source": url}
    entries = sorted(cached.values(), key=lambda e: e["report_date"], reverse=True)
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(entries, indent=1) + "\n")
    return entries


def main():
    config = json.loads((HERE / "funds.json").read_text())
    agent = os.environ.get("SEC_USER_AGENT")
    if not agent:
        sys.exit("set SEC_USER_AGENT to a contact the SEC can reach, e.g. SEC_USER_AGENT=you@example.com (their fair-access rule)")
    for fund, cfg in config.items():
        if cfg.get("official", {}).get("kind") == "sec-nmfp3":
            entries = update(fund, cfg["official"], agent)
            print(f"{fund}: {len(entries)} N-MFP3 report dates, latest {entries[0]['report_date']}")


if __name__ == "__main__":
    main()
