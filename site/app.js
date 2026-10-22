/* FutureWeather — model forecast console for standardweather.github.io.
 *
 *   Model maps    GFS, ECMWF, National Blend of Models and HRRR overlays drawn by make_plots.py (GitHub Actions)
 *   NWS API       7-day and hourly forecasts, and the forecast grid behind the 7-day meteogram and work windows (api.weather.gov)
 *   GEFS          NOAA's 31-member ensemble at the point, for forecast confidence (tiles packed by make_ensemble.py)
 *   WPC, SPC, CPC 7-day rainfall, excessive rainfall, day 1–8 severe and 6–14 day outlooks (NOAA map services, drawn live)
 *   Census TIGERweb / Geocoder, USGS GNIS   place search
 */
(() => {
  "use strict";

  const NWS = "https://api.weather.gov";
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const pad = (n, w = 2) => String(n).padStart(w, "0");
  const dayZ = (t) => { const d = new Date(t); return `${d.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric", timeZone: "UTC" })} ${pad(d.getUTCHours())}Z`; };
  const local = (t) => new Date(t).toLocaleString("en-US", { weekday: "short", hour: "numeric", timeZoneName: "short" });

  const params = new URLSearchParams(location.search);
  const state = {
    info: null, model: 0, product: null, hour: 0, timer: null,
    view: ["windows", "confidence"].includes(params.get("view")) ? params.get("view") : params.get("view") === "forecast" || params.get("loc") ? "forecast" : "maps",
    work: null,
    point: null, pointError: null, openPeriod: null, query: "",
    compare: params.get("compare") === "1", wpc: { qpf: false, ero: false, spc: false, cpc: false, eroDay: 0, spcDay: 0, cpcPick: 0 },
  };

  // —— Map ————————————————————————————————————————————————————————
  const map = L.map("map", { zoomControl: false, minZoom: 3, maxZoom: 10, zoomSnap: 0.5 });
  map.fitBounds([[24, -125], [50, -66]], { animate: false });
  L.control.zoom({ position: "topright" }).addTo(map);
  map.attributionControl.setPrefix("");
  [["model", 350], ["points", 620], ["labels", 640]].forEach(([n, z]) => { map.createPane(n).style.zIndex = z; });
  map.getPane("labels").style.pointerEvents = "none";
  map.getPane("model").style.pointerEvents = "none";
  L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}", { maxZoom: 16, attribution: "Esri" }).addTo(map);
  L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Reference/MapServer/tile/{z}/{y}/{x}", { maxZoom: 16, pane: "labels", opacity: 0.85 }).addTo(map);
  map.attributionControl.addAttribution("NOAA");
  const marker = L.layerGroup().addTo(map);
  let overlay = null;

  function setStatus(msg) { $("status").textContent = msg || ""; $("status").hidden = !msg; }

  const model = () => state.info.models[state.model];
  const product = () => model().products.find((p) => p.id === state.product) || model().products[0];
  const imgUrl = (m, pid, fhr) => `img/${m.id}/${pid}_f${pad(fhr, 3)}.png?run=${encodeURIComponent(m.cycle)}`;
  const validAt = (m, fhr) => new Date(m.init).getTime() + fhr * 3600e3;

  // Swap the overlay only once the next image has loaded, so the map never flashes empty.
  let want = null;
  function drawOverlay() {
    const m = model(), p = product(), fhr = m.hours[state.hour];
    const url = imgUrl(m, p.id, fhr);
    want = url;
    const img = new Image();
    img.onload = () => {
      if (want !== url) return;
      if (!overlay) overlay = L.imageOverlay(url, state.info.bounds, { pane: "model", opacity: 0.78, interactive: false }).addTo(map);
      else overlay.setUrl(url);
      setStatus("");
    };
    img.onerror = () => { if (want === url) setStatus("This map isn't available."); };
    img.src = url;
    drawCompare();
  }

  // —— Compare: GFS and ECMWF side by side, at the same valid time ————————————————
  const COMPARE = { gfs: "ecmwf", ecmwf: "gfs" };
  let map2 = null, overlay2 = null, syncing = false;
  /** The other model of the pair, when it has this product. */
  function partner() {
    const other = state.info?.models.find((x) => x.id === COMPARE[model().id]);
    return other && other.products.some((p) => p.id === product().id) ? other : null;
  }
  function nearestHour(m, t) {
    let best = m.hours[0];
    m.hours.forEach((h) => { if (Math.abs(validAt(m, h) - t) < Math.abs(validAt(m, best) - t)) best = h; });
    return best;
  }
  function drawCompare() {
    const other = state.compare ? partner() : null;
    $("map").parentElement.classList.toggle("split", !!other);
    $("tag1").hidden = $("tag2").hidden = !other;
    if (!other) { if (map2) setTimeout(() => map.invalidateSize(), 0); return; }
    if (!map2) {
      map2 = L.map("map2", { zoomControl: false, attributionControl: false, minZoom: 3, maxZoom: 10, zoomSnap: 0.5 });
      map2.createPane("model").style.zIndex = 350;
      map2.createPane("labels").style.zIndex = 640;
      map2.getPane("labels").style.pointerEvents = "none";
      L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}", { maxZoom: 16 }).addTo(map2);
      L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Reference/MapServer/tile/{z}/{y}/{x}", { maxZoom: 16, pane: "labels", opacity: 0.85 }).addTo(map2);
      const follow = (from, to) => from.on("move", () => { if (syncing) return; syncing = true; to.setView(from.getCenter(), from.getZoom(), { animate: false }); syncing = false; });
      follow(map, map2);
      follow(map2, map);
      map2.on("click", (e) => map.fire("click", e));
    }
    setTimeout(() => { map.invalidateSize(); map2.invalidateSize(); map2.setView(map.getCenter(), map.getZoom(), { animate: false }); }, 0);
    const m = model(), t = validAt(m, m.hours[state.hour]), h2 = nearestHour(other, t);
    const url = imgUrl(other, product().id, h2);
    if (!overlay2) overlay2 = L.imageOverlay(url, state.info.bounds, { pane: "model", opacity: 0.78, interactive: false }).addTo(map2);
    else overlay2.setUrl(url);
    const tag = (x, h) => `${x.name} ${pad(new Date(x.init).getUTCHours())}Z · F${pad(h, 3)}`;
    $("tag1").textContent = tag(m, m.hours[state.hour]);
    $("tag2").textContent = tag(other, h2) + (validAt(other, h2) !== t ? " (nearest)" : "");
  }

  // —— Outlook overlays: WPC rainfall, SPC severe and CPC days 6–14 (live from NOAA map services) ——————————
  const MAPSERVICES = "https://mapservices.weather.noaa.gov/vector/rest/services";
  const WPC_QPF = `${MAPSERVICES}/precip/wpc_qpf/MapServer`;
  const WPC_ERO = `${MAPSERVICES}/hazards/wpc_precip_hazards/MapServer`;
  const SPC_OUTLOOK = `${MAPSERVICES}/outlooks/SPC_wx_outlks/MapServer`;
  const SPC_LAYERS = [1, 9, 17, 21, 22, 23, 24, 25];   // days 1–3 categorical, days 4–8 probabilistic
  const CPC = [["6_10", 0, "6–10 day temperature"], ["6_10", 1, "6–10 day precipitation"], ["8_14", 0, "8–14 day temperature"], ["8_14", 1, "8–14 day precipitation"]];
  /** Calendar day an SPC outlook day covers (each runs 12 UTC to 12 UTC). */
  const spcDate = (n) => new Date(Date.now() - 12 * 3600e3 + n * 86_400_000).toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric", timeZone: "UTC" });
  const OUTLOOKS = {
    qpf: { credit: "WPC", opacity: 0.65, src: () => [WPC_QPF, 11], title: () => "WPC rainfall, next 7 days (in)" },
    ero: { credit: "WPC", opacity: 0.55, src: () => [WPC_ERO, state.wpc.eroDay], title: () => `Excessive rainfall risk, day ${state.wpc.eroDay + 1}` },
    spc: { credit: "SPC", opacity: 0.55, src: () => [SPC_OUTLOOK, SPC_LAYERS[state.wpc.spcDay]],
      title: () => `SPC severe weather outlook, day ${state.wpc.spcDay + 1} (${spcDate(state.wpc.spcDay)})${state.wpc.spcDay >= 3 ? " · chance of severe storms" : ""}` },
    cpc: { credit: "CPC", opacity: 0.55, src: () => [`${MAPSERVICES}/outlooks/cpc_${CPC[state.wpc.cpcPick][0]}_day_outlk/MapServer`, CPC[state.wpc.cpcPick][1]],
      title: () => `CPC ${CPC[state.wpc.cpcPick][2]} outlook · chance of above or below normal` },
  };
  map.createPane("wpc").style.zIndex = 360;
  map.getPane("wpc").style.pointerEvents = "none";
  const ExportLayer = L.TileLayer.extend({
    getTileUrl(c) {
      const size = this.getTileSize();
      const a = L.CRS.EPSG3857.project(map.unproject([c.x * size.x, c.y * size.y], c.z));
      const b = L.CRS.EPSG3857.project(map.unproject([(c.x + 1) * size.x, (c.y + 1) * size.y], c.z));
      return `${this.options.base}/export?bbox=${a.x},${b.y},${b.x},${a.y}&bboxSR=3857&imageSR=3857&size=${size.x},${size.y}&layers=show:${this.options.ids}&format=png32&transparent=true&dpi=96&f=image&_=${Math.floor(Date.now() / 1800e3)}`;
    },
  });
  const wpcLayers = {};
  const wpcLegends = {};
  const WpcLegend = L.Control.extend({ options: { position: "topleft" }, onAdd() { const d = L.DomUtil.create("div", "maplegend"); d.id = "wpcLegend"; return d; } });
  new WpcLegend().addTo(map);
  async function drawWpc() {
    const on = Object.keys(OUTLOOKS).filter((k) => state.wpc[k]);
    for (const k of Object.keys(OUTLOOKS)) {
      wpcLayers[k]?.remove();
      delete wpcLayers[k];
      if (!state.wpc[k]) continue;
      const [base, id] = OUTLOOKS[k].src();
      wpcLayers[k] = new ExportLayer("", { base, ids: id, pane: "wpc", opacity: OUTLOOKS[k].opacity, attribution: "" }).addTo(map);
    }
    for (const credit of ["WPC", "SPC", "CPC"]) {
      if (on.some((k) => OUTLOOKS[k].credit === credit)) map.attributionControl.addAttribution(credit);
      else map.attributionControl.removeAttribution(credit);
    }
    $("eroDay").hidden = !state.wpc.ero;
    $("spcDay").hidden = !state.wpc.spc;
    $("cpcPick").hidden = !state.wpc.cpc;
    const parts = await Promise.all(on.map(async (k) => {
      const [base, id] = OUTLOOKS[k].src();
      wpcLegends[base] ||= fetch(`${base}/legend?f=json`).then((r) => r.json()).then((d) => Object.fromEntries(d.layers.map((l) => [l.layerId, l]))).catch(() => ({}));
      const lg = (await wpcLegends[base])[id];
      let items = (lg?.legend || []).filter((x) => x.label !== "0");
      if (k === "qpf") items = items.slice().reverse();
      // CPC lists 15 shades; keep every other one so the key stays one or two lines.
      if (k === "cpc") items = items.filter((x, i) => i % 2 === 0 || /Near/.test(x.label));
      return `<h5 class="sub">${esc(OUTLOOKS[k].title())}</h5><div class="key">${items.map((x) =>
        `<span><img alt="" src="data:${x.contentType};base64,${x.imageData}" width="12" height="12" style="vertical-align:-2px;margin-right:.3rem">${esc((x.label || "").replace(" (inches)", "").replace(/ \(At Least (\d+)%\)/, " ($1%+)"))}</span>`).join("")}</div>`;
    }));
    $("wpcLegend").innerHTML = parts.join("");
  }
  $("overlays").addEventListener("change", (e) => { const k = e.target.dataset.wpc; if (k) { state.wpc[k] = e.target.checked; drawWpc(); } });
  document.querySelectorAll("[data-pick]").forEach((group) => group.addEventListener("click", (e) => {
    const v = e.target.closest("[data-v]")?.dataset.v;
    if (v == null) return;
    state.wpc[group.dataset.pick] = +v;
    group.querySelectorAll("[data-v]").forEach((b) => b.setAttribute("aria-pressed", b.dataset.v === v));
    drawWpc();
  }));

  function preload() {
    const m = model(), p = product();
    m.hours.forEach((h) => { new Image().src = imgUrl(m, p.id, h); });
  }

  // —— Legend ———————————————————————————————————————————————————————
  const Legend = L.Control.extend({ options: { position: "topleft" }, onAdd() { const d = L.DomUtil.create("div", "maplegend"); d.id = "legend"; return d; } });
  new Legend().addTo(map);
  function drawLegend() {
    const lg = product().legend;
    if (!lg) { $("legend").innerHTML = ""; return; }
    const n = lg.colors.length;
    // Label every edge when there's room, otherwise every second or third.
    const wide = lg.levels.some((v) => String(v).length >= 4);
    const every = n > 18 ? 6 : n > 10 || (wide && n > 6) ? 2 : 1;
    const ticks = lg.levels.map((v, i) => (i % every === 0 ? `<span style="left:${(i / n) * 100}%">${v}</span>` : "")).join("");
    $("legend").innerHTML = `<h5 class="sub">${esc(lg.label)}</h5>
      <div class="bins">${lg.colors.map((c) => `<i style="background:${c}"></i>`).join("")}</div><div class="ticks">${ticks}</div>
      ${lg.extra ? `<div class="key">${lg.extra.map(([c, l]) => `<span><i style="background:${c}"></i>${esc(l)}</span>`).join("")}</div>` : ""}
      ${lg.lines ? `<p class="lines">${esc(lg.lines)}</p>` : ""}`;
  }

  // —— Time controls ————————————————————————————————————————————————
  function drawHour() {
    const m = model(), fhr = m.hours[state.hour];
    $("hour").max = m.hours.length - 1;
    $("hour").value = state.hour;
    $("hourLabel").innerHTML = `F${pad(fhr, 3)} · ${dayZ(validAt(m, fhr))}<small>${esc(local(validAt(m, fhr)))}</small>`;
    drawOverlay();
    syncUrl();
  }
  function step(d) { const n = model().hours.length; state.hour = (state.hour + d + n) % n; drawHour(); }
  function togglePlay(force) {
    const playing = force ?? !state.timer;
    clearInterval(state.timer);
    state.timer = playing ? setInterval(() => step(1), 650) : null;
    $("play").textContent = playing ? "❚❚" : "▶";
    $("play").setAttribute("aria-label", playing ? "Pause" : "Play");
  }
  $("prev").onclick = () => { togglePlay(false); step(-1); };
  $("next").onclick = () => { togglePlay(false); step(1); };
  $("play").onclick = () => togglePlay();
  $("hour").oninput = (e) => { togglePlay(false); state.hour = +e.target.value; drawHour(); };
  document.addEventListener("keydown", (e) => {
    if (e.target.matches("input, textarea") && e.target.type !== "range") return;
    if (e.key === "ArrowLeft") { e.preventDefault(); togglePlay(false); step(-1); }
    if (e.key === "ArrowRight") { e.preventDefault(); togglePlay(false); step(1); }
  });

  // Switching models keeps the same valid time when the new model has it, or the nearest one.
  function setModel(i) {
    const before = state.info ? validAt(model(), model().hours[state.hour]) : null;
    state.model = i;
    const m = model();
    if (!m.products.some((p) => p.id === state.product)) state.product = m.products[0].id;
    if (before != null) {
      let best = 0;
      m.hours.forEach((h, j) => { if (Math.abs(validAt(m, h) - before) < Math.abs(validAt(m, m.hours[best]) - before)) best = j; });
      state.hour = best;
    }
    document.querySelectorAll("#models [data-model]").forEach((b) => b.setAttribute("aria-pressed", b.dataset.model === m.id));
    $("run").textContent = `${m.name} ${pad(new Date(m.init).getUTCHours())}Z ${new Date(m.init).toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" })}`;
    drawLegend();
    drawHour();
    preload();
    render();
  }

  function syncUrl() {
    if (!state.info) return;
    const q = new URLSearchParams();
    q.set("model", model().id);
    q.set("product", product().id);
    q.set("f", model().hours[state.hour]);
    if (state.point?.lat != null) q.set("loc", `${state.point.lat.toFixed(3)},${state.point.lon.toFixed(3)}`);
    if (state.view !== "maps") q.set("view", state.view);
    if (state.view === "windows") {
      q.set("job", state.work.preset);
      const base = WORK[state.work.preset];
      WORK_FIELDS.forEach(([k]) => { if ((state.work[k] ?? "") !== (base[k] ?? "")) q.set(k, state.work[k]); });
      if (state.work.day !== base.day) q.set("day", state.work.day ? "1" : "0");
    }
    if (state.compare) q.set("compare", "1");
    history.replaceState(null, "", `?${q}`);
  }

  // —— Sidebar ——————————————————————————————————————————————————————
  function mapsView() {
    const m = model(), p = product();
    const gen = new Date(m.generated);
    return `${m.products.map((x) => `<button class="prod" data-product="${x.id}" aria-pressed="${x.id === p.id}">${esc(x.name)}<small>${esc(x.legend?.label.split(" (")[0] || "")}</small></button>`).join("")}
      <div class="pad">
        ${p.note ? `<p class="note" style="margin-top:0">${esc(p.note)}</p>` : ""}
        <h5 class="sub">${esc(m.long)} · ${dayZ(m.init)} run</h5>
        <p class="note" style="margin-top:0">${m.hours.length} forecast hours, F${pad(m.hours[0], 3)}–F${pad(m.hours[m.hours.length - 1], 3)}. Processed ${esc(gen.toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZoneName: "short" }))}.
          ${m.note ? esc(m.note) : ""}</p>
        ${partner() ? `<p class="note"><button class="linkish" data-compare="1">${state.compare ? "Close the side-by-side view" : `Compare with ${esc(partner().name)} side by side`}</button></p>` : ""}
        <p class="note">Click anywhere on the map for the National Weather Service forecast there. Use ← → to step through hours.</p>
      </div>`;
  }

  function forecastView() {
    const f = state.point;
    let html = `<form class="search" id="search"><input id="q" placeholder="City, ST · street address · lat, lon" aria-label="Location" autocomplete="off" value="${esc(state.query)}" /><button class="btn btn--secondary">Go</button></form>`;
    if (state.pointError) return html + `<p class="empty">${esc(state.pointError)}</p>`;
    if (f === "loading") return html + `<p class="empty">Loading forecast…</p>`;
    if (!f) return html + `<p class="empty">Search a place or click anywhere on the map for the National Weather Service forecast at that spot.</p>`;
    return html + `<div class="pad"><h5 class="sub">${esc(f.place)} · NWS ${esc(f.office)}${f.updated ? ` · updated ${esc(dayZ(f.updated))}` : ""}</h5>
      ${hourlyChart(f.hourly)}
      <table class="t" style="margin-top:.6rem"><tbody>${f.periods.map((p, i) => `<tr data-period="${i}" style="cursor:pointer"><td style="width:6.5rem"><b style="color:#fff">${esc(p.name)}</b><small>${p.probabilityOfPrecipitation?.value ? `${p.probabilityOfPrecipitation.value}% precip` : ""}</small></td>
        <td style="width:3rem;color:${p.isDaytime ? "#ffbf47" : "#7cb8ff"}">${p.temperature}°</td><td>${esc(p.shortForecast)}<small>${esc(p.windDirection)} ${esc(p.windSpeed)}</small>
        ${state.openPeriod === i ? `<small style="color:var(--text2);margin-top:.3rem">${esc(p.detailedForecast)}</small>` : ""}</td></tr>`).join("")}</tbody></table>
      <h5 class="sub" style="margin-top:1rem">7-day meteogram</h5>
      ${f.grid ? meteogram(f.grid) : `<p class="note">Loading the forecast grid…</p>`}
      <p class="note">How sure is it? <button class="linkish" data-goview="confidence">See forecast confidence</button> from NOAA's 31-member ensemble.</p>
      <p class="note">The NWS forecast starts from the National Blend of Models, adjusted by local forecasters. Tap a day for details.
        <a href="${esc(f.link)}" target="_blank" rel="noopener">Full forecast at weather.gov ↗</a></p></div>`;
  }

  /** Hourly series from the NWS forecast grid ("2026-10-20T18:00:00+00:00/PT3H" intervals spread over each hour). */
  function gridSeries(prop, convert = (v) => v, spread = false) {
    const out = new Map();
    for (const { validTime, value } of prop?.values || []) {
      if (value == null) continue;
      const [start, dur] = validTime.split("/");
      const m = dur.match(/P(?:(\d+)D)?T?(?:(\d+)H)?/);
      const hours = Math.max(1, (+(m?.[1] || 0)) * 24 + (+(m?.[2] || 0)));
      const t0 = Date.parse(start);
      for (let i = 0; i < hours; i++) out.set(t0 + i * 3600e3, convert(spread ? value / hours : value));
    }
    return out;
  }
  function meteogram(g) {
    const now = Math.floor(Date.now() / 3600e3) * 3600e3, end = now + 7 * 86400e3;
    const hours = [];
    for (let t = now; t < end; t += 3600e3) hours.push(t);
    const val = (s, t) => (s.has(t) ? s.get(t) : null);
    const W = 336, L0 = 28, R0 = 8, PH = 70, GAP = 16, TOP = 6;
    const x = (t) => L0 + ((t - now) / (end - now)) * (W - L0 - R0);
    const mono = 'font-family="IBM Plex Mono, monospace" font-size="9" fill="rgba(255,255,255,.42)"';
    let svg = "";
    const panel = (i, lo, hi, step, unit) => {
      const top = TOP + i * (PH + GAP), y = (v) => top + (1 - (v - lo) / (hi - lo)) * PH;
      for (let v = lo; v <= hi; v += step) svg += `<line x1="${L0}" x2="${W - R0}" y1="${y(v)}" y2="${y(v)}" stroke="rgba(255,255,255,.07)"/><text x="${L0 - 4}" y="${y(v) + 3}" text-anchor="end" ${mono}>${v}${unit}</text>`;
      return y;
    };
    const line = (s, y, color, w = 1.6) => {
      let d = "", pen = false;
      hours.forEach((t) => { const v = val(s, t); if (v == null) { pen = false; return; } d += `${pen ? "L" : "M"}${x(t).toFixed(1)},${y(v).toFixed(1)}`; pen = true; });
      return d ? `<path d="${d}" fill="none" stroke="${color}" stroke-width="${w}"/>` : "";
    };
    // Panel 1: temperature and dew point.
    const temps = [...hours.map((t) => val(g.temp, t)), ...hours.map((t) => val(g.dew, t))].filter((v) => v != null);
    if (!temps.length) return `<p class="note">No forecast grid for this spot.</p>`;
    const tlo = Math.floor((Math.min(...temps) - 2) / 10) * 10, thi = Math.ceil((Math.max(...temps) + 2) / 10) * 10;
    const y1 = panel(0, tlo, thi, thi - tlo > 40 ? 20 : 10, "°");
    svg += line(g.dew, y1, "#3fcf9a", 1.2) + line(g.temp, y1, "#ffbf47");
    // Panel 2: wind and gusts.
    const winds = hours.map((t) => Math.max(val(g.wind, t) ?? 0, val(g.gust, t) ?? 0));
    const whi = Math.max(20, Math.ceil(Math.max(...winds) / 10) * 10);
    const y2 = panel(1, 0, whi, whi > 40 ? 20 : 10, "");
    svg += line(g.gust, y2, "#ff9548", 1.1) + line(g.wind, y2, "rgba(255,255,255,.75)");
    // Panel 3: precipitation chance (bars) and amount per 6 hours (labels), sky cover (shading).
    const top3 = TOP + 2 * (PH + GAP), y3 = (v) => top3 + (1 - v / 100) * PH;
    [0, 50, 100].forEach((v) => { svg += `<line x1="${L0}" x2="${W - R0}" y1="${y3(v)}" y2="${y3(v)}" stroke="rgba(255,255,255,.07)"/><text x="${L0 - 4}" y="${y3(v) + 3}" text-anchor="end" ${mono}>${v}%</text>`; });
    svg += line(g.sky, y3, "rgba(200,210,240,.35)", 1);
    const bw = (W - L0 - R0) / hours.length;
    hours.forEach((t) => { const p = val(g.pop, t); if (p) svg += `<rect x="${x(t).toFixed(1)}" y="${y3(p).toFixed(1)}" width="${bw.toFixed(2)}" height="${(top3 + PH - y3(p)).toFixed(1)}" fill="rgba(74,209,255,.4)"/>`; });
    let total = 0;
    // Day ticks and daily rain totals.
    const day0 = new Date(now); day0.setHours(24, 0, 0, 0);
    for (let t = day0.getTime() - 86400e3; t < end; t += 86400e3) {
      let sum = 0;
      for (let h = Math.max(t, now); h < Math.min(t + 86400e3, end); h += 3600e3) sum += val(g.qpf, h) ?? 0;
      total += sum;
      if (t >= now) svg += `<line x1="${x(t)}" x2="${x(t)}" y1="${TOP}" y2="${top3 + PH}" stroke="rgba(255,255,255,.12)"/>`;
      const mid = x(Math.max(t, now) + (Math.min(t + 86400e3, end) - Math.max(t, now)) / 2);
      if (Math.min(t + 86400e3, end) - Math.max(t, now) >= 8 * 3600e3) {
        svg += `<text x="${mid.toFixed(1)}" y="${top3 + PH + 11}" text-anchor="middle" ${mono}>${new Date(Math.max(t, now) + 3600e3).toLocaleDateString("en-US", { weekday: "short" })}</text>`;
        if (sum >= 0.01) svg += `<text x="${mid.toFixed(1)}" y="${top3 + 9}" text-anchor="middle" font-family="IBM Plex Mono, monospace" font-size="8.5" fill="#4ad1ff">${sum.toFixed(2)}"</text>`;
      }
    }
    const H = top3 + PH + 16;
    return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Seven-day meteogram">${svg}</svg>
      <p class="chart-key"><span style="color:#ffbf47">━</span> temp <span style="color:#3fcf9a">━</span> dew point (°F) ·
        <span style="color:rgba(255,255,255,.75)">━</span> wind <span style="color:#ff9548">━</span> gusts (mph) ·
        <span style="color:#4ad1ff">▮</span> precip chance, daily rain (in) <span style="color:rgba(200,210,240,.6)">━</span> sky cover · local days</p>
      <p class="note" style="margin-top:.2rem">7-day rain total ${total.toFixed(2)}".</p>`;
  }

  function hourlyChart(h) {
    if (h.length < 2) return "";
    const W = 336, H = 130, L0 = 26, R0 = 24, T0 = 8, B0 = 18;
    const temps = h.map((p) => p.temperature);
    const lo = Math.floor((Math.min(...temps) - 3) / 5) * 5, hi = Math.ceil((Math.max(...temps) + 3) / 5) * 5;
    const x = (i) => L0 + (i / (h.length - 1)) * (W - L0 - R0);
    const y = (v) => T0 + (1 - (v - lo) / (hi - lo)) * (H - T0 - B0);
    const yp = (v) => T0 + (1 - v / 100) * (H - T0 - B0);
    const mono = 'font-family="IBM Plex Mono, monospace" font-size="9.5" fill="rgba(255,255,255,.42)"';
    let svg = "";
    for (let v = lo; v <= hi; v += (hi - lo) > 30 ? 10 : 5) svg += `<line x1="${L0}" x2="${W - R0}" y1="${y(v)}" y2="${y(v)}" stroke="rgba(255,255,255,.07)"/><text x="${L0 - 5}" y="${y(v) + 3}" text-anchor="end" ${mono}>${v}°</text>`;
    [0, 50, 100].forEach((v) => { svg += `<text x="${W - R0 + 4}" y="${yp(v) + 3}" ${mono} fill="#4ad1ff">${v}%</text>`; });
    h.forEach((p, i) => {
      const pop = p.probabilityOfPrecipitation?.value || 0;
      if (pop) svg += `<rect x="${(x(i) - 2.4).toFixed(1)}" y="${yp(pop).toFixed(1)}" width="4.8" height="${(H - B0 - yp(pop)).toFixed(1)}" fill="rgba(74,209,255,.35)"/>`;
      const d = new Date(p.startTime);
      if (d.getHours() % 12 === 0) svg += `<text x="${x(i).toFixed(1)}" y="${H - 4}" text-anchor="middle" ${mono}>${d.toLocaleTimeString("en-US", { hour: "numeric" }).replace(":00", "")}</text>`;
    });
    svg += `<polyline fill="none" stroke="#ffbf47" stroke-width="2" points="${h.map((p, i) => `${x(i).toFixed(1)},${y(p.temperature).toFixed(1)}`).join(" ")}"/>`;
    return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Hourly temperature and precipitation chance">${svg}</svg>
      <p class="chart-key"><span style="color:#ffbf47">━</span> temperature &nbsp;<span style="color:#4ad1ff">▮</span> chance of precipitation · next 48 hours, local time</p>`;
  }

  // —— Work windows: hours in the next 7 days that meet a job's weather limits (NWS forecast grid) ——————————
  // Presets are common rules of thumb, not standards; every limit can be changed.
  const WORK = {
    concrete: { name: "Concrete pour", tmin: 40, tmax: 90, wind: 25, gust: 35, pop: 20, rain: 0, hours: 8, day: true, note: "ACI 306 and 305 treat below 40°F as cold-weather and above 90°F as hot-weather concreting." },
    crane: { name: "Crane lift", tmin: "", tmax: "", wind: 20, gust: 30, pop: 30, rain: 0.05, hours: 4, day: true, note: "Many crane charts limit lifts near 20 mph sustained; check the manufacturer's load chart. Forecast winds are at 33 ft, so winds at the boom tip are usually stronger." },
    roofing: { name: "Roofing", tmin: 40, tmax: "", wind: 25, gust: 35, pop: 20, rain: 0, hours: 6, day: true, note: "Many shingle makers call for 40°F and up for proper sealing." },
    paint: { name: "Exterior paint", tmin: 50, tmax: 90, wind: 15, gust: 25, pop: 10, rain: 0, rh: 85, hours: 6, day: true, note: "Most exterior coatings want 50°F and up, humidity under 85% and no rain for several hours after." },
    drone: { name: "Drone flight", tmin: 32, tmax: 100, wind: 15, gust: 22, pop: 20, rain: 0, hours: 2, day: true, thunder: 10, note: "FAA Part 107 allows daylight and civil twilight flights; check airspace and the drone's wind limit." },
    custom: { name: "Custom", tmin: "", tmax: "", wind: 20, gust: "", pop: 30, rain: "", hours: 4, day: false, note: "" },
  };
  const WORK_FIELDS = [["tmin", "Min temp °F"], ["tmax", "Max temp °F"], ["wind", "Max wind mph"], ["gust", "Max gust mph"], ["pop", "Max precip chance %"], ["rain", "Max rain in/hr"], ["rh", "Max humidity %"], ["thunder", "Max thunder chance %"], ["hours", "Shortest window, hours"]];
  function workFromUrl() {
    const k = params.get("job");
    const w = { preset: WORK[k] ? k : "concrete", ...WORK[WORK[k] ? k : "concrete"] };
    WORK_FIELDS.forEach(([f]) => { if (params.has(f)) w[f] = params.get(f) === "" ? "" : +params.get(f); });
    if (params.has("day")) w.day = params.get("day") === "1";
    return w;
  }
  state.work = workFromUrl();
  const lim = (v) => (v === "" || v == null || !Number.isFinite(+v) ? null : +v);

  /** Each hour in the next 7 days with what (if anything) rules it out. Daylight comes from the sun's elevation. */
  function workHours(pt, w) {
    const g = pt.grid, now = Math.floor(Date.now() / 3600e3) * 3600e3, out = [];
    const L = Object.fromEntries(WORK_FIELDS.map(([f]) => [f, lim(w[f])]));
    for (let t = now; t < now + 7 * 86400e3; t += 3600e3) {
      const v = (k) => (g[k]?.has(t) ? g[k].get(t) : null);
      const temp = v("temp");
      if (temp == null) { out.push({ t, fail: ["no forecast"], nodata: true }); continue; }
      const fail = [];
      if (w.day && sunElevation(t + 1800e3, pt.lat, pt.lon) < -0.833) fail.push("dark");
      if (L.tmin != null && temp < L.tmin) fail.push(`${Math.round(temp)}°F`);
      if (L.tmax != null && temp > L.tmax) fail.push(`${Math.round(temp)}°F`);
      if (L.wind != null && (v("wind") ?? 0) > L.wind) fail.push(`wind ${Math.round(v("wind"))} mph`);
      if (L.gust != null && (v("gust") ?? 0) > L.gust) fail.push(`gusts ${Math.round(v("gust"))} mph`);
      if (L.pop != null && (v("pop") ?? 0) > L.pop) fail.push(`${Math.round(v("pop"))}% precip`);
      if (L.rain != null && (v("qpf") ?? 0) > L.rain + 1e-6) fail.push(`${(v("qpf") ?? 0).toFixed(2)}" rain`);
      if (L.rh != null && (v("rh") ?? 0) > L.rh) fail.push(`${Math.round(v("rh"))}% humidity`);
      if (L.thunder != null && (v("thunder") ?? 0) > L.thunder) fail.push(`${Math.round(v("thunder"))}% thunder`);
      out.push({ t, fail, temp, wind: v("wind"), gust: v("gust"), pop: v("pop") });
    }
    return out;
  }
  /** Solar elevation in degrees (NOAA's simplified equations, good to a fraction of a degree). */
  function sunElevation(ms, lat, lon) {
    const d = ms / 86400e3 - 10957.5, r = Math.PI / 180;
    const g = (357.529 + 0.98560028 * d) * r, q = 280.459 + 0.98564736 * d;
    const L = (q + 1.915 * Math.sin(g) + 0.02 * Math.sin(2 * g)) * r, e = (23.439 - 0.00000036 * d) * r;
    const dec = Math.asin(Math.sin(e) * Math.sin(L));
    const ra = Math.atan2(Math.cos(e) * Math.sin(L), Math.cos(L)) / r;
    const gmst = (18.697374558 + 24.06570982441908 * d) % 24;
    const ha = ((gmst * 15 + lon - ra) % 360) * r;
    return Math.asin(Math.sin(lat * r) * Math.sin(dec) + Math.cos(lat * r) * Math.cos(dec) * Math.cos(ha)) / r;
  }
  function workWindows(hours, minH) {
    const out = [];
    let run = null;
    hours.forEach((h) => {
      if (!h.fail.length) { if (!run) run = { start: h.t, end: h.t + 3600e3, hrs: [] }; run.end = h.t + 3600e3; run.hrs.push(h); }
      else { if (run) out.push(run); run = null; }
    });
    if (run) out.push(run);
    return out.filter((r) => r.hrs.length >= Math.max(1, minH || 1));
  }

  function workSummary(w) {
    const L = (k) => lim(w[k]);
    return [L("tmin") != null && L("tmax") != null ? `${L("tmin")}–${L("tmax")}°F` : L("tmin") != null ? `${L("tmin")}°F+` : L("tmax") != null ? `≤ ${L("tmax")}°F` : "",
      L("wind") != null ? `wind ≤ ${L("wind")}` : "", L("gust") != null ? `gusts ≤ ${L("gust")}` : "", L("pop") != null ? `precip ≤ ${L("pop")}%` : "",
      L("rh") != null ? `humidity ≤ ${L("rh")}%` : "", L("thunder") != null ? `thunder ≤ ${L("thunder")}%` : "", w.day ? "daylight" : "", L("hours") ? `${L("hours")}+ h` : ""].filter(Boolean).join(" · ") || "none";
  }
  function windowsView() {
    const f = state.point, w = state.work;
    let html = `<form class="search" id="search"><input id="q" placeholder="City, ST · street address · lat, lon" aria-label="Location" autocomplete="off" value="${esc(state.query)}" /><button class="btn btn--secondary">Go</button></form>
      <div class="pad"><p class="note" style="margin-top:0">Find the hours in the next 7 days when the forecast meets a job's weather limits.</p>
      <div class="chips" role="group" aria-label="Job" style="padding:0;margin-top:.6rem">${Object.entries(WORK).map(([k, v]) => `<button data-job="${k}" aria-pressed="${k === w.preset}">${esc(v.name)}</button>`).join("")}</div>
      <details class="limits-box"${state.workOpen ? " open" : ""}><summary>Limits: ${esc(workSummary(w))}</summary>
      <form id="limits" class="limits">${WORK_FIELDS.map(([k, label]) => `<label>${label}<input name="${k}" inputmode="decimal" value="${esc(w[k] ?? "")}" placeholder="any" /></label>`).join("")}
        <label class="day"><input type="checkbox" name="day"${w.day ? " checked" : ""} /> Daylight only</label></form></details>
      ${WORK[w.preset].note ? `<p class="note">${esc(WORK[w.preset].note)}</p>` : ""}</div>`;
    if (state.pointError) return html + `<p class="empty">${esc(state.pointError)}</p>`;
    if (f === "loading") return html + `<p class="empty">Loading forecast…</p>`;
    if (!f) return html + `<p class="empty">Search a place or click the map to check it.</p>`;
    if (!f.grid) return html + `<p class="empty">Loading the forecast grid for ${esc(f.place)}…</p>`;
    if (!f.ens) loadConfidence(f);
    const hours = workHours(f, w);
    if (hours.every((h) => h.nodata)) return html + `<p class="empty">No forecast grid for this spot.</p>`;
    const wins = workWindows(hours, lim(w.hours));
    const tz = f.tz || undefined;
    const fmt = (t, o) => new Date(t).toLocaleString("en-US", { timeZone: tz, ...o });
    const span = (r) => `${fmt(r.start, { weekday: "short", hour: "numeric" })}–${fmt(r.end, { hour: "numeric", timeZoneName: "short" })}`;
    // Why hours fail, most common first.
    const why = {};
    hours.forEach((h) => h.fail.filter((x) => x !== "dark").forEach((x) => { const k = x.replace(/[\d.]+(°F|%|")?\s*/g, "").trim() || "temperature"; why[k] = (why[k] || 0) + 1; }));
    const ok = hours.filter((h) => !h.fail.length).length;
    html += `<div class="pad" style="padding-top:0"><h5 class="sub">${esc(f.place)} · next 7 days</h5>`;
    html += wins.length
      ? `<table class="t"><tbody>${wins.map((r) => { const odds = f.ens?.tmax ? ensWindowOdds(f.ens, w, r) : null; return `<tr><td><b style="color:#fff">${esc(span(r))}</b>${odds ? `<span class="odds" title="GEFS ensemble members that keep this window within the temperature, wind and rain limits">${odds.ok} of ${odds.n} members agree</span>` : ""}<small>${r.hrs.length} h · ${Math.round(Math.min(...r.hrs.map((h) => h.temp)))}–${Math.round(Math.max(...r.hrs.map((h) => h.temp)))}°F · wind to ${Math.round(Math.max(...r.hrs.map((h) => h.wind ?? 0)))} mph${r.hrs.some((h) => h.gust != null) ? `, gusts ${Math.round(Math.max(...r.hrs.map((h) => h.gust ?? 0)))}` : ""} · precip ≤ ${Math.round(Math.max(...r.hrs.map((h) => h.pop ?? 0)))}%</small></td></tr>`; }).join("")}</tbody></table>`
      : `<p class="note"><b>No window of ${lim(w.hours) || 1}+ hours</b> meets these limits in the next 7 days.</p>`;
    // Day-by-hour strip: green hours qualify.
    const days = [];
    hours.forEach((h) => { const k = fmt(h.t, { weekday: "short", month: "numeric", day: "numeric" }); (days.at(-1)?.k === k ? days.at(-1) : (days.push({ k, hrs: [] }), days.at(-1))).hrs.push(h); });
    html += `<div class="strip" aria-label="Hours that meet the limits">${days.map((d) => `<div class="strip__row"><span>${esc(d.k)}</span><div>${Array.from({ length: 24 }, (_, i) => {
      const h = d.hrs.find((x) => +fmt(x.t, { hour: "numeric", hourCycle: "h23" }) === i);
      if (!h) return `<i class="na"></i>`;
      const cls = h.nodata ? "na" : h.fail.length ? (h.fail.length === 1 && h.fail[0] === "dark" ? "dark" : "no") : "ok";
      return `<i class="${cls}" title="${esc(fmt(h.t, { weekday: "short", hour: "numeric" }))}: ${esc(h.fail.length ? h.fail.join(", ") : "OK")}"></i>`;
    }).join("")}</div></div>`).join("")}
      <div class="strip__row"><span></span><div class="strip__ticks"><b>12a</b><b>6a</b><b>12p</b><b>6p</b></div></div></div>
      <p class="chart-key"><span style="color:#3fcf9a">▮</span> meets limits <span style="color:#ff6b5a">▮</span> doesn't <span style="color:rgba(255,255,255,.25)">▮</span> dark · ${esc(fmt(Date.now(), { timeZoneName: "short" }).split(" ").pop())}. Hover an hour for the reason.</p>
      <p class="note">${ok} of ${hours.length} hours qualify.${Object.keys(why).length ? ` Most often ruled out by ${Object.entries(why).sort((a, b) => b[1] - a[1]).slice(0, 3).map(([k, n]) => `${esc(k)} (${n} h)`).join(", ")}.` : ""}
        From the NWS forecast grid, updated ${esc(dayZ(f.updated))}.${f.ens?.tmax ? ` “Members agree” counts the GEFS ensemble's 31 runs that stay within the temperature, wind and rain limits over the window's 6-hour steps.` : ""} Forecast winds are sustained at 33 ft; gusts and rain timing get less certain after 3 days.
        <button class="linkish" data-sharework="1">Copy link</button></p></div>`;
    return html;
  }

  function setTabs() { document.querySelectorAll("#views button").forEach((b) => b.setAttribute("aria-selected", b.dataset.view === state.view)); }
  function render() {
    const views = { forecast: forecastView, windows: windowsView, confidence: confidenceView };
    if (!state.info) { if (state.view !== "maps") $("panel").innerHTML = (views[state.view] || forecastView)(); return; }
    $("panel").innerHTML = (views[state.view] || mapsView)();
  }
  $("views").addEventListener("click", (e) => {
    const v = e.target.closest("[data-view]")?.dataset.view;
    if (!v) return;
    state.view = v; setTabs(); render(); syncUrl();
    if (v !== "maps" && !state.point) $("q")?.focus();
  });
  $("panel").addEventListener("click", (e) => {
    const job = e.target.closest("[data-job]")?.dataset.job;
    if (job) { state.work = { preset: job, ...WORK[job] }; render(); syncUrl(); return; }
    if (e.target.closest("[data-sharework]")) { navigator.clipboard?.writeText(location.href).then(() => { setStatus("Link copied"); setTimeout(() => setStatus(""), 2000); }); return; }
    const go = e.target.closest("[data-goview]")?.dataset.goview;
    if (go) { state.view = go; setTabs(); render(); syncUrl(); return; }
    if (e.target.closest("[data-compare]")) { state.compare = !state.compare; drawCompare(); render(); syncUrl(); return; }
    const b = e.target.closest("[data-product], [data-period]");
    if (!b) return;
    if (b.dataset.product) { state.product = b.dataset.product; drawLegend(); drawHour(); preload(); render(); return; }
    const i = +b.dataset.period;
    state.openPeriod = state.openPeriod === i ? null : i;
    render();
  });
  $("panel").addEventListener("change", (e) => {
    const form = e.target.closest("#limits");
    if (!form) return;
    state.workOpen = true;
    const w = state.work;
    WORK_FIELDS.forEach(([k]) => { const v = form.elements[k].value.trim(); w[k] = v === "" ? "" : +v; });
    w.day = form.elements.day.checked;
    render(); syncUrl();
  });
  $("panel").addEventListener("submit", async (e) => {
    if (e.target.id !== "search") return;
    e.preventDefault();
    state.query = $("q").value;
    state.pointError = null;
    state.point = "loading";
    render();
    try {
      const hit = await geocodePlace(state.query);
      map.setView([hit.lat, hit.lon], Math.max(map.getZoom(), 6), { animate: false });
      await showForecast(hit.lat, hit.lon);
    } catch (err) { state.point = null; state.pointError = err.message; render(); }
  });
  $("models").addEventListener("click", (e) => {
    const id = e.target.closest("[data-model]")?.dataset.model;
    if (id) setModel(state.info.models.findIndex((m) => m.id === id));
  });

  // —— Point forecast (NWS API) ———————————————————————————————————————
  map.on("click", (e) => {
    if (state.view !== "windows") state.view = "forecast";
    setTabs();
    showForecast(e.latlng.lat, e.latlng.lng);
  });

  async function showForecast(lat, lon) {
    state.point = "loading";
    state.pointError = null;
    state.openPeriod = null;
    marker.clearLayers();
    L.circleMarker([lat, lon], { pane: "points", radius: 6, color: "#fff", weight: 2, fillColor: "#4ad1ff", fillOpacity: 1, interactive: false }).addTo(marker);
    render();
    try {
      const res = await fetch(`${NWS}/points/${lat.toFixed(4)},${lon.toFixed(4)}`);
      const pr = res.ok ? (await res.json()).properties : null;
      if (!pr?.forecast) throw new Error("The National Weather Service doesn't forecast for this spot. Point forecasts cover the U.S. and its territories.");
      const [daily, hourly] = await Promise.all([pr.forecast, pr.forecastHourly].map((u) => fetch(u).then((r) => (r.ok ? r.json() : null)).catch(() => null)));
      if (!daily) throw new Error("The NWS forecast didn't load. It sometimes takes a second try.");
      const rel = pr.relativeLocation?.properties;
      state.point = {
        lat, lon, office: pr.gridId, tz: pr.timeZone,
        place: rel ? `${rel.city}, ${rel.state}` : `${lat.toFixed(2)}, ${lon.toFixed(2)}`,
        link: `https://forecast.weather.gov/MapClick.php?lat=${lat.toFixed(4)}&lon=${lon.toFixed(4)}`,
        updated: daily.properties?.updateTime, periods: daily.properties?.periods || [], hourly: (hourly?.properties?.periods || []).slice(0, 48),
        grid: null,
      };
      const pt = state.point;
      try { localStorage.setItem("sw-place", JSON.stringify({ label: state.query && !parseLatLon(state.query) ? state.query : pt.place, lat, lon })); } catch { /* private mode */ }
      fetch(pr.forecastGridData).then((r) => r.json()).then((d) => {
        if (state.point !== pt) return;
        const p = d.properties, f2c = (c) => c * 9 / 5 + 32, kmh = (v) => v / 1.60934;
        pt.grid = {
          temp: gridSeries(p.temperature, f2c), dew: gridSeries(p.dewpoint, f2c),
          wind: gridSeries(p.windSpeed, kmh), gust: gridSeries(p.windGust, kmh),
          sky: gridSeries(p.skyCover), pop: gridSeries(p.probabilityOfPrecipitation),
          qpf: gridSeries(p.quantitativePrecipitation, (mm) => mm / 25.4, true),
          rh: gridSeries(p.relativeHumidity), thunder: gridSeries(p.probabilityOfThunder),
        };

        if (state.view !== "maps") render();
      }).catch(() => { if (state.point === pt) { pt.grid = { temp: new Map(), dew: new Map() }; if (state.view !== "maps") render(); } });
    } catch (err) {
      state.point = null;
      state.pointError = err.message;
    }
    if (state.view !== "maps") render();
    syncUrl();
  }

  // —— Place search (Census TIGERweb, USGS GNIS, Census Geocoder; same as PastWeather) ——————
  function parseLatLon(text) {
    const m = text.trim().match(/^(-?\d+(?:\.\d+)?)\s*[, ]\s*(-?\d+(?:\.\d+)?)$/);
    if (!m) return null;
    const lat = Number(m[1]), lon = Number(m[2]);
    if (!Number.isFinite(lat) || !Number.isFinite(lon) || lat < -90 || lat > 90 || lon < -180 || lon > 180) return null;
    return { lat, lon };
  }
  const STATES = [
    ["AL", "01", "Alabama"], ["AK", "02", "Alaska"], ["AZ", "04", "Arizona"], ["AR", "05", "Arkansas"],
    ["CA", "06", "California"], ["CO", "08", "Colorado"], ["CT", "09", "Connecticut"], ["DE", "10", "Delaware"],
    ["DC", "11", "District of Columbia"], ["FL", "12", "Florida"], ["GA", "13", "Georgia"], ["HI", "15", "Hawaii"],
    ["ID", "16", "Idaho"], ["IL", "17", "Illinois"], ["IN", "18", "Indiana"], ["IA", "19", "Iowa"],
    ["KS", "20", "Kansas"], ["KY", "21", "Kentucky"], ["LA", "22", "Louisiana"], ["ME", "23", "Maine"],
    ["MD", "24", "Maryland"], ["MA", "25", "Massachusetts"], ["MI", "26", "Michigan"], ["MN", "27", "Minnesota"],
    ["MS", "28", "Mississippi"], ["MO", "29", "Missouri"], ["MT", "30", "Montana"], ["NE", "31", "Nebraska"],
    ["NV", "32", "Nevada"], ["NH", "33", "New Hampshire"], ["NJ", "34", "New Jersey"], ["NM", "35", "New Mexico"],
    ["NY", "36", "New York"], ["NC", "37", "North Carolina"], ["ND", "38", "North Dakota"], ["OH", "39", "Ohio"],
    ["OK", "40", "Oklahoma"], ["OR", "41", "Oregon"], ["PA", "42", "Pennsylvania"], ["RI", "44", "Rhode Island"],
    ["SC", "45", "South Carolina"], ["SD", "46", "South Dakota"], ["TN", "47", "Tennessee"], ["TX", "48", "Texas"],
    ["UT", "49", "Utah"], ["VT", "50", "Vermont"], ["VA", "51", "Virginia"], ["WA", "53", "Washington"],
    ["WV", "54", "West Virginia"], ["WI", "55", "Wisconsin"], ["WY", "56", "Wyoming"], ["PR", "72", "Puerto Rico"],
  ];
  function findState(s) {
    const t = s.trim().replace(/\./g, "").toLowerCase();
    return STATES.find(([code, , name]) => code.toLowerCase() === t || name.toLowerCase() === t) || null;
  }
  function splitPlace(text) {
    const comma = text.lastIndexOf(",");
    if (comma > 0) {
      const st = findState(text.slice(comma + 1));
      if (st) return { name: text.slice(0, comma).trim(), state: st };
    }
    for (const words of [2, 1]) {
      const parts = text.trim().split(/\s+/);
      if (parts.length <= words) continue;
      const st = findState(parts.slice(-words).join(" "));
      if (st) return { name: parts.slice(0, -words).join(" "), state: st };
    }
    return { name: text.trim(), state: null };
  }
  const sqlString = (s) => "'" + s.toUpperCase().replace(/'/g, "''") + "'";

  async function arcgisQuery(url, params) {
    const u = new URL(url);
    for (const [k, v] of Object.entries({ f: "json", ...params })) u.searchParams.set(k, v);
    const res = await fetch(u.toString());
    if (!res.ok) throw new Error(`Place search failed (${res.status})`);
    const json = await res.json();
    if (json.error) throw new Error(json.error.message || "Place search failed");
    return json.features || [];
  }
  async function searchTigerPlaces(name, st) {
    const base = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Places_CouSub_ConCity_SubMCD/MapServer";
    const where = `UPPER(BASENAME)=${sqlString(name)}` + (st ? ` AND STATE='${st[1]}'` : "");
    const params = { where, outFields: "BASENAME,STATE,INTPTLAT,INTPTLON,AREALAND", returnGeometry: "false" };
    const results = await Promise.all([4, 5].map((layer) => arcgisQuery(`${base}/${layer}/query`, params).catch(() => [])));
    const hits = results.flat().map((f) => f.attributes);
    if (!hits.length) return null;
    hits.sort((a, b) => (b.AREALAND || 0) - (a.AREALAND || 0));   // without a state, the largest place is the likeliest
    const h = hits[0];
    const s = STATES.find(([, fips]) => fips === h.STATE);
    return { label: `${h.BASENAME}${s ? ", " + s[0] : ""}`, lat: Number(h.INTPTLAT), lon: Number(h.INTPTLON) };
  }
  async function searchGnis(name, st) {
    const where = `UPPER(gaz_name)=${sqlString(name)}` + (st ? ` AND state_alpha='${st[0]}'` : "");
    const features = await arcgisQuery("https://carto.nationalmap.gov/arcgis/rest/services/geonames/MapServer/3/query", {
      where, outFields: "gaz_name,state_alpha", returnGeometry: "true", outSR: "4326",
    });
    const f = features.find((x) => x.geometry && x.geometry.points && x.geometry.points.length);
    if (!f) return null;
    const [lon, lat] = f.geometry.points[0];
    return { label: `${f.attributes.gaz_name}, ${f.attributes.state_alpha}`, lat, lon };
  }
  /** The Census Geocoder sends no CORS headers, so it is called with JSONP. Street addresses only. */
  function searchCensusAddress(text) {
    return new Promise((resolve, reject) => {
      const cb = "fwCensus" + Math.random().toString(36).slice(2);
      const u = new URL("https://geocoding.geo.census.gov/geocoder/locations/onelineaddress");
      u.searchParams.set("address", text);
      u.searchParams.set("benchmark", "Public_AR_Current");
      u.searchParams.set("format", "jsonp");
      u.searchParams.set("callback", cb);
      const script = document.createElement("script");
      const timer = setTimeout(() => done(new Error("Address search timed out")), 12000);
      function done(err, value) { clearTimeout(timer); delete window[cb]; script.remove(); err ? reject(err) : resolve(value); }
      window[cb] = (data) => {
        const m = data?.result?.addressMatches?.[0];
        done(null, m ? { label: m.matchedAddress, lat: m.coordinates.y, lon: m.coordinates.x } : null);
      };
      script.onerror = () => done(new Error("Address search failed"));
      script.src = u.toString();
      document.head.appendChild(script);
    });
  }
  async function geocodePlace(q) {
    const text = q.trim();
    if (!text) throw new Error("Enter a location or lat, lon");
    const direct = parseLatLon(text);
    if (direct) return { label: `${direct.lat.toFixed(4)}, ${direct.lon.toFixed(4)}`, ...direct };
    let hit = null;
    if (/^\d/.test(text)) hit = await searchCensusAddress(text).catch(() => null);
    else {
      const { name, state: st } = splitPlace(text);
      hit = (await searchTigerPlaces(name, st).catch(() => null)) || (await searchGnis(name, st).catch(() => null)) ||
        (await searchCensusAddress(text).catch(() => null));
    }
    if (!hit || !Number.isFinite(hit.lat) || !Number.isFinite(hit.lon)) throw new Error("Place not found. Try “City, ST”, a street address, or lat, lon.");
    return hit;
  }

  // —— Forecast confidence: the NOAA GEFS ensemble at the point (tiles packed by make_ensemble.py) ——————————
  // 31 runs of the GFS from slightly different starts. Each step covers the 6 hours before it.
  const ENS_RAIN = 0.05;   // inches in a day that count as "rain" for a member
  async function loadEnsIndex() {
    if (!state.ensIndex) state.ensIndex = fetch(`ens/index.json?t=${Date.now()}`, { cache: "no-store" }).then((r) => (r.ok ? r.json() : null)).catch(() => null);
    return state.ensIndex;
  }
  const ensTiles = new Map();
  /** Every member's series at the grid point nearest (lat, lon): { tmax, tmin, wind, apcp }[member][step], or null outside the grid. */
  async function ensAt(lat, lon) {
    const ix = await loadEnsIndex();
    if (!ix) throw new Error("The ensemble hasn't been published yet.");
    const g = ix.grid, M = ix.members.length, S = ix.hours.length;
    const r = Math.round((g.north - lat) / g.res), c = Math.round((lon - g.west) / g.res);
    if (r < 0 || c < 0 || r >= g.nlat || c >= g.nlon) return null;
    const key = `${Math.floor(r / g.tile)}_${Math.floor(c / g.tile)}`;
    if (!ensTiles.has(key)) ensTiles.set(key, fetch(`ens/${key}.bin?run=${encodeURIComponent(ix.cycle)}`).then((x) => { if (!x.ok) throw new Error(`ensemble tile ${x.status}`); return x.arrayBuffer(); }));
    const buf = await ensTiles.get(key).catch((e) => { ensTiles.delete(key); throw e; });
    const block = M * S * 5, at = ((r % g.tile) * g.tile + (c % g.tile)) * block;
    const u8 = new Uint8Array(buf, at, M * S * 3), dv = new DataView(buf, at + M * S * 3, M * S * 2);
    const series = (k, off) => Array.from({ length: M }, (_, m) => Array.from({ length: S }, (_, s) => { const v = u8[k * M * S + m * S + s]; return v === 255 ? null : v - off; }));
    return {
      ix, lat: g.north - r * g.res, lon: g.west + c * g.res,
      tmax: series(0, 80), tmin: series(1, 80), wind: series(2, 0),
      apcp: Array.from({ length: M }, (_, m) => Array.from({ length: S }, (_, s) => { const v = dv.getUint16((m * S + s) * 2, true); return v === 65535 ? null : v / 100; })),
    };
  }
  async function loadConfidence(pt) {
    if (pt.ens) return;
    pt.ens = "loading";
    try { pt.ens = (await ensAt(pt.lat, pt.lon)) || { error: "The ensemble covers the lower 48 states only." }; }
    catch (e) { pt.ens = { error: e.message }; }
    if (state.point === pt && ["confidence", "windows"].includes(state.view)) render();
  }

  const pct = (arr, p) => { const a = arr.filter((v) => v != null).sort((x, y) => x - y); if (!a.length) return null; const i = (a.length - 1) * p, lo = Math.floor(i); return a[lo] + (a[Math.min(a.length - 1, lo + 1)] - a[lo]) * (i - lo); };
  /** Local days (in the point's time zone) with every member's high, low, rain and peak wind. */
  function ensDays(e, tz) {
    const init = Date.parse(e.ix.init), M = e.tmax.length;
    const dayKey = (t) => new Date(t).toLocaleDateString("en-CA", { timeZone: tz });
    const days = new Map();
    e.ix.hours.forEach((h, s) => {
      const mid = init + (h - 3) * 3600e3, k = dayKey(mid);
      const d = days.get(k) || days.set(k, { k, t: mid, steps: [] }).get(k);
      d.steps.push(s);
    });
    return [...days.values()].filter((d) => d.steps.length === 4).map((d) => {
      const per = (fn) => Array.from({ length: M }, (_, m) => fn(m));
      const high = per((m) => { const v = d.steps.map((s) => e.tmax[m][s]).filter((x) => x != null); return v.length ? Math.max(...v) : null; });
      const low = per((m) => { const v = d.steps.map((s) => e.tmin[m][s]).filter((x) => x != null); return v.length ? Math.min(...v) : null; });
      const rain = per((m) => { const v = d.steps.map((s) => e.apcp[m][s]).filter((x) => x != null); return v.length ? v.reduce((a, b) => a + b, 0) : null; });
      const wind = per((m) => { const v = d.steps.map((s) => e.wind[m][s]).filter((x) => x != null); return v.length ? Math.max(...v) : null; });
      const n = rain.filter((x) => x != null).length, wet = rain.filter((x) => x != null && x >= ENS_RAIN).length;
      const spread = pct(high, 0.9) - pct(high, 0.1), frac = n ? wet / n : 0;
      const tConf = spread <= 6 ? 2 : spread <= 12 ? 1 : 0;
      const rConf = frac <= 0.15 || frac >= 0.85 ? 2 : frac >= 0.3 && frac <= 0.7 ? 0 : 1;
      return { ...d, high, low, rain, wind, n, wet, frac, spread, conf: Math.min(tConf, rConf) };
    });
  }
  const CONF = [["Low", "#ff6b5a"], ["Medium", "#ffbf47"], ["High", "#3fcf9a"]];

  function confidenceView() {
    const f = state.point;
    let html = `<form class="search" id="search"><input id="q" placeholder="City, ST · street address · lat, lon" aria-label="Location" autocomplete="off" value="${esc(state.query)}" /><button class="btn btn--secondary">Go</button></form>`;
    if (state.pointError) return html + `<p class="empty">${esc(state.pointError)}</p>`;
    if (f === "loading") return html + `<p class="empty">Loading forecast…</p>`;
    if (!f) return html + `<p class="empty">Search a place or click the map to see how much the 31 runs of NOAA's ensemble agree on its next 7 days.</p>`;
    if (!f.ens || f.ens === "loading") { loadConfidence(f); return html + `<p class="empty">Loading the GEFS ensemble for ${esc(f.place)}…</p>`; }
    if (f.ens.error) return html + `<p class="empty">${esc(f.ens.error)}</p>`;
    const e = f.ens, M = e.tmax.length, tz = f.tz || undefined;
    const days = ensDays(e, tz);
    if (!days.length) return html + `<p class="empty">No ensemble data for this spot.</p>`;
    const dname = (d) => new Date(d.t).toLocaleDateString("en-US", { timeZone: tz, weekday: "short" });
    // The NWS high for each day, to set beside the ensemble's range.
    const nwsHigh = {};
    (f.periods || []).filter((p) => p.isDaytime).forEach((p) => { nwsHigh[new Date(p.startTime).toLocaleDateString("en-CA", { timeZone: tz })] = p.temperature; });
    const r0 = (v) => (v == null ? "—" : Math.round(v));
    html += `<div class="pad"><h5 class="sub">${esc(f.place)} · GEFS ${esc(dayZ(e.ix.init))} run · ${M} members</h5>`;
    html += `<div class="conf-sum">${confSummary(days, M, dname).map((t) => `<p>${t}</p>`).join("")}</div>`;
    html += `<table class="t conf"><thead><tr><th>Day</th><th>High °F<small>10–90%</small></th><th>Rain<small>members ≥ ${ENS_RAIN}"</small></th><th>Peak wind<small>median · 90%</small></th><th>Confidence</th></tr></thead><tbody>${days.map((d) => {
      const nws = nwsHigh[d.k];
      const lo = pct(d.high, 0.1), hi = pct(d.high, 0.9);
      const outside = nws != null && (nws < lo - 1 || nws > hi + 1);
      return `<tr><td><b style="color:#fff">${esc(dname(d))}</b><small>${esc(new Date(d.t).toLocaleDateString("en-US", { timeZone: tz, month: "numeric", day: "numeric" }))}</small></td>
        <td><b style="color:#fff">${r0(pct(d.high, 0.5))}°</b><small>${r0(lo)}–${r0(hi)}°${nws != null ? ` · NWS ${nws}°${outside ? " ⚑" : ""}` : ""}</small></td>
        <td><b style="color:#fff">${d.wet} of ${d.n}</b><div class="bar"><i style="width:${Math.round(d.frac * 100)}%"></i></div><small>${d.wet ? `median ${pct(d.rain.filter((x) => x >= ENS_RAIN), 0.5).toFixed(2)}" when wet` : "dry"}</small></td>
        <td>${r0(pct(d.wind, 0.5))} · ${r0(pct(d.wind, 0.9))}<small>mph</small></td>
        <td><span class="conf-tag" style="color:${CONF[d.conf][1]}">● ${CONF[d.conf][0]}</span></td></tr>`;
    }).join("")}</tbody></table>`;
    html += `<p class="chart-key">⚑ the NWS forecast high is outside the ensemble's middle 80%.</p>`;
    html += `<h5 class="sub" style="margin-top:1rem">Temperature plume</h5>${tempPlume(e, tz)}<h5 class="sub" style="margin-top:1rem">Rain piling up, member by member</h5>${rainPlume(e, tz)}`;
    html += `<p class="note">Each thin line is one run of NOAA's Global Ensemble Forecast System, started from slightly different conditions; the bold line is their median.
      Where the lines bunch together the forecast is more certain. Confidence is high when the 10–90% spread of highs is 6°F or less and nearly all members agree on rain or no rain, and low when highs spread more than 12°F or members split near half and half.
      GEFS is a 0.5° (about 30 mile) grid, so it smooths out mountains, coasts and thunderstorms; use the NWS point forecast for the details.</p></div>`;
    return html;
  }

  /** A few plain sentences: rain chances, the least certain day, and the run's overall confidence. */
  function confSummary(days, M, dname) {
    const out = [];
    const wet = days.filter((d) => d.frac >= 0.3);
    if (!wet.length) out.push(`<b>Dry week likely:</b> no day has rain in more than ${Math.max(...days.map((d) => d.wet))} of ${M} members.`);
    else out.push(`<b>Rain:</b> ${wet.map((d) => `${dname(d)} ${d.wet} of ${d.n} members`).join(" · ")}.`);
    const loose = days.reduce((m, d) => (d.spread > m.spread ? d : m));
    if (loose.spread > 8) out.push(`<b>Least certain:</b> ${dname(loose)}, with highs from ${Math.round(pct(loose.high, 0.1))}° to ${Math.round(pct(loose.high, 0.9))}°.`);
    const firstLow = days.findIndex((d) => d.conf === 0);
    out.push(firstLow === -1 ? "<b>Confidence holds up all week.</b>" : firstLow === 0 ? `<b>Confidence is low from the start</b>; plan for a range of outcomes.` : `<b>Confidence is good through ${dname(days[firstLow - 1])}</b> and drops after that.`);
    return out;
  }

  function plumeFrame(e, tz, lo, hi, unit, H) {
    const init = Date.parse(e.ix.init), hrs = e.ix.hours;
    const t0 = init + (hrs[0] - 6) * 3600e3, t1 = init + hrs[hrs.length - 1] * 3600e3;
    const W = 336, L0 = 28, R0 = 8, T0 = 6, B0 = 16;
    const x = (t) => L0 + ((t - t0) / (t1 - t0)) * (W - L0 - R0);
    const y = (v) => T0 + (1 - (v - lo) / (hi - lo)) * (H - T0 - B0);
    const mono = 'font-family="IBM Plex Mono, monospace" font-size="9" fill="rgba(255,255,255,.42)"';
    let svg = "";
    const step = (hi - lo) > 40 ? 20 : (hi - lo) > 4 ? 10 : (hi - lo) > 2 ? 1 : 0.5;
    for (let v = lo; v <= hi + 1e-9; v += step) svg += `<line x1="${L0}" x2="${W - R0}" y1="${y(v)}" y2="${y(v)}" stroke="rgba(255,255,255,.07)"/><text x="${L0 - 4}" y="${y(v) + 3}" text-anchor="end" ${mono}>${+v.toFixed(1)}${unit}</text>`;
    const day0 = new Date(new Date(t0).toLocaleString("en-US", { timeZone: tz }));
    const offset = day0.getTime() - t0;   // local wall clock minus UTC, near enough for day lines
    for (let t = Math.ceil((t0 + offset) / 86400e3) * 86400e3 - offset; t < t1; t += 86400e3) {
      svg += `<line x1="${x(t)}" x2="${x(t)}" y1="${T0}" y2="${H - B0}" stroke="rgba(255,255,255,.12)"/>`;
      if (t + 12 * 3600e3 < t1) svg += `<text x="${x(t + 12 * 3600e3).toFixed(1)}" y="${H - 4}" text-anchor="middle" ${mono}>${new Date(t + 12 * 3600e3).toLocaleDateString("en-US", { timeZone: tz, weekday: "short" })}</text>`;
    }
    return { W, H, x, y, svg, init, hrs };
  }
  const pathOf = (pts) => { let d = "", pen = false; pts.forEach(([px, py]) => { if (py == null) { pen = false; return; } d += `${pen ? "L" : "M"}${px.toFixed(1)},${py.toFixed(1)}`; pen = true; }); return d; };

  function tempPlume(e, tz) {
    // Alternate each member's 6-hour maximum and minimum at the middle of each step: a daily wave per member.
    const vals = [...e.tmax.flat(), ...e.tmin.flat()].filter((v) => v != null);
    if (!vals.length) return "";
    const lo = Math.floor((Math.min(...vals) - 2) / 10) * 10, hi = Math.ceil((Math.max(...vals) + 2) / 10) * 10;
    const fr = plumeFrame(e, tz, lo, hi, "°", 130);
    const hourAt = (t) => +new Date(t).toLocaleString("en-US", { timeZone: tz, hour: "numeric", hourCycle: "h23" });
    const series = (m) => fr.hrs.map((h, s) => { const t = fr.init + (h - 3) * 3600e3, hr = hourAt(t); return [t, hr >= 9 && hr < 21 ? e.tmax[m][s] : e.tmin[m][s]]; });
    let svg = fr.svg;
    const all = e.tmax.map((_, m) => series(m));
    const band = fr.hrs.map((_, s) => { const col = all.map((sr) => sr[s][1]); return [all[0][s][0], pct(col, 0.1), pct(col, 0.5), pct(col, 0.9)]; });
    svg += `<path d="${pathOf(band.map(([t, a]) => [fr.x(t), a == null ? null : fr.y(a)]))}${band.slice().reverse().map(([t, , , b]) => `L${fr.x(t).toFixed(1)},${fr.y(b).toFixed(1)}`).join("")}Z" fill="rgba(255,191,71,.14)"/>`;
    all.forEach((sr) => { svg += `<path d="${pathOf(sr.map(([t, v]) => [fr.x(t), v == null ? null : fr.y(v)]))}" fill="none" stroke="rgba(255,191,71,.28)" stroke-width=".7"/>`; });
    svg += `<path d="${pathOf(band.map(([t, , md]) => [fr.x(t), md == null ? null : fr.y(md)]))}" fill="none" stroke="#ffbf47" stroke-width="2"/>`;
    return `<svg class="chart" viewBox="0 0 ${fr.W} ${fr.H}" role="img" aria-label="Temperature forecast from each ensemble member">${svg}</svg>
      <p class="chart-key"><span style="color:rgba(255,191,71,.6)">━</span> each member <span style="color:#ffbf47">━</span> median <span style="color:rgba(255,191,71,.5)">▮</span> 10–90% · daytime highs and overnight lows, °F</p>`;
  }

  function rainPlume(e, tz) {
    const totals = e.apcp.map((row) => { let sum = 0; return row.map((v) => (sum += v ?? 0)); });
    const top = Math.max(0.5, ...totals.map((r) => r[r.length - 1]));
    const hi = top > 4 ? Math.ceil(top) : Math.ceil(top * 2) / 2;
    const fr = plumeFrame(e, tz, 0, hi, '"', 120);
    let svg = fr.svg;
    const xs = fr.hrs.map((h) => fr.x(fr.init + h * 3600e3));
    totals.forEach((r) => { svg += `<path d="${pathOf(r.map((v, s) => [xs[s], fr.y(v)]))}" fill="none" stroke="rgba(74,209,255,.32)" stroke-width=".8"/>`; });
    const med = fr.hrs.map((_, s) => pct(totals.map((r) => r[s]), 0.5));
    svg += `<path d="${pathOf(med.map((v, s) => [xs[s], fr.y(v)]))}" fill="none" stroke="#4ad1ff" stroke-width="2"/>`;
    const end = totals.map((r) => r[r.length - 1]);
    return `<svg class="chart" viewBox="0 0 ${fr.W} ${fr.H}" role="img" aria-label="Accumulated rain from each ensemble member">${svg}</svg>
      <p class="chart-key"><span style="color:rgba(74,209,255,.6)">━</span> each member <span style="color:#4ad1ff">━</span> median · 7-day total ${pct(end, 0.5).toFixed(2)}" median, ${pct(end, 0.1).toFixed(2)}–${pct(end, 0.9).toFixed(2)}" for the middle 80%</p>`;
  }

  /** How many members keep a work window within its limits (6-hour steps, so an approximation). */
  function ensWindowOdds(e, w, r) {
    const init = Date.parse(e.ix.init), L = Object.fromEntries(WORK_FIELDS.map(([k]) => [k, lim(w[k])]));
    const steps = e.ix.hours.map((h, s) => [s, init + (h - 6) * 3600e3, init + h * 3600e3]).filter(([, a, b]) => a < r.end && b > r.start).map(([s]) => s);
    if (!steps.length) return null;
    const rainCap = L.rain != null ? Math.max(0.02, L.rain * 6) : 0.1;
    let ok = 0, n = 0;
    e.tmax.forEach((_, m) => {
      const v = steps.map((s) => [e.tmax[m][s], e.tmin[m][s], e.wind[m][s], e.apcp[m][s]]);
      if (v.some((x) => x.includes(null))) return;
      n++;
      if (v.every(([tx, tn, wd, rn]) => (L.tmin == null || tn >= L.tmin - 2) && (L.tmax == null || tx <= L.tmax + 2) && (L.wind == null || wd <= L.wind) && rn <= rainCap)) ok++;
    });
    return n ? { ok, n } : null;
  }

  // —— Load ————————————————————————————————————————————————————————
  async function init() {
    setTabs();
    setStatus("Loading model maps…");
    try {
      const res = await fetch(`data.json?t=${Date.now()}`, { cache: "no-store" });
      if (!res.ok) throw new Error(res.status);
      state.info = await res.json();
    } catch {
      setStatus("No model run published yet. Maps appear after the first run is processed.");
      state.info = null;
      state.view = "forecast"; setTabs(); render();
      return;
    }
    const models = state.info.models;
    $("models").insertAdjacentHTML("beforeend", models.map((m) => `<button data-model="${m.id}" aria-pressed="false" title="${esc(m.long)}">${esc(m.name)}</button>`).join(""));
    const mi = Math.max(0, models.findIndex((m) => m.id === params.get("model")));
    state.product = params.get("product");
    const m = models[mi];
    const f = +params.get("f");
    state.hour = Math.max(0, m.hours.indexOf(f));
    state.model = mi;
    setModel(mi);
    const loc = (params.get("loc") || "").split(",").map(Number);
    if (loc.length === 2 && loc.every(Number.isFinite)) {
      map.setView(loc, 6, { animate: false });
      showForecast(loc[0], loc[1]);
    } else {
      // Offer the place last used on any Standard Weather page.
      try { const saved = JSON.parse(localStorage.getItem("sw-place") || "null"); if (saved?.label) state.query = saved.label; } catch { /* none saved */ }
      render();
    }
  }

  init();
})();
