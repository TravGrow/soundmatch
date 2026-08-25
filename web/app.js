const $ = (s) => document.querySelector(s);
const el = (tag, cls, html) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html != null) n.innerHTML = html;
  return n;
};
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const mmss = (s) => {
  if (s == null) return "—";
  const m = Math.floor(s / 60), r = Math.round(s % 60);
  return `${m}:${String(r).padStart(2, "0")}`;
};

let current = null;   // latest result payload
let playing = null;   // the <audio> element currently sounding

// Every candidate is analysed, but only the best few are worth showing by
// default -- the long tail exists to make the ranking trustworthy, not to be
// read. The rest stay one click away.
const TOP_N = 10;
let showAll = false;

// -------------------------------------------------------------------- submit

$("#form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const url = $("#url").value.trim();
  if (!url) return;

  $("#go").disabled = true;
  for (const id of ["#error", "#source", "#exact", "#results"]) $(id).hidden = true;
  $("#steps").innerHTML = "";
  showAll = false;
  $("#spinner").classList.remove("done");
  $("#stage-msg").textContent = "Starting…";
  $("#progress").hidden = false;

  try {
    const r = await fetch("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    if (!r.ok) throw new Error((await r.json()).detail || "Request failed");
    const { job_id } = await r.json();
    listen(job_id);
  } catch (err) {
    fail(err.message);
  }
});

function fail(msg) {
  $("#progress").hidden = true;
  $("#error").hidden = false;
  $("#error").textContent = msg;
  $("#go").disabled = false;
}

// Stream progress events, then pull the finished result.
function listen(jobId) {
  const src = new EventSource(`/api/stream/${jobId}`);
  const seen = new Set();

  src.onmessage = async (e) => {
    const ev = JSON.parse(e.data);
    if (ev.stage === "_end") {
      src.close();
      const r = await (await fetch(`/api/result/${jobId}`)).json();
      $("#go").disabled = false;
      if (r.status === "error") return fail(r.error || "Something went wrong");
      $("#spinner").classList.add("done");
      $("#stage-msg").textContent = "Done";
      render(r.result);
      return;
    }
    if (ev.stage === "error") return fail(ev.message);

    $("#stage-msg").textContent = ev.message;
    if (ev.transient) return;          // live-only ticks like download progress
    const key = ev.stage + "|" + ev.message;
    if (!seen.has(key)) {
      seen.add(key);
      $("#steps").append(el("li", null, esc(ev.message)));
    }
  };
  src.onerror = () => { src.close(); $("#go").disabled = false; };
}

// -------------------------------------------------------------------- render

function render(res) {
  current = res;
  renderSource(res);
  renderExact(res);
  renderList(res.results);
  const shown = Math.min(TOP_N, res.results.length);
  $("#results-note").textContent =
    `Best ${shown} of ${res.results.length} tracks compared against your clip`;
  $("#results").hidden = res.results.length === 0;
}

function renderSource(res) {
  const f = res.features, d = res.descriptors, s = res.source;
  const id = res.identified;
  const name = (id && id.title) || s.track || s.post_title || "Your clip";
  const by = (id && id.artist) || s.artist || s.uploader;

  // When the recording is a known commercial release, say so plainly: it is
  // usually the thing the person actually wanted to know.
  const idBlock = id ? `
    <div class="ident">
      <span class="ident-label">Track identified</span>
      <div class="ident-name">${esc(id.title)} <span>— ${esc(id.artist)}</span></div>
      <div class="src-sub">${[id.album, id.label, id.released, id.genre]
        .filter(Boolean).map(esc).join(" · ")}</div>
      ${id.url ? `<a class="btn" style="margin-top:10px;display:inline-block"
         href="${esc(id.url)}" target="_blank" rel="noopener">View on Shazam</a>` : ""}
    </div>` : "";

  const chips = [
    ["Tempo", `${Math.round(f.tempo)} BPM`],
    ["Key", f.key],
    ["Length", mmss(f.duration)],
    ["Feel", `${d.pace}, ${d.brightness}`],
    ["Energy", d.energy],
    ["Texture", d.texture],
  ].map(([k, v]) => `<span class="chip">${esc(k)} <b>${esc(v)}</b></span>`).join("");

  const searched = res.queries.slice(0, 6)
    .map((q) => `<span class="chip">${esc(q.q)}</span>`).join("");

  $("#source").innerHTML = `
    <div class="src-head">
      <div>
        <h2>${esc(name)}</h2>
        <p class="src-sub">${by ? "by " + esc(by) + " · " : ""}${esc(s.extractor || "")}</p>
      </div>
    </div>
    ${idBlock}
    <audio controls preload="none" src="${esc(res.query_audio)}"></audio>
    <div class="chips">${chips}</div>
    <p class="src-sub" style="margin-top:16px">Searched Pixabay for</p>
    <div class="chips">${searched}</div>`;
  $("#source").hidden = false;
}

function renderExact(res) {
  const m = res.exact_match;
  const box = $("#exact");
  if (!m) { box.hidden = true; return; }
  box.innerHTML = `
    <div class="exact-card">
      <span class="exact-badge">✓ ${m.match_type === "exact" ? "Exact match found" : "Almost certainly the same track"}</span>
      <h3>${esc(m.name)}</h3>
      <p class="src-sub">${esc(m.match_reason)} · ${m.fingerprint_peak} aligned fingerprint points · ${mmss(m.duration)} · ${Math.round(m.tempo)} BPM · key ${esc(m.key)}</p>
      <audio controls preload="none" src="${esc(m.audio)}"></audio>
      <div class="chips" style="margin-top:14px">
        <a class="btn" href="${esc(m.url)}" target="_blank" rel="noopener">Open on Pixabay</a>
        <a class="btn" href="${esc(m.download)}" target="_blank" rel="noopener">Download</a>
      </div>
    </div>`;
  box.hidden = false;
}

const SORTS = {
  score: (a, b) => b.score - a.score,
  tempo: (a, b) => b.parts.tempo - a.parts.tempo,
  timbre: (a, b) => b.parts.timbre - a.parts.timbre,
  harmony: (a, b) => b.parts.harmony - a.parts.harmony,
  likes: (a, b) => (b.likes || 0) - (a.likes || 0),
};

$("#sort").addEventListener("change", () => {
  if (current) renderList(current.results);
});

function renderList(results) {
  const mode = $("#sort").value;
  const rows = [...results].sort(SORTS[mode]);
  const wrap = $("#cards");
  wrap.innerHTML = "";

  (showAll ? rows : rows.slice(0, TOP_N)).forEach((r) => wrap.append(card(r)));

  if (rows.length > TOP_N) {
    const btn = el("button", "showmore", showAll
      ? `Show top ${TOP_N} only`
      : `Show all ${rows.length} ranked results`);
    btn.type = "button";
    btn.addEventListener("click", () => { showAll = !showAll; renderList(results); });
    wrap.append(btn);
  }
}

function ring(score) {
  const R = 22, C = 2 * Math.PI * R;
  const hue = score >= 85 ? "#4ade80" : score >= 72 ? "#38bdf8" : score >= 60 ? "#94a3b8" : "#64748b";
  return `<div class="ring">
    <svg width="54" height="54" viewBox="0 0 54 54">
      <circle cx="27" cy="27" r="${R}" fill="none" stroke="#262f3d" stroke-width="4"/>
      <circle cx="27" cy="27" r="${R}" fill="none" stroke="${hue}" stroke-width="4"
              stroke-linecap="round" stroke-dasharray="${C}"
              stroke-dashoffset="${C * (1 - score / 100)}"/>
    </svg><span>${Math.round(score)}</span></div>`;
}

const PART_LABELS = {
  timbre: "Texture", harmony: "Harmony", tempo: "Tempo",
  energy: "Energy", rhythm: "Rhythm", tags: "Tags",
};

function card(r) {
  const n = el("div", "card" + (r.fingerprint_peak >= 12 ? " is-exact" : ""));

  const bars = Object.entries(PART_LABELS).map(([k, label]) => {
    const v = r.parts[k] ?? 0;
    return `<div class="bar">${label} ${Math.round(v)}<i><b style="width:${v}%"></b></i></div>`;
  }).join("");

  const badges = [
    `<span class="badge b-${r.match_type}">${esc(r.match_reason)}</span>`,
    r.ai_generated ? '<span class="badge b-ai">AI generated</span>' : "",
  ].join("");

  n.innerHTML = `
    ${ring(r.score)}
    <div>
      <div class="c-title"><a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.name)}</a></div>
      <div class="c-meta">${mmss(r.duration)} · ${Math.round(r.tempo)} BPM · key ${esc(r.key)}${r.same_key ? " (same key)" : ""}${r.likes ? " · " + r.likes.toLocaleString() + " likes" : ""}</div>
      <div class="c-why">${badges}</div>
      <div class="bars">${bars}</div>
    </div>
    <div class="c-actions">
      <button class="btn play" type="button">▶ Play</button>
      <a class="btn" href="${esc(r.download)}" target="_blank" rel="noopener">Download</a>
    </div>`;

  // One shared player: starting a track stops whatever else was sounding.
  const btn = n.querySelector(".play");
  const audio = new Audio(r.audio);
  audio.preload = "none";
  audio.addEventListener("ended", () => { btn.textContent = "▶ Play"; });
  btn.addEventListener("click", () => {
    if (playing && playing !== audio) {
      playing.pause();
      playing.dispatchEvent(new Event("stopped"));
    }
    if (audio.paused) {
      audio.play();
      playing = audio;
      btn.textContent = "❚❚ Pause";
    } else {
      audio.pause();
      btn.textContent = "▶ Play";
    }
  });
  audio.addEventListener("stopped", () => { btn.textContent = "▶ Play"; });

  return n;
}
