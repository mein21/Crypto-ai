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
    // map [-1, 1] → rgb gradient red ↔ neutral ↔ green
    const t = (v + 1) / 2; // 0..1
    const r = Math.round(220 * (1 - t) + 60 * t);
    const g = Math.round(60 * (1 - t) + 200 * t);
    const b = 90;
    const a = 0.18 + Math.abs(v) * 0.55;
    return `rgba(${r}, ${g}, ${b}, ${a.toFixed(2)})`;
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

  function renderNews(news) {
    const ul = $("news-list");
    ul.innerHTML = "";
    if (!news || !news.length) {
      ul.innerHTML = `<li class="muted">Свежих новостей не найдено</li>`;
      return;
    }
    news.forEach((n) => {
      const li = document.createElement("li");
      const date = n.ts ? new Date(n.ts * 1000).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" }) : "";
      li.innerHTML = `
        <a href="${n.url}" target="_blank" rel="noreferrer noopener">${n.title}</a>
        <div class="news-meta">${n.source || ""} · ${date}</div>
      `;
      ul.appendChild(li);
    });
  }

  function renderResult(resp) {
    const { analysis, chart_png_b64, indicators, last_price, htf_trends, news, fear_greed } = resp;
    $("result").hidden = false;
    renderHtfStrip(htf_trends);
    renderNews(news);
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
    const rows = [
      ["Направление", dirText, sig.direction || "flat"],
      ["Вход", fmtPrice(sig.entry), "warn"],
      ["Stop-loss", fmtPrice(sig.stop_loss), "short"],
      ["Take-profit 1", fmtPrice(sig.take_profit_1), "long"],
      ["Take-profit 2", fmtPrice(sig.take_profit_2), "long"],
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

  function init() {
    buildChips("coin-row", COINS, "coin");
    buildChips("tf-row", TFS, "tf");
    $("analyze-btn").addEventListener("click", analyze);
    $("best-deal-btn").addEventListener("click", bestDeal);
    checkHealth();
    loadContext();
  }

  document.addEventListener("DOMContentLoaded", init);
})();
