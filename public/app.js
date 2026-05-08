(() => {
  const API_BASE = (window.CRYPTO_AI_API || "").replace(/\/$/, "") || inferDevApiBase();
  const API_AUTH = window.CRYPTO_AI_AUTH || ""; // "Basic xxxx" or empty

  function withAuth(init) {
    const opts = init ? { ...init } : {};
    const headers = new Headers(opts.headers || {});
    if (API_AUTH) headers.set("Authorization", API_AUTH);
    opts.headers = headers;
    return opts;
  }

  const COINS = ["BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "DOGE", "AVAX", "TON", "DOT"];
  const TFS = ["15m", "1h", "4h", "1d", "1w"];

  let state = { coin: "BTC", tf: "1h", loading: false };
  let watches = loadWatches();
  let pollTimer = null;
  const POLL_INTERVAL = 10000;

  const $ = (id) => document.getElementById(id);

  function inferDevApiBase() {
    if (typeof window === "undefined") return "";
    const host = window.location.hostname;
    if (host === "localhost" || host === "127.0.0.1") return "http://localhost:8000";
    // Same-origin: build absolute URL from origin (without any embedded credentials,
    // which would otherwise be inherited from window.location and blocked by fetch).
    return `${window.location.protocol}//${window.location.host}`;
  }

  function setStatus(kind, text) {
    $("status-dot").className = "dot " + (kind || "");
    $("status-text").textContent = text;
  }

  function buildChips(rowId, items, key) {
    const row = $(rowId);
    row.innerHTML = "";
    items.forEach((it) => {
      const b = document.createElement("button");
      b.className = "chip" + (state[key] === it ? " active" : "");
      b.textContent = it;
      b.dataset.value = it;
      b.addEventListener("click", () => {
        state[key] = it;
        row.querySelectorAll(".chip").forEach((c) =>
          c.classList.toggle("active", c.dataset.value === it),
        );
      });
      row.appendChild(b);
    });
  }

  async function checkHealth() {
    try {
      const r = await fetch(`${API_BASE}/healthz`, withAuth({ mode: "cors" }));
      if (!r.ok) throw new Error("status " + r.status);
      const j = await r.json();
      const llm = j.llm || {};
      const provider = llm.groq ? "Groq" : llm.gemini ? "Gemini" : "Rules-based";
      setStatus("ok", `Сервер на связи · ИИ: ${provider}`);
    } catch (e) {
      setStatus("err", `Сервер недоступен: ${e.message}`);
    }
  }

  function fngColor(value) {
    if (value <= 24) return "var(--red)";
    if (value <= 44) return "#ef9a4a";
    if (value <= 55) return "var(--yellow)";
    if (value <= 74) return "#9ccc65";
    return "var(--green)";
  }

  function ruFngLabel(en) {
    const map = {
      "extreme fear": "экстремальный страх",
      "fear": "страх",
      "neutral": "нейтрально",
      "greed": "жадность",
      "extreme greed": "экстремальная жадность",
    };
    return map[(en || "").toLowerCase()] || en || "";
  }

  function corrColor(v) {
    // Monochrome heatmap matching the aurora palette: stronger |v| → brighter
    // white tint; sign is reflected by hue (positive → neutral white, negative
    // → very subtle warm tint). Works in both light and dark themes via
    // alpha-on-current background.
    const isLight = document.documentElement.getAttribute("data-theme") === "light";
    const mag = Math.min(1, Math.abs(v));
    const baseAlpha = 0.04 + 0.22 * mag;
    if (isLight) {
      // Black ink on white
      return v >= 0
        ? `rgba(0, 0, 0, ${baseAlpha})`
        : `rgba(120, 60, 60, ${baseAlpha + 0.02})`;
    }
    // White ink on black
    return v >= 0
      ? `rgba(255, 255, 255, ${baseAlpha})`
      : `rgba(255, 200, 200, ${baseAlpha + 0.02})`;
  }

  function renderFearGreed(fng) {
    if (!fng) return false;
    $("fng-value").textContent = fng.value;
    const ruLabel = ruFngLabel(fng.classification);
    $("fng-label").textContent = ruLabel;
    $("fng-value").style.color = fngColor(fng.value);
    const fill = $("fng-bar-fill");
    fill.style.width = `${Math.max(2, fng.value)}%`;
    fill.style.background = fngColor(fng.value);
    return true;
  }

  function renderCorrelation(corr) {
    if (!corr || !corr.pairs || !corr.pairs.length) return false;
    const tbl = $("corr-table");
    tbl.innerHTML = "";
    const thead = document.createElement("thead");
    const headerRow = document.createElement("tr");
    ["Монета", "vs BTC"].forEach((t) => {
      const th = document.createElement("th");
      th.textContent = t;
      headerRow.appendChild(th);
    });
    thead.appendChild(headerRow);
    tbl.appendChild(thead);
    const tbody = document.createElement("tbody");
    corr.pairs.forEach((p) => {
      const tr = document.createElement("tr");
      const th = document.createElement("th");
      th.textContent = p.coin;
      tr.appendChild(th);
      const td = document.createElement("td");
      td.textContent = p.value.toFixed(3);
      td.style.background = corrColor(p.value);
      tr.appendChild(td);
      tbody.appendChild(tr);
    });
    tbl.appendChild(tbody);
    $("corr-meta").textContent = `${corr.pairs.length} монет · окно ${corr.window_days} дней · ${corr.n_observations} наблюдений`;
    return true;
  }

  async function loadContext() {
    try {
      const r = await fetch(`${API_BASE}/context`, withAuth({ mode: "cors" }));
      if (!r.ok) return;
      const j = await r.json();
      const fngOk = renderFearGreed(j.fear_greed);
      const corrOk = renderCorrelation(j.correlation);
      if (fngOk || corrOk) $("market-context").hidden = false;
    } catch (e) {
      // non-fatal
    }
  }

  function showError(msg) {
    const box = $("error-box");
    box.hidden = false;
    box.textContent = msg;
  }
  function clearError() {
    $("error-box").hidden = true;
  }

  function fmtRelativeTs(ts) {
    if (!ts) return "";
    const ms = ts * 1000;
    const diff = Date.now() - ms;
    if (diff < 0) return "только что";
    const m = Math.round(diff / 60000);
    if (m < 60) return `${m} мин назад`;
    const h = Math.round(diff / 3600000);
    if (h < 24) return `${h} ч назад`;
    const d = Math.round(diff / 86400000);
    return `${d} дн назад`;
  }

  function patternBiasIcon(bias) {
    if (bias === "bullish") return "▲";
    if (bias === "bearish") return "▼";
    return "●";
  }

  function patternBiasLabel(bias) {
    if (bias === "bullish") return "bullish";
    if (bias === "bearish") return "bearish";
    return "neutral";
  }

  function renderPatterns(list) {
    const ul = $("patterns-list");
    ul.innerHTML = "";
    if (!list || !list.length) {
      const li = document.createElement("li");
      li.className = "pattern-empty muted";
      li.textContent = "Свежих паттернов не обнаружено";
      ul.appendChild(li);
      return;
    }
    list.slice(0, 6).forEach((p) => {
      const li = document.createElement("li");
      li.className = "pattern-row " + patternBiasLabel(p.bias);
      const stars = "★".repeat(p.strength || 1) + "☆".repeat(Math.max(0, 3 - (p.strength || 1)));
      const when = fmtRelativeTs(p.ts);
      li.innerHTML = `
        <div class="pattern-head">
          <span class="pattern-icon">${patternBiasIcon(p.bias)}</span>
          <span class="pattern-name">${p.name_ru || p.name || ""}</span>
          <span class="pattern-strength" title="Сила паттерна">${stars}</span>
        </div>
        <div class="pattern-meta">
          <span class="pattern-bias">${patternBiasLabel(p.bias)}</span>
          ${when ? `<span class="pattern-when">· ${when}</span>` : ""}
        </div>
        <div class="pattern-context">${p.context || ""}</div>
      `;
      ul.appendChild(li);
    });
  }

  function fmtPrice(v) {
    if (v == null || isNaN(v)) return "—";
    const abs = Math.abs(v);
    if (abs >= 1000) return v.toLocaleString("ru-RU", { maximumFractionDigits: 0 });
    if (abs >= 1) return v.toFixed(2);
    return v.toFixed(6);
  }

  function trendBadgeClass(trend) {
    if ((trend || "").includes("восход")) return "long";
    if ((trend || "").includes("нисход")) return "short";
    return "flat";
  }

  function renderHtfStrip(htf) {
    const strip = $("htf-strip");
    strip.innerHTML = "";
    if (!htf || !htf.length) {
      strip.hidden = true;
      return;
    }
    htf.forEach((h) => {
      const tile = document.createElement("div");
      tile.className = "htf-tile " + trendBadgeClass(h.trend);
      const sign = h.change_pct_30bars >= 0 ? "+" : "";
      tile.innerHTML = `
        <div class="htf-tf">${h.tf}</div>
        <div class="htf-trend">${h.trend}</div>
        <div class="htf-meta">RSI ${h.rsi} · MACD ${h.macd_state} · ${sign}${h.change_pct_30bars}%</div>
      `;
      strip.appendChild(tile);
    });
    strip.hidden = false;
  }

  const IMPACT_LABEL = { high: "высокое", medium: "среднее", low: "низкое" };
  const TREND_LABEL = { bullish: "бычий", bearish: "медвежий", neutral: "нейтр." };
  const TREND_ARROW = { bullish: "▲", bearish: "▼", neutral: "—" };

  function escapeHtml(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function renderNews(news) {
    const ul = $("news-list");
    ul.innerHTML = "";
    if (!news || !news.length) {
      ul.innerHTML = `<li class="news-empty">Свежих новостей не найдено</li>`;
      return;
    }
    news.forEach((n) => {
      const li = document.createElement("li");
      li.className = "news-item";
      const date = n.ts
        ? new Date(n.ts * 1000).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" })
        : "";
      const ru = (n.title_ru || "").trim();
      const orig = (n.title || "").trim();
      const titleMain = ru || orig;
      const showOrig = ru && orig && ru !== orig;
      const initial = (state.coin || "?").slice(0, 1);
      const thumb = n.image
        ? `<img src="${escapeHtml(n.image)}" alt="" loading="lazy" referrerpolicy="no-referrer" />`
        : `<span class="news-thumb-fallback">${escapeHtml(initial)}</span>`;
      const badges = [];
      if (n.impact) {
        badges.push(
          `<span class="news-badge impact-${escapeHtml(n.impact)}">
             <span class="badge-key">влияние</span> ${escapeHtml(IMPACT_LABEL[n.impact] || n.impact)}
           </span>`,
        );
      }
      if (n.sentiment) {
        badges.push(
          `<span class="news-badge trend-${escapeHtml(n.sentiment)}">
             <span class="arrow">${TREND_ARROW[n.sentiment] || ""}</span>
             ${escapeHtml(TREND_LABEL[n.sentiment] || n.sentiment)}
           </span>`,
        );
      }
      li.innerHTML = `
        <a class="news-link" href="${escapeHtml(n.url)}" target="_blank" rel="noreferrer noopener">
          <div class="news-thumb">${thumb}</div>
          <div class="news-body">
            <div class="news-title">${escapeHtml(titleMain)}</div>
            ${showOrig ? `<div class="news-orig">${escapeHtml(orig)}</div>` : ""}
            <div class="news-meta">${escapeHtml(n.source || "")}${date ? " · " + escapeHtml(date) : ""}</div>
          </div>
          ${badges.length ? `<div class="news-badges">${badges.join("")}</div>` : ""}
        </a>
      `;
      ul.appendChild(li);
    });
  }

  function congestionLevel(pct) {
    if (pct == null) return { tone: "muted", label: "—" };
    if (pct >= 80) return { tone: "short", label: "перегружено" };
    if (pct >= 50) return { tone: "warn", label: "повышенная" };
    return { tone: "long", label: "спокойно" };
  }

  function gasLevel(gwei) {
    if (gwei == null) return { tone: "muted", label: "—" };
    if (gwei >= 50) return { tone: "short", label: "высокий" };
    if (gwei >= 15) return { tone: "warn", label: "средний" };
    return { tone: "long", label: "низкий" };
  }

  function btcFeeLevel(satvb) {
    if (satvb == null) return { tone: "muted", label: "—" };
    if (satvb >= 50) return { tone: "short", label: "высокие" };
    if (satvb >= 15) return { tone: "warn", label: "средние" };
    return { tone: "long", label: "спокойные" };
  }

  function renderOnchain(coin, onchain) {
    const card = $("onchain-card");
    const grid = $("onchain-grid");
    grid.innerHTML = "";
    if (!onchain) {
      card.hidden = true;
      return;
    }
    const tile = (label, value, sub, tone) => {
      const el = document.createElement("div");
      el.className = "onchain-tile " + (tone || "");
      el.innerHTML = `
        <div class="onchain-label">${label}</div>
        <div class="onchain-value">${value}</div>
        <div class="onchain-sub">${sub || ""}</div>
      `;
      grid.appendChild(el);
    };
    if (coin === "BTC" && onchain.btc) {
      const b = onchain.btc;
      $("onchain-title").textContent = "Он-чейн · Bitcoin";
      $("onchain-source").textContent = "Источник: mempool.space · обновляется каждые 3 мин";
      const fee = b.fees_sat_per_vb || {};
      const lvl = btcFeeLevel(fee.fastest);
      tile("Комиссии (sat/vB)", `${fee.fastest}/${fee.half_hour}/${fee.hour}`, `fastest · 30 min · 1 h · ${lvl.label}`, lvl.tone);
      tile("Мемпул", `${b.mempool_count.toLocaleString("ru-RU")} tx`, `${b.mempool_vsize_mb} MB · ${b.mempool_total_fee_btc} BTC fee`, "");
      tile("Хэшрейт", b.hashrate_eh != null ? `${b.hashrate_eh} EH/s` : "—", "среднее за 3 дня", "long");
      const sign = b.difficulty_change_pct >= 0 ? "+" : "";
      tile("Сложность", `${b.difficulty_progress_pct}%`, `до ретаргета ${b.blocks_to_retarget} блоков · ${sign}${b.difficulty_change_pct}%`, b.difficulty_change_pct >= 0 ? "long" : "short");
      tile("Высота блока", b.block_height.toLocaleString("ru-RU"), "последний блок BTC", "");
      card.hidden = false;
      return;
    }
    if (coin === "ETH" && onchain.eth) {
      const e = onchain.eth;
      $("onchain-title").textContent = "Он-чейн · Ethereum";
      $("onchain-source").textContent = "Источник: публичный JSON-RPC · обновляется каждые 2 мин";
      const gas = e.gas_gwei || {};
      const gl = gasLevel(gas.standard);
      tile("Газ (gwei)", `${gas.slow} / ${gas.standard} / ${gas.fast}`, `slow · standard · fast · ${gl.label}`, gl.tone);
      tile("Base fee", `${e.base_fee_gwei} gwei`, "EIP-1559 базовая ставка", "");
      const cl = congestionLevel(e.congestion_pct);
      tile("Загрузка блоков", e.congestion_pct != null ? `${e.congestion_pct}%` : "—", `средняя за 10 блоков · ${cl.label}`, cl.tone);
      tile("Высота блока", e.block_number.toLocaleString("ru-RU"), "последний блок ETH", "");
      card.hidden = false;
      return;
    }
    card.hidden = true;
  }

  function renderResult(resp) {
    const { analysis, chart_png_b64, indicators, last_price, htf_trends, news, fear_greed, onchain } = resp;
    $("result").hidden = false;
    renderHtfStrip(htf_trends);
    renderNews(news);
    renderOnchain(analysis.coin, onchain);
    if (fear_greed) {
      renderFearGreed(fear_greed);
      $("market-context").hidden = false;
    }

    const dataUrl = `data:image/png;base64,${chart_png_b64}`;
    $("chart-img").src = dataUrl;
    const dl = $("chart-download");
    dl.href = dataUrl;
    dl.download = `${analysis.coin}_${analysis.timeframe}_${Date.now()}.png`;

    $("result-title").textContent = `${analysis.coin}/USDT · ${analysis.timeframe}`;
    $("result-subtitle").textContent = `Цена: ${fmtPrice(last_price)} · Тренд: ${analysis.trend} · Режим: ${analysis.market_regime || "—"}`;

    const sig = analysis.signal || {};
    const dirText =
      sig.direction === "long" ? "ЛОНГ" : sig.direction === "short" ? "ШОРТ" : "ВНЕ ПОЗИЦИИ";
    const badge = $("signal-badge");
    badge.className = "signal-badge " + (sig.direction || "flat");
    badge.textContent = `${dirText} · ${sig.confidence ?? 0}%`;

    const idea = $("trade-idea");
    idea.innerHTML = "";
    const rr = (target) => {
      if (
        sig.entry == null ||
        sig.stop_loss == null ||
        target == null ||
        sig.entry === sig.stop_loss
      )
        return null;
      const sign = sig.direction === "short" ? -1 : 1;
      return ((target - sig.entry) / Math.abs(sig.entry - sig.stop_loss)) * sign;
    };
    const rrText = (target) => {
      const v = rr(target);
      return v == null ? "" : ` · RR ${v.toFixed(2)}`;
    };
    const rows = [
      ["Направление", dirText, sig.direction || "flat"],
      ["Вход", fmtPrice(sig.entry), "warn"],
      ["Stop-loss", fmtPrice(sig.stop_loss), "short"],
      ["Take-profit 1", `${fmtPrice(sig.take_profit_1)}${rrText(sig.take_profit_1)}`, "long"],
      ["Take-profit 2", `${fmtPrice(sig.take_profit_2)}${rrText(sig.take_profit_2)}`, "long"],
      ["Уверенность", `${sig.confidence ?? 0}%`, ""],
    ];
    rows.forEach(([k, v, cls]) => {
      const kEl = document.createElement("div");
      kEl.className = "k";
      kEl.textContent = k;
      const vEl = document.createElement("div");
      vEl.className = "v " + (cls || "");
      vEl.textContent = v;
      idea.appendChild(kEl);
      idea.appendChild(vEl);
    });

    $("rationale").textContent = sig.rationale || "";

    const existingTrackBtn = idea.parentElement.querySelector(".track-btn");
    if (existingTrackBtn) existingTrackBtn.remove();
    if (sig.direction && sig.direction !== "flat" && sig.entry != null && sig.stop_loss != null) {
      const trackBtn = document.createElement("button");
      trackBtn.className = "track-btn";
      trackBtn.textContent = "\uD83D\uDCCC \u041E\u0442\u0441\u043B\u0435\u0436\u0438\u0432\u0430\u0442\u044C";
      const watchData = {
        coin: analysis.coin,
        timeframe: analysis.timeframe || state.tf,
        direction: sig.direction,
        entry: sig.entry,
        stop_loss: sig.stop_loss,
        take_profit_1: sig.take_profit_1,
        take_profit_2: sig.take_profit_2,
      };
      const alreadyTracked = watches.some(
        (w) => w.coin === watchData.coin && w.timeframe === watchData.timeframe && w.entry === watchData.entry
      );
      if (alreadyTracked) {
        trackBtn.classList.add("tracking");
        trackBtn.textContent = "\u2705 \u041E\u0442\u0441\u043B\u0435\u0436\u0438\u0432\u0430\u0435\u0442\u0441\u044F";
      }
      trackBtn.addEventListener("click", () => {
        addWatch(watchData);
        trackBtn.classList.add("tracking");
        trackBtn.textContent = "\u2705 \u041E\u0442\u0441\u043B\u0435\u0436\u0438\u0432\u0430\u0435\u0442\u0441\u044F";
      });
      idea.parentElement.appendChild(trackBtn);
    }

    const ind = $("indicators-kv");
    ind.innerHTML = "";
    const indicatorsObj = analysis.indicators_summary || {};
    const indLabels = { rsi: "RSI", macd: "MACD", ema: "EMA", bollinger: "Bollinger" };
    Object.entries(indicatorsObj).forEach(([k, v]) => {
      const kEl = document.createElement("div");
      kEl.className = "k";
      kEl.textContent = indLabels[k] || k;
      const vEl = document.createElement("div");
      vEl.className = "v";
      vEl.textContent = v;
      ind.appendChild(kEl);
      ind.appendChild(vEl);
    });
    if (indicators) {
      const extras = [
        ["ATR", fmtPrice(indicators.atr)],
        ["BB верх", fmtPrice(indicators.bb_upper)],
        ["BB низ", fmtPrice(indicators.bb_lower)],
      ];
      extras.forEach(([k, v]) => {
        const kEl = document.createElement("div");
        kEl.className = "k";
        kEl.textContent = k;
        const vEl = document.createElement("div");
        vEl.className = "v";
        vEl.textContent = v;
        ind.appendChild(kEl);
        ind.appendChild(vEl);
      });
    }

    const lvl = $("levels-list");
    lvl.innerHTML = "";
    (analysis.key_levels?.resistance || []).forEach((p) => {
      const r = document.createElement("div");
      r.className = "level-row resistance";
      r.innerHTML = `<span>Сопротивление</span><span>${fmtPrice(p)}</span>`;
      lvl.appendChild(r);
    });
    (analysis.key_levels?.support || []).forEach((p) => {
      const r = document.createElement("div");
      r.className = "level-row support";
      r.innerHTML = `<span>Поддержка</span><span>${fmtPrice(p)}</span>`;
      lvl.appendChild(r);
    });
    if (!lvl.children.length) {
      lvl.innerHTML = `<p class="muted">Уровни не определены</p>`;
    }

    renderPatterns(analysis.patterns || []);

    $("narrative").textContent = analysis.narrative || "";
    const risks = $("risks");
    risks.innerHTML = "";
    (analysis.risks || []).forEach((r) => {
      const li = document.createElement("li");
      li.textContent = r;
      risks.appendChild(li);
    });

    $("disclaimer").textContent =
      analysis.disclaimer || "Это не финансовый совет. Крипторынок крайне волатилен.";

    window.scrollTo({ top: $("result").offsetTop - 20, behavior: "smooth" });
  }

  function renderBestDealResult(resp) {
    const section = $("best-deal-result");
    section.hidden = false;
    const best = resp.best;
    if (!best) {
      $("best-deal-main").innerHTML = `<p class="muted">Не удалось найти подходящую сделку</p>`;
      $("best-deal-badge").className = "signal-badge flat";
      $("best-deal-badge").textContent = "—";
      $("best-deal-rationale").textContent = "";
      $("best-deal-footer").textContent = `Просканировано монет: ${resp.scanned}`;
      $("best-deal-runners").innerHTML = "";
      return;
    }

    const dirText =
      best.direction === "long" ? "ЛОНГ" : best.direction === "short" ? "ШОРТ" : "ВНЕ ПОЗИЦИИ";
    const badge = $("best-deal-badge");
    badge.className = "signal-badge " + (best.direction || "flat");
    badge.textContent = `${dirText} · ${best.confidence}%`;

    const main = $("best-deal-main");
    main.innerHTML = "";
    const rows = [
      ["Монета", `${best.coin}/USDT`, ""],
      ["Цена", fmtPrice(best.last_price), ""],
      ["Тренд", best.trend, ""],
      ["Направление", dirText, best.direction || "flat"],
      ["Вход", fmtPrice(best.entry), "warn"],
      ["Stop-loss", fmtPrice(best.stop_loss), "short"],
      ["Take-profit 1", fmtPrice(best.take_profit_1), "long"],
      ["Take-profit 2", fmtPrice(best.take_profit_2), "long"],
      ["RSI", best.rsi.toFixed(1), ""],
      ["Уверенность", `${best.confidence}%`, ""],
    ];
    rows.forEach(([k, v, cls]) => {
      const kEl = document.createElement("div");
      kEl.className = "k";
      kEl.textContent = k;
      const vEl = document.createElement("div");
      vEl.className = "v " + (cls || "");
      vEl.textContent = v;
      main.appendChild(kEl);
      main.appendChild(vEl);
    });

    $("best-deal-rationale").textContent = best.rationale || "";

    const existingBdTrack = main.parentElement.querySelector(".track-btn");
    if (existingBdTrack) existingBdTrack.remove();
    if (best.direction && best.direction !== "flat" && best.entry != null && best.stop_loss != null) {
      const bdTrackBtn = document.createElement("button");
      bdTrackBtn.className = "track-btn";
      bdTrackBtn.textContent = "\uD83D\uDCCC \u041E\u0442\u0441\u043B\u0435\u0436\u0438\u0432\u0430\u0442\u044C";
      const bdWatchData = {
        coin: best.coin,
        timeframe: best.timeframe,
        direction: best.direction,
        entry: best.entry,
        stop_loss: best.stop_loss,
        take_profit_1: best.take_profit_1,
        take_profit_2: best.take_profit_2,
      };
      const bdAlreadyTracked = watches.some(
        (w) => w.coin === bdWatchData.coin && w.timeframe === bdWatchData.timeframe && w.entry === bdWatchData.entry
      );
      if (bdAlreadyTracked) {
        bdTrackBtn.classList.add("tracking");
        bdTrackBtn.textContent = "\u2705 \u041E\u0442\u0441\u043B\u0435\u0436\u0438\u0432\u0430\u0435\u0442\u0441\u044F";
      }
      bdTrackBtn.addEventListener("click", () => {
        addWatch(bdWatchData);
        bdTrackBtn.classList.add("tracking");
        bdTrackBtn.textContent = "\u2705 \u041E\u0442\u0441\u043B\u0435\u0436\u0438\u0432\u0430\u0435\u0442\u0441\u044F";
      });
      main.parentElement.appendChild(bdTrackBtn);
    }
    $("best-deal-footer").textContent = `Просканировано монет: ${resp.scanned} · Таймфрейм: ${best.timeframe}`;

    const runnersEl = $("best-deal-runners");
    runnersEl.innerHTML = "";
    (resp.all_deals || []).forEach((deal) => {
      const row = document.createElement("div");
      row.className = "runner-row " + (deal.direction || "flat");
      const dir =
        deal.direction === "long" ? "ЛОНГ" : deal.direction === "short" ? "ШОРТ" : "ФЛЭТ";
      row.innerHTML = `
        <span class="runner-coin">${deal.coin}</span>
        <span class="runner-dir">${dir}</span>
        <span class="runner-conf">${deal.confidence}%</span>
        <span class="runner-price">${fmtPrice(deal.last_price)}</span>
      `;
      row.addEventListener("click", () => {
        state.coin = deal.coin;
        buildChips("coin-row", COINS, "coin");
        analyze();
      });
      runnersEl.appendChild(row);
    });

    if (resp.full_analysis) {
      renderResult(resp.full_analysis);
    }

    window.scrollTo({ top: section.offsetTop - 20, behavior: "smooth" });
  }

  async function bestDeal() {
    if (state.loading) return;
    clearError();
    state.loading = true;
    const btn = $("best-deal-btn");
    btn.disabled = true;
    btn.querySelector(".btn-content").hidden = true;
    btn.querySelector(".btn-spinner").hidden = false;
    try {
      const r = await fetch(
        `${API_BASE}/best-deal`,
        withAuth({
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ timeframe: state.tf }),
        }),
      );
      if (!r.ok) {
        const errText = await r.text().catch(() => "");
        let msg = `Ошибка ${r.status}`;
        try {
          const j = JSON.parse(errText);
          if (j.detail) msg += `: ${j.detail}`;
        } catch {
          if (errText) msg += `: ${errText.slice(0, 200)}`;
        }
        throw new Error(msg);
      }
      const j = await r.json();
      renderBestDealResult(j);
    } catch (e) {
      showError(`Не удалось найти лучшую сделку — ${e.message}`);
    } finally {
      state.loading = false;
      btn.disabled = false;
      btn.querySelector(".btn-content").hidden = false;
      btn.querySelector(".btn-spinner").hidden = true;
    }
  }

  async function analyze() {
    if (state.loading) return;
    clearError();
    state.loading = true;
    const btn = $("analyze-btn");
    btn.disabled = true;
    btn.querySelector(".btn-content").hidden = true;
    btn.querySelector(".btn-spinner").hidden = false;
    try {
      const r = await fetch(
        `${API_BASE}/analyze`,
        withAuth({
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ coin: state.coin, timeframe: state.tf }),
        }),
      );
      if (!r.ok) {
        const errText = await r.text().catch(() => "");
        let msg = `Ошибка ${r.status}`;
        try {
          const j = JSON.parse(errText);
          if (j.detail) msg += `: ${j.detail}`;
        } catch {
          if (errText) msg += `: ${errText.slice(0, 200)}`;
        }
        throw new Error(msg);
      }
      const j = await r.json();
      renderResult(j);
    } catch (e) {
      showError(`Не удалось получить анализ — ${e.message}`);
    } finally {
      state.loading = false;
      btn.disabled = false;
      btn.querySelector(".btn-content").hidden = false;
      btn.querySelector(".btn-spinner").hidden = true;
    }
  }

  async function notifyCheck() {
    if (state.loading) return;
    clearError();
    state.loading = true;
    const btn = $("notify-btn");
    btn.disabled = true;
    btn.querySelector(".btn-content").hidden = true;
    btn.querySelector(".btn-spinner").hidden = false;
    try {
      const r = await fetch(
        `${API_BASE}/notify-check`,
        withAuth({
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ timeframe: state.tf }),
        }),
      );
      if (!r.ok) {
        const errText = await r.text().catch(() => "");
        let msg = `Ошибка ${r.status}`;
        try {
          const j = JSON.parse(errText);
          if (j.detail) msg += `: ${j.detail}`;
        } catch {
          if (errText) msg += `: ${errText.slice(0, 200)}`;
        }
        throw new Error(msg);
      }
      const j = await r.json();
      if (j.notified && j.notified.length > 0) {
        alert(`Отправлены уведомления в Telegram: ${j.notified.join(", ")}`);
      } else {
        alert(`Просканировано ${j.scanned} монет. Сигналов для уведомления не найдено (макс. уверенность: ${j.best_confidence}%).`);
      }
    } catch (e) {
      showError(`Не удалось проверить сигналы — ${e.message}`);
    } finally {
      state.loading = false;
      btn.disabled = false;
      btn.querySelector(".btn-content").hidden = false;
      btn.querySelector(".btn-spinner").hidden = true;
    }
  }

  // --- Watchlist management ---

  function loadWatches() {
    try {
      return JSON.parse(localStorage.getItem("crypto_watches") || "[]");
    } catch {
      return [];
    }
  }

  function saveWatches() {
    try {
      localStorage.setItem("crypto_watches", JSON.stringify(watches));
    } catch {
      // ignore
    }
    syncWatchesToServer();
  }

  async function syncWatchesToServer() {
    try {
      await fetch(
        `${API_BASE}/watches/sync`,
        withAuth({
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ watches }),
        }),
      );
    } catch {
      // ignore
    }
  }

  function addWatch(data) {
    const exists = watches.some(
      (w) => w.coin === data.coin && w.timeframe === data.timeframe && w.entry === data.entry
    );
    if (exists) return;
    const watch = { ...data, id: Date.now(), created: new Date().toISOString(), tp1_hit: false, tp2_hit: false };
    watches.push(watch);
    saveWatches();
    renderWatches();
    startPolling();
  }

  function removeWatch(id) {
    watches = watches.filter((w) => w.id !== id);
    saveWatches();
    renderWatches();
    if (watches.length === 0) stopPolling();
  }

  function renderWatches() {
    const panel = $("watches-panel");
    const list = $("watches-list");
    const count = $("watches-count");
    if (!watches.length) {
      panel.hidden = true;
      return;
    }
    panel.hidden = false;
    count.textContent = watches.length;
    list.innerHTML = "";
    watches.forEach((w) => {
      const item = document.createElement("div");
      item.className = "watch-item " + (w.direction || "flat");
      const dirLabel = w.direction === "long" ? "\u041B\u041E\u041D\u0413" : w.direction === "short" ? "\u0428\u041E\u0420\u0422" : "\u2014";
      item.innerHTML = `
        <span class="watch-coin">${w.coin}/USDT \u00b7 ${w.timeframe} \u00b7 ${dirLabel}</span>
        <span class="watch-price">\u0412\u0445\u043E\u0434: ${fmtPrice(w.entry)}</span>
        <span class="watch-target sl">SL: ${fmtPrice(w.stop_loss)}</span>
        <span class="watch-target tp">TP1: ${fmtPrice(w.take_profit_1)}${w.tp1_hit ? " \u2705" : ""}</span>
        <button class="watch-remove" data-id="${w.id}">\u2716</button>
      `;
      item.querySelector(".watch-remove").addEventListener("click", () => removeWatch(w.id));
      list.appendChild(item);
    });
  }

  function startPolling() {
    if (pollTimer) return;
    pollTimer = setInterval(pollPrices, POLL_INTERVAL);
    pollPrices();
  }

  function stopPolling() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  async function pollPrices() {
    const activeWatches = [...watches];
    const coinSet = new Set(activeWatches.map((w) => w.coin));
    for (const coin of coinSet) {
      try {
        const r = await fetch(`${API_BASE}/price/${coin}`, withAuth());
        if (!r.ok) continue;
        const data = await r.json();
        const price = data.price;
        for (const w of activeWatches.filter((x) => x.coin === coin)) {
          checkPriceHit(w, price);
        }
      } catch {
        // ignore fetch errors
      }
    }
  }

  async function checkPriceHit(watch, price) {
    const isLong = watch.direction === "long";
    const slHit = isLong ? price <= watch.stop_loss : price >= watch.stop_loss;
    const tp1Hit = !watch.tp1_hit && watch.take_profit_1 != null && (isLong ? price >= watch.take_profit_1 : price <= watch.take_profit_1);
    const tp2Hit = watch.take_profit_2 != null && (isLong ? price >= watch.take_profit_2 : price <= watch.take_profit_2);

    if (slHit) {
      await sendAlert(watch, "SL", price);
      removeWatch(watch.id);
      return;
    }
    if (tp2Hit) {
      await sendAlert(watch, "TP2", price);
      removeWatch(watch.id);
      return;
    }
    if (tp1Hit) {
      await sendAlert(watch, "TP1", price);
      const idx = watches.findIndex((w) => w.id === watch.id);
      if (idx !== -1) {
        watches[idx].tp1_hit = true;
        saveWatches();
        renderWatches();
      }
    }
  }

  async function sendAlert(watch, hitType, hitPrice) {
    try {
      await fetch(
        `${API_BASE}/send-alert`,
        withAuth({
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            coin: watch.coin,
            timeframe: watch.timeframe,
            direction: watch.direction,
            hit_type: hitType,
            hit_price: hitPrice,
            entry: watch.entry,
            stop_loss: watch.stop_loss,
            take_profit_1: watch.take_profit_1,
            take_profit_2: watch.take_profit_2,
          }),
        }),
      );
    } catch {
      // ignore
    }
  }

  async function sendWatchesToBot() {
    const btn = $("send-to-bot-btn");
    btn.querySelector(".btn-content").hidden = true;
    btn.querySelector(".btn-spinner").hidden = false;
    btn.disabled = true;
    try {
      const r = await fetch(
        `${API_BASE}/watches/analyze-and-send`,
        withAuth({
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ watches }),
        }),
      );
      const data = await r.json();
      if (data.ok && data.sent > 0) {
        alert(`\u2705 \u041e\u0442\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u043e ${data.sent} \u0430\u043d\u0430\u043b\u0438\u0437(\u043e\u0432) \u0432 Telegram`);
      } else if (data.ok && data.sent === 0) {
        alert("\u26a0\ufe0f \u041d\u0435\u0442 \u0434\u0430\u043d\u043d\u044b\u0445 \u0434\u043b\u044f \u043e\u0442\u043f\u0440\u0430\u0432\u043a\u0438 (\u043e\u0448\u0438\u0431\u043a\u0430 \u043f\u043e\u043b\u0443\u0447\u0435\u043d\u0438\u044f \u0446\u0435\u043d\u044b)");
      } else {
        alert("\u274c \u041e\u0448\u0438\u0431\u043a\u0430 \u043e\u0442\u043f\u0440\u0430\u0432\u043a\u0438");
      }
    } catch {
      alert("\u274c \u041e\u0448\u0438\u0431\u043a\u0430 \u0441\u0435\u0442\u0438");
    } finally {
      btn.querySelector(".btn-content").hidden = false;
      btn.querySelector(".btn-spinner").hidden = true;
      btn.disabled = false;
    }
  }

  function setupThemeToggle() {
    const btn = $("theme-toggle");
    if (!btn) return;
    btn.addEventListener("click", () => {
      const cur = document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
      const next = cur === "light" ? "dark" : "light";
      document.documentElement.setAttribute("data-theme", next);
      try {
        localStorage.setItem("theme", next);
      } catch (e) {
        // ignore quota / privacy-mode errors
      }
    });
  }

  function init() {
    setupThemeToggle();
    buildChips("coin-row", COINS, "coin");
    buildChips("tf-row", TFS, "tf");
    $("analyze-btn").addEventListener("click", analyze);
    $("best-deal-btn").addEventListener("click", bestDeal);
    $("notify-btn").addEventListener("click", notifyCheck);
    $("send-to-bot-btn").addEventListener("click", sendWatchesToBot);
    checkHealth();
    loadContext();
    renderWatches();
    if (watches.length > 0) {
      startPolling();
      syncWatchesToServer();
    }
  }

  document.addEventListener("DOMContentLoaded", init);
})();
