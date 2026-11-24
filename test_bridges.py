import unittest
from decimal import Decimal as D

import bridges


class EscrowTest(unittest.TestCase):
    SUPPLIES = {"ethereum": D("1000"), "base": D("300"), "arbitrum": D("200")}

    def test_a_holder_sitting_on_other_chains_supply_is_flagged(self):
        flags = bridges.escrow_flags("ethereum", [("0xlock", D("500.4"), "")], self.SUPPLIES, D("1500"))
        self.assertEqual(flags[0]["matches"], ["arbitrum", "base"])
        self.assertIn("balance equals supply on arbitrum, base", flags[0]["why"])
        self.assertFalse(flags[0]["exact"])
        self.assertTrue(bridges.escrow_flags("ethereum", [("0xm", D("300.004"), "")], self.SUPPLIES, D("1500"))[0]["exact"])

    def test_ordinary_holders_and_small_ones_pass(self):
        holders = [("0xa", D("410"), ""), ("0xdust", D("2"), "SomeBridge")]
        self.assertEqual(bridges.escrow_flags("ethereum", holders, self.SUPPLIES, D("1500")), [])

    def test_a_bridge_named_contract_is_flagged_even_without_a_match(self):
        flags = bridges.escrow_flags("ethereum", [("0xb", D("77"), "OFTAdapter")], self.SUPPLIES, D("1500"))
        self.assertEqual(flags[0]["why"], "named OFTAdapter")

    def test_only_a_new_exact_match_alerts(self):
        f = {"chain": "ethereum", "holder": "0x1", "exact": True}
        old, same, more = {"funds": {"J": {"flags": [f]}}}, {"funds": {"J": {"flags": [f]}}}, {"funds": {"J": {"flags": [f, {**f, "holder": "0x2"}]}}}
        self.assertFalse(bridges.new_exact(old, same))
        self.assertEqual(bridges.new_exact(old, more), {("J", "ethereum", "0x2")})

    def test_fund_result_names_checked_and_skipped_chains(self):
        cfg = {"tokens": [{"chain": "ethereum", "kind": "evm"}, {"chain": "solana", "kind": "solana"}, {"chain": "base", "kind": "evm"}]}
        rows = [{"chain": "ethereum", "supply": "1000"}, {"chain": "solana", "supply": "300"}, {"chain": "base", "supply": "200"}]

        def down(_t):
            raise OSError("429")
        res = bridges.check_fund(cfg, rows, {("ethereum", "evm"): lambda _t: [("0xa", D("10"), "")], ("solana", "solana"): down})
        self.assertEqual(res["checked"], ["ethereum"])
        self.assertEqual(res["skipped"], ["solana: 429"])
        self.assertEqual(res["flags"], [])


if __name__ == "__main__":
    unittest.main()
