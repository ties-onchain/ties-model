import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from unittest import mock

import check

TOKEN = {"chain": "x", "class": "BUIDL", "kind": "fake", "address": "0xAbC", "rpcs": ["https://a.io/rpc", "https://b.io/rpc"]}


def readers(by_host):
    def read(url, _address):
        result = by_host[url.split("/")[2]]
        if isinstance(result, Exception):
            raise result
        return result
    return {"fake": read}


def ok(supply, name="BlackRock USD Institutional Digital Liquidity Fund"):
    return {"name": name, "supply": Decimal(supply), "at": "block 1"}


class SameHeightTest(unittest.TestCase):
    """Both providers read at the lower of their two finalized heads, so agreement means the same block."""
    def setup(self, fail_pinned_on=None):
        seen = []

        def read(url, _address, height=None):
            host = url.split("/")[2]
            seen.append((host, height))
            if height is not None and host == fail_pinned_on:
                raise OSError("state not available")
            return {"name": "BlackRock", "supply": Decimal("5"), "at": f"block {height or 'own'}"}
        heads = {"fake": lambda url: {"a.io": 120, "b.io": 117}[url.split("/")[2]]}
        row = check.check_token(TOKEN, ["BlackRock"], {"fake": read}, sleep=lambda _s: None, heads=heads)
        return row, seen

    def test_both_read_at_the_lower_head(self):
        row, seen = self.setup()
        self.assertEqual(seen, [("a.io", 117), ("b.io", 117)])
        self.assertTrue(row["same_height"])
        self.assertEqual(row["status"], "ok")

    def test_a_node_that_cannot_serve_the_height_falls_back_and_says_so(self):
        row, _ = self.setup(fail_pinned_on="b.io")
        self.assertEqual(row["status"], "ok")
        self.assertFalse(row["same_height"])
        self.assertTrue(row["reads"][1]["unpinned"])

    def test_chains_without_history_are_not_pinned(self):
        row = check.check_token(TOKEN, ["BlackRock"], readers({"a.io": ok("1"), "b.io": ok("1")}), sleep=lambda _s: None, heads={})
        self.assertFalse(row["same_height"])


class CheckTokenTest(unittest.TestCase):
    def run_check(self, a, b):
        return check.check_token(TOKEN, ["BlackRock"], readers({"a.io": a, "b.io": b}), sleep=lambda _s: None)

    def test_rate_limited_provider_is_retried(self):
        calls = []

        def flaky(url, _address):
            calls.append(url)
            if url.startswith("https://b.io") and calls.count(url) < 3:
                raise OSError("429")
            return ok("100")
        row = check.check_token(TOKEN, ["BlackRock"], {"fake": flaky}, sleep=lambda _s: None)
        self.assertEqual(row["status"], "ok")

    def test_two_providers_agree(self):
        row = self.run_check(ok("100.00"), ok("100.00"))
        self.assertEqual((row["status"], row["supply"]), ("ok", Decimal("100.00")))

    def test_providers_disagree_beyond_a_cent(self):
        self.assertEqual(self.run_check(ok("100.00"), ok("100.02"))["status"], "providers disagree")

    def test_one_provider_down_is_flagged_not_hidden(self):
        row = self.run_check(ok("100"), OSError("429"))
        self.assertEqual(row["status"], "single provider")
        self.assertEqual(len(row["errors"]), 1)

    def test_both_down_reports_unreadable(self):
        row = self.run_check(OSError("403"), OSError("429"))
        self.assertEqual((row["status"], row["supply"]), ("unreadable", None))

    def test_wrong_contract_is_caught_by_name(self):
        self.assertEqual(self.run_check(ok("5", "Fake Coin"), ok("5", "Fake Coin"))["status"], "name mismatch")

    def test_any_accepted_name_passes(self):
        row = check.check_token(TOKEN, ["Franklin", "BENJI"], readers({"a.io": ok("1", "BENJI"), "b.io": ok("1", "Franklin OnChain")}), sleep=lambda _s: None)
        self.assertEqual(row["status"], "ok")


class TokenNameOverrideTest(unittest.TestCase):
    def test_token_level_names_replace_the_funds(self):
        token = {**TOKEN, "expect_name": [""]}  # a legacy SPL mint carries no on-chain name
        row = check.check_token(token, ["Ondo"], readers({"a.io": ok("7", ""), "b.io": ok("7", "")}), sleep=lambda _s: None)
        self.assertEqual(row["status"], "ok")

    def test_fund_names_still_apply_without_override(self):
        row = check.check_token(TOKEN, ["Ondo"], readers({"a.io": ok("7", ""), "b.io": ok("7", "")}), sleep=lambda _s: None)
        self.assertEqual(row["status"], "name mismatch")


class NewReadersTest(unittest.TestCase):
    def test_aptos_coin_scales_by_decimals_and_pins_one_version(self):
        urls = []

        def fake(url, payload=None):
            urls.append(url)
            if payload is None:
                return {"ledger_version": "42"}
            return {"supply": [{"vec": ["1683253542"]}], "decimals": [6], "name": ["Ondo US Dollar Yield"]}[payload["function"].split("::")[-1]]
        with mock.patch.object(check, "http_json", side_effect=fake):
            r = check.read_aptos_coin("https://x/v1", "0xcf::usdy::USDY")
        self.assertEqual((r["supply"], r["name"]), (Decimal("1683.253542"), "Ondo US Dollar Yield"))
        self.assertTrue(all("ledger_version=42" in u for u in urls[1:]))

    def test_sui_reads_supply_and_checkpoint_from_one_query(self):
        resp = {"data": {"checkpoint": {"sequenceNumber": 7}, "coinMetadata": {"decimals": 6, "name": "Ondo US Dollar Yield", "supply": "12122664157396"}}}
        with mock.patch.object(check, "http_json", return_value=resp) as m:
            r = check.read_sui("https://g/graphql", "0x96::usdy::USDY")
        self.assertEqual((r["supply"], r["at"]), (Decimal("12122664.157396"), "checkpoint 7"))
        self.assertEqual(m.call_count, 1)

    def test_cosmos_uses_the_largest_exponent(self):
        meta = {"metadata": {"name": "Ondo US Dollar Yield", "denom_units": [{"denom": "ausdy", "exponent": 0}, {"denom": "usdy", "exponent": 18}]}}
        supply = {"amount": {"denom": "ausdy", "amount": "62531369928814821270000"}}
        with mock.patch.object(check, "http_json", side_effect=[meta, supply]):
            r = check.read_cosmos("https://n", "ausdy")
        self.assertEqual(r["supply"], Decimal("62531.36992881482127"))


class StellarTest(unittest.TestCase):
    def test_supply_counts_every_auth_state_and_parked_amounts(self):
        asset = {"balances": {"authorized": "100.0000001", "authorized_to_maintain_liabilities": "1", "unauthorized": "25.0697887"},
                 "claimable_balances_amount": "2", "liquidity_pools_amount": "0", "contracts_amount": "3"}
        self.assertEqual(check.stellar_supply(asset), Decimal("131.0697888"))


class AbiTest(unittest.TestCase):
    def test_decodes_abi_string(self):
        word = lambda n: n.to_bytes(32, "big").hex()
        data = "0x" + word(32) + word(5) + b"BUIDL".hex().ljust(64, "0")
        self.assertEqual(check.abi_string(data), "BUIDL")


class ReportTest(unittest.TestCase):
    def test_report_totals_ignore_unreadable_rows(self):
        rows = [{"chain": "a", "class": "B", "supply": Decimal("3"), "status": "ok"}, {"chain": "b", "class": "B", "supply": None, "status": "unreadable"}]
        self.assertIn("| **Total** | | **3.00** | |", check.report("BUIDL", rows, "t"))

    def test_help_and_unknown_funds_answer_without_reading_any_chain(self):
        with mock.patch.object(check, "run_fund", side_effect=AssertionError("read a chain")):
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(check.main("--help"), 0)
            self.assertIn("Usage: python3 check.py", out.getvalue())
            with redirect_stderr(io.StringIO()) as err:
                self.assertEqual(check.main("NOPE"), 2)
            self.assertIn("unknown fund NOPE", err.getvalue())

if __name__ == "__main__":
    unittest.main()


class XrplNearTest(unittest.TestCase):
    def test_xrpl_reads_issuer_obligations_at_one_ledger(self):
        calls = []

        def rpc(_url, method, params):
            calls.append((method, params[0]["ledger_index"]))
            if method == "gateway_balances":
                return {"ledger_index": 7, "obligations": {"4F55534700000000000000000000000000000000": "1584220.17"}}
            return {"account_data": {"Domain": "6F6E646F2E66696E616E6365"}}
        with mock.patch.object(check, "jsonrpc", rpc):
            out = check.read_xrpl("https://x.io", "OUSG-rIssuer")
        self.assertEqual((out["name"], out["supply"]), ("OUSG (ondo.finance)", Decimal("1584220.17")))
        self.assertEqual(calls, [("gateway_balances", "validated"), ("account_info", 7)])

    def test_xrpl_three_letter_code_and_nothing_issued(self):
        self.assertEqual(check.xrpl_currency("USD"), "USD")
        with mock.patch.object(check, "jsonrpc", lambda _u, m, _p: {"ledger_index": 1} if m == "gateway_balances" else {"account_data": {}}):
            self.assertEqual(check.read_xrpl("https://x.io", "USD-rIssuer")["supply"], Decimal(0))

    def test_xrpl_mpt_scales_outstanding_and_names_from_metadata(self):
        meta = '{"t": "sgBENJI", "in": "Franklin Templeton Investments"}'.encode().hex().upper()
        seen = []

        def rpc(_url, method, params):
            seen.append((method, params[0]["ledger_index"]))
            return {"ledger_index": 9, "node": {"OutstandingAmount": "1946177954184529", "AssetScale": 7, "MPTokenMetadata": meta}}
        with mock.patch.object(check, "jsonrpc", rpc):
            out = check.read_xrpl_mpt("https://x.io", "064E", height=9)
        self.assertEqual(out["supply"], Decimal("194617795.4184529"))
        self.assertEqual(out["name"], "sgBENJI (Franklin Templeton Investments)")
        self.assertEqual(seen, [("ledger_entry", 9)])

    def test_xrpl_mpt_without_metadata_fails_the_name_check(self):
        with mock.patch.object(check, "jsonrpc", lambda *_: {"ledger_index": 1, "node": {"OutstandingAmount": "5"}}):
            out = check.read_xrpl_mpt("https://x.io", "064E")
        self.assertEqual((out["supply"], out["name"]), (Decimal(5), "? ()"))
        self.assertIn("xrpl_mpt", check.HEADS)

    def test_near_scales_by_decimals_and_pins_the_block(self):
        refs = []

        def rpc(_url, _method, params):
            refs.append(params.get("block_id", params.get("finality")))
            if params["method_name"] == "ft_total_supply":
                return {"result": list(b'"123456789"'), "block_height": 42}
            return {"result": list(b'{"name": "US Yield Coin", "decimals": 6}')}
        with mock.patch.object(check, "jsonrpc", rpc):
            out = check.read_near("https://n.io", "usyc.near")
        self.assertEqual((out["supply"], refs), (Decimal("123.456789"), ["final", 42]))


class KeyedProviderTest(unittest.TestCase):
    def test_tatum_gets_its_key_only_when_set(self):
        seen = []

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"{}"
        with mock.patch.object(check.urllib.request, "urlopen", lambda req, timeout: seen.append(dict(req.header_items())) or Resp()):
            with mock.patch.dict(check.os.environ, {"TATUM_API_KEY": "t-1"}):
                check.http_json("https://solana-mainnet.gateway.tatum.io", {"a": 1})
                check.http_json("https://api.mainnet-beta.solana.com", {"a": 1})
            with mock.patch.dict(check.os.environ, {}, clear=True):
                check.http_json("https://solana-mainnet.gateway.tatum.io", {"a": 1})
        self.assertEqual([h.get("X-api-key") for h in seen], ["t-1", None, None])
