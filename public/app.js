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

  function fmtPct(v) {
    if (v == null || isNaN(v)) return "—";
    const sign = v > 0 ? "+" : "";
    return `${sign}${Number(v).toFixed(2)}%`;
  }

  function renderVolumeProfile(vp) {
    const card = $("vp-card");
    if (!vp) {
      card.hidden = true;
      return;
    }
    card.hidden = false;
    const kv = $("vp-kv");
    kv.innerHTML = "";
    const posLabels = {
      above_va: "выше зоны стоимости",
      inside_va: "внутри зоны стоимости",
      below_va: "ниже зоны стоимости",
    };
    const rows = [
      ["POC", fmtPrice(vp.poc), "warn"],
      ["VAH", fmtPrice(vp.vah), ""],
      ["VAL", fmtPrice(vp.val), ""],
      ["Позиция", posLabels[vp.position] || vp.position || "—", ""],
      ["Δ к POC", fmtPct(vp.distance_to_poc_pct), ""],
      ["Окно", `${vp.lookback_bars} баров`, ""],
    ];
    if (Array.isArray(vp.hvn) && vp.hvn.length) {
      rows.push(["HVN", vp.hvn.map(fmtPrice).join(" · "), ""]);
    }
    if (Array.isArray(vp.lvn) && vp.lvn.length) {
      rows.push(["LVN", vp.lvn.map(fmtPrice).join(" · "), ""]);
    }
    rows.forEach(([k, v, cls]) => {
      const kEl = document.createElement("div");
      kEl.className = "k";
      kEl.textContent = k;
      const vEl = document.createElement("div");
      vEl.className = "v " + (cls || "");
      vEl.textContent = v;
      kv.appendChild(kEl);
      kv.appendChild(vEl);
    });

    const bars = $("vp-bars");
    bars.innerHTML = "";
    const bins = Array.isArray(vp.bins) ? vp.bins.slice().reverse() : [];
    if (!bins.length) return;
    const maxShare = bins.reduce((m, b) => Math.max(m, b.share || 0), 0) || 1;
    bins.forEach((b) => {
      const row = document.createElement("div");
      row.className = "vp-row";
      const isPoc = Math.abs(b.price - vp.poc) < 1e-6;
      const inVa = b.price >= vp.val && b.price <= vp.vah;
      if (isPoc) row.classList.add("vp-poc");
      else if (inVa) row.classList.add("vp-va");
      const px = document.createElement("span");
      px.className = "vp-price";
      px.textContent = fmtPrice(b.price);
      const bar = document.createElement("span");
      bar.className = "vp-bar";
      bar.style.width = `${(b.share / maxShare) * 100}%`;
      const pct = document.createElement("span");
      pct.className = "vp-share";
      pct.textContent = `${(b.share * 100).toFixed(1)}%`;
      row.appendChild(px);
      row.appendChild(bar);
      row.appendChild(pct);
      bars.appendChild(row);
    });
  }

  function renderOrderFlow(flow) {
    const card = $("flow-card");
    if (!flow) {
      card.hidden = true;
      return;
    }
    card.hidden = false;
    const kv = $("flow-kv");
    kv.innerHTML = "";
    const divLabels = {
      bullish: ["бычья", "long"],
      bearish: ["медвежья", "short"],
      none: ["нет", ""],
    };
    const [divText, divCls] = divLabels[flow.divergence] || ["—", ""];
    const slopeCls = flow.cvd_slope > 0 ? "long" : flow.cvd_slope < 0 ? "short" : "";
    const rows = [
      ["CVD", Number(flow.cvd_value).toFixed(2), slopeCls],
      ["Наклон", Number(flow.cvd_slope).toFixed(4), slopeCls],
      ["Давление покупок", `${Number(flow.buy_pressure_pct).toFixed(1)}%`, ""],
      ["Дивергенция", divText, divCls],
    ];
    rows.forEach(([k, v, cls]) => {
      const kEl = document.createElement("div");
      kEl.className = "k";
      kEl.textContent = k;
      const vEl = document.createElement("div");
      vEl.className = "v " + (cls || "");
      vEl.textContent = v;
      kv.appendChild(kEl);
      kv.appendChild(vEl);
    });
    $("flow-note").textContent = flow.divergence_note || "";
  }

  function renderAlignment(al) {
    const card = $("alignment-card");
    if (!al) {
      card.hidden = true;
      return;
    }
    card.hidden = false;
    const score = Number(al.score || 0);
    $("alignment-num").textContent = score.toFixed(0);
    $("alignment-label").textContent = al.label || "—";
    const dirCls = al.direction > 0 ? "long" : al.direction < 0 ? "short" : "flat";
    $("alignment-num").className = "alignment-num " + dirCls;
    const fill = $("alignment-fill");
    fill.style.width = `${Math.max(2, Math.min(100, score))}%`;
    fill.className = "alignment-fill " + dirCls;
    const list = $("alignment-list");
    list.innerHTML = "";
    (al.breakdown || []).forEach((b) => {
      const li = document.createElement("li");
      const blockDir = b.score > 0 ? "long" : b.score < 0 ? "short" : "flat";
      li.className = "alignment-row " + blockDir;
      li.innerHTML = `
        <span class="alignment-tf">${b.tf || "—"}</span>
        <span class="alignment-trend">${b.trend || "—"}</span>
        <span class="alignment-meta">MACD ${b.macd_state || "—"} · RSI ${b.rsi != null ? Number(b.rsi).toFixed(1) : "—"} · w ${b.weight}</span>
      `;
      list.appendChild(li);
    });
  }

  function renderSentiment(s) {
    const card = $("sentiment-card");
    if (!s) {
      card.hidden = true;
      return;
    }
    card.hidden = false;
    const score = Number(s.score || 50);
    $("sentiment-num").textContent = score.toFixed(0);
    $("sentiment-label").textContent = s.label || "—";
    const cls = score >= 60 ? "long" : score <= 40 ? "short" : "flat";
    $("sentiment-num").className = "sentiment-num " + cls;
    const fill = $("sentiment-fill");
    fill.style.width = `${Math.max(2, Math.min(100, score))}%`;
    fill.className = "sentiment-fill " + cls;
    const c = s.components || {};
    const kv = $("sentiment-kv");
    kv.innerHTML = "";
    const rows = [
      ["F&G", `${Number(c.fear_greed ?? 50).toFixed(0)}/100`, ""],
      ["Новости", `${Number(c.news ?? 50).toFixed(0)}/100`, ""],
      ["Моментум 1d", `${Number(c.momentum ?? 50).toFixed(0)}/100 (${fmtPct(c.momentum_pct)})`, ""],
    ];
    if (c.news_breakdown && (c.news_breakdown.bullish_hits || c.news_breakdown.bearish_hits)) {
      rows.push([
        "Слова",
        `+${c.news_breakdown.bullish_hits || 0} / -${c.news_breakdown.bearish_hits || 0} в ${c.news_breakdown.n || 0} заголовках`,
        "",
      ]);
    }
    rows.forEach(([k, v, cls]) => {
      const kEl = document.createElement("div");
      kEl.className = "k";
      kEl.textContent = k;
      const vEl = document.createElement("div");
      vEl.className = "v " + (cls || "");
      vEl.textContent = v;
      kv.appendChild(kEl);
      kv.appendChild(vEl);
    });
  }

  function renderStrategy(stats) {
    const card = $("strategy-card");
    if (!stats || !stats.total_trades) {
      card.hidden = true;
      return;
    }
    card.hidden = false;
    const kv = $("strategy-kv");
    kv.innerHTML = "";
    const pfCls = stats.profit_factor >= 1.3 ? "long" : stats.profit_factor < 1 ? "short" : "";
    const expCls = stats.expectancy_atr > 0 ? "long" : stats.expectancy_atr < 0 ? "short" : "";
    const rows = [
      ["Сделок", stats.total_trades, ""],
      ["Winrate", `${Number(stats.win_rate).toFixed(1)}%`, stats.win_rate >= 50 ? "long" : "short"],
      ["Profit factor", Number(stats.profit_factor).toFixed(2), pfCls],
      ["Avg R:R", Number(stats.avg_rr).toFixed(2), ""],
      ["Expectancy", `${Number(stats.expectancy_atr).toFixed(3)} ATR`, expCls],
      ["L / S", `${stats.longs} / ${stats.shorts}`, ""],
      ["Окно", `${stats.lookback_bars} баров`, ""],
    ];
    rows.forEach(([k, v, cls]) => {
      const kEl = document.createElement("div");
      kEl.className = "k";
      kEl.textContent = k;
      const vEl = document.createElement("div");
      vEl.className = "v " + (cls || "");
      vEl.textContent = v;
      kv.appendChild(kEl);
      kv.appendChild(vEl);
    });
    const wins = $("strategy-windows");
    wins.innerHTML = "";
    (stats.windows || []).forEach((w) => {
      const tile = document.createElement("div");
      const cls = w.total_trades === 0 ? "flat" : w.win_rate >= 50 ? "long" : "short";
      tile.className = "strategy-tile " + cls;
      tile.innerHTML = `
        <div class="strategy-w-tf">Окно ${w.window}</div>
        <div class="strategy-w-meta">${w.total_trades} сделок · ${Number(w.win_rate).toFixed(0)}%</div>
        <div class="strategy-w-meta">PF ${Number(w.profit_factor).toFixed(2)} · ${Number(w.expectancy_atr).toFixed(2)} ATR</div>
      `;
      wins.appendChild(tile);
    });
  }

  function renderResult(resp) {
    const {
      analysis,
      chart_png_b64,
      indicators,
      last_price,
      htf_trends,
      news,
      fear_greed,
      volume_profile,
      order_flow,
      alignment,
      sentiment,
      strategy_stats,
    } = resp;
    $("result").hidden = false;
    renderHtfStrip(htf_trends);
    renderNews(news);
    renderVolumeProfile(volume_profile);
    renderOrderFlow(order_flow);
    renderAlignment(alignment);
    renderSentiment(sentiment);
    renderStrategy(strategy_stats);
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
    checkHealth();
    loadContext();
  }

  document.addEventListener("DOMContentLoaded", init);
})();
