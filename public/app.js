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
    if (!corr || !corr.labels || !corr.matrix) return false;
    const tbl = $("corr-table");
    tbl.innerHTML = "";
    const labels = corr.labels;
    const thead = document.createElement("thead");
    const headerRow = document.createElement("tr");
    headerRow.appendChild(document.createElement("th"));
    labels.forEach((l) => {
      const th = document.createElement("th");
      th.textContent = l;
      headerRow.appendChild(th);
    });
    thead.appendChild(headerRow);
    tbl.appendChild(thead);
    const tbody = document.createElement("tbody");
    labels.forEach((row, i) => {
      const tr = document.createElement("tr");
      const lh = document.createElement("th");
      lh.textContent = row;
      tr.appendChild(lh);
      labels.forEach((_, j) => {
        const td = document.createElement("td");
        const v = corr.matrix[i][j];
        td.textContent = v.toFixed(2);
        td.style.background = corrColor(v);
        if (i === j) td.style.opacity = "0.55";
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    tbl.appendChild(tbody);

    const mobile = $("corr-mobile");
    mobile.innerHTML = "";
    const btcIdx = labels.indexOf("BTC");
    if (btcIdx !== -1) {
      labels.forEach((coin, i) => {
        if (i === btcIdx) return;
        const v = corr.matrix[i][btcIdx];
        const li = document.createElement("li");
        li.className = "corr-mobile-row";
        const label = document.createElement("span");
        label.className = "corr-mobile-coin";
        label.textContent = coin;
        const bar = document.createElement("span");
        bar.className = "corr-mobile-bar";
        const fill = document.createElement("span");
        fill.className = "corr-mobile-fill";
        fill.style.width = `${Math.max(2, Math.abs(v) * 100)}%`;
        fill.style.background = corrColor(v);
        bar.appendChild(fill);
        const value = document.createElement("span");
        value.className = "corr-mobile-value";
        value.textContent = v.toFixed(2);
        li.appendChild(label);
        li.appendChild(bar);
        li.appendChild(value);
        mobile.appendChild(li);
      });
    }

    $("corr-meta").textContent = `${labels.length} монет · окно ${corr.window_days} дней · ${corr.n_observations} наблюдений`;
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

  // =========================================================================
  // Autotrade panel
  // =========================================================================
  const at = {
    workerUrl: null,
    pollTimer: null,
    busy: false,
  };

  async function fetchPublicConfig() {
    try {
      const res = await fetch(`${API_BASE}/config`, withAuth());
      if (!res.ok) return null;
      return await res.json();
    } catch (e) {
      return null;
    }
  }

  function atFmtUsd(v, sign = false) {
    if (v == null || isNaN(v)) return "—";
    const n = Number(v);
    const s = (sign && n > 0 ? "+" : "") + n.toFixed(Math.abs(n) >= 1000 ? 0 : 2);
    return `${s} $`;
  }

  function atSecsToHuman(secs) {
    if (!secs) return "—";
    if (secs < 60) return `${secs} с`;
    if (secs < 3600) return `${Math.round(secs / 60)} мин`;
    return `${(secs / 3600).toFixed(1)} ч`;
  }

  async function atFetch(path, init) {
    if (!at.workerUrl) throw new Error("autotrade worker недоступен");
    const url = `${at.workerUrl.replace(/\/$/, "")}${path}`;
    const opts = init ? { ...init } : {};
    const headers = new Headers(opts.headers || {});
    headers.set("Content-Type", "application/json");
    opts.headers = headers;
    const res = await fetch(url, opts);
    let body = null;
    try {
      body = await res.json();
    } catch (_) {}
    if (!res.ok) {
      const msg = (body && (body.detail || body.error)) || res.statusText;
      throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
    }
    return body || {};
  }

  async function atRefreshStatus() {
    const card = $("autotrade-card");
    if (!card || card.hidden) return;
    let s;
    try {
      s = await atFetch("/autotrade/status");
    } catch (e) {
      $("at-keys-status").textContent = `Воркер не отвечает: ${e.message}`;
      return;
    }

    const networkPill = $("at-network-pill");
    networkPill.textContent =
      s.network === "testnet" ? "TESTNET" : s.network === "mainnet" ? "MAINNET" : "—";
    networkPill.className = "at-pill " + (s.network === "mainnet" ? "at-pill-live" : "at-pill-paper");

    const instr = $("at-instrument-pill");
    instr.textContent = s.instrument === "spot" ? "SPOT" : s.instrument === "linear" ? "PERP" : "—";

    $("at-equity").textContent = atFmtUsd(s.equity_usdt);
    $("at-free").textContent = atFmtUsd(s.free_usdt);
    const dpnl = $("at-day-pnl");
    dpnl.textContent = atFmtUsd(s.today_pnl_usdt, true);
    dpnl.dataset.tone = (s.today_pnl_usdt || 0) > 0 ? "pos" : (s.today_pnl_usdt || 0) < 0 ? "neg" : "neutral";
    const wpnl = $("at-week-pnl");
    wpnl.textContent = atFmtUsd(s.week_pnl_usdt, true);
    wpnl.dataset.tone = (s.week_pnl_usdt || 0) > 0 ? "pos" : (s.week_pnl_usdt || 0) < 0 ? "neg" : "neutral";

    const positions = s.open_positions || [];
    $("at-open-count").textContent = String(positions.length);
    $("at-interval").textContent = atSecsToHuman(s.scan_interval_sec);

    const toggle = $("at-toggle");
    if (!at.busy) toggle.checked = !!s.running;

    if (s.network) $("at-network").value = s.network;
    if (s.instrument) $("at-instrument").value = s.instrument;
    if (s.leverage) $("at-leverage").value = s.leverage;
    if (s.position_pct) $("at-position-pct").value = s.position_pct;

    const ks = $("at-keys-status");
    const nets = s.available_networks || [];
    if (nets.length === 0) {
      ks.textContent = "API-ключи не сохранены. Сохрани, чтобы запустить.";
    } else {
      ks.textContent = `Сохранены: ${nets.map((n) => n.toUpperCase()).join(", ")}`;
    }
    if (s.balance_error) {
      ks.textContent += ` · Bybit: ${s.balance_error}`;
    }
    if (s.halted_reason && !s.running) {
      ks.textContent += ` · Остановлен: ${s.halted_reason}`;
    }

    const wrap = $("at-positions-wrap");
    const ul = $("at-positions");
    if (positions.length) {
      wrap.hidden = false;
      ul.innerHTML = positions
        .map((p) => {
          const tone = (p.unrealised || 0) > 0 ? "pos" : (p.unrealised || 0) < 0 ? "neg" : "neutral";
          return `<li>
              <span class="at-pos-sym">${escapeHtml(p.symbol || "")}</span>
              <span class="at-pos-side ${p.side === "Buy" ? "long" : "short"}">${p.side === "Buy" ? "LONG" : "SHORT"}</span>
              <span class="muted">×${escapeHtml(String(p.size))}</span>
              <span class="muted">@ ${escapeHtml(String(p.entry || "—"))}</span>
              <span class="at-pos-pnl" data-tone="${tone}">${atFmtUsd(p.unrealised, true)}</span>
            </li>`;
        })
        .join("");
    } else {
      wrap.hidden = true;
      ul.innerHTML = "";
    }
  }

  async function atRefreshHistory() {
    const list = $("at-history");
    if (!list) return;
    let h;
    try {
      h = await atFetch("/autotrade/history?limit=20");
    } catch (_) {
      return;
    }
    const trades = h.trades || [];
    const runs = h.runs || [];
    if (!trades.length && !runs.length) {
      list.innerHTML = `<li class="muted">Истории пока нет.</li>`;
      return;
    }
    const items = [];
    trades.slice(0, 8).forEach((t) => {
      const ts = t.opened_at ? new Date(t.opened_at * 1000).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" }) : "—";
      const closed = t.closed_at != null;
      const tone = closed ? ((t.pnl_usdt || 0) > 0 ? "pos" : (t.pnl_usdt || 0) < 0 ? "neg" : "neutral") : "neutral";
      items.push(`<li>
        <span class="at-h-time">${escapeHtml(ts)}</span>
        <span class="at-h-tag ${closed ? "closed" : "open"}">${closed ? "CLOSED" : "OPEN"}</span>
        <span class="at-h-sym">${escapeHtml(t.symbol)} · ${t.direction === "long" ? "LONG" : "SHORT"}</span>
        <span class="muted">conf ${t.confidence ?? "—"}</span>
        ${closed ? `<span class="at-pos-pnl" data-tone="${tone}">${atFmtUsd(t.pnl_usdt, true)}</span>` : `<span class="muted">в позиции</span>`}
      </li>`);
    });
    runs.slice(0, 5).forEach((r) => {
      const ts = r.started_at ? new Date(r.started_at * 1000).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" }) : "—";
      let tag;
      if (r.action === "entered") tag = `вошёл ${r.chosen_coin || "?"} (${r.chosen_direction || "?"}, conf ${r.chosen_confidence ?? "—"})`;
      else if (r.action === "skip_no_signal") tag = "нет идей с порогом";
      else if (r.action === "skip_full") tag = "лимит позиций";
      else if (r.action === "halted") tag = "стоп: " + (r.error || "cap");
      else if (r.action === "error") tag = "ошибка: " + (r.error || "");
      else tag = r.action || "—";
      items.push(`<li class="at-h-run">
        <span class="at-h-time">${escapeHtml(ts)}</span>
        <span class="at-h-tag scan">SCAN</span>
        <span class="muted">${escapeHtml(tag)}</span>
        <span class="muted">${r.coins_scanned || 0} coins</span>
      </li>`);
    });
    list.innerHTML = items.join("");
  }

  async function atSaveKeys() {
    const apiKey = $("at-api-key").value.trim();
    const apiSecret = $("at-api-secret").value.trim();
    const network = $("at-network").value || "mainnet";
    if (!apiKey || !apiSecret) {
      $("at-keys-status").textContent = "Введи API key и secret.";
      return;
    }
    $("at-save-keys").disabled = true;
    $("at-keys-status").textContent = "Проверяю ключ на Bybit…";
    try {
      const r = await atFetch("/autotrade/keys", {
        method: "POST",
        body: JSON.stringify({ api_key: apiKey, api_secret: apiSecret, network }),
      });
      $("at-keys-status").textContent = `OK · ${network.toUpperCase()} · equity ${atFmtUsd(r.equity_usdt)} · free ${atFmtUsd(r.free_usdt)}`;
      $("at-api-key").value = "";
      $("at-api-secret").value = "";
      atRefreshStatus();
    } catch (e) {
      $("at-keys-status").textContent = `Ошибка: ${e.message}`;
    } finally {
      $("at-save-keys").disabled = false;
    }
  }

  async function atToggle(on) {
    at.busy = true;
    try {
      if (on) {
        const body = {
          network: $("at-network").value || "mainnet",
          instrument: $("at-instrument").value || "linear",
          leverage: parseFloat($("at-leverage").value) || 3,
          position_pct: parseFloat($("at-position-pct").value) || 2,
        };
        await atFetch("/autotrade/start", { method: "POST", body: JSON.stringify(body) });
      } else {
        await atFetch("/autotrade/stop", { method: "POST" });
      }
    } catch (e) {
      $("at-keys-status").textContent = `Не удалось: ${e.message}`;
      $("at-toggle").checked = !on;
    } finally {
      at.busy = false;
      atRefreshStatus();
    }
  }

  async function atEmergency() {
    if (!confirm("Закрыть все открытые позиции маркетом и остановить бота на час?")) return;
    try {
      const r = await atFetch("/autotrade/emergency", { method: "POST" });
      $("at-keys-status").textContent = `Аварийный стоп · закрыто позиций: ${r.closed_positions ?? 0}`;
    } catch (e) {
      $("at-keys-status").textContent = `Ошибка: ${e.message}`;
    } finally {
      atRefreshStatus();
    }
  }

  async function setupAutotrade() {
    const cfg = await fetchPublicConfig();
    if (!cfg || !cfg.worker_url) return;
    at.workerUrl = cfg.worker_url;
    const card = $("autotrade-card");
    card.hidden = false;

    $("at-toggle").addEventListener("change", (e) => atToggle(e.target.checked));
    $("at-save-keys").addEventListener("click", atSaveKeys);
    $("at-stop").addEventListener("click", () => atToggle(false));
    $("at-emergency").addEventListener("click", atEmergency);

    await atRefreshStatus();
    atRefreshHistory();
    if (at.pollTimer) clearInterval(at.pollTimer);
    at.pollTimer = setInterval(() => {
      atRefreshStatus();
      atRefreshHistory();
    }, 20_000);
  }

  function init() {
    setupThemeToggle();
    buildChips("coin-row", COINS, "coin");
    buildChips("tf-row", TFS, "tf");
    $("analyze-btn").addEventListener("click", analyze);
    checkHealth();
    loadContext();
    setupAutotrade();
  }

  document.addEventListener("DOMContentLoaded", init);
})();
