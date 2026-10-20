/* FutureWeather — model forecast console for standardweather.github.io.
 *
 *   Model maps    GFS, National Blend of Models and HRRR overlays drawn by make_plots.py (GitHub Actions)
 *   NWS API       7-day and hourly forecasts for any U.S. point (api.weather.gov)
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
    view: params.get("view") === "forecast" || params.get("loc") ? "forecast" : "maps",
    point: null, pointError: null, openPeriod: null, query: "",
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
  }

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
    const every = n > 18 ? 6 : n > 10 ? 2 : 1;
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
    if (state.view === "forecast") q.set("view", "forecast");
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
      <p class="note">The NWS forecast starts from the National Blend of Models, adjusted by local forecasters. Tap a day for details.
        <a href="${esc(f.link)}" target="_blank" rel="noopener">Full forecast at weather.gov ↗</a></p></div>`;
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

  function setTabs() { document.querySelectorAll("#views button").forEach((b) => b.setAttribute("aria-selected", b.dataset.view === state.view)); }
  function render() {
    if (!state.info) { if (state.view === "forecast") $("panel").innerHTML = forecastView(); return; }
    $("panel").innerHTML = state.view === "forecast" ? forecastView() : mapsView();
  }
  $("views").addEventListener("click", (e) => {
    const v = e.target.closest("[data-view]")?.dataset.view;
    if (!v) return;
    state.view = v; setTabs(); render(); syncUrl();
    if (v === "forecast" && !state.point) $("q")?.focus();
  });
  $("panel").addEventListener("click", (e) => {
    const b = e.target.closest("[data-product], [data-period]");
    if (!b) return;
    if (b.dataset.product) { state.product = b.dataset.product; drawLegend(); drawHour(); preload(); render(); return; }
    const i = +b.dataset.period;
    state.openPeriod = state.openPeriod === i ? null : i;
    render();
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
    state.view = "forecast"; setTabs();
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
        lat, lon, office: pr.gridId,
        place: rel ? `${rel.city}, ${rel.state}` : `${lat.toFixed(2)}, ${lon.toFixed(2)}`,
        link: `https://forecast.weather.gov/MapClick.php?lat=${lat.toFixed(4)}&lon=${lon.toFixed(4)}`,
        updated: daily.properties?.updateTime, periods: daily.properties?.periods || [], hourly: (hourly?.properties?.periods || []).slice(0, 48),
      };
    } catch (err) {
      state.point = null;
      state.pointError = err.message;
    }
    if (state.view === "forecast") render();
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
    }
  }

  init();
})();
