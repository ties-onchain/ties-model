"""Other public numbers for the same fund, to show next to the chains. Today: CoinGecko (free public API, credited on the page).
ponytail: DefiLlama's RWA supply is not in its free API (rwa/stats tvlUsd is null), add it when it is."""
import json
import urllib.request

COINGECKO = "https://api.coingecko.com/api/v3/coins/{}?localization=false&tickers=false&community_data=false&developer_data=false"


def parse_coingecko(data):
    m = data.get("market_data") or {}
    return {"circulating": m.get("circulating_supply"), "total": m.get("total_supply"),
            "price_usd": (m.get("current_price") or {}).get("usd"), "updated": data.get("last_updated"),
            "symbol": data.get("symbol"), "platforms": {k: v for k, v in (data.get("platforms") or {}).items() if v}}


def coingecko(coin_id):
    """CoinGecko's supply for one coin. A dead source must not cost the night's on-chain read, so errors come back as data."""
    try:
        req = urllib.request.Request(COINGECKO.format(coin_id), headers={"User-Agent": "ties-engine/0.1"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return {"id": coin_id, **parse_coingecko(json.loads(resp.read()))}
    except Exception as exc:  # noqa: BLE001
        return {"id": coin_id, "error": str(exc)}
