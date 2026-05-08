"""On-chain market context (free, no API keys).

- Bitcoin: mempool.space — recommended fees, mempool size, hashrate, difficulty
  adjustment, latest block height.
- Ethereum: public JSON-RPC (Allnodes / Flashbots / Blast as fallback chain) —
  current gas price, percentile-based slow / standard / fast estimates derived
  from `eth_feeHistory`, base fee, latest block number.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)


# ---- Tiny TTL cache (independent of context.py to keep modules decoupled) --

class _TTLCache:
    def __init__(self) -> None:
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str, ttl: float) -> Any | None:
        rec = self._store.get(key)
        if rec is None:
            return None
        ts, value = rec
        if time.time() - ts > ttl:
            return None
        return value

    def set(self, key: str, value: Any) -> None:
        self._store[key] = (time.time(), value)


_cache = _TTLCache()


# ---- Bitcoin (mempool.space) -------------------------------------------

_MEMPOOL_BASE = "https://mempool.space/api"


def _btc_get(client: httpx.Client, path: str) -> Any:
    r = client.get(f"{_MEMPOOL_BASE}{path}")
    r.raise_for_status()
    ct = r.headers.get("content-type", "")
    return r.json() if "application/json" in ct else r.text.strip()


def fetch_btc_onchain() -> dict | None:
    cached = _cache.get("btc", ttl=180)  # 3 min
    if cached is not None:
        return cached
    try:
        with httpx.Client(timeout=8, headers={"User-Agent": "crypto-ai/1.0"}) as c:
            fees = _btc_get(c, "/v1/fees/recommended")
            mempool = _btc_get(c, "/mempool")
            hashrate = _btc_get(c, "/v1/mining/hashrate/3d")
            diff_adj = _btc_get(c, "/v1/difficulty-adjustment")
            tip = _btc_get(c, "/blocks/tip/height")
        try:
            block_height = int(tip)
        except (ValueError, TypeError):
            block_height = 0
        ph = None
        cur_hash = hashrate.get("currentHashrate") if isinstance(hashrate, dict) else None
        if isinstance(cur_hash, (int, float)) and cur_hash > 0:
            # mempool.space returns plain H/s — convert to EH/s (1 EH = 1e18 H)
            ph = round(float(cur_hash) / 1e18, 2)
        out = {
            "fees_sat_per_vb": {
                "fastest": int(fees.get("fastestFee", 0) or 0),
                "half_hour": int(fees.get("halfHourFee", 0) or 0),
                "hour": int(fees.get("hourFee", 0) or 0),
                "economy": int(fees.get("economyFee", 0) or 0),
                "minimum": int(fees.get("minimumFee", 0) or 0),
            },
            "mempool_count": int(mempool.get("count", 0) or 0),
            "mempool_vsize_mb": round(float(mempool.get("vsize", 0) or 0) / 1_000_000, 2),
            "mempool_total_fee_btc": round(float(mempool.get("total_fee", 0) or 0) / 1e8, 4),
            "block_height": block_height,
            "hashrate_eh": ph,
            "difficulty_progress_pct": round(float(diff_adj.get("progressPercent", 0) or 0), 2),
            "difficulty_change_pct": round(float(diff_adj.get("difficultyChange", 0) or 0), 2),
            "blocks_to_retarget": int(diff_adj.get("remainingBlocks", 0) or 0),
        }
        _cache.set("btc", out)
        return out
    except Exception as e:  # noqa: BLE001
        log.warning("BTC on-chain fetch failed: %s", e)
        return None


# ---- Ethereum (public JSON-RPC) ----------------------------------------

_ETH_RPC_URLS = (
    "https://ethereum.publicnode.com",
    "https://rpc.flashbots.net",
    "https://eth-mainnet.public.blastapi.io",
)


def _eth_rpc(method: str, params: list | None = None) -> Any:
    payload = {"jsonrpc": "2.0", "method": method, "params": params or [], "id": 1}
    last_err: Exception | None = None
    for url in _ETH_RPC_URLS:
        try:
            with httpx.Client(timeout=8) as c:
                r = c.post(url, json=payload)
                r.raise_for_status()
                data = r.json()
            if "error" in data:
                raise RuntimeError(data["error"].get("message", "rpc error"))
            return data["result"]
        except Exception as e:  # noqa: BLE001
            last_err = e
            continue
    if last_err:
        raise last_err
    raise RuntimeError("all ETH RPCs failed")


def _hex_to_int(h: str | int) -> int:
    if isinstance(h, int):
        return h
    return int(h, 16) if isinstance(h, str) and h.startswith("0x") else int(h)


def _wei_to_gwei(wei: int) -> float:
    return round(wei / 1e9, 2)


def fetch_eth_onchain() -> dict | None:
    cached = _cache.get("eth", ttl=120)  # 2 min
    if cached is not None:
        return cached
    try:
        block_hex = _eth_rpc("eth_blockNumber")
        gas_hex = _eth_rpc("eth_gasPrice")
        # last 10 blocks, percentiles for tip (priority fee)
        fh = _eth_rpc("eth_feeHistory", ["0xa", "latest", [25, 50, 75]])
        block = _hex_to_int(block_hex)
        gas_wei = _hex_to_int(gas_hex)
        base_fees = [_hex_to_int(b) for b in (fh.get("baseFeePerGas") or [])]
        # baseFeePerGas has length+1 vs reward; use the most recent entry
        latest_base = base_fees[-1] if base_fees else gas_wei
        rewards = fh.get("reward") or []
        if rewards:
            tips_p25 = sum(_hex_to_int(r[0]) for r in rewards) / len(rewards)
            tips_p50 = sum(_hex_to_int(r[1]) for r in rewards) / len(rewards)
            tips_p75 = sum(_hex_to_int(r[2]) for r in rewards) / len(rewards)
        else:
            tips_p25 = tips_p50 = tips_p75 = 1e9  # 1 gwei default
        gas_used_ratio = fh.get("gasUsedRatio") or []
        congestion = (
            round(sum(gas_used_ratio) / len(gas_used_ratio) * 100, 1)
            if gas_used_ratio
            else None
        )
        out = {
            "gas_gwei": {
                "slow": _wei_to_gwei(int(latest_base + tips_p25)),
                "standard": _wei_to_gwei(int(latest_base + tips_p50)),
                "fast": _wei_to_gwei(int(latest_base + tips_p75)),
            },
            "base_fee_gwei": _wei_to_gwei(latest_base),
            "current_gas_gwei": _wei_to_gwei(gas_wei),
            "block_number": block,
            "congestion_pct": congestion,
        }
        _cache.set("eth", out)
        return out
    except Exception as e:  # noqa: BLE001
        log.warning("ETH on-chain fetch failed: %s", e)
        return None


# ---- Public entry point ------------------------------------------------

def fetch_onchain(coin: str) -> dict | None:
    if coin == "BTC":
        return fetch_btc_onchain()
    if coin == "ETH":
        return fetch_eth_onchain()
    return None
