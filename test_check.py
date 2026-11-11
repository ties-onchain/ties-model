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
