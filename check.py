"""Read tokenized funds' supply on every chain from two providers.

Usage: python3 check.py [FUND ...]   (default: every fund in funds.json; writes snapshots/FUND-<time>.json)
"""
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import soroban
import sources

try:
    import rwa  # optional comparison module, not part of ties-model
except ImportError:
    rwa = None

HERE = Path(__file__).resolve().parent
HEADERS = {"User-Agent": "ties-engine/0.1", "Content-Type": "application/json"}
NAME, DECIMALS, TOTAL_SUPPLY = "0x06fdde03", "0x313ce567", "0x18160ddd"
TOLERANCE = Decimal("0.01")  # one cent of a $1 share: below that, sources agree


def http_json(url, payload=None, headers=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers=HEADERS | (headers or {}))
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def jsonrpc(url, method, params):
    out = http_json(url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    if "error" in out:
        raise RuntimeError(f"{method}: {out['error']}")
    return out["result"]


def abi_string(hexdata):
    raw = bytes.fromhex(hexdata[2:])
    length = int.from_bytes(raw[32:64], "big")
    return raw[64:64 + length].decode(errors="replace")


def read_evm(url, address):
    block = jsonrpc(url, "eth_getBlockByNumber", ["finalized", False])["number"]
    call = lambda data: jsonrpc(url, "eth_call", [{"to": address, "data": data}, block])
    decimals = int(call(DECIMALS), 16)
    supply = Decimal(int(call(TOTAL_SUPPLY), 16)).scaleb(-decimals)
    return {"name": abi_string(call(NAME)), "supply": supply, "at": f"finalized block {int(block, 16)}"}


def read_solana(url, mint):
    res = jsonrpc(url, "getTokenSupply", [mint, {"commitment": "finalized"}])
    info = jsonrpc(url, "getAccountInfo", [mint, {"encoding": "jsonParsed", "commitment": "finalized"}])
    exts = info["value"]["data"]["parsed"]["info"].get("extensions", [])
    name = next((e["state"]["name"] for e in exts if e["extension"] == "tokenMetadata"), "")
    return {"name": name, "supply": Decimal(res["value"]["uiAmountString"]), "at": f"finalized slot {res['context']['slot']}"}


def read_aptos(base, metadata):
    version = http_json(base)["ledger_version"]  # pin both reads to one ledger version
    resource = lambda r: http_json(f"{base}/accounts/{metadata}/resource/0x1::fungible_asset::{r}?ledger_version={version}")["data"]
    meta = resource("Metadata")
    supply = Decimal(int(resource("ConcurrentSupply")["current"]["value"])).scaleb(-meta["decimals"])
    return {"name": meta["name"], "supply": supply, "at": f"ledger version {version}"}


def read_aptos_coin(base, coin_type):
    """Legacy Aptos coin (module::struct type, e.g. Ondo's USDY): supply, name and decimals from the framework's view functions."""
    version = http_json(base)["ledger_version"]
    view = lambda fn: http_json(f"{base}/view?ledger_version={version}",
                                {"function": f"0x1::coin::{fn}", "type_arguments": [coin_type], "arguments": []})[0]
    supply = Decimal(int(view("supply")["vec"][0])).scaleb(-int(view("decimals")))
    return {"name": view("name"), "supply": supply, "at": f"ledger version {version}"}


def read_sui(url, coin_type):
    """Sui coin supply over GraphQL (public JSON-RPC is retired); checkpoint and metadata come from one query, one snapshot."""
    query = '{ checkpoint { sequenceNumber } coinMetadata(coinType: "%s") { decimals name supply } }' % coin_type
    data = http_json(url, {"query": query})["data"]
    meta = data["coinMetadata"]
    return {"name": meta["name"], "supply": Decimal(int(meta["supply"])).scaleb(-meta["decimals"]),
            "at": f"checkpoint {data['checkpoint']['sequenceNumber']}"}


def read_cosmos(base, denom):
    """Cosmos SDK bank module (Noble): denom supply, scaled by the largest exponent in the denom's metadata."""
    meta = http_json(f"{base}/cosmos/bank/v1beta1/denoms_metadata/{denom}")["metadata"]
    amount = http_json(f"{base}/cosmos/bank/v1beta1/supply/by_denom?denom={denom}")["amount"]["amount"]
    exponent = max(unit["exponent"] for unit in meta["denom_units"])
    # ponytail: unpinned latest height; two providers within a cent is the guard, pin via x-cosmos-block-height if they drift
    return {"name": meta["name"], "supply": Decimal(int(amount)).scaleb(-exponent), "at": "latest block"}


def stellar_supply(asset):
    """Every place a Stellar asset can sit: trustlines (any auth state), claimable balances, pools, contracts."""
    held = sum(Decimal(v) for v in asset["balances"].values())
    parked = sum(Decimal(asset.get(k, "0")) for k in ("claimable_balances_amount", "liquidity_pools_amount", "contracts_amount"))
    return held + parked


def read_stellar(horizon, asset_id):
    code, issuer = asset_id.split("-")
    ledger = http_json(horizon)["history_latest_ledger"]  # closed ledgers are final on Stellar
    asset = http_json(f"{horizon}/assets?asset_code={code}&asset_issuer={issuer}")["_embedded"]["records"][0]
    domain = http_json(f"{horizon}/accounts/{issuer}").get("home_domain", "")
    return {"name": f"{code} ({domain})", "supply": stellar_supply(asset), "at": f"ledger {ledger}"}


# Starknet selectors are starknet_keccak of the function name (keccak-256 masked to 250 bits)
STARKNET_TOTAL_SUPPLY = "0x1557182e4359a1f0c6301278e8f5b35a776ab58d39892581e357578fb287836"
STARKNET_DECIMALS = "0x4c4fb1ab068f6039d5780c68dd0fa2f8742cceb3426d19667778ca7f3518a9"
STARKNET_NAME = "0x361458367e696363fbcc70777d07ebbd2394e89fd0adcaf147faccd1d294d60"


def byte_array(felts):
    """Cairo ByteArray: [full 31-byte words count, words..., pending word, pending length]."""
    full = int(felts[0], 16)
    words = [int(w, 16).to_bytes(31, "big") for w in felts[1:1 + full]]
    pending_len = int(felts[2 + full], 16)
    return (b"".join(words) + int(felts[1 + full], 16).to_bytes(pending_len, "big")).decode(errors="replace")


def read_starknet(url, address):
    block = jsonrpc(url, "starknet_blockNumber", [])  # pin all calls to one block
    call = lambda sel: jsonrpc(url, "starknet_call", {"request": {"contract_address": address, "entry_point_selector": sel, "calldata": []},
                                                      "block_id": {"block_number": block}})
    low, high = (int(v, 16) for v in call(STARKNET_TOTAL_SUPPLY)[:2])
    supply = Decimal((high << 128) | low).scaleb(-int(call(STARKNET_DECIMALS)[0], 16))
    return {"name": byte_array(call(STARKNET_NAME)), "supply": supply, "at": f"block {block}"}


def xrpl_currency(code):
    """XRPL currency field: 3-letter codes as is, longer ones as 40 hex chars (OUSG -> 4F555347 padded)."""
    return code if len(code) == 3 else code.encode().hex().upper().ljust(40, "0")


def read_xrpl(url, asset_id):
    """Issued currency on the XRP Ledger: the issuer's obligations (everything held on trustlines) at a validated ledger."""
    code, issuer = asset_id.split("-")
    res = jsonrpc(url, "gateway_balances", [{"account": issuer, "ledger_index": "validated", "strict": True}])
    ledger = res["ledger_index"]
    acct = jsonrpc(url, "account_info", [{"account": issuer, "ledger_index": ledger}])["account_data"]
    domain = bytes.fromhex(acct.get("Domain", "")).decode(errors="replace")
    supply = Decimal(res.get("obligations", {}).get(xrpl_currency(code), "0"))
    return {"name": f"{code} ({domain})", "supply": supply, "at": f"validated ledger {ledger}"}


def read_near(url, account):
    """NEP-141 token on NEAR: ft_total_supply and ft_metadata, the second pinned to the first call's final block."""
    def view(method, block):
        ref = {"block_id": block} if block else {"finality": "final"}
        return jsonrpc(url, "query", {"request_type": "call_function", "account_id": account, "method_name": method,
                                      "args_base64": "e30=", **ref})  # e30= is base64 of {}
    total = view("ft_total_supply", None)
    meta = json.loads(bytes(view("ft_metadata", total["block_height"])["result"]))
    supply = Decimal(int(json.loads(bytes(total["result"])))).scaleb(-meta["decimals"])
    return {"name": meta["name"], "supply": supply, "at": f"final block {total['block_height']}"}


READERS = {"xrpl": read_xrpl, "near": read_near, "evm": read_evm, "solana": read_solana, "aptos": read_aptos, "stellar": read_stellar, "starknet": read_starknet,
           "soroban": lambda url, address: soroban.read_soroban(url, address, jsonrpc),
           "aptos-coin": read_aptos_coin, "sui": read_sui, "cosmos": read_cosmos}


RETRY_WAITS = (3, 10)  # seconds; public RPCs rate-limit (429) and blip, a nightly job should outlast that


def read_with_retry(read, url, address, waits=RETRY_WAITS, sleep=time.sleep):
    for wait in waits:
        try:
            return read(url, address)
        except Exception:
            sleep(wait)
    return read(url, address)


def check_token(token, expect_names, readers=READERS, sleep=time.sleep):
    reads, errors = [], []
    for url in token["rpcs"]:
        try:
            reads.append({"rpc": url.split("/")[2], **read_with_retry(readers[token["kind"]], url, token["address"], sleep=sleep)})
        except Exception as exc:
            errors.append(f"{url.split('/')[2]}: {exc}")
    row = {**{k: token[k] for k in ("chain", "class", "address")}, "reads": reads, "errors": errors}
    if "rwa_address" in token:  # how rwa.xyz names the same token, when it differs (comparison only)
        row["rwa_address"] = token["rwa_address"]
    if not reads:
        return {**row, "status": "unreadable", "supply": None}
    names = token.get("expect_name", expect_names)  # per token, for mints with no on-chain name (legacy SPL)
    if not all(any(n in r["name"] for n in names) for r in reads):
        return {**row, "status": "name mismatch", "supply": None}
    supplies = {r["supply"] for r in reads}
    if max(supplies) - min(supplies) > TOLERANCE:
        status = "providers disagree"
    else:
        status = "ok" if len(reads) > 1 else "single provider"
    return {**row, "status": status, "supply": max(supplies)}


def money(v):
    return "n/a" if v is None else f"{v:,.2f}"


def report(fund, rows, taken):
    lines = [f"{fund} supply, read {taken} (finalized).", "", "| Chain | Class | On-chain | Providers |", "|---|---|---:|---|"]
    lines += [f"| {r['chain']} | {r['class']} | {money(r['supply'])} | {r['status']} |" for r in rows]
    lines.append(f"| **Total** | | **{money(sum(r['supply'] for r in rows if r['supply'] is not None))}** | |")
    return "\n".join(lines)


def run_fund(fund, cfg, taken):
    rows = []
    for token in cfg["tokens"]:
        rows.append(check_token(token, cfg["expect_name"]))
        time.sleep(1)  # public RPCs rate-limit bursts
    extra = {}
    if rwa:
        rows, extra["rwa_updated"] = rwa.annotate(rows, cfg["rwa_slug"])
    if cfg.get("coingecko"):
        extra["coingecko"] = sources.coingecko(cfg["coingecko"])
    (HERE / "snapshots").mkdir(exist_ok=True)
    (HERE / "snapshots" / f"{fund}-{taken}.json").write_text(
        json.dumps({"fund": fund, "taken": taken, **extra, "rows": rows}, indent=1, default=str))
    print(rwa.report(fund, rows, extra["rwa_updated"], taken) if rwa else report(fund, rows, taken), end="\n\n")
    return all(r["status"] == "ok" for r in rows)


def main(*funds):
    config = json.loads((HERE / "funds.json").read_text())
    if any(f.startswith("-") for f in funds):
        print(__doc__.strip())
        return 0
    unknown = [f for f in funds if f not in config]
    if unknown:
        print(f"unknown fund {', '.join(unknown)}; known: {', '.join(config)}", file=sys.stderr)
        return 2
    taken = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    results = [run_fund(f, config[f], taken) for f in (funds or config)]
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
