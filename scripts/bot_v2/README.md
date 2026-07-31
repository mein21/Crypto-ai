# bot_v2 — Bybit Scalping Bot для PythonAnywhere

Переписан с нуля по итогам ревью первой версии. Все 28 проблем из `prod-smoke-test.md` (review) закрыты:

- Секреты в env-vars (`os.environ` + `python-dotenv`), не в коде.
- `qtyStep` / `tickSize` / `minOrderQty` тянутся из `instruments-info`, не хардкод.
- Дневной убыток (`daily_pnl_usdt`) реально считается из `get_closed_pnl` за период с прошлого запуска. Лимит работает.
- Сигнал смотрит на **закрытую** свечу (`closes[-2]`), не на формирующуюся.
- Стопы по ATR, не по фиксированному %.
- Спред-фильтр через `tickers` (bid/ask).
- One-Way mode форсится на старте; код плеча умеет различать «не изменено» (110043) от ошибки.
- `pybit.unified_trading.HTTP` — за нас делает signing, retry-on-network.
- `STATE_FILE` через абсолютный путь от `__file__` (cron-safe).
- `RotatingFileHandler` для логов (1 MB × 5 файлов).
- `try/finally` в `run_bot` — state сохраняется даже при early return или crash.
- Telegram c retry / backoff, не блокирует торговлю при сбое.
- Timezone настраиваемая (для дневного reset).

## Установка на PythonAnywhere

1. **Залогинься** на `pythonanywhere.com` (Hacker $5/мес или выше — нужен cron каждые 15 мин; на free доступен только daily cron).
2. **Bash console** → склонь репо или просто загрузи файлы из `scripts/bot_v2/`:
   ```bash
   mkdir -p ~/bot_v2 && cd ~/bot_v2
   # Загрузи bot_v2.py, .env.example, requirements.txt через Files-вкладку
   cp .env.example .env
   chmod 600 .env
   ```
3. **Заполни `.env`** через Files → Edit:
   - `BYBIT_API_KEY` / `BYBIT_API_SECRET` — testnet ключи для первого запуска (`testnet.bybit.com → Account → API Management`).
   - `TG_TOKEN` / `TG_CHAT_ID` — Telegram bot.
   - `BOT_TESTNET=true` — оставь так пока не убедишься что сигналы и ордера работают.
4. **Установи зависимости** (для своей версии Python — на PA по умолчанию это 3.10+):
   ```bash
   pip3.10 install --user -r requirements.txt
   ```
5. **Прогон вручную**:
   ```bash
   python3.10 bot_v2.py
   ```
   В Telegram должно прийти уведомление либо «нет сигнала» в логе.
6. **Cron task**: PA Dashboard → **Tasks** → Schedule a task → Hourly (PA не даёт «каждые 15 мин» прямо, поэтому делаешь 4 hourly task’а на :00 / :15 / :30 / :45 либо подписываешься на план Web Dev / выше где есть `every minute`):
   ```
   cd /home/ТВОЙ_ЮЗЕР/bot_v2 && python3.10 bot_v2.py
   ```
7. **Проверь логи**: PA → Tasks → View output → последний запуск. Локально лог: `~/bot_v2/bot.log`.

## Перевод на mainnet

После 1–2 недель тестов на тестнете:
1. Создай **mainnet API ключи** (Read + Trade, **без** Withdraw). IP-whitelist на IP PythonAnywhere.
2. В `.env`:
   ```
   BYBIT_API_KEY=<новый mainnet>
   BYBIT_API_SECRET=<новый mainnet>
   BOT_TESTNET=false
   BOT_MARGIN_USDT=10        # начни с минимума
   BOT_MAX_DAILY_LOSS_USDT=3.0  # пока не уверен в стратегии
   ```
3. Первый запуск с `BOT_DRY_RUN=true` — ордера не отправляются, но в логах видно как считаются qty/TP/SL/спред.

## Локальные unit-тесты

```bash
cd scripts/bot_v2
pip install -r requirements.txt pytest
pytest tests/
```

## Сигнал

Сигнал на закрытии 15m свечи:
- **LONG**: `close > EMA20`, предыдущая свеча красная (откат), текущая зелёная и выше предыдущей, объём > 1.5× среднего за 20 баров.
- **SHORT**: зеркально.
- TP/SL: ATR(14) × `BOT_TP_ATR_MULT` / `BOT_SL_ATR_MULT`. По умолчанию RR ≈ 1.5.
- Размер: `qty = (margin × leverage) / entry`, округлено к `qtyStep`. Если результат < `minOrderQty` — пропуск + предупреждение в Telegram.

## Известные ограничения

- Только USDT-перпетуалы (`linear`).
- Только One-Way mode (Hedge не поддерживается, форсится `mode=0` на старте).
- Cron-периодичность должна быть ≤ интервал свечи. Для 15m → бот должен запускаться ≥ 1 раз каждые 15 мин.
- Нет trailing stop / break-even — TP/SL фиксированные на бирже после открытия.
- Не учитывает funding rate (для скальпинга на 15m обычно не успеет проявиться).

## Если что-то идёт не так

- **`Missing required env var`** — не загрузился `.env`. Проверь что файл лежит рядом с `bot_v2.py` и `python-dotenv` установлен.
- **`set_leverage failed`** — у тебя hedge-mode либо превышен лимит маржи. Проверь Bybit Account → Position Mode.
- **`size < minQty`** — увеличь `BOT_MARGIN_USDT` или `BOT_LEVERAGE`. Для BTCUSDT mainnet `minQty=0.001` ≈ $80 нотионал.
- **`tickSize` round error** — если ордер отвергает биржа c кодом 10001/170140 — pybit вернёт исключение, поправь в .env символ или жди обновления `instruments-info`.
- **Telegram молчит** — `TG_CHAT_ID` пустой / неправильный. Проверь через `https://api.telegram.org/bot<TOKEN>/getMe`.
