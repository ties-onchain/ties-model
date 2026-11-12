"""Fund events from the chain: classify a Stellar issuer's operations and compare daily flows with the SEC filing.

Usage: python3 events.py FUND YYYY-MM          month of events vs the SEC filing (e.g. events.py BENJI 2026-10)
       python3 events.py capture FUND [DAYS]   archive the last DAYS full UTC days (default 7), nightly
       python3 events.py closure FUND [PAIRS]  do archived events explain each chain's supply change between nightly reads

Kinds: subscription (mint), dividend (mint inside the nightly dividend run), redemption (burn; on Stellar also a
claimable balance left to the issuer), migration (burn on one chain, same amount minted on another within minutes),
unknown (touches the token but fits no rule: listed, never dropped).
The run is found on Stellar (about 1,000 single mints a night) and applied to every chain: the EVM chains have a
handful of holders, too few to show a batch, and their mints land in the same minutes. A fund without a Stellar issuer
(BUIDL) pays its run as batch transactions, one transaction minting to many holders; those set the run instead.
Nightly capture writes archive/<FUND>/<chain>/<day>.json.gz (committed: dated evidence, and the only copy for chains
whose providers forget, like Aptos after about 11 days). A month is read from the archive when every day is there,
otherwise fetched whole and cached in cache/ (not committed). The daily table goes to flows/<FUND>-<YYYY-MM>.json.
"""
import bisect
import gzip
import itertools
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode

import soroban
from check import DECIMALS, TOLERANCE, http_json, jsonrpc, money

HERE = Path(__file__).resolve().parent
BATCH_WINDOW, BATCH_MIN = 1800, 50  # the dividend run is ~1,000 single mints in two hours; subscriptions are a few a day
RUN_PAD = 1800  # EVM dividends land within minutes of the Stellar run
BATCH_TX_MIN = 3  # BUIDL's daily dividend: one transaction minting to every holder on the chain (30 on Ethereum, 5 on BUIDL-I)
MIGRATION_WINDOW = 1800  # 4 Nov 2026: 10 EVM burns and their Stellar mints, same amounts, all within 13 minutes
IGNORED = {"allow_trust", "set_trust_line_flags", "begin_sponsoring_future_reserves", "end_sponsoring_future_reserves",
           "change_trust", "claim_claimable_balance"}  # a claim settles a balance already counted when it was created
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"  # ERC-20 Transfer(from, to, value)
ZERO_TOPIC = "0x" + "0" * 64
CX_TOPICS = ("0x92a2e75dcc9a877e15e8c693d39030c8d8a6d772c9178427a8678461af9f58fc",  # Franklin InstantCXTransferOut(address,uint256,string)
             "0x1916d8e383c33d1b51f2fa903da30c5d1e0a477cbc6b815b9ac0d23599a7dc1a")  # InstantCXTransferIn(address,uint256,uint256,string)
ETHERSCAN = "https://api.etherscan.io/v2/api"  # one free key for many chains, 3 calls/s, 100k calls/day
KEEP = ("id", "type", "created_at", "transaction_hash", "source_account", "from", "to", "asset_code", "asset", "amount", "claimants")


def stamp(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def ledger_at(horizon, iso, get=http_json):
    """First ledger closed at or after iso. Secant steps on close time; ledgers are about 5.5 s apart."""
    target, seq = stamp(iso), get(horizon)["history_latest_ledger"]
    closed = lambda s: stamp(get(f"{horizon}/ledgers/{s}")["closed_at"])
    for _ in range(8):
        step = round((target - closed(seq)) / 5.5)
        if step == 0:
            break
        seq += step
    while closed(seq) >= target:
        seq -= 1
    while closed(seq) < target:
        seq += 1
    return seq


def retry(call, sleep, waits=(1.5, 15, 60, 180)):
    """Paced call; free providers answer bursts with 429 for minutes, so back off hard before giving up."""
    for wait in waits:
        sleep(wait)
        try:
            return call()
        except Exception as exc:
            error = exc
    raise error


def fetch(horizon, issuer, start, end, get=http_json, sleep=time.sleep):
    """Every operation on the issuer account in [start, end), oldest first. SDF Horizon keeps a year; LOBSTR about 35 days."""
    def page(cursor):
        return retry(lambda: get(f"{horizon}/accounts/{issuer}/operations?order=asc&limit=200&cursor={cursor}")["_embedded"]["records"], sleep)
    cursor, ops = (ledger_at(horizon, start, get) << 32) - 1, []
    while records := page(cursor):
        for r in records:
            if r["created_at"] >= end:
                return ops
            ops.append({k: r[k] for k in KEEP if k in r})
        cursor = records[-1]["paging_token"]
    return ops


def providers(urls, rpc=jsonrpc, sleep=time.sleep):
    """call(method, params) that moves to the next RPC on every failure: free log providers fail one at a time."""
    def call(method, params):
        turn = itertools.cycle(urls)
        return retry(lambda: rpc(next(turn), method, params), sleep)
    return call


def block_at(call, iso):
    """First block with timestamp at or after iso. Gallops back from the head, then bisects: block times differ per chain,
    and free RPCs refuse old blocks as archive requests, so the search never goes deeper than it must."""
    stamp_of = lambda n: int(call("eth_getBlockByNumber", [hex(n), False])["timestamp"], 16)
    target, hi, step = stamp(iso), int(call("eth_blockNumber", []), 16), 1024
    lo = max(hi - step, 0)
    while lo > 0 and stamp_of(lo) >= target:
        hi, step = lo, step * 2
        lo = max(lo - step, 0)
    while lo < hi:
        mid = (lo + hi) // 2
        if stamp_of(mid) < target:
            lo = mid + 1
        else:
            hi = mid
    return lo


def fetch_evm(chain, address, source, start, end, rpc=jsonrpc, sleep=time.sleep):
    """Mints (Transfer from 0x0) and burns (Transfer to 0x0) in [start, end), in slices every listed RPC accepts.
    For slow providers a source can ask for every Transfer in one query and sort mints and burns here ("one_query"), and
    skip Franklin's cross-chain labels when the issuer emits none ("labels": false)."""
    call, span = providers(source["rpcs"], rpc, sleep), source["span"]
    first, stop = block_at(call, start), block_at(call, end)
    decimals = int(call("eth_call", [{"to": address, "data": DECIMALS}, "latest"]), 16)
    moves, labelled = [], set()
    for lo in range(first, stop, span):
        window = {"address": address, "fromBlock": hex(lo), "toBlock": hex(min(lo + span, stop) - 1)}
        if source.get("one_query"):
            for log in call("eth_getLogs", [window | {"topics": [TRANSFER]}]):
                if ZERO_TOPIC in log["topics"][1:3]:
                    moves.append(to_move(chain, "mint" if log["topics"][1] == ZERO_TOPIC else "burn", log, decimals))
        else:
            for side, topics in (("mint", [TRANSFER, ZERO_TOPIC]), ("burn", [TRANSFER, None, ZERO_TOPIC])):
                for log in call("eth_getLogs", [window | {"topics": topics}]):
                    moves.append(to_move(chain, side, log, decimals))
        if source.get("labels", True):
            labelled |= {log["transactionHash"] for topic in CX_TOPICS for log in call("eth_getLogs", [window | {"topics": [topic]}])}
    label_migrations(moves, labelled)
    stamps = {b: int(call("eth_getBlockByNumber", [hex(b), False])["timestamp"], 16) for b in {m["block"] for m in moves}}
    for m in moves:
        m["at"] = datetime.fromtimestamp(stamps[m["block"]], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return sorted(moves, key=lambda m: (m["block"], m["tx"]))


def label_migrations(moves, txs):
    """The issuer's own cross-chain label (Franklin's InstantCXTransferIn/Out) marks every mint or burn in that transaction."""
    for m in moves:
        if m["tx"] in txs:
            m["label"] = "migration"


def to_move(chain, side, log, decimals):
    holder = log["topics"][2 if side == "mint" else 1]
    return {"chain": chain, "side": side, "block": int(log["blockNumber"], 16), "tx": log["transactionHash"],
            "account": "0x" + holder[-40:], "amount": str(Decimal(int(log["data"], 16)).scaleb(-decimals))}


def fetch_etherscan(chain, address, chainid, start, end, key, get=http_json, sleep=time.sleep):
    """Same moves as fetch_evm, from Etherscan's API: for chains whose free RPCs refuse logs older than about a month."""
    def call(**params):
        def once():
            out = get(f"{ETHERSCAN}?{urlencode(params | {'chainid': chainid, 'apikey': key})}")
            if "status" in out and out["status"] != "1" and out.get("message") != "No records found":
                raise RuntimeError(f"etherscan: {out.get('message')}: {out.get('result')}")
            return out["result"]
        return retry(once, sleep, waits=(0.4, 5, 30, 90))
    block = lambda iso: int(call(module="block", action="getblocknobytime", timestamp=int(stamp(iso)), closest="after"))
    first, stop = block(start), block(end)
    decimals = int(call(module="proxy", action="eth_call", to=address, data=DECIMALS, tag="latest"), 16)
    moves = []
    for side, topic in (("mint", "topic1"), ("burn", "topic2")):
        for page in range(1, 11):  # Etherscan stops at 10,000 results per query
            logs = call(module="logs", action="getLogs", address=address, fromBlock=first, toBlock=stop - 1, topic0=TRANSFER,
                        **{topic: ZERO_TOPIC, f"topic0_{topic[-1]}_opr": "and"}, page=page, offset=1000)
            for log in logs:
                moves.append(to_move(chain, side, log, decimals) | {"at": datetime.fromtimestamp(int(log["timeStamp"], 16), timezone.utc)
                                                                    .strftime("%Y-%m-%dT%H:%M:%SZ")})
            if len(logs) < 1000:
                break
        else:
            raise RuntimeError(f"{chain}: more than 10,000 {side} logs in one month, split the range")
    label_migrations(moves, {log["transactionHash"] for topic0 in CX_TOPICS
                             for log in call(module="logs", action="getLogs", address=address, fromBlock=first, toBlock=stop - 1,
                                             topic0=topic0, page=1, offset=1000)})
    return sorted(moves, key=lambda m: (m["block"], m["tx"]))


APTOS_INDEXER = "https://api.mainnet.aptoslabs.com/v1/graphql"
APTOS_TOUCHED = """query($asset: String!, $after: bigint!, $start: timestamp!, $end: timestamp!) {
  fungible_asset_activities(where: {asset_type: {_eq: $asset}, transaction_version: {_gt: $after}, is_transaction_success: {_eq: true},
                                    transaction_timestamp: {_gte: $start, _lt: $end}}, order_by: {transaction_version: asc}, limit: 100) {
    transaction_version } }"""
APTOS_TRANSFER_ONLY = {"InstantTransferEvent", "TransferSettledEvent", "AccountRecoveredEvent", "AssetRecoveredEvent", "Transfer"}


def aptos_moves(tx, module):
    """Supply changes in one transaction, read from the issuer's own fund events (raw share units). The indexer's
    deposits and withdrawals are not enough: netting them invented a 14.76M mint on 26 Oct 2026."""
    moves, names = [], set()
    for e in tx["events"]:
        if not e["type"].startswith(module + "::"):
            continue
        name, d = e["type"].rsplit("::", 1)[1], e["data"]
        names.add(name)
        if name == "DividendDistributedEvent":  # cash paid as shares at the fund price, both with 9 decimals
            move = ("mint", "dividend", int(d["dividend_cash_amount"]) * 10**9 // int(d["price"]), d["account"])
        elif name == "MintSharesEvent":
            move = ("mint", None, int(d["amount"]), d["to"])
        elif name == "BurnSharesEvent":
            move = ("burn", None, int(d["amount"]), d["from"])
        elif name in ("InstantCXTransferIn", "InstantCXTransferOut"):
            move = ("mint" if name.endswith("In") else "burn", "migration", int(d["amount"]), d["account"])
        elif name == "BalanceAdjustedEvent":
            move = ("mint" if d["mint"] else "burn", None, int(d["shares"]), d["account"])
        elif name == "Transfer" and "0x0" in (d.get("from"), d.get("to")):  # Securitize ds_token (BUIDL): mint and burn
            move = ("mint", None, int(d["value"]), d["to"]) if d["from"] == "0x0" else ("burn", None, int(d["value"]), d["from"])
        else:
            continue
        moves.append(dict(zip(("side", "label", "raw", "account"), move)))
    if not moves and not names & APTOS_TRANSFER_ONLY:  # touched the token, no supply label we know: surface it
        moves.append({"side": "unknown", "label": "unknown", "raw": 0, "account": tx.get("sender")})
    return moves


def fetch_aptos(asset, module, rest, start, end, key, post=http_json, get=http_json, sleep=time.sleep):
    """Supply changes of one fungible asset in [start, end): the indexer finds the transactions, the REST node gives their
    events. Both keep only about 11 days, so this runs nightly into the archive."""
    def touched(after):
        def once():
            out = post(APTOS_INDEXER, {"query": APTOS_TOUCHED, "variables": {"asset": asset, "after": after, "start": start[:19], "end": end[:19]}},
                       headers={"Authorization": f"Bearer {key}"})
            if "errors" in out:
                raise RuntimeError(f"aptos indexer: {out['errors'][0]['message']}")
            return [r["transaction_version"] for r in out["data"]["fungible_asset_activities"]]
        return retry(once, sleep)
    oldest = int(retry(lambda: get(rest), sleep)["oldest_block_height"]) + 1000  # the advertised oldest block is often pruned already
    kept_from = int(retry(lambda: get(f"{rest}/blocks/by_height/{oldest}"), sleep)["block_timestamp"]) / 1e6
    if kept_from > stamp(start):  # the indexer answers an empty list for pruned days, which would read as "nothing happened"
        raise RuntimeError(f"aptos history pruned before {datetime.fromtimestamp(kept_from, timezone.utc):%Y-%m-%d %H:%M} UTC")
    versions, after = [], 0
    while batch := touched(after):
        versions += batch
        after = batch[-1]
    decimals = retry(lambda: get(f"{rest}/accounts/{asset}/resource/0x1::fungible_asset::Metadata"), sleep)["data"]["decimals"]
    moves = []
    for v in sorted(set(versions)):
        tx = retry(lambda: get(f"{rest}/transactions/by_version/{v}"), sleep)
        at = datetime.fromtimestamp(int(tx["timestamp"]) / 1e6, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for m in aptos_moves(tx, module):
            moves.append({"chain": "aptos", "side": m["side"], "label": m["label"], "block": v, "tx": str(v), "account": m["account"],
                          "amount": str(Decimal(m["raw"]).scaleb(-decimals)), "at": at})
    return moves


SOLANA_SUPPLY_CHANGES = {"mintTo": "mint", "mintToChecked": "mint", "burn": "burn", "burnChecked": "burn"}


def fetch_solana(mint, urls, start, end, rpc=jsonrpc, sleep=time.sleep):
    """Mints and burns of one SPL mint in [start, end), from every transaction touching the mint (inner instructions too)."""
    call = providers(urls, rpc, sleep)
    lo, hi, sigs, before = stamp(start), stamp(end), [], None
    while True:
        page = call("getSignaturesForAddress", [mint, {"limit": 1000} | ({"before": before} if before else {})])
        sigs += [s["signature"] for s in page if s["err"] is None and lo <= (s["blockTime"] or 0) < hi]
        if len(page) < 1000 or (page[-1]["blockTime"] or 0) < lo:
            break
        before = page[-1]["signature"]
    decimals = call("getTokenSupply", [mint])["value"]["decimals"]
    moves = []
    for sig in sigs:
        tx = call("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
        inner = [i for group in tx["meta"]["innerInstructions"] or [] for i in group["instructions"]]
        for ins in tx["transaction"]["message"]["instructions"] + inner:
            parsed = ins.get("parsed") if isinstance(ins.get("parsed"), dict) else {}
            info = parsed.get("info", {})
            if parsed.get("type") in SOLANA_SUPPLY_CHANGES and info.get("mint") == mint:
                raw = info.get("amount") or info["tokenAmount"]["amount"]
                moves.append({"chain": "solana", "side": SOLANA_SUPPLY_CHANGES[parsed["type"]], "block": tx["slot"], "tx": sig,
                              "account": info["account"], "amount": str(Decimal(int(raw)).scaleb(-decimals)),
                              "at": datetime.fromtimestamp(tx["blockTime"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
    return sorted(moves, key=lambda m: (m["block"], m["tx"]))


SOROBAN_SUPPLY_CHANGES = {"mint": "mint", "burn": "burn", "clawback": "burn"}  # SEP-41 token events; transfers keep supply
LEDGER_SECONDS = 5  # mainnet average, only to pick a start ledger a little before the day; events are filtered by time


def fetch_soroban(contract, urls, start, end, rpc=jsonrpc, sleep=time.sleep):
    """Mints and burns of one Soroban token in [start, end), from its contract events. The RPC keeps about 7 days, so this
    runs nightly into the archive and refuses a day the RPC no longer holds instead of archiving it empty."""
    call = providers(urls, rpc, sleep)
    health = call("getHealth", {})
    if int(health["oldestLedgerCloseTime"]) > stamp(start):
        raise RuntimeError(f"soroban history pruned before {datetime.fromtimestamp(int(health['oldestLedgerCloseTime']), timezone.utc):%Y-%m-%d %H:%M} UTC")
    behind = (int(health["latestLedgerCloseTime"]) - stamp(start)) // LEDGER_SECONDS + 720  # an hour of slack
    params = {"startLedger": max(int(health["oldestLedger"]), int(health["latestLedger"]) - int(behind)),
              "filters": [{"type": "contract", "contractIds": [contract]}], "pagination": {"limit": 1000}}
    decimals = soroban.read_instance(urls[0], contract, lambda _url, method, p: call(method, p))[0]["METADATA"]["decimals"]
    moves = []
    while True:
        page = call("getEvents", params)
        for e in page["events"]:
            side = SOROBAN_SUPPLY_CHANGES.get(soroban.decode(e["topic"][0]))
            if side and start <= e["ledgerClosedAt"] < end:
                holder = soroban.decode(e["topic"][-1 if side == "mint" else 1])
                moves.append({"chain": "stellar", "side": side, "block": e["ledger"], "tx": e["txHash"], "account": soroban.strkey(holder),
                              "amount": str(Decimal(soroban.decode(e["value"])).scaleb(-decimals)), "at": e["ledgerClosedAt"]})
        if not page["events"] or page["events"][-1]["ledgerClosedAt"] >= end:
            return moves
        params = {"filters": params["filters"], "pagination": {"cursor": page["cursor"], "limit": 1000}}


def read_events(token, start, end, now=None):
    """A source may name a faster provider for recent days ("recent": {"days": N, ...}) whose history is short: the
    BNB Chain node that answers in 0.3 s keeps only 2 to 5 days, the one that keeps a week takes 28 s a query."""
    source = token["events"]
    recent = source.get("recent")
    if recent and stamp(start) >= (now or time.time()) - recent["days"] * 86400:
        source = {k: v for k, v in source.items() if k != "recent"} | {k: v for k, v in recent.items() if k != "days"}
    if token["kind"] == "soroban":
        return fetch_soroban(token["address"], source["rpcs"], start, end)
    if token["kind"] == "solana":
        return fetch_solana(token["address"], source["rpcs"], start, end)
    if token["kind"] == "aptos":
        return fetch_aptos(token["address"], source["module"], token["rpcs"][0], start, end, required_env("APTOS_API_KEY"))
    if "etherscan" not in source:
        return fetch_evm(token["chain"], token["address"], source, start, end)
    return fetch_etherscan(token["chain"], token["address"], source["etherscan"], start, end, required_env("ETHERSCAN_API_KEY"))


def required_env(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"needs {name} in .env")
    return value


def is_token(op, code, issuer):
    return op.get("asset_code") == code or op.get("asset") == f"{code}:{issuer}"


def classify(ops, code, issuer):
    """[{at, day, kind, amount, account, tx}] for every operation that moves the token."""
    mints = sorted((stamp(o["created_at"]) for o in ops if o["type"] == "payment" and is_token(o, code, issuer) and o["from"] == issuer))
    in_batch = lambda t: bisect.bisect_left(mints, t + BATCH_WINDOW) - bisect.bisect_left(mints, t - BATCH_WINDOW) > BATCH_MIN
    paid, events = set(), []
    for o in ops:
        if not is_token(o, code, issuer) or o["type"] in IGNORED:
            continue
        kind, side, account = "unknown", "other", o.get("source_account")
        if o["type"] == "payment" and o["from"] == issuer:
            side, account = "mint", o["to"]
            if not in_batch(stamp(o["created_at"])):
                kind = "subscription"
            elif (o["created_at"][:10], account) not in paid:  # one dividend per holder per night; a second one is not a dividend
                kind = "dividend"
                paid.add((o["created_at"][:10], account))
        elif o["type"] == "payment" and o["to"] == issuer:
            kind, side, account = "redemption", "burn", o["from"]
        elif o["type"] == "create_claimable_balance" and o["source_account"] != issuer and any(c["destination"] == issuer for c in o["claimants"]):
            kind, side = "redemption", "burn"
        events.append({"chain": "stellar", "at": o["created_at"], "day": o["created_at"][:10], "kind": kind, "side": side,
                       "amount": o.get("amount", "0"), "account": account, "tx": o["transaction_hash"], "id": f"stellar-{o['id']}"})
    return events


def dividend_runs(events):
    """[(start, end)] per night, padded, from the Stellar dividend mints."""
    nights = {}
    for e in events:
        if e["kind"] == "dividend":
            t = stamp(e["at"])
            lo, hi = nights.get(e["day"], (t, t))
            nights[e["day"]] = (min(lo, t), max(hi, t))
    return [(lo - RUN_PAD, hi + RUN_PAD) for lo, hi in nights.values()]


def batch_runs(moves):
    """[(start, end)] around every transaction that mints to BATCH_TX_MIN holders or more: the dividend run of a fund
    without a Stellar issuer."""
    holders = {}
    for m in moves:
        if m["side"] == "mint":
            holders.setdefault(m["tx"], (stamp(m["at"]), set()))[1].add(m["account"])
    return [(t - RUN_PAD, t + RUN_PAD) for t, accounts in holders.values() if len(accounts) >= BATCH_TX_MIN]


def classify_evm(moves, runs):
    """The issuer's own label when the chain carries one (Aptos). Otherwise: mint inside a dividend run = dividend (once per
    holder per night), other mint = subscription, burn = redemption. A mint on a day with no known run is unknown:
    without the run there is no telling a dividend from a subscription."""
    run_days = {datetime.fromtimestamp(lo + RUN_PAD, timezone.utc).strftime("%Y-%m-%d") for lo, _ in runs}
    paid, events = set(), []
    for m in moves:
        t, day = stamp(m["at"]), m["at"][:10]
        if m.get("label"):
            kind = m["label"]
        elif m["side"] == "burn":
            kind = "redemption"
        elif any(lo <= t <= hi for lo, hi in runs):
            kind = "dividend" if (m["chain"], day, m["account"]) not in paid else "unknown"
            paid.add((m["chain"], day, m["account"]))
        else:
            kind = "subscription" if day in run_days else "unknown"
        events.append({"chain": m["chain"], "at": m["at"], "day": day, "kind": kind, "side": m["side"], "amount": m["amount"],
                       "account": m["account"], "tx": m["tx"]})
    return events


def tag_migrations(events):
    """A burn on one chain and a mint of the same amount on another, minutes apart, are one holder moving chains. Burns
    already labelled by the issuer still need their other side found: Stellar carries no label. Mints tagged dividend or
    unknown are candidates too: a migration near the nightly run (4 Nov 2026: 21:47 to 22:00 UTC) can fall inside the batch
    window. Each event pairs once."""
    paired = set()
    mints = [e for e in events if e["side"] == "mint"]
    for burn in (e for e in events if e["side"] == "burn" and e["kind"] in {"redemption", "migration"}):
        match = next((m for m in mints if id(m) not in paired and m["chain"] != burn["chain"] and Decimal(m["amount"]) == Decimal(burn["amount"])
                      and abs(stamp(m["at"]) - stamp(burn["at"])) <= MIGRATION_WINDOW), None)
        if match:
            burn["kind"] = match["kind"] = "migration"
            paired |= {id(burn), id(match)}
    return events


def booking_day(day, sec_days):
    """SEC date a chain day lands on: itself, or the next date the fund reported (weekend and holiday dividends land there)."""
    later = [d for d in sec_days if d >= day]
    return min(later) if later else None


def compare(events, sec_flows):
    """Daily rows: SEC flows, on-chain flows booked to the same SEC date, and the gap the chains leave unexplained."""
    sec_days = sorted(f["date"] for f in sec_flows)
    rows = {f["date"]: {"date": f["date"], "sec_subscriptions": Decimal(f["subscriptions"]), "sec_redemptions": Decimal(f["redemptions"]),
                        "subscription": Decimal(0), "dividend": Decimal(0), "redemption": Decimal(0), "migration": Decimal(0),
                        "unknown": Decimal(0)} for f in sec_flows}
    unbooked = []
    for e in events:
        day = booking_day(e["day"], sec_days)
        if day is None:
            unbooked.append(e)
        else:
            rows[day][e["kind"]] += Decimal(e["amount"])
    for r in rows.values():
        r["gap_subscriptions"] = r["sec_subscriptions"] - r["subscription"] - r["dividend"]
        r["gap_redemptions"] = r["sec_redemptions"] - r["redemption"]
    return [rows[d] for d in sec_days], unbooked


def other_chains(tokens, runs, load):
    """Events from every non-Stellar token, and {chain: why not} for those that could not be read. A chain that fails
    stays visible in the report instead of silently shrinking the totals."""
    events, missing = [], {}
    for t in (t for t in tokens if t["kind"] != "stellar"):
        if "events" not in t:
            missing[t["chain"]] = "no event reader yet"
            continue
        try:
            events += classify_evm(load(t), runs)
        except Exception as exc:
            missing[t["chain"]] = f"read failed: {exc}"
    return events, missing


def report(fund, month, rows, unbooked, events, missing):
    cols = ("sec_subscriptions", "subscription", "dividend", "gap_subscriptions", "sec_redemptions", "redemption", "gap_redemptions")
    chains = sorted({e["chain"] for e in events})
    lines = [f"## {fund} {month}: on-chain events ({', '.join(chains)}) vs SEC N-MFP3 daily flows", "",
             *[f"Not read: {chain} ({why})" for chain, why in sorted(missing.items())], "",
             "| Date | SEC subs | Chain subs | Chain dividends | Gap | SEC reds | Chain reds | Gap |",
             "|---|" + "---:|" * len(cols)]
    lines += [f"| {r['date']} | " + " | ".join(money(r[c]) for c in cols) + " |" for r in rows]
    lines.append("| **Total** | " + " | ".join(f"**{money(sum(r[c] for r in rows))}**" for c in cols) + " |")
    unknown = [e for e in events if e["kind"] == "unknown"]
    migrations = [e for e in events if e["kind"] == "migration"]
    lines += ["", f"Events: {len(events)}. Migrations (net zero, left out): {len(migrations)}. Unknown: {len(unknown)}."]
    lines += [f"- {e['kind']} {e['chain']} {e['at']} {e['amount']} tx {e['tx']}" for e in migrations + unknown]
    if unbooked:
        lines.append(f"After the last SEC date (next filing): {len(unbooked)} events, {money(sum(Decimal(e['amount']) for e in unbooked))}.")
    return "\n".join(lines)


def month_bounds(month):
    first = date.fromisoformat(f"{month}-01")
    nxt = (first + timedelta(days=32)).replace(day=1)
    return f"{first}T00:00:00Z", f"{nxt}T00:00:00Z"


def cached(path, pull):
    if not path.exists():
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(pull()))
    return json.loads(path.read_text())


def read_raw(token, start, end):
    """What gets archived per chain: issuer operations on Stellar (token ones only), supply moves elsewhere."""
    if token["kind"] != "stellar":
        return read_events(token, start, end)
    code, issuer = token["address"].split("-")
    return [o for o in fetch("https://horizon.stellar.org", issuer, start, end) if is_token(o, code, issuer)]


def archive_path(fund, chain, day):
    return HERE / "archive" / fund / chain / f"{day}.json.gz"


def slot(token):
    """Where a token's events are archived and how its events are named: the chain, unless the fund has a second share
    class on the same chain (BUIDL-I on Ethereum), which names its own slot in funds.json."""
    return token.get("slot", token["chain"])


def readable(token):
    return token["kind"] == "stellar" or "events" in token


def capture(fund, tokens, days, read=read_raw):
    """Archive each listed day for every chain with a reader; days already archived are skipped. Returns {chain: first
    error}: a failing day or chain does not stop the others."""
    failed = {}
    for t in filter(readable, tokens):
        for day in days:
            path = archive_path(fund, slot(t), day)
            if path.exists():
                continue
            try:
                moves = read(t, f"{day}T00:00:00Z", f"{date.fromisoformat(day) + timedelta(days=1)}T00:00:00Z")
            except Exception as exc:
                failed.setdefault(t["chain"], f"{day}: {exc}")
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(gzip.compress(json.dumps(moves).encode()))
    return failed


def move_ids(chain, moves):
    """Permanent ids for one chain's archived moves: the tx, plus its position when one tx holds several."""
    seen = {}
    for m in moves:
        n = seen[m["tx"]] = seen.get(m["tx"], -1) + 1
        yield f"{chain}-{m['tx']}" + (f"-{n}" if n else "")


def classifiable(tokens):
    """Fund events need the dividend run: from a classic Stellar issuer (BENJI), or from batch mints when every chain has
    a reader (BUIDL). A fund with chains still unread (EUTBL) gets no event pages."""
    return any(t["kind"] == "stellar" for t in tokens) or all(readable(t) for t in tokens)


def archived(fund, tokens, load=None):
    """(days, events): the days the archive holds for every chain with a reader, and their events classified as one
    stream (dividend runs and migrations need every chain at once), oldest first, each with a permanent id."""
    load = load or (lambda chain, day: json.loads(gzip.decompress(archive_path(fund, chain, day).read_bytes())))
    readers = list(filter(readable, tokens))
    days = sorted(set.intersection(*({p.name[:10] for p in (HERE / "archive" / fund / slot(t)).glob("*.json.gz")} for t in readers)))
    stellar = next((t for t in readers if t["kind"] == "stellar"), None)
    moves = {slot(t): [m for day in days for m in load(slot(t), day)] for t in readers if t is not stellar}
    if stellar:
        code, issuer = stellar["address"].split("-")
        events = classify([o for day in days for o in load("stellar", day)], code, issuer)
        runs = dividend_runs(events)
    else:
        events, runs = [], batch_runs([m for ms in moves.values() for m in ms])
    for t in (t for t in readers if t is not stellar):
        name = slot(t)
        extra = {"class": t["class"]} if name != t["chain"] else {}
        for e, eid in zip(classify_evm(moves[name], runs), move_ids(name, moves[name])):
            events.append(e | {"id": eid} | extra)
    return days, tag_migrations(sorted(events, key=lambda e: (e["at"], e["id"])))


def month_days(month):
    start, end = (date.fromisoformat(b[:10]) for b in month_bounds(month))
    return [str(start + timedelta(days=i)) for i in range((end - start).days)]


def month_raw(fund, token, month, read=read_raw):
    """The archive when it holds every day of the month, else one fetch for the whole month, cached."""
    paths = [archive_path(fund, slot(token), day) for day in month_days(month)]
    if all(p.exists() for p in paths):
        return [m for p in paths for m in json.loads(gzip.decompress(p.read_bytes()))]
    return cached(HERE / "cache" / f"{fund}-{slot(token)}-{month}.json", lambda: read(token, *month_bounds(month)))


def main(fund, month):
    cfg = json.loads((HERE / "funds.json").read_text())[fund]
    stellar = next(t for t in cfg["tokens"] if t["kind"] == "stellar")
    code, issuer = stellar["address"].split("-")
    events = classify(month_raw(fund, stellar, month), code, issuer)
    load = lambda t: month_raw(fund, t, month)
    more, missing = other_chains(cfg["tokens"], dividend_runs(events), load)
    events += more
    events = tag_migrations(sorted(events, key=lambda e: e["at"]))
    official = json.loads((HERE / "official" / f"{fund}.json").read_text())
    filing = next(e for e in official if e["report_date"].startswith(month))
    rows, unbooked = compare(events, filing["daily_flows"])
    out = HERE / "flows" / f"{fund}-{month}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"fund": fund, "month": month, "source": filing.get("source"), "rows": rows}, indent=1, default=str) + "\n")
    print(report(fund, month, rows, unbooked, events, missing))


def read_block(at):
    """'finalized block 55341176', 'ledger 65394755', 'ledger version 7978034023', 'finalized slot 467469116' -> the number."""
    return int(at.rsplit(" ", 1)[1].replace(",", ""))


def supply_effect(chain, item, issuer=None):
    """(block, signed supply change) of one archived item. A Stellar claimable balance left to the issuer counts when it is
    created; Horizon's supply drops only when the issuer claims it, so a pending one shows as a gap until then. Moves (every
    reader but classic Stellar, which archives raw Horizon operations) carry their side."""
    if "side" in item:
        sign = {"mint": 1, "burn": -1}.get(item["side"], 0)
        return item["block"], sign * Decimal(item["amount"])
    ledger, amount = int(item["id"]) >> 32, Decimal(item.get("amount", "0"))  # Horizon op ids carry the ledger in the high bits
    if item["type"] == "payment":
        return ledger, amount if item["from"] == issuer else -amount if item["to"] == issuer else 0
    if item["type"] == "create_claimable_balance" and item["source_account"] != issuer and any(c["destination"] == issuer for c in item["claimants"]):
        return ledger, -amount
    return ledger, 0


def closure(fund, before, after, issuer, load=None, readers=None, slots=None):
    """Per chain: supply change between two nightly reads vs the net of archived mints and burns between the same blocks.
    readers, when given, names the slots with an event reader; the others are listed as such, not as incomplete.
    slots maps a read's (chain, class) to its archive slot; by default the chain."""
    name = lambda r: (slots or {}).get((r["chain"], r.get("class")), r["chain"])
    load = load or (lambda chain, day: json.loads(gzip.decompress(archive_path(fund, chain, day).read_bytes())))
    first, last = date.fromisoformat(before["taken"][:10]), date.fromisoformat(after["taken"][:10])
    days = [str(first + timedelta(days=i)) for i in range((last - first).days + 1)]
    prev = {name(r): r for r in before["rows"]}
    rows = []
    for r in after["rows"]:
        p, chain = prev.get(name(r)), name(r)
        if readers is not None and chain not in readers:
            rows.append({"chain": chain, "status": "no event reader"})
            continue
        if not p or p["supply"] is None or r["supply"] is None:
            rows.append({"chain": chain, "status": "not read"})
            continue
        try:
            items = [i for day in days for i in load(chain, day)]
        except FileNotFoundError:
            rows.append({"chain": chain, "status": "archive incomplete"})
            continue
        lo, hi = read_block(p["reads"][0]["at"]), read_block(r["reads"][0]["at"])
        net = sum((d for b, d in (supply_effect(chain, i, issuer) for i in items) if lo < b <= hi), Decimal(0))
        change = Decimal(str(r["supply"])) - Decimal(str(p["supply"]))
        gap = change - net
        rows.append({"chain": chain, "status": "ok" if abs(gap) <= TOLERANCE else "gap", "supply_change": change, "events": net, "gap": gap})
    return rows


def closure_pairs(fund, pairs=None):
    """[(before, after, rows)] for consecutive reads of the fund, the last `pairs` of them (all when None)."""
    snaps = sorted((json.loads(p.read_text()) for p in (HERE / "snapshots").glob(f"{fund}-*.json")), key=lambda s: s["taken"])
    tokens = json.loads((HERE / "funds.json").read_text())[fund]["tokens"]
    stellar = next((t for t in tokens if t["kind"] == "stellar"), None)
    issuer = stellar["address"].split("-")[1] if stellar else None
    chosen = list(zip(snaps, snaps[1:]))[-int(pairs):] if pairs else list(zip(snaps, snaps[1:]))
    return [(before, after, closure(fund, before, after, issuer, readers={slot(t) for t in tokens if readable(t)},
                                    slots={(t["chain"], t["class"]): slot(t) for t in tokens}))
            for before, after in chosen]


def main_closure(fund, pairs="3"):
    bad = False
    for before, after, rows in closure_pairs(fund, pairs):
        print(f"{fund} {before['taken']} to {after['taken']}")
        for r in rows:
            figures = f" supply {money(r['supply_change'])}, events {money(r['events'])}, gap {money(r['gap'])}" if "gap" in r else ""
            print(f"  {r['chain']}: {r['status']}{figures}")
            bad |= r["status"] == "gap"
    sys.exit(1 if bad else 0)


def main_capture(fund, days="7"):
    tokens = json.loads((HERE / "funds.json").read_text())[fund]["tokens"]
    today = datetime.now(timezone.utc).date()
    failed = capture(fund, tokens, [str(today - timedelta(days=n)) for n in range(int(days), 0, -1)])
    for chain, why in failed.items():
        print(f"{fund} {chain}: not captured, {why}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    args, usage = sys.argv[1:], __doc__.split("\n\n")[1]
    if args[:1] in (["-h"], ["--help"]):
        sys.exit(print(usage))
    if len(args) < 2 or args[0].startswith("-"):
        sys.exit(usage)
    known = json.loads((HERE / "funds.json").read_text())
    fund = args[1] if args[0] in ("capture", "closure") else args[0]
    if fund not in known:
        sys.exit(f"unknown fund {fund}; known: {', '.join(known)}")
    if args[0] == "capture":
        main_capture(*args[1:3])
    elif args[0] == "closure":
        main_closure(*args[1:3])
    else:
        main(*args[:2])
