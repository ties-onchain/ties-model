import gzip
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import events
from test_soroban import EUTBL, INSTANCE

ISS = "GISSUER"


def at(day, hh, mm=0, ss=0):
    return f"{day}T{hh:02d}:{mm:02d}:{ss:02d}Z"


def pay(when, frm, to, amount, code="BENJI", n=[0]):
    n[0] += 1
    return {"id": str(n[0]), "type": "payment", "created_at": when, "transaction_hash": f"tx{n[0]}", "source_account": frm,
            "from": frm, "to": to, "asset_code": code, "amount": amount}


def batch(day, count, start_hour=22):
    t0 = datetime.fromisoformat(f"{day}T{start_hour:02d}:00:00+00:00")
    return [pay((t0 + timedelta(seconds=5 * i)).strftime("%Y-%m-%dT%H:%M:%SZ"), ISS, f"GH{i}", "10.0000000") for i in range(count)]


def kinds(ops):
    return [e["kind"] for e in events.classify(ops, "BENJI", ISS)]


class ClassifyTest(unittest.TestCase):
    def test_nightly_batch_is_dividend_and_daytime_mint_is_subscription(self):
        ks = kinds(batch("2026-10-07", 60) + [pay(at("2026-10-07", 15), ISS, "GBUYER", "8000000")])
        self.assertEqual((ks.count("dividend"), ks.count("subscription")), (60, 1))

    def test_batch_threshold_needs_more_than_batch_min_mints_in_the_window(self):
        self.assertEqual(set(kinds(batch("2026-10-07", events.BATCH_MIN))), {"subscription"})
        self.assertEqual(set(kinds(batch("2026-10-07", events.BATCH_MIN + 1))), {"dividend"})

    def test_second_mint_to_a_holder_inside_the_batch_is_unknown_not_dividend(self):
        ops = batch("2026-10-07", 60)
        ops.append(pay(at("2026-10-07", 22, 1), ISS, "GH0", "5000000"))
        ks = kinds(ops)
        self.assertEqual((ks.count("dividend"), ks.count("unknown")), (60, 1))

    def test_redemptions_payment_and_claimable_balance_to_the_issuer(self):
        ccb = {"id": "c1", "type": "create_claimable_balance", "created_at": at("2026-10-29", 8), "transaction_hash": "txc",
               "source_account": "GHOLDER", "asset": f"BENJI:{ISS}", "amount": "50.1951878",
               "claimants": [{"destination": "GHOLDER"}, {"destination": ISS}]}
        self.assertEqual(kinds([pay(at("2026-10-29", 16), "GHOLDER", ISS, "7865.02"), ccb]), ["redemption", "redemption"])

    def test_unfamiliar_token_operations_surface_as_unknown(self):
        issuer_ccb = {"id": "c2", "type": "create_claimable_balance", "created_at": at("2026-10-01", 9), "transaction_hash": "txd",
                      "source_account": ISS, "asset": f"BENJI:{ISS}", "amount": "1", "claimants": [{"destination": "GX"}]}
        clawback = {"id": "c3", "type": "clawback", "created_at": at("2026-10-01", 9), "transaction_hash": "txe",
                    "source_account": ISS, "asset_code": "BENJI", "amount": "2"}
        self.assertEqual(kinds([issuer_ccb, clawback]), ["unknown", "unknown"])

    def test_other_assets_and_bookkeeping_operations_are_ignored(self):
        trust = {"id": "t", "type": "allow_trust", "created_at": at("2026-10-01", 9), "transaction_hash": "txt",
                 "source_account": ISS, "asset_code": "BENJI"}
        self.assertEqual(kinds([pay(at("2026-10-01", 16), "GHOLDER", ISS, "400", code="USDPEND"), trust]), [])

    def test_regression_1_oct_2026_redemptions_match_sec_to_the_cent(self):
        burns = [pay(at("2026-10-01", 16, 10), "GCJU26", ISS, "125000.0000000"), pay(at("2026-10-01", 17, 3), "GAOVIO", ISS, "5000.0000000"),
                 pay(at("2026-10-01", 20, 55), "GCNO6E", ISS, "14695.3000000")]
        rows, _ = events.compare(events.classify(burns, "BENJI", ISS), [{"date": "2026-10-01", "subscriptions": "0", "redemptions": "144695.30"}])
        self.assertEqual(rows[0]["gap_redemptions"], 0)


def move(chain, side, when, amount, account="0xh"):
    return {"chain": chain, "side": side, "at": when, "amount": amount, "account": account, "tx": f"{chain}-{when}"}


class CrossChainTest(unittest.TestCase):
    def stellar_night(self, day):
        return events.classify(batch(day, 60), "BENJI", ISS)

    def test_evm_mint_in_the_stellar_run_is_dividend_daytime_mint_is_subscription(self):
        runs = events.dividend_runs(self.stellar_night("2026-10-07"))
        evm = events.classify_evm([move("base", "mint", at("2026-10-07", 22, 28), "1637.09"),
                                   move("arbitrum", "mint", at("2026-10-07", 15), "5000000"),
                                   move("arbitrum", "burn", at("2026-10-07", 16), "3000000")], runs)
        self.assertEqual([e["kind"] for e in evm], ["dividend", "subscription", "redemption"])

    def test_evm_mint_on_a_night_without_a_known_run_is_unknown(self):
        runs = events.dividend_runs(self.stellar_night("2026-10-07"))
        self.assertEqual([e["kind"] for e in events.classify_evm([move("base", "mint", at("2026-10-08", 22, 28), "1637.09")], runs)], ["unknown"])

    def test_second_evm_mint_to_a_holder_in_one_run_is_unknown(self):
        runs = events.dividend_runs(self.stellar_night("2026-10-07"))
        evm = events.classify_evm([move("base", "mint", at("2026-10-07", 22, 28), "1637.09"), move("base", "mint", at("2026-10-07", 22, 29), "9")], runs)
        self.assertEqual([e["kind"] for e in evm], ["dividend", "unknown"])

    def test_burn_and_same_amount_minted_on_another_chain_within_minutes_is_a_migration(self):
        night = self.stellar_night("2026-11-04")
        mint = pay(at("2026-11-04", 21, 55), ISS, "GAHPQ5V2", "16167142.7300000")  # lands inside the batch window, like 4 Nov
        evs = events.classify(batch("2026-11-04", 60) + [mint], "BENJI", ISS)
        evs += events.classify_evm([move("ethereum", "burn", at("2026-11-04", 21, 54), "16167142.73")], events.dividend_runs(night))
        tagged = events.tag_migrations(evs)
        self.assertEqual(sorted(e["chain"] for e in tagged if e["kind"] == "migration"), ["ethereum", "stellar"])

    def test_same_amount_on_the_same_chain_or_hours_apart_is_not_a_migration(self):
        evs = [{"chain": "base", "at": at("2026-10-07", 15), "kind": "redemption", "side": "burn", "amount": "100"},
               {"chain": "base", "at": at("2026-10-07", 15, 5), "kind": "subscription", "side": "mint", "amount": "100"},
               {"chain": "stellar", "at": at("2026-10-07", 18), "kind": "subscription", "side": "mint", "amount": "100"}]
        self.assertEqual([e["kind"] for e in events.tag_migrations(evs)], ["redemption", "subscription", "subscription"])


class FranklinLabelTest(unittest.TestCase):
    def test_issuer_labelled_evm_burn_pairs_with_the_unlabelled_stellar_mint(self):
        """4 Nov 2026: Ethereum carries InstantCXTransferOut, Stellar only an issuer payment."""
        burn = move("ethereum", "burn", at("2026-11-04", 21, 54), "16167142.73") | {"label": "migration"}
        evs = events.classify_evm([burn], []) + events.classify([pay(at("2026-11-04", 21, 55), ISS, "GAHPQ5V2", "16167142.7300000")], "BENJI", ISS)
        self.assertEqual([(e["chain"], e["kind"]) for e in events.tag_migrations(evs)], [("ethereum", "migration"), ("stellar", "migration")])

    def test_one_mint_pairs_with_one_burn_only(self):
        burns = [move(c, "burn", at("2026-11-04", 21, 54), "100") for c in ("base", "polygon")]
        evs = events.classify_evm(burns, []) + events.classify([pay(at("2026-11-04", 21, 55), ISS, "GX", "100.0000000")], "BENJI", ISS)
        self.assertEqual(sorted(e["kind"] for e in events.tag_migrations(evs)), ["migration", "migration", "redemption"])

    def test_cx_logs_label_every_move_in_their_transaction(self):
        mint, other = events.to_move("arbitrum", "mint", EvmTest().log(5, 10**18, "0xcx"), 18), events.to_move("arbitrum", "mint", EvmTest().log(6, 10**18, "0xsub"), 18)
        events.label_migrations([mint, other], {"0xcx"})
        self.assertEqual((mint.get("label"), other.get("label")), ("migration", None))


class OtherChainsTest(unittest.TestCase):
    def test_a_chain_that_fails_or_has_no_reader_is_named_and_the_rest_still_count(self):
        tokens = [{"chain": "stellar", "kind": "stellar"}, {"chain": "base", "kind": "evm", "events": {}},
                  {"chain": "polygon", "kind": "evm", "events": {}}, {"chain": "aptos", "kind": "aptos"}]

        def load(t):
            if t["chain"] == "polygon":
                raise OSError("HTTP Error 403: Forbidden")
            return [move("base", "burn", at("2026-10-07", 16), "3000000")]
        evs, missing = events.other_chains(tokens, [], load)
        self.assertEqual([(e["chain"], e["kind"]) for e in evs], [("base", "redemption")])
        self.assertEqual(missing, {"polygon": "read failed: HTTP Error 403: Forbidden", "aptos": "no event reader yet"})


class CompareTest(unittest.TestCase):
    SEC = [{"date": "2026-10-02", "subscriptions": "100", "redemptions": "5"}, {"date": "2026-10-05", "subscriptions": "70", "redemptions": "0"}]

    def ev(self, day, kind, amount):
        return {"at": f"{day}T22:00:00Z", "day": day, "kind": kind, "side": "mint", "amount": amount, "account": "G", "tx": "t"}

    def test_weekend_events_land_on_the_next_sec_date_and_rest_is_what_stellar_does_not_explain(self):
        evs = [self.ev("2026-10-02", "subscription", "90"), self.ev("2026-10-03", "dividend", "3"), self.ev("2026-10-04", "dividend", "3"),
               self.ev("2026-10-05", "dividend", "3"), self.ev("2026-10-02", "redemption", "5")]
        rows, unbooked = events.compare(evs, self.SEC)
        self.assertEqual([(r["dividend"], r["gap_subscriptions"], r["gap_redemptions"]) for r in rows],
                         [(0, Decimal(10), 0), (Decimal(9), Decimal(61), 0)])
        self.assertEqual(unbooked, [])

    def test_events_after_the_last_sec_date_are_held_back_not_dropped(self):
        rows, unbooked = events.compare([self.ev("2026-10-31", "dividend", "3")], self.SEC)
        self.assertEqual((len(unbooked), sum(r["dividend"] for r in rows)), (1, 0))

    def test_unknown_is_kept_out_of_the_explained_flows(self):
        rows, _ = events.compare([self.ev("2026-10-02", "unknown", "40")], self.SEC)
        self.assertEqual((rows[0]["unknown"], rows[0]["gap_subscriptions"]), (Decimal(40), Decimal(100)))


class FetchTest(unittest.TestCase):
    T0 = datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp()

    def horizon(self, pages, fail_first=0):
        """Fake Horizon: ledger n closes at T0 + 5 s * (n - 1000); operation pages keyed by cursor."""
        calls = {"ops": 0}

        def get(url):
            if url == "H":
                return {"history_latest_ledger": 900_000}
            if "/ledgers/" in url:
                n = int(url.rsplit("/", 1)[1])
                return {"closed_at": datetime.fromtimestamp(self.T0 + 5 * (n - 1000), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
            calls["ops"] += 1
            if calls["ops"] <= fail_first:
                raise OSError("429")
            return {"_embedded": {"records": pages.get(url.rsplit("cursor=", 1)[1], [])}}
        return get, calls

    def op(self, i, when):
        return {"id": str(i), "paging_token": str(i), "type": "payment", "created_at": when, "transaction_hash": "x", "_links": {}}

    def test_ledger_lookup_lands_on_the_first_ledger_of_the_day(self):
        get, _ = self.horizon({})
        self.assertEqual(events.ledger_at("H", "2026-10-01T00:00:00Z", get), 1000)

    def test_pages_until_the_end_bound_drops_links_and_retries_rate_limits(self):
        start = str((1000 << 32) - 1)
        pages = {start: [self.op(1, "2026-10-01T01:00:00Z"), self.op(2, "2026-10-02T01:00:00Z")],
                 "2": [self.op(3, "2026-10-31T23:59:59Z"), self.op(4, "2026-11-01T00:00:00Z")]}
        get, calls = self.horizon(pages, fail_first=1)
        ops = events.fetch("H", ISS, "2026-10-01T00:00:00Z", "2026-11-01T00:00:00Z", get, sleep=lambda _s: None)
        self.assertEqual([o["id"] for o in ops], ["1", "2", "3"])
        self.assertNotIn("_links", ops[0])
        self.assertEqual(calls["ops"], 3)  # one 429, then two pages

    def test_gives_up_loudly_when_horizon_keeps_refusing(self):
        get, _ = self.horizon({}, fail_first=99)
        with self.assertRaises(OSError):
            events.fetch("H", ISS, "2026-10-01T00:00:00Z", "2026-11-01T00:00:00Z", get, sleep=lambda _s: None)


class EvmTest(unittest.TestCase):
    T0 = datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp()
    ADDR = "0x00000000000000000000000000000000000000aa"
    HOLDER = "0x" + "0" * 24 + "b" * 40

    def chain(self, head=200_000, logs=()):
        """Fake EVM RPC: block n is mined at T0 + 2 s * (n - 100_000); records which blocks and ranges were asked for."""
        seen = {"blocks": [], "ranges": []}

        def rpc(_url, method, params):
            if method == "eth_blockNumber":
                return hex(head)
            if method == "eth_getBlockByNumber":
                n = int(params[0], 16)
                seen["blocks"].append(n)
                return {"timestamp": hex(int(self.T0 + 2 * (n - 100_000)))}
            if method == "eth_call":
                return hex(18)
            q = params[0]
            lo, hi = int(q["fromBlock"], 16), int(q["toBlock"], 16)
            seen["ranges"].append((lo, hi))
            if len(q["topics"]) == 1:
                assert isinstance(q["topics"][0], str), "one value per position: Arbitrum refuses OR lists over 100k blocks"
                return [log for s, log in logs if s == "cx" and lo <= int(log["blockNumber"], 16) <= hi]
            side = "mint" if q["topics"][1] else "burn"
            return [log for s, log in logs if s == side and lo <= int(log["blockNumber"], 16) <= hi]
        return rpc, seen

    def log(self, block, wei, tx):
        topics = [events.TRANSFER, events.ZERO_TOPIC, self.HOLDER]
        return {"blockNumber": hex(block), "transactionHash": tx, "data": hex(wei), "topics": topics}

    def test_block_at_finds_the_first_block_of_the_day_without_reaching_back_to_genesis(self):
        rpc, seen = self.chain()
        call = events.providers(["u"], rpc, sleep=lambda _s: None)
        self.assertEqual(events.block_at(call, "2026-10-01T00:00:00Z"), 100_000)
        self.assertGreater(min(seen["blocks"]), 0)  # free RPCs refuse old blocks as archive requests

    def test_fetch_slices_the_range_decodes_18_decimals_and_stamps_each_move(self):
        mint, migrated = ("mint", self.log(100_010, 5 * 10**18 + 1, "0xm")), ("mint", self.log(101_500, 10**18, "0xcx"))
        rpc, seen = self.chain(logs=[mint, migrated, ("cx", self.log(101_500, 0, "0xcx"))])
        moves = events.fetch_evm("base", self.ADDR, {"rpcs": ["u"], "span": 1000}, "2026-10-01T00:00:00Z", "2026-10-01T01:00:00Z",
                                 rpc, sleep=lambda _s: None)
        self.assertEqual([(m["side"], m["amount"], m["account"], m["at"], m.get("label")) for m in moves],
                         [("mint", "5.000000000000000001", "0x" + "b" * 40, "2026-10-01T00:00:20Z", None),
                          ("mint", "1.000000000000000000", "0x" + "b" * 40, "2026-10-01T00:50:00Z", "migration")])
        self.assertEqual(sorted({r for r in seen["ranges"]}), [(100_000, 100_999), (101_000, 101_799)])  # one hour = 1,800 blocks


    def test_one_query_sorts_mints_and_burns_and_skips_labels_when_asked(self):
        burn = {"blockNumber": hex(100_020), "transactionHash": "0xb", "data": hex(10**18), "topics": [events.TRANSFER, self.HOLDER, events.ZERO_TOPIC]}
        between = {"blockNumber": hex(100_030), "transactionHash": "0xt", "data": hex(10**18), "topics": [events.TRANSFER, self.HOLDER, self.HOLDER]}
        logs = [self.log(100_010, 2 * 10**18, "0xm"), burn, between]
        asked = []

        def rpc(url, method, params):
            if method == "eth_getLogs":
                asked.append(params[0]["topics"])
                return [log for log in logs if int(params[0]["fromBlock"], 16) <= int(log["blockNumber"], 16) <= int(params[0]["toBlock"], 16)]
            return self.chain()[0](url, method, params)
        moves = events.fetch_evm("bsc", self.ADDR, {"rpcs": ["u"], "span": 5000, "one_query": True, "labels": False},
                                 "2026-10-01T00:00:00Z", "2026-10-01T01:00:00Z", rpc, sleep=lambda _s: None)
        self.assertEqual([(m["side"], m["tx"]) for m in moves], [("mint", "0xm"), ("burn", "0xb")])
        self.assertEqual(asked, [[events.TRANSFER]])

    def test_recent_days_use_the_fast_short_history_source_older_days_the_deep_one(self):
        token = {"chain": "bsc", "kind": "evm", "address": "0xa", "events": {"rpcs": ["deep"], "span": 4999,
                                                                              "recent": {"days": 2, "rpcs": ["fast"], "span": 50000}}}
        used = []
        with mock.patch.object(events, "fetch_evm", lambda chain, address, source, start, end: used.append((source["rpcs"], source["span"], "recent" in source))):
            now = events.stamp("2026-11-12T07:00:00Z")
            events.read_events(token, "2026-11-11T00:00:00Z", "2026-11-12T00:00:00Z", now=now)
            events.read_events(token, "2026-11-05T00:00:00Z", "2026-11-06T00:00:00Z", now=now)
        self.assertEqual(used, [(["fast"], 50000, False), (["deep"], 4999, True)])


class EtherscanTest(unittest.TestCase):
    HOLDER = "0x" + "0" * 24 + "c" * 40

    def api(self, mints=0, burn_reply=None):
        """Fake Etherscan v2: logs for mints come in pages of 1,000; records every query."""
        asked = []

        def get(url):
            q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
            asked.append(q)
            if q["module"] == "block":
                return {"status": "1", "message": "OK", "result": "1000" if q["timestamp"] == str(int(EvmTest.T0)) else "5000"}
            if q["module"] == "proxy":
                return {"jsonrpc": "2.0", "id": 1, "result": hex(18)}
            if "topic1" in q:
                page = int(q["page"])
                n = max(0, min(1000, mints - 1000 * (page - 1)))
                logs = [{"blockNumber": hex(1000 + i), "transactionHash": f"0x{page}-{i}", "timeStamp": hex(int(EvmTest.T0) + i),
                         "data": hex(10**18), "topics": [events.TRANSFER, events.ZERO_TOPIC, self.HOLDER]} for i in range(n)]
                return {"status": "1", "message": "OK", "result": logs} if logs else {"status": "0", "message": "No records found", "result": []}
            return burn_reply or {"status": "0", "message": "No records found", "result": []}
        return get, asked

    def fetch(self, get):
        return events.fetch_etherscan("polygon", "0xaa", 137, "2026-10-01T00:00:00Z", "2026-11-01T00:00:00Z", "k", get, sleep=lambda _s: None)

    def test_pages_past_1000_results_and_stamps_from_the_log(self):
        get, asked = self.api(mints=1003)
        moves = self.fetch(get)
        self.assertEqual(len(moves), 1003)
        self.assertEqual((moves[0]["side"], moves[0]["amount"], moves[0]["at"]), ("mint", "1.000000000000000000", "2026-10-01T00:00:00Z"))
        self.assertEqual([q["page"] for q in asked if "topic1" in q], ["1", "2"])
        self.assertEqual({q["toBlock"] for q in asked if q["module"] == "logs"}, {"4999"})  # end block is exclusive

    def test_a_refused_key_raises_instead_of_reading_as_no_moves(self):
        get, _ = self.api(burn_reply={"status": "0", "message": "NOTOK", "result": "Invalid API Key"})
        with self.assertRaises(RuntimeError):
            self.fetch(get)

    def test_more_than_10000_logs_is_an_error_not_a_silent_cut(self):
        get, _ = self.api(mints=10_000)
        with self.assertRaises(RuntimeError):
            self.fetch(get)

    def test_missing_key_says_what_to_do(self):
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(RuntimeError, "ETHERSCAN_API_KEY"):
            events.read_events({"chain": "polygon", "kind": "evm", "address": "0xaa", "events": {"etherscan": 137}}, "a", "b")


class SolanaTest(unittest.TestCase):
    MINT = "BenjiMint111"

    def ix(self, kind, amount, mint=None, key="amount"):
        info = {"account": "Holder1", "mint": mint or self.MINT}
        info |= {"amount": str(amount)} if key == "amount" else {"tokenAmount": {"amount": str(amount)}}
        return {"program": "spl-token", "parsed": {"type": kind, "info": info}}

    def rpc(self, txs):
        """Fake Solana RPC: newest first signatures, one transaction per signature."""
        def call(_url, method, params):
            if method == "getSignaturesForAddress":
                return [{"signature": sig, "err": err, "blockTime": t} for sig, (t, err, _top, _inner) in txs.items()]
            if method == "getTokenSupply":
                return {"value": {"decimals": 9}}
            t, _err, top, inner = txs[params[0]]
            return {"slot": t, "blockTime": t, "transaction": {"message": {"instructions": top}},
                    "meta": {"innerInstructions": [{"instructions": inner}] if inner else None}}
        return call

    def test_reads_mints_and_inner_burns_of_this_mint_inside_the_window_only(self):
        oct2, before, after = int(EvmTest.T0) + 86400, int(EvmTest.T0) - 1, int(EvmTest.T0) + 40 * 86400
        txs = {"late": (after, None, [self.ix("mintTo", 5)], []),
               "burned": (oct2 + 60, None, [{"program": "system"}], [self.ix("burnChecked", 2 * 10**9, key="tokenAmount")]),
               "div": (oct2, None, [self.ix("mintTo", 53_001_007_578), self.ix("mintTo", 7, mint="OtherMint")], []),
               "failed": (oct2, {"InstructionError": [0, "x"]}, [self.ix("mintTo", 9)], []),
               "early": (before, None, [self.ix("mintTo", 5)], [])}
        moves = events.fetch_solana(self.MINT, ["u"], "2026-10-01T00:00:00Z", "2026-11-01T00:00:00Z", self.rpc(txs), sleep=lambda _s: None)
        self.assertEqual([(m["side"], m["amount"], m["tx"]) for m in moves], [("mint", "53.001007578", "div"), ("burn", "2.000000000", "burned")])



class SorobanEventsTest(unittest.TestCase):
    # real EUTBL events, 4 Nov 2026 (Soroban RPC getEvents); the clawback is built by hand, none seen yet
    MINT = {"topic": ["AAAADwAAAARtaW50", "AAAAEgAAAAAAAAAADu6mASH7IgadzMFGlgZXRZ8RP0m8RrWF/Iw/0mOnC08="], "value": "AAAACgAAAAAAAAAAAAAAAH5C85E=",
            "ledger": 65300722, "ledgerClosedAt": "2026-11-04T10:54:09Z", "txHash": "33ca"}
    BURN = {"topic": ["AAAADwAAAARidXJu", "AAAAEgAAAAErlWvEjUyjXXAxHKeCBkZlrCm8DK7sDZTG/dJX3stkUg=="], "value": "AAAACgAAAAAAAAAAAAAAABwO4Mo=",
            "ledger": 65300675, "ledgerClosedAt": "2026-11-04T10:50:14Z", "txHash": "c487"}
    CLAWBACK = BURN | {"topic": ["AAAADwAAAAhjbGF3YmFjaw==", BURN["topic"][1]], "ledgerClosedAt": "2026-11-04T23:59:59Z", "txHash": "claw"}
    TRANSFER = BURN | {"topic": ["AAAADwAAAAh0cmFuc2Zlcg==", BURN["topic"][1], MINT["topic"][1]], "txHash": "move"}
    NEXT_DAY = MINT | {"ledgerClosedAt": "2026-11-05T00:00:00Z", "txHash": "late"}

    def rpc(self, pages, oldest="1793732959"):
        asked = []

        def call(_url, method, params):
            asked.append((method, params))
            if method == "getHealth":
                return {"oldestLedger": 65289384, "oldestLedgerCloseTime": oldest, "latestLedger": 65410343, "latestLedgerCloseTime": "1794337757"}
            if method == "getLedgerEntries":
                return {"entries": [{"xdr": INSTANCE}], "latestLedger": 1}
            cursor = params["pagination"].get("cursor", "0")
            return {"events": pages[int(cursor)], "cursor": str(int(cursor) + 1)}
        return call, asked

    def test_mints_burns_and_clawbacks_of_the_day_across_pages_transfers_left_out(self):
        call, asked = self.rpc([[self.BURN, self.TRANSFER], [self.MINT, self.CLAWBACK], [self.NEXT_DAY], []])
        moves = events.fetch_soroban(EUTBL, ["u"], "2026-11-04T00:00:00Z", "2026-11-05T00:00:00Z", call, sleep=lambda _s: None)
        self.assertEqual([(m["side"], m["amount"], m["tx"], m["account"][:8]) for m in moves],
                         [("burn", "4707.37098", "c487", "CAVZK26E"), ("mint", "21183.16945", "33ca", "GAHO5JQB"), ("burn", "4707.37098", "claw", "CAVZK26E")])
        self.assertEqual(sum(m == "getEvents" for m, _ in asked), 3)  # stops at the page that reaches the next day

    def test_refuses_a_day_the_rpc_no_longer_holds(self):
        call, _ = self.rpc([[]], oldest=str(int(datetime(2026, 11, 4, 1, tzinfo=timezone.utc).timestamp())))
        with self.assertRaisesRegex(RuntimeError, "pruned"):
            events.fetch_soroban(EUTBL, ["u"], "2026-11-04T00:00:00Z", "2026-11-05T00:00:00Z", call, sleep=lambda _s: None)

    def test_closure_names_chains_without_a_reader(self):
        snap = lambda taken, block: {"taken": taken, "rows": [{"chain": c, "supply": "1", "reads": [{"at": f"ledger {block}"}]} for c in ("stellar", "base")]}
        rows = events.closure("F", snap("2026-11-08T21:30Z", 1), snap("2026-11-09T21:30Z", 2), None, load=lambda c, d: [], readers={"stellar"})
        self.assertEqual([(r["chain"], r["status"]) for r in rows], [("stellar", "ok"), ("base", "no event reader")])

    def test_closure_counts_soroban_moves_on_stellar(self):
        self.assertEqual(events.supply_effect("stellar", {"side": "burn", "amount": "4707.37098", "block": 65300675}), (65300675, Decimal("-4707.37098")))

    def test_a_fund_is_classifiable_with_a_stellar_issuer_or_a_reader_on_every_chain(self):
        self.assertTrue(events.classifiable([{"kind": "stellar"}, {"kind": "evm"}]))
        self.assertFalse(events.classifiable([{"kind": "soroban", "events": {}}, {"kind": "evm"}]))
        self.assertTrue(events.classifiable([{"kind": "evm", "events": {}}, {"kind": "aptos", "events": {}}]))

    def test_closure_keeps_two_share_classes_on_one_chain_apart(self):
        snap = lambda taken, a, b: {"taken": taken, "rows": [{"chain": "ethereum", "class": "F", "supply": a, "reads": [{"at": "finalized block 20"}]},
                                                             {"chain": "ethereum", "class": "F-I", "supply": b, "reads": [{"at": "finalized block 20"}]}]}
        before, after = snap("2026-11-05T21:30Z", "100", "500"), snap("2026-11-06T21:30Z", "105", "400")
        before["rows"][0]["reads"] = before["rows"][1]["reads"] = [{"at": "finalized block 10"}]
        archive = {"ethereum": [{"side": "mint", "amount": "5", "block": 15}], "ethereum-i": [{"side": "burn", "amount": "100", "block": 15}]}
        rows = events.closure("F", before, after, None, load=lambda c, d: archive[c] if d == "2026-11-06" else [], slots={("ethereum", "F"): "ethereum", ("ethereum", "F-I"): "ethereum-i"})
        self.assertEqual([(r["chain"], r["status"]) for r in rows], [("ethereum", "ok"), ("ethereum-i", "ok")])


MODULE = "0xfr::fund_token"


def aptos_tx(*evs, sender="0xops"):
    return {"timestamp": str(int(EvmTest.T0 * 1e6) + 79_200_000_000), "sender": sender,
            "events": [{"type": f"{MODULE}::{name}", "data": data} for name, data in evs] + [{"type": "0x1::fungible_asset::Deposit", "data": {}}]}


class AptosTest(unittest.TestCase):
    def test_issuer_labels_become_supply_moves(self):
        tx = aptos_tx(("UpdatePriceEvent", {"price": "1000000000"}),
                      ("DividendDistributedEvent", {"account": "0xh", "dividend_cash_amount": "1533930226424", "price": "1000000000"}),
                      ("InstantCXTransferOut", {"account": "0xh", "amount": "5"}), ("BurnSharesEvent", {"from": "0xh", "amount": "7"}),
                      ("BalanceAdjustedEvent", {"account": "0xh", "shares": "3", "mint": True}))
        self.assertEqual([(m["side"], m["label"], m["raw"]) for m in events.aptos_moves(tx, MODULE)],
                         [("mint", "dividend", 1533930226424), ("burn", "migration", 5), ("burn", None, 7), ("mint", None, 3)])

    def test_dividend_cash_is_converted_at_the_fund_price(self):
        tx = aptos_tx(("DividendDistributedEvent", {"account": "0xh", "dividend_cash_amount": "2000000000", "price": "2000000000"}))
        self.assertEqual(events.aptos_moves(tx, MODULE)[0]["raw"], 1_000_000_000)

    def test_plain_transfer_is_not_a_supply_change(self):
        self.assertEqual(events.aptos_moves(aptos_tx(("InstantTransferEvent", {"from": "0xa", "to": "0xb", "amount": "9"})), MODULE), [])

    def test_securitize_ds_token_mints_and_burns_are_transfers_from_and_to_zero(self):
        ds = "0xsec::ds_token"
        tx = {"sender": "0xops", "events": [{"type": f"{ds}::Issue", "data": {"to": "0xa", "value": "15059470000"}},
                                            {"type": f"{ds}::Transfer", "data": {"from": "0x0", "to": "0xa", "value": "15059470000"}},
                                            {"type": f"{ds}::Transfer", "data": {"from": "0xb", "to": "0x0", "value": "7"}}]}
        self.assertEqual(events.aptos_moves(tx, ds), [{"side": "mint", "label": None, "raw": 15059470000, "account": "0xa"},
                                                     {"side": "burn", "label": None, "raw": 7, "account": "0xb"}])
        holder_to_holder = {"sender": "0xa", "events": [{"type": f"{ds}::Transfer", "data": {"from": "0xa", "to": "0xc", "value": "3"}}]}
        self.assertEqual(events.aptos_moves(holder_to_holder, ds), [])

    def test_touching_the_token_without_a_known_label_is_unknown(self):
        """26 Oct 2026: 14.76M showed up as a deposit with no matching withdrawal. Never count that as a mint."""
        moves = events.aptos_moves(aptos_tx(("TransactionSettledEvent", {"account": "0xh", "shares": "14760299200000000"})), MODULE)
        self.assertEqual([(m["side"], m["label"]) for m in moves], [("unknown", "unknown")])

    def test_fetch_pages_the_indexer_and_reads_each_transaction_once(self):
        pages = [[{"transaction_version": 7}, {"transaction_version": 7}, {"transaction_version": 9}], []]
        posted = []

        def post(_url, payload, headers):
            posted.append((payload["variables"]["after"], headers["Authorization"]))
            return {"data": {"fungible_asset_activities": pages.pop(0)}}

        def get(url):
            if url == "rest":
                return {"oldest_block_height": "1"}
            if url == "rest/blocks/by_height/1001":
                return {"block_timestamp": str(int((EvmTest.T0 - 86400) * 1e6))}
            if url.endswith("Metadata"):
                return {"data": {"decimals": 9}}
            v = url.rsplit("/", 1)[1]
            return aptos_tx(("MintSharesEvent", {"to": f"0x{v}", "amount": "1500000000"}))
        moves = events.fetch_aptos("0xasset", MODULE, "rest", "2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z", "key", post, get, sleep=lambda _s: None)
        self.assertEqual([(m["tx"], m["amount"], m["at"]) for m in moves], [("7", "1.500000000", "2026-10-01T22:00:00Z"), ("9", "1.500000000", "2026-10-01T22:00:00Z")])
        self.assertEqual(posted, [(0, "Bearer key"), (9, "Bearer key")])

    def test_a_pruned_day_is_an_error_not_an_empty_day(self):
        """25 Oct 2026: the indexer answered [] for a pruned day; archived, that would claim nothing happened."""
        def get(url):
            return {"oldest_block_height": "1"} if url == "rest" else {"block_timestamp": str(int((EvmTest.T0 + 86400) * 1e6))}
        with self.assertRaisesRegex(RuntimeError, "pruned"):
            events.fetch_aptos("0xasset", MODULE, "rest", "2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z", "key",
                               post=lambda *a, **k: self.fail("indexer asked"), get=get, sleep=lambda _s: None)

    def test_labelled_moves_keep_the_issuer_label_in_classification(self):
        moves = [move("aptos", "mint", at("2026-10-07", 15), "1533.9") | {"label": "dividend"}, move("aptos", "unknown", at("2026-10-07", 15), "0") | {"label": "unknown"}]
        self.assertEqual([e["kind"] for e in events.classify_evm(moves, [])], ["dividend", "unknown"])


class ArchiveTest(unittest.TestCase):
    TOKENS = [{"chain": "stellar", "kind": "stellar"}, {"chain": "base", "kind": "evm", "events": {}},
              {"chain": "aptos", "kind": "aptos", "events": {}}, {"chain": "bnb", "kind": "evm"}]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(events, "HERE", Path(self.tmp.name))
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_captures_each_day_skips_archived_ones_and_keeps_going_when_a_chain_fails(self):
        asked = []

        def read(t, start, end):
            asked.append((t["chain"], start, end))
            if t["chain"] == "aptos":
                raise RuntimeError("HTTP Error 410: Gone")
            return [{"chain": t["chain"], "day": start[:10]}]
        events.archive_path("F", "base", "2026-11-01").parent.mkdir(parents=True)
        events.archive_path("F", "base", "2026-11-01").write_bytes(gzip.compress(b"[]"))
        failed = events.capture("F", self.TOKENS, ["2026-11-01", "2026-11-02"], read)
        self.assertEqual(failed, {"aptos": "2026-11-01: HTTP Error 410: Gone"})
        self.assertEqual(asked, [("stellar", "2026-11-01T00:00:00Z", "2026-11-02T00:00:00Z"), ("stellar", "2026-11-02T00:00:00Z", "2026-11-03T00:00:00Z"),
                                 ("base", "2026-11-02T00:00:00Z", "2026-11-03T00:00:00Z"), ("aptos", "2026-11-01T00:00:00Z", "2026-11-02T00:00:00Z"),
                                 ("aptos", "2026-11-02T00:00:00Z", "2026-11-03T00:00:00Z")])
        self.assertFalse(events.archive_path("F", "aptos", "2026-11-01").exists())
        self.assertEqual(json.loads(gzip.decompress(events.archive_path("F", "base", "2026-11-02").read_bytes())), [{"chain": "base", "day": "2026-11-02"}])

    def test_month_comes_from_the_archive_only_when_every_day_is_there(self):
        token = {"chain": "base", "kind": "evm", "events": {}}
        events.capture("F", [token], events.month_days("2026-02"), lambda t, s, e: [{"day": s[:10]}])
        self.assertEqual(len(events.month_raw("F", token, "2026-02", read=lambda *a: self.fail("fetched"))), 28)
        events.archive_path("F", "base", "2026-02-14").unlink()
        self.assertEqual(events.month_raw("F", token, "2026-02", read=lambda t, s, e: [{"whole": s}]), [{"whole": "2026-02-01T00:00:00Z"}])

    def write(self, chain, day, items):
        path = events.archive_path("F", chain, day)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(gzip.compress(json.dumps(items).encode()))

    def test_archived_uses_only_days_every_chain_holds_and_ids_every_event(self):
        tokens = [{"chain": "stellar", "kind": "stellar", "address": f"BENJI-{ISS}"}, {"chain": "base", "kind": "evm", "events": {}}]
        self.write("stellar", "2026-11-03", [pay(at("2026-11-03", 15), ISS, "GEARLY", "5")])
        mint = pay(at("2026-11-04", 21, 47, 39), ISS, "GHOLDER", "100.0000000")
        self.write("stellar", "2026-11-04", [mint])
        self.write("base", "2026-11-04", [move("base", "burn", at("2026-11-04", 21, 47, 29), "100") | {"tx": "0xb"},
                                          move("base", "mint", at("2026-11-04", 21, 47, 29), "1") | {"tx": "0xb"}])
        days, evs = events.archived("F", tokens)
        self.assertEqual(days, ["2026-11-04"])
        self.assertEqual([(e["id"], e["kind"]) for e in evs],
                         [("base-0xb", "migration"), ("base-0xb-1", "unknown"), (f"stellar-{mint['id']}", "migration")])

    def test_a_fund_without_stellar_finds_its_dividends_in_batch_mints_and_keeps_share_classes_apart(self):
        tokens = [{"chain": "ethereum", "class": "F", "kind": "evm", "events": {}},
                  {"chain": "ethereum", "class": "F-I", "kind": "evm", "events": {}, "slot": "ethereum-i"},
                  {"chain": "solana", "class": "F", "kind": "solana", "events": {}}]
        div = at("2026-11-06", 15, 33, 47)
        batch = [move("ethereum", "mint", div, "5", f"0xh{i}") | {"tx": "0xd"} for i in range(3)]
        self.write("ethereum", "2026-11-06", batch + [move("ethereum", "mint", at("2026-11-06", 18), "900000", "0xnew")])
        self.write("ethereum-i", "2026-11-06", [move("ethereum", "mint", at("2026-11-06", 15, 34), "1", "0xi") | {"tx": "0xd"},
                                                move("ethereum", "burn", at("2026-11-06", 20, 1, 11), "85075162.42", "0x8780") | {"tx": "0xr"}])
        self.write("solana", "2026-11-06", [move("solana", "mint", at("2026-11-06", 15, 40), "2", "S1")])
        days, evs = events.archived("F", tokens)
        self.assertEqual(days, ["2026-11-06"])
        got = [(e["id"], e["kind"], e.get("class")) for e in evs]
        self.assertEqual(got, [("ethereum-0xd", "dividend", None), ("ethereum-0xd-1", "dividend", None), ("ethereum-0xd-2", "dividend", None),
                               ("ethereum-i-0xd", "dividend", "F-I"), ("solana-solana-2026-11-06T15:40:00Z", "dividend", None),
                               ("ethereum-ethereum-2026-11-06T18:00:00Z", "subscription", None), ("ethereum-i-0xr", "redemption", "F-I")])

    def test_no_batch_means_no_dividend_run(self):
        self.assertEqual(events.batch_runs([move("ethereum", "mint", at("2026-11-06", 15), "5", f"0xh{i}") | {"tx": "0xd"} for i in range(2)]), [])

    def test_move_ids_number_repeats_of_one_tx(self):
        self.assertEqual(list(events.move_ids("solana", [{"tx": "s1"}, {"tx": "s2"}, {"tx": "s1"}])), ["solana-s1", "solana-s2", "solana-s1-1"])


class ClosureTest(unittest.TestCase):
    def snap(self, taken, **chains):
        reads = {"stellar": "ledger", "base": "finalized block", "aptos": "ledger version"}
        return {"taken": taken, "rows": [{"chain": c, "supply": supply, "reads": [{"at": f"{reads[c]} {block}"}]}
                                         for c, (supply, block) in chains.items()]}

    def op(self, ledger, **kw):
        return {"id": str((ledger << 32) + 1), "type": "payment", "amount": "0", "from": "G", "to": "G"} | kw

    def test_events_between_the_two_reads_explain_the_supply_change(self):
        before = self.snap("2026-11-08T21:30Z", stellar=("1000", 100), base=("50", 10), aptos=("7", 5))
        after = self.snap("2026-11-09T21:30Z", stellar=("1090", 200), base=("40", 20), aptos=("7", 6))
        archive = {("stellar", "2026-11-08"): [self.op(100, amount="999", **{"from": ISS})],  # at the read block: already counted
                   ("stellar", "2026-11-09"): [self.op(150, amount="100", **{"from": ISS}), self.op(180, amount="10", to=ISS),
                                               self.op(201, amount="5", **{"from": ISS})],  # after the read: next window
                   ("base", "2026-11-08"): [], ("base", "2026-11-09"): [{"side": "burn", "amount": "10", "block": 15}],
                   ("aptos", "2026-11-08"): [], ("aptos", "2026-11-09"): [{"side": "unknown", "amount": "0", "block": 6}]}
        rows = events.closure("F", before, after, ISS, load=lambda c, d: archive[(c, d)])
        self.assertEqual([(r["chain"], r["status"], r["gap"]) for r in rows], [("stellar", "ok", 0), ("base", "ok", 0), ("aptos", "ok", 0)])

    def test_a_missing_event_shows_as_a_gap_and_a_missing_day_as_incomplete(self):
        before = self.snap("2026-11-08T21:30Z", base=("50", 10), aptos=("7", 5))
        after = self.snap("2026-11-09T21:30Z", base=("60", 20), aptos=("8", 6))
        archive = {("base", "2026-11-08"): [], ("base", "2026-11-09"): [{"side": "mint", "amount": "4", "block": 12}]}

        def load(c, d):
            if (c, d) not in archive:
                raise FileNotFoundError(d)
            return archive[(c, d)]
        rows = events.closure("F", before, after, ISS, load=load)
        self.assertEqual([(r["chain"], r["status"], r.get("gap")) for r in rows], [("base", "gap", Decimal(6)), ("aptos", "archive incomplete", None)])

    def test_claimable_balance_to_the_issuer_counts_as_a_burn(self):
        ccb = {"id": str(7 << 32), "type": "create_claimable_balance", "amount": "50.19", "source_account": "GH", "claimants": [{"destination": ISS}]}
        self.assertEqual(events.supply_effect("stellar", ccb, ISS), (7, Decimal("-50.19")))

    def test_read_block_parses_every_snapshot_format(self):
        self.assertEqual([events.read_block(a) for a in ("ledger 65394755", "finalized block 55341176", "ledger version 7978034023",
                                                         "finalized slot 467469116")], [65394755, 55341176, 7978034023, 467469116])


class ProvidersTest(unittest.TestCase):
    def test_a_failing_rpc_hands_over_to_the_next_one(self):
        asked = []

        def rpc(url, _method, _params):
            asked.append(url)
            if url == "down":
                raise RuntimeError("service temporarily unavailable")
            return "0x1"
        self.assertEqual(events.providers(["down", "up"], rpc, sleep=lambda _s: None)("eth_blockNumber", []), "0x1")
        self.assertEqual(asked, ["down", "up"])

    def test_raises_when_every_rpc_keeps_failing(self):
        def rpc(_url, _method, _params):
            raise RuntimeError("429")
        with self.assertRaises(RuntimeError):
            events.providers(["a", "b"], rpc, sleep=lambda _s: None)("eth_blockNumber", [])

class MonthTest(unittest.TestCase):
    def test_month_bounds_cross_the_year(self):
        self.assertEqual(events.month_bounds("2026-12"), ("2026-12-01T00:00:00Z", "2027-01-01T00:00:00Z"))


if __name__ == "__main__":
    unittest.main()
