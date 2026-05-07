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

  function init() {
    buildChips("coin-row", COINS, "coin");
    buildChips("tf-row", TFS, "tf");
    $("analyze-btn").addEventListener("click", analyze);
    checkHealth();
    loadContext();
  }

  document.addEventListener("DOMContentLoaded", init);
})();
