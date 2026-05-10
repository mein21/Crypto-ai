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

  // --- Failed trades lockout management (UTC+3 daily reset) ---
  const MAX_FAILED_TRADES = 3;
  const UTC_OFFSET_HOURS = 3;

  function getTodayKeyUtc3() {
    const now = new Date();
    const utc3 = new Date(now.getTime() + UTC_OFFSET_HOURS * 3600000);
    return utc3.toISOString().slice(0, 10);
  }

  function loadFailedTrades() {
    try {
      const raw = JSON.parse(localStorage.getItem("crypto_failed_trades") || "{}");
      const today = getTodayKeyUtc3();
      if (raw.date !== today) return { date: today, count: 0 };
      return { date: today, count: raw.count || 0 };
    } catch { return { date: getTodayKeyUtc3(), count: 0 }; }
  }

  function saveFailedTrades(data) {
    try { localStorage.setItem("crypto_failed_trades", JSON.stringify(data)); } catch {}
  }

  let failedTrades = loadFailedTrades();

  function incrementFailedTrades() {
    failedTrades = loadFailedTrades();
    failedTrades.count = Math.min(failedTrades.count + 1, MAX_FAILED_TRADES);
    saveFailedTrades(failedTrades);
    renderFailedTradesWidget();
    if (failedTrades.count >= MAX_FAILED_TRADES) {
      activateLockout();
    }
  }

  function isLockedOut() {
    failedTrades = loadFailedTrades();
    return failedTrades.count >= MAX_FAILED_TRADES;
  }

  function renderFailedTradesWidget() {
    const el = $("failed-trades-counter");
    const widget = $("failed-trades-widget");
    if (!el || !widget) return;
    failedTrades = loadFailedTrades();
    el.textContent = `${failedTrades.count}/${MAX_FAILED_TRADES}`;
    widget.classList.remove("warning", "danger");
    if (failedTrades.count >= MAX_FAILED_TRADES) {
      widget.classList.add("danger");
    } else if (failedTrades.count >= 2) {
      widget.classList.add("warning");
    }
  }

  function getMidnightUtc3() {
    const now = new Date();
    const utc3Now = new Date(now.getTime() + UTC_OFFSET_HOURS * 3600000);
    const tomorrow = new Date(utc3Now);
    tomorrow.setUTCHours(0, 0, 0, 0);
    tomorrow.setUTCDate(tomorrow.getUTCDate() + 1);
    return new Date(tomorrow.getTime() - UTC_OFFSET_HOURS * 3600000);
  }

  let lockoutCountdownTimer = null;
  let lockoutScrollY = 0;

  // Lock body scroll when the lockout overlay is open. Without this, swipes on
  // the dark backdrop (and on the snake canvas, which is what the user is
  // trying to control) bleed through and scroll the page underneath, making
  // the mini-game unplayable on phones. Use the iOS-friendly fixed-position
  // pattern so the original scroll position is restored on close.
  function lockBodyScroll() {
    if (document.body.dataset.lockoutScrollLock === "1") return;
    lockoutScrollY = window.scrollY || window.pageYOffset || 0;
    document.body.dataset.lockoutScrollLock = "1";
    document.body.style.position = "fixed";
    document.body.style.top = `-${lockoutScrollY}px`;
    document.body.style.left = "0";
    document.body.style.right = "0";
    document.body.style.width = "100%";
  }

  function unlockBodyScroll() {
    if (document.body.dataset.lockoutScrollLock !== "1") return;
    delete document.body.dataset.lockoutScrollLock;
    document.body.style.position = "";
    document.body.style.top = "";
    document.body.style.left = "";
    document.body.style.right = "";
    document.body.style.width = "";
    window.scrollTo(0, lockoutScrollY);
  }

  function activateLockout() {
    const overlay = $("lockout-overlay");
    if (!overlay) return;
    overlay.hidden = false;
    lockBodyScroll();
    startLockoutCountdown();
  }

  function deactivateLockout() {
    const overlay = $("lockout-overlay");
    if (overlay) overlay.hidden = true;
    if (lockoutCountdownTimer) {
      clearInterval(lockoutCountdownTimer);
      lockoutCountdownTimer = null;
    }
    stopSnake();
    unlockBodyScroll();
  }

  function startLockoutCountdown() {
    const countdownEl = $("lockout-countdown");
    if (!countdownEl) return;
    function tick() {
      const now = new Date();
      const target = getMidnightUtc3();
      const diff = target.getTime() - now.getTime();
      if (diff <= 0) {
        failedTrades = { date: getTodayKeyUtc3(), count: 0 };
        saveFailedTrades(failedTrades);
        renderFailedTradesWidget();
        deactivateLockout();
        return;
      }
      const h = Math.floor(diff / 3600000);
      const m = Math.floor((diff % 3600000) / 60000);
      const s = Math.floor((diff % 60000) / 1000);
      countdownEl.textContent = `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
    }
    tick();
    lockoutCountdownTimer = setInterval(tick, 1000);
  }

  // --- Snake game ---
  let snakeInterval = null;
  let snakeState = null;

  function initSnake() {
    const canvas = $("snake-canvas");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    const gridSize = 16;
    const tileCount = canvas.width / gridSize;

    snakeState = {
      ctx, canvas, gridSize, tileCount,
      snake: [{ x: 10, y: 10 }],
      food: { x: 5, y: 5 },
      dx: 1, dy: 0,
      score: 0,
      running: false,
    };
    drawSnake();
  }

  function drawSnake() {
    if (!snakeState) return;
    const { ctx, canvas, gridSize, snake, food } = snakeState;
    const isLight = document.documentElement.getAttribute("data-theme") === "light";
    ctx.fillStyle = isLight ? "#f0f0f0" : "#0a0a0a";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = isLight ? "#333" : "#f5f5f5";
    snake.forEach((seg) => {
      ctx.fillRect(seg.x * gridSize + 1, seg.y * gridSize + 1, gridSize - 2, gridSize - 2);
    });
    ctx.fillStyle = isLight ? "#cc3333" : "#ff6b6b";
    ctx.beginPath();
    ctx.arc(food.x * gridSize + gridSize / 2, food.y * gridSize + gridSize / 2, gridSize / 2 - 2, 0, 2 * Math.PI);
    ctx.fill();
    $("snake-score").textContent = snakeState.score;
  }

  function stepSnake() {
    if (!snakeState || !snakeState.running) return;
    const { snake, food, tileCount } = snakeState;
    const head = { x: snake[0].x + snakeState.dx, y: snake[0].y + snakeState.dy };
    if (head.x < 0 || head.x >= tileCount || head.y < 0 || head.y >= tileCount) {
      stopSnake(); return;
    }
    if (snake.some((s) => s.x === head.x && s.y === head.y)) {
      stopSnake(); return;
    }
    snake.unshift(head);
    if (head.x === food.x && head.y === food.y) {
      snakeState.score++;
      spawnFood();
    } else {
      snake.pop();
    }
    drawSnake();
  }

  function spawnFood() {
    if (!snakeState) return;
    const { tileCount, snake } = snakeState;
    let pos;
    do {
      pos = { x: Math.floor(Math.random() * tileCount), y: Math.floor(Math.random() * tileCount) };
    } while (snake.some((s) => s.x === pos.x && s.y === pos.y));
    snakeState.food = pos;
  }

  function startSnake() {
    if (!snakeState) initSnake();
    if (!snakeState) return;
    snakeState.snake = [{ x: 10, y: 10 }];
    snakeState.food = { x: 5, y: 5 };
    snakeState.dx = 1;
    snakeState.dy = 0;
    snakeState.score = 0;
    snakeState.running = true;
    if (snakeInterval) clearInterval(snakeInterval);
    snakeInterval = setInterval(stepSnake, 120);
    drawSnake();
    $("snake-start-btn").textContent = "Рестарт";
  }

  function stopSnake() {
    if (snakeInterval) { clearInterval(snakeInterval); snakeInterval = null; }
    if (snakeState) snakeState.running = false;
    const btn = $("snake-start-btn");
    if (btn) btn.textContent = "Начать игру";
  }

  function setupSnakeControls() {
    document.addEventListener("keydown", (e) => {
      if (!snakeState || !snakeState.running) return;
      let handled = true;
      switch (e.key) {
        case "ArrowUp": case "w": if (snakeState.dy !== 1) { snakeState.dx = 0; snakeState.dy = -1; } break;
        case "ArrowDown": case "s": if (snakeState.dy !== -1) { snakeState.dx = 0; snakeState.dy = 1; } break;
        case "ArrowLeft": case "a": if (snakeState.dx !== 1) { snakeState.dx = -1; snakeState.dy = 0; } break;
        case "ArrowRight": case "d": if (snakeState.dx !== -1) { snakeState.dx = 1; snakeState.dy = 0; } break;
        default: handled = false;
      }
      if (handled) e.preventDefault();
    });
    const startBtn = $("snake-start-btn");
    if (startBtn) startBtn.addEventListener("click", startSnake);

    // Touch controls for mobile.
    //
    // Two critical bits here:
    //   1. `passive: false` on touchstart/touchmove + `preventDefault()` so
    //      the page underneath does NOT scroll while the user is trying to
    //      swipe-control the snake. This was the main reason the game was
    //      "unplayable" — the browser was eating the swipe as a page scroll.
    //   2. The swipe threshold is checked against the start of THIS gesture,
    //      and we re-baseline `touchStart{X,Y}` only after we successfully
    //      register a direction change. That makes a single sustained swipe
    //      register *one* clean direction change instead of jittering, and
    //      lets the user chain consecutive direction changes within one
    //      finger-down without lifting.
    const SWIPE_THRESHOLD = 14;
    let touchStartX = 0, touchStartY = 0;
    const canvas = $("snake-canvas");
    if (canvas) {
      canvas.addEventListener("touchstart", (e) => {
        if (!e.touches[0]) return;
        touchStartX = e.touches[0].clientX;
        touchStartY = e.touches[0].clientY;
        // Do NOT preventDefault here — we want the synthetic click to still
        // fire on a stationary tap so the D-pad fallback works. The actual
        // page-scroll prevention is handled by `touch-action: none` in CSS
        // and by preventDefault on touchmove below.
      }, { passive: true });
      canvas.addEventListener("touchmove", (e) => {
        // Always preventDefault on canvas touchmove so the page can never
        // scroll while the finger is on the snake field, even before the
        // game has been started.
        e.preventDefault();
        if (!snakeState || !snakeState.running) return;
        if (!e.touches[0]) return;
        const dx = e.touches[0].clientX - touchStartX;
        const dy = e.touches[0].clientY - touchStartY;
        const absDx = Math.abs(dx);
        const absDy = Math.abs(dy);
        if (Math.max(absDx, absDy) < SWIPE_THRESHOLD) return;
        let changed = false;
        if (absDx > absDy) {
          if (dx > 0 && snakeState.dx !== -1) { snakeState.dx = 1; snakeState.dy = 0; changed = true; }
          else if (dx < 0 && snakeState.dx !== 1) { snakeState.dx = -1; snakeState.dy = 0; changed = true; }
        } else {
          if (dy > 0 && snakeState.dy !== -1) { snakeState.dx = 0; snakeState.dy = 1; changed = true; }
          else if (dy < 0 && snakeState.dy !== 1) { snakeState.dx = 0; snakeState.dy = -1; changed = true; }
        }
        if (changed) {
          // Re-baseline so the next direction in the same gesture is measured
          // from the current finger position rather than from where the
          // gesture originally started.
          touchStartX = e.touches[0].clientX;
          touchStartY = e.touches[0].clientY;
        }
      }, { passive: false });


      // On-screen D-pad fallback: tapping each edge of the canvas turns the
      // snake that way. Helps users who can't reliably swipe (e.g. small
      // screens, big fingers, or anyone who tried swiping and got page
      // scroll the first time and gave up).
      canvas.addEventListener("click", (e) => {
        if (!snakeState || !snakeState.running) return;
        const rect = canvas.getBoundingClientRect();
        const x = e.clientX - rect.left;
        const y = e.clientY - rect.top;
        const w = rect.width;
        const h = rect.height;
        // Diagonal split: pick the closest edge.
        const left = x;
        const right = w - x;
        const top = y;
        const bot = h - y;
        const min = Math.min(left, right, top, bot);
        if (min === left && snakeState.dx !== 1) { snakeState.dx = -1; snakeState.dy = 0; }
        else if (min === right && snakeState.dx !== -1) { snakeState.dx = 1; snakeState.dy = 0; }
        else if (min === top && snakeState.dy !== 1) { snakeState.dx = 0; snakeState.dy = -1; }
        else if (min === bot && snakeState.dy !== -1) { snakeState.dx = 0; snakeState.dy = 1; }
      });
    }
  }

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

    const mobileList = $("corr-mobile");
    mobileList.innerHTML = "";
    corr.pairs.forEach((p) => {
      const li = document.createElement("li");
      li.className = "corr-mobile-row";
      const pct = Math.round(Math.abs(p.value) * 100);
      li.innerHTML = `
        <span class="corr-mobile-coin">${p.coin}</span>
        <span class="corr-mobile-bar"><span class="corr-mobile-fill" style="width:${pct}%;background:${corrColor(p.value)}"></span></span>
        <span class="corr-mobile-value" style="color:${corrColor(p.value)}">${p.value.toFixed(3)}</span>
      `;
      mobileList.appendChild(li);
    });

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

  function renderAnalytics(data) {
    const card = $("analytics-card");
    if (!data) { card.hidden = true; return; }
    card.hidden = false;
    const kv = $("analytics-kv");
    kv.innerHTML = "";
    const rows = [];
    if (data.funding_rate_pct != null) {
      const fr = data.funding_rate_pct;
      const cls = fr > 0.01 ? "long" : fr < -0.01 ? "short" : "";
      const label = fr > 0.01 ? "бычий" : fr < -0.01 ? "медвежий" : "нейтрально";
      rows.push(["Funding rate", `${fr}% (${label})`, cls]);
    }
    if (data.open_interest != null) {
      rows.push(["Open interest", Number(data.open_interest).toLocaleString(), ""]);
    }
    if (data.long_short_ratio != null) {
      const ls = data.long_short_ratio;
      const cls = ls > 1.2 ? "long" : ls < 0.8 ? "short" : "";
      const lbl = ls > 1.2 ? "больше лонгов" : ls < 0.8 ? "больше шортов" : "баланс";
      rows.push(["L/S ratio", `${ls.toFixed(2)} (${lbl})`, cls]);
    }
    if (data.long_account_pct != null) {
      rows.push(["Лонги / Шорты", `${data.long_account_pct.toFixed(1)}% / ${data.short_account_pct.toFixed(1)}%`, ""]);
    }
    if (rows.length === 0) { card.hidden = true; return; }
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
    const {
      analysis,
      chart_png_b64,
      indicators,
      last_price,
      htf_trends,
      news,
      fear_greed,
      onchain,
      volume_profile,
      order_flow,
      alignment,
      sentiment,
      strategy_stats,
      analytics,
      liquidations,
    } = resp;
    $("result").hidden = false;
    renderHtfStrip(htf_trends);
    renderNews(news);
    renderOnchain(analysis.coin, onchain);
    renderVolumeProfile(volume_profile);
    renderOrderFlow(order_flow);
    renderAlignment(alignment);
    renderSentiment(sentiment);
    renderStrategy(strategy_stats);
    renderAnalytics(analytics);
    renderLiquidations(liquidations);
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
    const oldRisk = idea.parentElement.querySelector(".risk-info");
    if (oldRisk) oldRisk.remove();
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
    const entryType = sig.entry_type || "market";
    const entryTypeLabels = {
      market: { label: "По рынку", title: "Market — открыть прямо сейчас по цене close" },
      limit: { label: "Лимит", title: "Limit — пассивный ордер у уровня (long: ниже close, short: выше close)" },
      stop: { label: "По пробою", title: "Stop — войти только при пробое уровня (long: выше close, short: ниже close)" },
    };
    const isLimit = entryType === "limit";
    const rows = [
      ["Направление", dirText, sig.direction || "flat", null],
      ["Вход", fmtPrice(sig.entry), "warn", { kind: "entry-type", type: entryType }],
      ["Stop-loss", fmtPrice(sig.stop_loss), "short", null],
      ["Take-profit 1", `${fmtPrice(sig.take_profit_1)}${rrText(sig.take_profit_1)}`, "long", null],
      ...(!isLimit ? [["Take-profit 2", `${fmtPrice(sig.take_profit_2)}${rrText(sig.take_profit_2)}`, "long", null]] : []),
      ["Уверенность", `${sig.confidence ?? 0}%`, "", null],
    ];
    rows.forEach(([k, v, cls, extra]) => {
      const kEl = document.createElement("div");
      kEl.className = "k";
      kEl.textContent = k;
      const vEl = document.createElement("div");
      vEl.className = "v " + (cls || "");
      vEl.textContent = v;
      if (extra && extra.kind === "entry-type" && sig.entry != null && (sig.direction === "long" || sig.direction === "short")) {
        const badge = document.createElement("span");
        badge.className = "entry-type-badge " + extra.type;
        const conf = entryTypeLabels[extra.type] || entryTypeLabels.market;
        badge.textContent = conf.label;
        badge.title = conf.title;
        vEl.appendChild(document.createTextNode(" "));
        vEl.appendChild(badge);
      }
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
    const bdEntryType = best.entry_type || "market";
    const bdIsLimit = bdEntryType === "limit";
    const rows = [
      ["Монета", `${best.coin}/USDT`, ""],
      ["Цена", fmtPrice(best.last_price), ""],
      ["Тренд", best.trend, ""],
      ["Направление", dirText, best.direction || "flat"],
      ["Вход", fmtPrice(best.entry), "warn"],
      ["Stop-loss", fmtPrice(best.stop_loss), "short"],
      ["Take-profit 1", fmtPrice(best.take_profit_1), "long"],
      ...(!bdIsLimit ? [["Take-profit 2", fmtPrice(best.take_profit_2), "long"]] : []),
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
    if (isLockedOut()) { activateLockout(); return; }
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
    if (isLockedOut()) { activateLockout(); return; }
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
    if (isLockedOut()) { activateLockout(); return; }
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
        <div class="watch-info">
          <span class="watch-coin">${w.coin}/USDT \u00b7 ${w.timeframe} \u00b7 ${dirLabel}</span>
          <span class="watch-price">\u0412\u0445\u043E\u0434: ${fmtPrice(w.entry)}</span>
          <span class="watch-target sl">SL: ${fmtPrice(w.stop_loss)}</span>
          <span class="watch-target tp">TP1: ${fmtPrice(w.take_profit_1)}${w.tp1_hit ? " \u2705" : ""}</span>
        </div>
        <div class="watch-actions">
          <button class="watch-check" data-id="${w.id}" title="\u041F\u0440\u043E\u0432\u0435\u0440\u0438\u0442\u044C">\uD83D\uDD0D</button>
          <button class="watch-remove" data-id="${w.id}">\u2716</button>
        </div>
      `;
      item.querySelector(".watch-check").addEventListener("click", () => checkWatch(w));
      item.querySelector(".watch-remove").addEventListener("click", () => removeWatch(w.id));
      list.appendChild(item);
    });
  }

  async function checkWatch(watch) {
    const btn = document.querySelector(`.watch-check[data-id="${watch.id}"]`);
    if (!btn) return;
    btn.disabled = true;
    btn.textContent = "\u23F3";
    try {
      const r = await fetch(
        `${API_BASE}/analyze`,
        withAuth({
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ coin: watch.coin, timeframe: watch.timeframe }),
        }),
      );
      if (!r.ok) throw new Error("Ошибка анализа");
      const j = await r.json();
      const sig = j.analysis?.signal || {};
      const price = j.last_price;

      const item = btn.closest(".watch-item");
      let statusEl = item.querySelector(".watch-status");
      if (!statusEl) {
        statusEl = document.createElement("div");
        statusEl.className = "watch-status";
        item.appendChild(statusEl);
      }

      // Strategy validity
      const sameDir = sig.direction === watch.direction;
      const isFlat = sig.direction === "flat";
      const newConf = sig.confidence ?? 0;

      let verdict, verdictClass;
      if (isFlat) {
        verdict = "\u274C Сигнал потерян — рекомендуется закрыть";
        verdictClass = "loss";
      } else if (!sameDir) {
        verdict = `\u274C Сигнал сменился на ${sig.direction === "long" ? "ЛОНГ" : "ШОРТ"} — стратегия неактуальна`;
        verdictClass = "loss";
      } else if (newConf < 40) {
        verdict = `\u26A0 Актуальна, но уверенность низкая (${newConf}%)`;
        verdictClass = "warn";
      } else {
        verdict = `\u2705 Актуальна (${newConf}%)`;
        verdictClass = "profit";
      }

      // PnL calculation
      const isLong = watch.direction === "long";
      const pnl = isLong ? price - watch.entry : watch.entry - price;
      const pnlPct = ((pnl / watch.entry) * 100).toFixed(2);
      const pnlSign = pnl >= 0 ? "+" : "";
      const pnlClass = pnl >= 0 ? "profit" : "loss";

      // Distances
      const distToSl = ((Math.abs(price - watch.stop_loss) / price) * 100).toFixed(2);
      const distToTp1 = watch.take_profit_1 != null ? ((Math.abs(watch.take_profit_1 - price) / price) * 100).toFixed(2) : null;

      statusEl.innerHTML = `
        <span class="watch-status-verdict ${verdictClass}">${verdict}</span>
        <span class="watch-status-price">Цена: ${fmtPrice(price)} · Тренд: ${j.analysis?.trend || "—"}</span>
        <span class="watch-status-pnl ${pnlClass}">PnL: ${pnlSign}${pnlPct}%</span>
        <span class="watch-status-dist">До SL: ${distToSl}%${distToTp1 ? ` · До TP1: ${distToTp1}%` : ""}</span>
      `;
      btn.textContent = "\uD83D\uDD0D";
    } catch {
      btn.textContent = "\u274C";
      setTimeout(() => { btn.textContent = "\uD83D\uDD0D"; }, 2000);
    } finally {
      btn.disabled = false;
    }
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
      const pct = watch.tp1_hit ? 0.5 : 1.0;
      addTradeToHistory(watch, "SL", price, pct);
      await sendAlert(watch, "SL", price);
      removeWatch(watch.id);
      playHitSound("sl");
      return;
    }
    if (tp2Hit) {
      addTradeToHistory(watch, "TP2", price, 0.5);
      await sendAlert(watch, "TP2", price);
      removeWatch(watch.id);
      playHitSound("tp");
      return;
    }
    if (tp1Hit) {
      addTradeToHistory(watch, "TP1", price, 0.5);
      await sendAlert(watch, "TP1", price);
      const idx = watches.findIndex((w) => w.id === watch.id);
      if (idx !== -1) {
        watches[idx].tp1_hit = true;
        watches[idx].stop_loss = watch.entry;
        saveWatches();
        renderWatches();
      }
      playHitSound("tp");
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

  // --- Sound notifications ---
  function playHitSound(type) {
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.connect(gain);
      gain.connect(ctx.destination);
      if (type === "tp") {
        osc.frequency.value = 880;
        osc.type = "sine";
        gain.gain.setValueAtTime(0.3, ctx.currentTime);
        gain.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.5);
        osc.start(); osc.stop(ctx.currentTime + 0.5);
      } else {
        osc.frequency.value = 330;
        osc.type = "square";
        gain.gain.setValueAtTime(0.25, ctx.currentTime);
        gain.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.4);
        osc.start(); osc.stop(ctx.currentTime + 0.4);
      }
    } catch { /* audio not available */ }
  }

  // --- Trade History ---
  function loadTradeHistory() {
    try { return JSON.parse(localStorage.getItem("crypto_trade_history") || "[]"); } catch { return []; }
  }
  function saveTradeHistory(history) {
    try { localStorage.setItem("crypto_trade_history", JSON.stringify(history)); } catch {}
  }
  function addTradeToHistory(watch, hitType, hitPrice, pct) {
    const history = loadTradeHistory();
    const isLong = watch.direction === "long";
    const sign = isLong ? 1 : -1;
    const pnlPct = ((hitPrice - watch.entry) / watch.entry) * 100 * sign;
    history.unshift({
      id: Date.now(),
      coin: watch.coin,
      timeframe: watch.timeframe,
      direction: watch.direction,
      entry: watch.entry,
      stop_loss: watch.stop_loss,
      take_profit_1: watch.take_profit_1,
      take_profit_2: watch.take_profit_2,
      hit_type: hitType,
      hit_price: hitPrice,
      pnl_pct: Math.round(pnlPct * 100) / 100,
      portion: pct,
      closed_at: new Date().toISOString(),
      note: "",
    });
    if (history.length > 200) history.length = 200;
    saveTradeHistory(history);
    renderTradeHistory();
  }
  function renderTradeHistory() {
    const panel = $("history-panel");
    const list = $("history-list");
    const statsEl = $("history-stats");
    if (!panel || !list) return;
    const history = loadTradeHistory();
    if (!history.length) { panel.hidden = true; return; }
    panel.hidden = false;
    const wins = history.filter(h => h.hit_type !== "SL" || h.portion < 1);
    const losses = history.filter(h => h.hit_type === "SL" && h.portion >= 1);
    const winrate = history.length > 0 ? ((wins.length / history.length) * 100).toFixed(1) : "0";
    const avgPnl = history.length > 0 ? (history.reduce((s, h) => s + h.pnl_pct, 0) / history.length).toFixed(2) : "0";
    const bestTrade = history.reduce((best, h) => h.pnl_pct > best.pnl_pct ? h : best, history[0]);
    const worstTrade = history.reduce((worst, h) => h.pnl_pct < worst.pnl_pct ? h : worst, history[0]);
    if (statsEl) {
      statsEl.innerHTML = `
        <div class="stat-chip"><span class="stat-label">Сделок</span><span class="stat-value">${history.length}</span></div>
        <div class="stat-chip ${parseFloat(winrate) >= 50 ? "long" : "short"}"><span class="stat-label">Winrate</span><span class="stat-value">${winrate}%</span></div>
        <div class="stat-chip ${parseFloat(avgPnl) >= 0 ? "long" : "short"}"><span class="stat-label">Ср. PnL</span><span class="stat-value">${avgPnl}%</span></div>
        <div class="stat-chip long"><span class="stat-label">Лучшая</span><span class="stat-value">+${bestTrade.pnl_pct}%</span></div>
        <div class="stat-chip short"><span class="stat-label">Худшая</span><span class="stat-value">${worstTrade.pnl_pct}%</span></div>
      `;
    }
    list.innerHTML = "";
    history.slice(0, 20).forEach(h => {
      const row = document.createElement("div");
      row.className = "history-row " + (h.hit_type === "SL" && h.portion >= 1 ? "loss" : "win");
      const hitIcon = h.hit_type === "SL" ? "\uD83D\uDED1" : "\uD83C\uDFAF";
      const pnlCls = h.pnl_pct >= 0 ? "long" : "short";
      row.innerHTML = `
        <div class="history-main">
          <span class="history-coin">${h.coin}</span>
          <span class="history-dir ${h.direction}">${h.direction === "long" ? "\u25B2" : "\u25BC"}</span>
          <span class="history-tf">${h.timeframe}</span>
          <span class="history-hit">${hitIcon} ${h.hit_type}${h.portion < 1 ? " (50%)" : ""}</span>
          <span class="history-pnl ${pnlCls}">${h.pnl_pct >= 0 ? "+" : ""}${h.pnl_pct}%</span>
          <span class="history-date">${new Date(h.closed_at).toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit" })}</span>
        </div>
        <div class="history-note-wrap">
          <input class="history-note-input" type="text" placeholder="Заметка..." value="${(h.note || "").replace(/"/g, "&quot;")}" data-trade-id="${h.id}" />
        </div>
      `;
      const noteInput = row.querySelector(".history-note-input");
      noteInput.addEventListener("change", (e) => {
        const allHistory = loadTradeHistory();
        const t = allHistory.find(x => x.id === h.id);
        if (t) { t.note = e.target.value; saveTradeHistory(allHistory); }
      });
      list.appendChild(row);
    });
    if (history.length > 20) {
      const more = document.createElement("div");
      more.className = "history-more muted";
      more.textContent = `+ ещё ${history.length - 20} сделок`;
      list.appendChild(more);
    }
  }

  // --- Multi-coin Dashboard ---
  async function loadDashboard() {
    const grid = $("dashboard-grid");
    const section = $("dashboard-section");
    if (!grid || !section) return;
    section.hidden = false;
    grid.innerHTML = '<div class="dashboard-loading">Загрузка цен...</div>';
    const prices = {};
    const promises = COINS.map(async (coin) => {
      try {
        const r = await fetch(`${API_BASE}/price/${coin}`, withAuth());
        if (r.ok) { const d = await r.json(); prices[coin] = d.price; }
      } catch { /* ignore */ }
    });
    await Promise.all(promises);
    grid.innerHTML = "";
    COINS.forEach(coin => {
      const price = prices[coin];
      const tile = document.createElement("div");
      tile.className = "dash-tile";
      tile.innerHTML = `
        <div class="dash-coin">${coin}</div>
        <div class="dash-price">${price != null ? fmtPrice(price) : "—"}</div>
      `;
      tile.addEventListener("click", () => {
        state.coin = coin;
        const row = $("coin-row");
        row.querySelectorAll(".chip").forEach(c => c.classList.toggle("active", c.dataset.value === coin));
        section.scrollIntoView({ behavior: "smooth", block: "start" });
      });
      grid.appendChild(tile);
    });
  }

  // --- Liquidations display ---
  function renderLiquidations(data) {
    const card = $("liquidations-card");
    if (!card) return;
    if (!data) { card.hidden = true; return; }
    card.hidden = false;
    const kv = $("liquidations-kv");
    kv.innerHTML = "";
    const rows = [];
    if (data.taker_buy_sell_ratio != null) {
      const r = data.taker_buy_sell_ratio;
      const cls = r > 1.15 ? "long" : r < 0.85 ? "short" : "";
      const lbl = r > 1.15 ? "покупатели" : r < 0.85 ? "продавцы" : "баланс";
      rows.push(["Taker B/S ratio", `${r.toFixed(4)} (${lbl})`, cls]);
    }
    if (data.buy_pct != null) {
      rows.push(["Покупки / Продажи", `${data.buy_pct}% / ${data.sell_pct}%`, ""]);
    }
    if (data.avg_ratio_5h != null) {
      rows.push(["Средний ratio 5ч", data.avg_ratio_5h.toFixed(4), ""]);
    }
    if (!rows.length) { card.hidden = true; return; }
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

  function setupFailedTradesClick() {
    const widget = $("failed-trades-widget");
    if (!widget) return;
    widget.style.cursor = "pointer";
    widget.addEventListener("click", () => {
      if (isLockedOut()) return;
      incrementFailedTrades();
    });
  }

  function init() {
    setupThemeToggle();
    renderFailedTradesWidget();
    setupFailedTradesClick();
    setupSnakeControls();
    if (isLockedOut()) activateLockout();
    buildChips("coin-row", COINS, "coin");
    buildChips("tf-row", TFS, "tf");
    $("analyze-btn").addEventListener("click", analyze);
    $("best-deal-btn").addEventListener("click", bestDeal);
    $("notify-btn").addEventListener("click", notifyCheck);
    $("send-to-bot-btn").addEventListener("click", sendWatchesToBot);
    checkHealth();
    loadContext();
    renderWatches();
    renderTradeHistory();
    loadDashboard();
    if (watches.length > 0) {
      startPolling();
      syncWatchesToServer();
    }
  }

  document.addEventListener("DOMContentLoaded", init);
})();
