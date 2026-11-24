"""Bridge double count test: is any large holder on one chain sitting on another chain's supply?

Adding chains up is right only when tokens move between chains by burning on one and minting on the other. A bridge that
locks tokens on chain A and mints copies on chain B leaves the locked tokens in A's supply: the copies on B are counted
twice. The locked tokens sit at one address on A (the bridge's escrow), so: list the largest holders on A and flag any
whose balance matches the supply of another chain, or of a set of other chains. Free holder lists exist for Ethereum
(Blockscout), Stellar (Horizon) and Solana (largest accounts); the chains checked are named in the output.

    python3 bridges.py          -> writes bridges.json, prints one line per fund, exit 1 when a new exact match appears
"""
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
UA = {"User-Agent": "ties-bridges/0.1", "Content-Type": "application/json"}
LARGE = Decimal("0.005")   # holders under 0.5% of the fund cannot hide a meaningful double count
MATCH = Decimal("0.0025")  # an escrow equals the copies it backs; 0.25% allows for flows between the two reads
WORDS = ("bridge", "lockbox", "adapter", "escrow", "portal", "tokenpool", "ntt")  # contract names that hint at a lock


def get(url, payload=None):
    for wait in (3, 10, 30, None):
        try:
            req = urllib.request.Request(url, json.dumps(payload).encode() if payload else None, UA)
            return json.load(urllib.request.urlopen(req, timeout=30))
        except Exception:
            if wait is None:
                raise
            time.sleep(wait)


def holders_ethereum(token):
    """Top 50 holders from Blockscout: (address, balance, contract name or '')."""
    base = f"https://eth.blockscout.com/api/v2/tokens/{token['address']}"
    decimals = int(get(base)["decimals"])
    return [(h["address"]["hash"], Decimal(int(h["value"])).scaleb(-decimals),
             (h["address"].get("name") or "") if h["address"].get("is_contract") else "") for h in get(f"{base}/holders")["items"]]


def holders_stellar(token):
    """Every account holding the asset (Horizon pages through all of them), plus contract, pool and claimable amounts."""
    code, issuer = token["address"].split("-")
    horizon = token["rpcs"][0]
    out, url = [], f"{horizon}/accounts?asset={code}:{issuer}&limit=200"
    while url:
        page = get(url)
        recs = page["_embedded"]["records"]
        for a in recs:
            bal = next((b["balance"] for b in a["balances"] if b.get("asset_code") == code and b.get("asset_issuer") == issuer), "0")
            if Decimal(bal):
                out.append((a["account_id"], Decimal(bal), ""))
        url = page["_links"]["next"]["href"] if len(recs) == 200 else None
    asset = get(f"{horizon}/assets?asset_code={code}&asset_issuer={issuer}")["_embedded"]["records"][0]
    for key, label in (("contracts_amount", "Soroban contracts"), ("liquidity_pools_amount", "liquidity pools"),
                       ("claimable_balances_amount", "claimable balances")):
        if Decimal(asset.get(key, "0")):
            out.append((label, Decimal(asset[key]), label))
    return out


def holders_solana(token):
    """The 20 largest token accounts, from the first provider that answers."""
    for url in token["rpcs"]:
        try:
            res = get(url, {"jsonrpc": "2.0", "id": 1, "method": "getTokenLargestAccounts", "params": [token["address"], {"commitment": "finalized"}]})
            return [(a["address"], Decimal(a["uiAmountString"]), "") for a in res["result"]["value"]]
        except Exception:
            continue
    raise OSError("no Solana provider answered")


SOURCES = {("ethereum", "evm"): holders_ethereum, ("stellar", "stellar"): holders_stellar, ("solana", "solana"): holders_solana}


def subset_matching(supplies, value, tol):
    """Chains (from {chain: supply}) whose supplies add up to value within tol, smallest set first; None if none."""
    items = sorted(supplies.items())
    if len(items) > 14:
        return None
    hits = [[c for i, (c, _) in enumerate(items) if m >> i & 1] for m in range(1, 1 << len(items))
            if abs(sum(v for i, (_, v) in enumerate(items) if m >> i & 1) - value) <= tol]
    return min(hits, key=len) if hits else None


def escrow_flags(chain, holders, supplies, total):
    """Holders on chain that look like a lock: balance equals another chain's supply (or a set of them), or a bridge name."""
    others = {c: v for c, v in supplies.items() if c != chain and v > Decimal("0.01")}
    flags = []
    for addr, bal, name in holders:
        if bal < total * LARGE:
            continue
        match = subset_matching(others, bal, max(bal * MATCH, Decimal("1")))
        named = any(w in name.lower() for w in WORDS)
        if match or named:
            gap = abs(bal - sum(others[c] for c in match)) if match else None
            flags.append({"chain": chain, "holder": addr, "balance": str(bal), "name": name, "matches": match or [],
                          "exact": gap is not None and gap <= Decimal("0.01"),  # to the hundredth: a mirror, not a coincidence
                          "gap": str(gap) if gap is not None else None,
                          "why": "balance equals supply on " + ", ".join(match) if match else f"named {name}"})
    return flags


def check_fund(cfg, rows, sources=SOURCES):
    supplies = {}
    for r in rows:
        if r.get("supply") is not None:
            supplies[r["chain"]] = supplies.get(r["chain"], Decimal(0)) + Decimal(str(r["supply"]))
    total = sum(supplies.values())
    checked, skipped, flags = [], [], []
    for token in cfg["tokens"]:
        read = sources.get((token["chain"], token["kind"]))
        if not read or token["chain"] not in supplies:
            continue
        try:
            holders = read(token)
        except Exception as exc:
            skipped.append(f"{token['chain']}: {exc}")
            continue
        checked.append(token["chain"])
        flags += escrow_flags(token["chain"], holders, supplies, total)
    return {"checked": sorted(set(checked)), "skipped": skipped, "flags": flags,
            "chains": sorted(c for c, v in supplies.items() if v > Decimal("0.01"))}


def main():
    config = json.loads((HERE / "funds.json").read_text())
    latest = {}  # newest snapshot per fund
    for path in (HERE / "snapshots").glob("*.json"):
        snap = json.loads(path.read_text())
        if snap["fund"] not in latest or snap["taken"] > latest[snap["fund"]]["taken"]:
            latest[snap["fund"]] = snap
    old = json.loads((HERE / "bridges.json").read_text()) if (HERE / "bridges.json").exists() else {"funds": {}}
    out = {"taken": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"), "funds": {}}
    for fund, cfg in config.items():
        if fund in latest:
            res = check_fund(cfg, latest[fund]["rows"])
            out["funds"][fund] = res
            print(f"{fund}: checked {', '.join(res['checked']) or 'none'}; {len(res['flags'])} possible escrow(s)"
                  + "".join(f"\n  {f['chain']} {f['holder']} {f['balance']}: {f['why']}" for f in res["flags"])
                  + "".join(f"\n  not checked {s}" for s in res["skipped"]))
    (HERE / "bridges.json").write_text(json.dumps(out, indent=1) + "\n")
    return 1 if new_exact(old, out) else 0


def new_exact(old, new):
    """Exact matches (likely mirrors) not in the previous run: the only thing worth a phone alert."""
    key = lambda d: {(fund, f["chain"], f["holder"]) for fund, r in d["funds"].items() for f in r["flags"] if f.get("exact")}
    return key(new) - key(old)


if __name__ == "__main__":
    sys.exit(main())
