# crypto-ai autotrade worker

Long-running FastAPI service that scans markets every 5 minutes, picks the
highest-confidence trade idea (`confidence × RR(TP1)`) and forwards it to
Bybit V5 via [pybit](https://github.com/bybit-exchange/pybit).

## Why a separate service?

- Bybit blocks Vercel IPs (US-East geofence).
- Vercel functions are stateless and capped at 10 s — useless for a
  scheduler that must hold positions and persist a DB.
- Fly.io fra (Frankfurt) is in the EU and free for one 256–512 MB machine.

## One-time setup

```bash
# from repo root
fly auth login
fly launch --config worker/fly.toml --copy-config --no-deploy --name crypto-ai-worker

# generate a Fernet master key once
MASTER_KEY=$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')

# secrets
fly -a crypto-ai-worker secrets set \
  AUTOTRADE_MASTER_KEY="$MASTER_KEY" \
  GROQ_API_KEY="$GROQ_API_KEY" \
  CORS_ORIGINS="https://crypto-ai-eta.vercel.app,http://localhost:8000"

# volume for SQLite
fly -a crypto-ai-worker volumes create autotrade_data --region fra --size 1

# deploy
fly -a crypto-ai-worker deploy --config worker/fly.toml
```

## Environment variables

All optional except `AUTOTRADE_MASTER_KEY` and `AUTOTRADE_ENABLED`.

| Var | Default | Meaning |
|---|---|---|
| `AUTOTRADE_ENABLED` | `0` | Set to `1` on the worker to mount routes |
| `AUTOTRADE_MASTER_KEY` | — | base64 Fernet key (32 bytes) |
| `AT_DB_PATH` | `/data/autotrade.db` | sqlite file (mounted volume) |
| `AT_SCAN_INTERVAL_SEC` | `300` | scheduler tick |
| `AT_MIN_CONFIDENCE` | `60` | skip ideas below |
| `AT_MIN_RR` | `1.3` | skip ideas with TP1 RR below |
| `AT_MAX_CONCURRENT` | `1` | open positions cap |
| `AT_LEVERAGE` | `3` | default leverage for perp |
| `AT_POSITION_PCT` | `2.0` | % of free margin risked per trade |
| `AT_DAILY_LOSS_PCT` | `5.0` | halt bot if daily loss ≥ this |
| `AT_WEEKLY_LOSS_PCT` | `12.0` | halt bot if weekly loss ≥ this |
| `AT_BREAKEVEN_AT_R` | `0.7` | move SL to entry once trade is in this R |

## Endpoints

```
GET    /healthz
GET    /config
GET    /autotrade/status
POST   /autotrade/keys              {api_key, api_secret, network}
DELETE /autotrade/keys?network=mainnet
POST   /autotrade/start             {network, instrument, leverage, position_pct}
POST   /autotrade/stop
POST   /autotrade/emergency
GET    /autotrade/history
GET    /autotrade/equity
```
