/* AIRMETcast — experimental, unlisted page. AIRMET- and convective SIGMET-style areas that make_airmetcast.py works out
 * from raw GFS and HRRR output, drawn per forecast hour, with an altitude filter and the official G-AIRMETs for comparison.
 *
 *   data/<model>.json, data/<model>/f<hhh>.geojson   make_airmetcast.py (GitHub Actions)
 *   NOAA map services (AWC layer 110)                  official G-AIRMET snapshots, for the Compare option
 *   artcc.json                                         ARTCC (center) boundaries, FAA Aeronautical Information Services
 *                                                      Boundary_Airspace layer, simplified to about 2 km
 */
(() => {
  "use strict";

  const AWC = "https://mapservices.weather.noaa.gov/vector/rest/services/aviation/awc_aviation_weather/MapServer/110/query";
  // Hazard: [color, name, series, line?]. Colors follow AviationWeather's G-AIRMET layer.
  const HZ = {
    IFR: ["#b38bff", "IFR", "Sierra"],
    "MTN OBSCN": ["#ff7ab8", "Mountain obscuration", "Sierra"],
    "TURB-HI": ["#ff9548", "Turbulence above FL180", "Tango"],
    "TURB-LO": ["#ffbf47", "Turbulence FL180 and below", "Tango"],
    "SFC WND": ["#d2b48c", "Surface wind 30 kt+", "Tango"],
    LLWS: ["#ffd84a", "Low-level wind shear", "Tango"],
    ICE: ["#6fe3e8", "Icing", "Zulu"],
    M_FZLVL: ["#7cb8ff", "Multiple freezing levels", "Zulu"],
    FZLVL: ["#7cb8ff", "Freezing level lines", "Zulu", true],
    "TS AREA": ["#ff4d4d", "Thunderstorm areas", "Convective"],
    "TS LINE": ["#ff4d4d", "Thunderstorm lines", "Convective"],
    "TS ISOL": ["#ff4d4d", "Isolated strong storms", "Convective"],
  };
  const OFFICIAL = { MT_OBSC: "MTN OBSCN", SFC_WND: "SFC WND" };   // AWC hazard names -> ours
  const SERIES = ["Sierra", "Tango", "Zulu", "Convective"];

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const pad = (n, w = 2) => String(n).padStart(w, "0");
  const dayZ = (t) => { const d = new Date(t); return `${d.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric", timeZone: "UTC" })} ${pad(d.getUTCHours())}Z`; };
  const local = (t) => new Date(t).toLocaleString("en-US", { weekday: "short", hour: "numeric", timeZoneName: "short" });
  const altText = (ft) => (ft <= 0 ? "SFC" : ft >= 18000 ? `FL${pad(Math.round(ft / 1000) * 10, 3)}` : `${ft.toLocaleString()} ft`);

  const params = new URLSearchParams(location.search);
  const state = {
    models: [], model: 0, hour: 0, alt: -1, timer: null, cache: {}, fc: null, official: null,
    on: Object.fromEntries(Object.keys(HZ).map((k) => [k, k !== "FZLVL"])), labels: true, showOfficial: false,
  };

  // —— Map ————————————————————————————————————————————————————————
  const map = L.map("map", { zoomControl: false, minZoom: 3, maxZoom: 10, zoomSnap: 0.5 });
  const at = (params.get("at") || "").split(",").map(Number);   // ?at=lat,lon,zoom
  if (at.length === 3 && at.every(Number.isFinite)) map.setView([at[0], at[1]], at[2], { animate: false });
  else map.fitBounds([[24, -125], [50, -66]], { animate: false });
  L.control.zoom({ position: "topright" }).addTo(map);
  map.attributionControl.setPrefix("");
  map.createPane("labels").style.zIndex = 640;
  map.getPane("labels").style.pointerEvents = "none";
  L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}", { maxZoom: 16, attribution: "Esri" }).addTo(map);
  L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Reference/MapServer/tile/{z}/{y}/{x}", { maxZoom: 16, pane: "labels", opacity: 0.85 }).addTo(map);
  map.attributionControl.addAttribution("NOAA GFS and HRRR");
  const renderer = L.svg({ padding: 0.5 }).addTo(map);
  const shapesLayer = L.layerGroup().addTo(map);
  const officialLayer = L.layerGroup().addTo(map);
  const artccLayer = L.layerGroup().addTo(map);

  // Diagonal hatching in each hazard color, so the areas never pass for the official solid ones.
  (function hatches() {
    const svg = renderer._container;
    const ns = "http://www.w3.org/2000/svg";
    const defs = document.createElementNS(ns, "defs");
    Object.entries(HZ).forEach(([k, [c]]) => {
      const p = document.createElementNS(ns, "pattern");
      p.setAttribute("id", hatchId(k));
      p.setAttribute("patternUnits", "userSpaceOnUse");
      p.setAttribute("width", "8"); p.setAttribute("height", "8");
      p.setAttribute("patternTransform", "rotate(45)");
      p.innerHTML = `<rect width="8" height="8" fill="${c}" fill-opacity="0.07"/><line x1="0" y1="0" x2="0" y2="8" stroke="${c}" stroke-width="2" stroke-opacity="0.5"/>`;
      defs.appendChild(p);
    });
    svg.insertBefore(defs, svg.firstChild);
  })();
  function hatchId(k) { return `hatch-${k.replace(/[^A-Za-z]/g, "")}`; }

  function setStatus(msg) { $("status").textContent = msg || ""; $("status").hidden = !msg; }
  const model = () => state.models[state.model];
  const fhr = () => model().hours[state.hour];
  const validAt = (m, h) => new Date(m.init).getTime() + h * 3600e3;

  // —— Data ————————————————————————————————————————————————————————
  async function load() {
    const got = await Promise.all(["gfs", "hrrr"].map((id) =>
      fetch(`data/${id}.json?t=${Math.floor(Date.now() / 600000)}`).then((r) => (r.ok ? r.json() : null)).catch(() => null)));
    state.models = got.filter(Boolean);
    if (!state.models.length) { setStatus("No AIRMETcast has been made yet."); return; }
    const want = state.models.findIndex((m) => m.id === params.get("model"));
    state.model = want >= 0 ? want : 0;
    buildModels();
    buildHazards();
    if (params.has("alt")) { $("alt").value = params.get("alt"); $("alt").dispatchEvent(new Event("input")); }
    setModel(state.model, +params.get("hour") || 0);
  }

  async function hourData(m, h) {
    const key = `${m.id}/${h}`;
    state.cache[key] ||= fetch(`data/${m.id}/f${pad(h, 3)}.geojson?run=${encodeURIComponent(m.cycle)}`).then((r) => r.json());
    return state.cache[key];
  }

  // —— Controls ————————————————————————————————————————————————————
  function buildModels() {
    $("models").insertAdjacentHTML("beforeend", state.models.map((m, i) =>
      `<button data-i="${i}" aria-pressed="${i === state.model}">${esc(m.name)}</button>`).join(""));
    $("models").addEventListener("click", (e) => {
      const b = e.target.closest("button[data-i]");
      if (b) setModel(+b.dataset.i, 0);
    });
  }

  function buildHazards() {
    $("hazards").innerHTML = SERIES.map((s) => {
      const items = Object.entries(HZ).filter(([, v]) => v[2] === s);
      return `<div class="grp" data-series="${s}"><h4>${s === "Convective" ? "Convective (SIGMET-style)" : `${s} (AIRMET-style)`}</h4><div class="checks one">${items.map(([k, [c, name, , line]]) =>
        `<label data-hz="${esc(k)}"><input type="checkbox" data-hz="${esc(k)}" ${state.on[k] ? "checked" : ""} /><span class="sw${line ? " line" : ""}" style="color:${c}"></span>${esc(name)}<small></small></label>`).join("")}</div></div>`;
    }).join("");
    $("hazards").addEventListener("change", (e) => {
      const k = e.target.dataset.hz;
      if (!k) return;
      state.on[k] = e.target.checked;
      draw();
    });
  }

  $("alt").addEventListener("input", (e) => {
    state.alt = +e.target.value;
    $("altLabel").textContent = state.alt < 0 ? "All altitudes" : `At ${altText(state.alt * 1000)}`;
    draw();
  });
  $("labels").addEventListener("change", (e) => { state.labels = e.target.checked; draw(); });
  map.on("zoomend", () => { if (state.fc) draw(); });

  // ARTCC boundaries: thin gray lines under the hazard areas, each center's identifier at its middle.
  let artcc = null;
  $("artcc").addEventListener("change", async (e) => {
    artccLayer.clearLayers();
    if (!e.target.checked) return;
    artcc ||= fetch("artcc.json").then((r) => r.json()).catch(() => null);
    const gj = await artcc;
    if (!gj || !$("artcc").checked) return;
    L.geoJSON(gj, { renderer, interactive: false, style: { color: "#c9d2f0", weight: 1, opacity: 0.55, fill: false } }).addTo(artccLayer);
    gj.features.forEach((f) => {
      const c = L.geoJSON(f).getBounds().getCenter();
      artccLayer.addLayer(L.tooltip({ permanent: true, direction: "center", className: "artcc-label", pane: "labels" }).setLatLng(c).setContent(esc(f.properties.id)));
    });
  });
  $("official").addEventListener("change", (e) => { state.showOfficial = e.target.checked; drawOfficial(); });

  function setModel(i, hourIndex) {
    state.model = i;
    const m = model();
    [...$("models").querySelectorAll("button")].forEach((b) => b.setAttribute("aria-pressed", String(+b.dataset.i === i)));
    // Only the hazards this model makes are offered.
    document.querySelectorAll("#hazards label[data-hz]").forEach((l) => { l.hidden = !m.hazards.includes(l.dataset.hz); });
    document.querySelectorAll("#hazards .grp").forEach((g) => { g.hidden = ![...g.querySelectorAll("label")].some((l) => !l.hidden); });
    $("run").textContent = `${m.long} · run ${dayZ(m.init)}`;
    $("hour").max = m.hours.length - 1;
    setHour(Math.min(hourIndex, m.hours.length - 1));
  }

  async function setHour(i) {
    state.hour = i;
    $("hour").value = i;
    const m = model(), h = fhr(), t = validAt(m, h);
    $("hourLabel").innerHTML = `${esc(dayZ(t))} <small>F${pad(h, 3)} · ${esc(local(t))}</small>`;
    syncUrl();
    try {
      const fc = await hourData(m, h);
      if (model() !== m || fhr() !== h) return;
      state.fc = fc;
      setStatus("");
      draw();
      drawOfficial();
    } catch (e) {
      setStatus(`F${pad(h, 3)} isn't available.`);
    }
  }

  $("hour").addEventListener("input", (e) => setHour(+e.target.value));
  $("prev").addEventListener("click", () => setHour((state.hour - 1 + model().hours.length) % model().hours.length));
  $("next").addEventListener("click", () => setHour((state.hour + 1) % model().hours.length));
  $("play").addEventListener("click", () => {
    if (state.timer) { clearInterval(state.timer); state.timer = null; $("play").textContent = "▶"; return; }
    $("play").textContent = "❚❚";
    state.timer = setInterval(() => setHour((state.hour + 1) % model().hours.length), 1200);
  });

  function syncUrl() {
    const q = new URLSearchParams();
    q.set("model", model().id);
    if (state.hour) q.set("hour", state.hour);
    history.replaceState(null, "", `?${q}`);
  }

  // —— Drawing ————————————————————————————————————————————————————
  function atAltitude(p) {
    if (state.alt < 0 || p.hazard === "FZLVL") return true;
    const a = state.alt * 1000;
    return a >= p.base_ft && a <= Math.max(p.top_ft, p.base_ft);
  }

  function labelText(p) {
    const alt = `${p.base}–${p.top}`;
    switch (p.hazard) {
      case "IFR": return "IFR";
      case "MTN OBSCN": return "MTN OBSC";
      case "SFC WND": return `SFC WND ${p.peak_kt}KT`;
      case "LLWS": return "LLWS";
      case "ICE": return `ICE ${alt}`;
      case "M_FZLVL": return `M FZLVL ${alt}`;
      case "FZLVL": return p.level_ft ? `${pad(p.level_ft / 100, 3)}` : "SFC";
      case "TS AREA": case "TS LINE": case "TS ISOL":
        return `${p.hazard.replace("TS ", "")} TOPS ${p.tops} · ${pad(p.mov_from, 3)}${pad(p.mov_kt)}KT`;
      default: return `${p.hazard} ${alt}`;
    }
  }

  function rows(p) {
    const r = [];
    const alt = p.hazard === "FZLVL" ? altText(p.level_ft)
      : p.hazard === "IFR" ? "Surface (ceiling below 1,000 ft / visibility below 3 SM)"
      : p.hazard === "LLWS" ? "Surface to 2,000 ft AGL"
      : p.hazard === "SFC WND" ? "Surface"
      : `${p.base === "FRZLVL" ? "Freezing level" : altText(p.base_ft)} to ${altText(p.top_ft)}`;
    r.push(["Altitude", esc(alt)]);
    if (p.severity) r.push(["Severity", esc(p.severity)]);
    if (p.cause) r.push(["Due to", esc(p.cause)]);
    if (p.fzlvl) r.push(["Freezing level", esc(p.fzlvl)]);
    if (p.sld) r.push(["Large drops", "Freezing rain or drizzle possible"]);
    if (p.peak_edr != null) r.push(["Peak EDR", esc(p.peak_edr)]);
    if (p.peak != null) r.push(["Icing score", `${esc(p.peak)} (0–1)`]);
    if (p.lwc) r.push(["Liquid water", `${esc(p.lwc)} g/m³`]);
    if (p.peak_kt != null) r.push(["Peak", `${esc(p.peak_kt)} kt`]);
    if (p.tops) r.push(["Tops", esc(p.tops)]);
    if (p.mov_kt != null) r.push(["Moving", `from ${pad(p.mov_from, 3)}° at ${p.mov_kt} kt`]);
    if (p.width_nm) r.push(["Line", `${p.length_nm} nm long, ${p.width_nm} nm wide`]);
    if (p.diameter_nm) r.push(["Cell", `${p.diameter_nm} nm across`]);
    if (p.tags?.length) r.push(["Also", esc(p.tags.join(" · "))]);
    if (p.coverage != null) r.push(["Coverage", `${p.coverage}%`]);
    if (p.area_sqmi) r.push(["Area", `${p.area_sqmi.toLocaleString()} sq mi`]);
    return r;
  }

  // A small altitude bar: the hazard's layer from the surface to FL450.
  function altBar(p) {
    if (["IFR", "SFC WND", "LLWS", "FZLVL"].includes(p.hazard)) return "";
    const H = 90, y = (ft) => H - (Math.min(ft, 45000) / 45000) * H;
    const top = y(Math.max(p.top_ft, p.base_ft + 500)), base = y(p.base_ft);
    const c = HZ[p.hazard]?.[0] || "#fff";
    return `<svg width="92" height="${H + 4}" viewBox="0 -2 92 ${H + 4}" style="float:right;margin-left:.6rem">
      <rect x="0" y="0" width="14" height="${H}" fill="rgba(255,255,255,.06)" />
      <rect x="0" y="${top.toFixed(1)}" width="14" height="${Math.max(base - top, 2).toFixed(1)}" fill="${c}" fill-opacity=".7" />
      ${[0, 10000, 18000, 30000, 45000].map((f) => `<text x="20" y="${(y(f) + 3).toFixed(1)}" font-size="8" fill="#8a90ad">${f ? (f >= 18000 ? `FL${f / 100}` : `${f / 1000}k`) : "SFC"}</text>`).join("")}
    </svg>`;
  }

  function popup(p) {
    const [c, name] = HZ[p.hazard] || ["#fff", p.hazard];
    const m = model();
    return `<div class="pop">${altBar(p)}<h4><span class="dot" style="background:${c}"></span>${esc(name)}</h4>
      <table>${rows(p).map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join("")}</table>
      <p class="xp">Experimental, from ${esc(m.name)} ${esc(dayZ(m.init))} F${pad(fhr(), 3)}. Not an AIRMET or SIGMET.</p></div>`;
  }

  // Where a storm area will be in an hour, from its motion.
  function motionArrow(center, p, color) {
    const toward = ((p.mov_from + 180) % 360) * Math.PI / 180, km = p.mov_kt * 1.852;
    const dLat = (km * Math.cos(toward)) / 111.2, dLon = (km * Math.sin(toward)) / (111.2 * Math.cos(center.lat * Math.PI / 180));
    const end = L.latLng(center.lat + dLat, center.lng + dLon);
    const wing = (a) => {
      const b = toward + Math.PI + a, k = km * 0.3;
      return L.latLng(end.lat + (k * Math.cos(b)) / 111.2, end.lng + (k * Math.sin(b)) / (111.2 * Math.cos(end.lat * Math.PI / 180)));
    };
    return L.polyline([[center, end], [wing(0.45), end, wing(-0.45)]], { color, weight: 2, renderer, interactive: false });
  }

  // Label a shape only when it is wide enough on screen for its label, so small areas don't bury the map.
  function roomFor(bounds, p) {
    const a = map.latLngToContainerPoint(bounds.getNorthWest()), b = map.latLngToContainerPoint(bounds.getSouthEast());
    return Math.abs(b.x - a.x) >= Math.min(labelText(p).length * 7, 150) && Math.abs(b.y - a.y) >= 24;
  }

  function draw() {
    shapesLayer.clearLayers();
    const counts = {};
    const shown = [];
    (state.fc?.features || []).forEach((f) => {
      const p = f.properties;
      counts[p.hazard] = (counts[p.hazard] || 0) + 1;
      if (!state.on[p.hazard] || !atAltitude(p)) return;
      const c = HZ[p.hazard]?.[0] || "#fff";
      if (p.hazard === "FZLVL") {
        const line = L.geoJSON(f, { renderer, style: { color: c, weight: p.level_ft ? 1.2 : 2, dashArray: "2 5", opacity: 0.9 }, interactive: false });
        shapesLayer.addLayer(line);
        if (state.labels && map.getZoom() >= 5) {
          const coords = f.geometry.coordinates;
          const mid = coords[Math.floor(coords.length / 2)];
          if (mid) shapesLayer.addLayer(L.tooltip({ permanent: true, direction: "center", className: "hz-label", pane: "labels" }).setLatLng([mid[1], mid[0]]).setContent(labelText(p)));
        }
        return;
      }
      const layer = L.geoJSON(f, {
        renderer,
        style: { color: c, weight: 2, dashArray: "6 4", fillColor: `url(#${hatchId(p.hazard)})`, fillOpacity: 1 },
      });
      layer.bindPopup(popup(p), { maxWidth: 340 });
      shapesLayer.addLayer(layer);
      const center = layer.getBounds().getCenter();
      if (p.mov_kt) shapesLayer.addLayer(motionArrow(center, p, c));
      if (state.labels && roomFor(layer.getBounds(), p)) {
        shapesLayer.addLayer(L.tooltip({ permanent: true, direction: "center", className: "hz-label", pane: "labels" }).setLatLng(center).setContent(esc(labelText(p))));
      }
      shown.push({ p, layer });
    });
    document.querySelectorAll("#hazards label[data-hz] small").forEach((s) => { s.textContent = counts[s.parentElement.dataset.hz] || ""; });
    listAreas(shown);
  }

  function listAreas(shown) {
    const order = Object.keys(HZ);
    shown.sort((a, b) => order.indexOf(a.p.hazard) - order.indexOf(b.p.hazard) || (b.p.area_sqmi || 0) - (a.p.area_sqmi || 0));
    $("areas").innerHTML = `<h4>${shown.length ? `${shown.length} area${shown.length === 1 ? "" : "s"} at this hour` : "No areas shown at this hour"}</h4>` +
      shown.map(({ p }, i) => {
        const [c, name] = HZ[p.hazard];
        const where = p.cause || p.severity || (p.tags || []).join(" · ") || "";
        return `<button class="area" data-i="${i}"><i style="background:${c}"></i><span>${esc(name)}<small>${esc(where)}</small></span><b>${esc(p.hazard === "FZLVL" ? "" : `${p.base}–${p.top}`)}</b></button>`;
      }).join("");
    $("areas").onclick = (e) => {
      const b = e.target.closest(".area");
      if (!b) return;
      const { layer } = shown[+b.dataset.i];
      map.fitBounds(layer.getBounds(), { maxZoom: 7, padding: [40, 40] });
      layer.openPopup(layer.getBounds().getCenter());
    };
  }

  // —— Official G-AIRMETs at the same valid time (AWC issues snapshots 0–12 hours ahead) ————————————
  async function drawOfficial() {
    officialLayer.clearLayers();
    $("offNote").textContent = "";
    if (!state.showOfficial || !state.fc) return;
    state.official ||= fetch(`${AWC}?${new URLSearchParams({ where: "1=1", outFields: "*", f: "geojson" })}`).then((r) => r.json()).catch(() => null);
    const gj = await state.official;
    if (!gj) { $("offNote").textContent = "unavailable"; return; }
    const t = validAt(model(), fhr());
    const parse = (s) => Date.parse(String(s).replace(/\//g, "-").replace(" ", "T").replace(/\+00$/, "Z"));
    const now = (gj.features || []).filter((f) => Math.abs(parse(f.properties.validtime) - t) < 90 * 60000);
    // AWC publishes only the current snapshot, so this works for one valid time at a time.
    const times = [...new Set((gj.features || []).map((f) => parse(f.properties.validtime)))].filter(Number.isFinite);
    $("offNote").textContent = now.length ? `${now.length} shown` : times.length ? `only ${times.map((x) => `${pad(new Date(x).getUTCHours())}Z`).join(", ")} published` : "none published";
    now.forEach((f) => {
      const p = f.properties, hz = OFFICIAL[p.hazard] || p.hazard;
      if (!state.on[hz]) return;
      const c = HZ[hz]?.[0] || "#9aa3c0";
      const alt = /FZLVL/.test(p.hazard) ? "" : ` ${p.base || "SFC"}–${p.top || ""}`;
      L.geoJSON(f, { renderer, style: { color: c, weight: 2.5, opacity: 1, fill: false } })
        .bindPopup(`<div class="pop"><h4>Official G-AIRMET · ${esc(p.hazard)}</h4><table><tr><td>Altitude</td><td>${esc(alt.trim() || "—")}</td></tr><tr><td>Severity</td><td>${esc(p.severity || "")}</td></tr><tr><td>Due to</td><td>${esc(p.dueto || "")}</td></tr><tr><td>Valid</td><td>${esc(p.validtime)} (+${esc(p.forecast)} h)</td></tr></table></div>`)
        .addTo(officialLayer);
    });
  }

  load();
})();
