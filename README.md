# ties-model

Reads tokenized funds straight from every chain they live on, and checks the result against the fund's official record. Python standard library only, no keys for the supply reads.

Live output, every night: https://ties-onchain.github.io

| Fund | Chains | Official record |
|---|---|---|
| BlackRock BUIDL | Ethereum (BUIDL and BUIDL-I), Solana, Avalanche, BNB Chain, Aptos, Optimism, Tempo, Arbitrum, Polygon | none public (private fund) |
| Franklin BENJI | Stellar, Base, Arbitrum, Ethereum, Avalanche, Polygon, Aptos, Solana | SEC Form N-MFP3 (FOBXX), daily flows |
| Spiko EUTBL | Ethereum, Arbitrum, Base, Polygon, Etherlink, Starknet, Stellar | the prospectus names these 7 contracts as the share register |

Every address and where it came from is in `funds.json`.

## Run it

```
python3 check.py                      # supply of every fund on every chain, two providers each
python3 check.py BENJI                # one fund
python3 events.py capture BUIDL 7     # archive the last 7 days of mints and burns
python3 events.py closure BUIDL 2     # do the archived events explain each chain's supply change
SEC_USER_AGENT="you@example.com" python3 sec.py              # latest N-MFP3 filings
ETHERSCAN_API_KEY=... python3 events.py BENJI 2026-10       # a month of events vs the SEC daily flows
python3 -m unittest test_check test_sec test_events test_soroban
```

What a read looks like (BENJI, 10 Nov 2026, 21:30 UTC):

```
| Chain | Class | On-chain | Providers |
|---|---|---:|---|
| stellar | BENJI | 1,172,569,580.69 | ok |
| base | BENJI | 27,655,261.26 | ok |
| arbitrum | BENJI | 20,849,018.74 | ok |
| ethereum | BENJI | 16,085,674.70 | ok |
| aptos | BENJI | 14,783,332.51 | ok |
| avalanche | BENJI | 2,118,291.93 | ok |
| solana | BENJI | 511,342.20 | ok |
| polygon | BENJI | 5,408.70 | ok |
| **Total** | | **1,254,577,910.74** | |
```

And a closure check, one line per chain:

```
BUIDL 2026-11-05T21:30Z to 2026-11-06T21:30Z
  ethereum-i: ok supply -85,051,268.49, events -85,051,268.49, gap 0.00
  solana: ok supply -27,990,291.73, events -27,990,291.73, gap 0.00
```

Event readers on Ethereum, Arbitrum and Polygon use Etherscan's free API (`ETHERSCAN_API_KEY`); Aptos uses the Aptos Labs indexer (`APTOS_API_KEY`). Everything else is public RPC.

## How supply is read

| Chain | Read | Pinned to |
|---|---|---|
| EVM | `totalSupply()` | the finalized block |
| Solana | `getTokenSupply` | finalized commitment |
| Aptos | `fungible_asset::ConcurrentSupply` | one ledger version |
| Stellar (classic) | Horizon asset: trustlines, claimable balances, pools, contracts | Horizon's latest closed ledger |
| Stellar (Soroban) | `TotalSupply` from the contract's instance storage | one ledger |
| Starknet | `total_supply()` | one block |

A contract counts as confirmed only when two independent providers return the same figure to the cent. Anything else is reported as it is: `single provider`, `providers disagree`, `name mismatch`, `unreadable`.

## Event model

Every mint and burn becomes one event:

| Field | Meaning |
|---|---|
| `id` | permanent: `<chain>-<tx>` (plus `-n` when one tx holds several), Stellar `stellar-<operation id>` |
| `chain`, `class` | where it happened; `class` only when a second share class shares the chain |
| `at` | UTC time of the block |
| `kind` | `subscription`, `redemption`, `dividend`, `migration`, `unknown` |
| `side` | `mint` or `burn` |
| `amount`, `account`, `tx` | tokens, holder, transaction |

| Kind | Rule |
|---|---|
| dividend | a mint inside the fund's daily run: found on Stellar for BENJI (about 1,000 single mints a night), from batch transactions for BUIDL (one transaction minting to every holder) |
| subscription | any other mint on a day the run happened |
| redemption | a burn |
| migration | a burn on one chain and the same amount minted on another within 30 minutes, or the issuer's own label (Franklin's `InstantCXTransfer`) |
| unknown | touches supply but fits no rule: listed, never dropped |

The closure check is the test of the whole model: for each chain, the supply change between two reads must equal the archived mints minus burns between the same blocks.

## Known limits

- Free providers forget. Aptos keeps about 11 days, Soroban RPC 7, the free BNB Chain node about a week, so events are archived nightly. Aptos and Soroban readers refuse a day the provider has pruned. A plain EVM node answers a pruned range with no logs, not an error: the closure check is what catches that.
- BUIDL has no public daily record, so its events are classified but cannot be matched to an official figure.

## Add a fund or a chain

One entry in `funds.json`, a reader in `events.py` if the chain is new, a test. Pull requests welcome.

Apache-2.0
