// o!ily website: a small single-page app that reads bancho.py's v2 api at /api/v2.
// No build step: plain modern JavaScript, served as static files.

const API = "/api/v2";
const SERVER_DOMAIN = "oily.ilystudios.net";
const AVATARS = `https://a.${SERVER_DOMAIN}`;

// ---------- leaderboards ----------
// Each board maps the four rulesets (osu, taiko, catch, mania) to a bancho.py mode id.
// null = not available on that board. Lazer boards are placeholders until o!ily's lazer server exists.
const BOARDS = [
  { key: "stable", label: "Stable", modes: [0, 1, 2, 3] },
  { key: "stable-rx", label: "Stable Relax", modes: [4, 5, 6, null] },
  { key: "lazer", label: "Lazer", lazer: true },
  { key: "lazer-rx", label: "Lazer Relax", lazer: true },
  { key: "lazer-mods", label: "Lazer Mods", lazer: true, note: "fun and conversion mods" },
];
const RULESETS = ["osu!", "taiko", "catch", "mania"];
const SORTS = [
  ["pp", "Performance"], ["rscore", "Ranked score"], ["acc", "Accuracy"],
  ["plays", "Play count"], ["tscore", "Total score"],
];

function boardFor(key) { return BOARDS.find((b) => b.key === key) || BOARDS[0]; }

// ---------- small helpers ----------
const $ = (sel, root = document) => root.querySelector(sel);
const app = $("#app");

function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
const fmt = (n) => Number(n || 0).toLocaleString("en-US");
const pct = (n) => `${Number(n || 0).toFixed(2)}%`;
const ppFmt = (n) => `${Math.round(Number(n || 0)).toLocaleString("en-US")}pp`;
function hours(sec) { const h = Math.floor((sec || 0) / 3600); return `${fmt(h)}h`; }
function ago(input) {
  const t = typeof input === "number" ? input * 1000 : Date.parse(input);
  if (!t) return "";
  const s = Math.max(1, Math.round((Date.now() - t) / 1000));
  const units = [[31536000, "year"], [2592000, "month"], [86400, "day"], [3600, "hour"], [60, "minute"], [1, "second"]];
  for (const [n, name] of units) if (s >= n) { const v = Math.floor(s / n); return `${v} ${name}${v > 1 ? "s" : ""} ago`; }
  return "just now";
}
function flag(cc) {
  if (!cc || cc === "xx") return "";
  return `<img class="flag" src="https://flagcdn.com/w40/${esc(cc.toLowerCase())}.png" alt="${esc(cc.toUpperCase())}" title="${esc(cc.toUpperCase())}" loading="lazy">`;
}
function avatar(id, size) {
  return `<img class="avatar" src="${AVATARS}/${Number(id)}" width="${size}" height="${size}" alt="" loading="lazy" onerror="this.onerror=null;this.src='/assets/default-avatar.jpg'">`;
}
function cover(setId, kind = "list") { return `https://assets.ppy.sh/beatmaps/${Number(setId)}/covers/${kind}.jpg`; }

const MOD_BITS = [
  [1, "NF"], [2, "EZ"], [4, "TD"], [8, "HD"], [16, "HR"], [32, "SD"], [64, "DT"], [128, "RX"],
  [256, "HT"], [512, "NC"], [1024, "FL"], [2048, "AT"], [4096, "SO"], [8192, "AP"], [16384, "PF"],
  [1048576, "FI"], [2097152, "RD"], [4194304, "CN"], [8388608, "TP"], [536870912, "SV2"], [1073741824, "MR"],
];
function mods(bits) {
  let out = [];
  for (const [b, name] of MOD_BITS) if (bits & b) out.push(name);
  if (out.includes("NC")) out = out.filter((m) => m !== "DT");
  if (out.includes("PF")) out = out.filter((m) => m !== "SD");
  return out.length ? `+${out.join("")}` : "";
}
const STATUS = { "-2": "Graveyard", "-1": "WIP", 0: "Pending", 1: "Update", 2: "Ranked", 3: "Approved", 4: "Qualified", 5: "Loved" };
function grade(g) { return `<span class="grade grade-${esc(g)}">${esc(g === "XH" ? "SS" : g === "X" ? "SS" : g === "SH" ? "S" : g)}</span>`; }

// ---------- api ----------
async function api(path, opts = {}) {
  const res = await fetch(API + path, {
    credentials: "same-origin",
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    ...opts,
  });
  let body = null;
  try { body = await res.json(); } catch { /* empty body */ }
  if (!res.ok || !body || body.status !== "success") {
    const msg = body?.error || (Array.isArray(body?.detail) ? body.detail.map((d) => d.msg).join(" ") : null) || `Request failed (${res.status})`;
    const err = new Error(msg);
    err.status = res.status;
    throw err;
  }
  return body;
}

// ---------- session ----------
let me = null;
async function loadSession() {
  try { me = (await api("/sessions/current")).data; } catch { me = null; }
  renderAccount();
}
function renderAccount() {
  const box = $("#nav-account");
  if (me) {
    box.innerHTML = `<a class="player" href="/u/${me.id}" data-link>${avatar(me.id, 28)}<span>${esc(me.name)}</span></a>
      <button class="btn btn-small" id="logout">Log out</button>`;
    $("#logout").onclick = async () => {
      try { await api("/sessions/current", { method: "DELETE" }); } catch { /* already gone */ }
      me = null; renderAccount(); navigate("/");
    };
  } else {
    box.innerHTML = `<a class="btn btn-small" href="/login" data-link>Log in</a><a class="btn btn-small btn-accent" href="/register" data-link>Sign up</a>`;
  }
}

// ---------- router ----------
const routes = [
  [/^\/$/, home],
  [/^\/leaderboard$/, leaderboard],
  [/^\/connect$/, connect],
  [/^\/about$/, about],
  [/^\/login$/, login],
  [/^\/register$/, register],
  [/^\/u\/([^/]+)$/, profile],
  [/^\/b\/(\d+)$/, beatmap],
];

function navigate(path) {
  history.pushState(null, "", path);
  render();
}
document.addEventListener("click", (e) => {
  const a = e.target.closest("a[data-link]");
  if (!a || e.ctrlKey || e.metaKey || e.shiftKey || a.target === "_blank") return;
  e.preventDefault();
  navigate(a.getAttribute("href"));
});
window.addEventListener("popstate", render);

async function render() {
  const path = location.pathname.replace(/\/+$/, "") || "/";
  for (const a of document.querySelectorAll(".nav-links a")) a.classList.toggle("active", a.getAttribute("href") === path);
  window.scrollTo(0, 0);
  for (const [re, view] of routes) {
    const m = path.match(re);
    if (m) {
      app.innerHTML = `<div class="loading">Loading…</div>`;
      try { await view(...m.slice(1)); } catch (err) { app.innerHTML = `<div class="card error">${esc(err.message)}</div>`; }
      return;
    }
  }
  document.title = "Not found · o!ily";
  app.innerHTML = `<div class="card center"><h2>Page not found</h2><a href="/" data-link>Back home</a></div>`;
}

// ---------- pages ----------
async function home() {
  document.title = "o!ily";
  let stats = { online_players: "–", total_players: "–" };
  try { stats = (await api("/server/stats")).data; } catch { /* show dashes */ }
  app.innerHTML = `
    <section class="hero">
      <img src="/assets/logo.svg" alt="o!ily">
      <h1>o!ily</h1>
      <p>A relax-first osu! server. Climb the relax leaderboards, fresh ranked maps every week, and one-click video renders with the o!ily app.</p>
      <div class="row" style="justify-content:center">
        ${me ? `<a class="btn btn-accent" href="/connect" data-link>How to play</a>` : `<a class="btn btn-accent" href="/register" data-link>Create an account</a>`}
        <a class="btn" href="/leaderboard" data-link>Leaderboard</a>
      </div>
    </section>
    <div class="grid grid-2">
      <div class="card stat"><b><span class="dot"></span>${fmt(stats.online_players)}</b><span>players online</span></div>
      <div class="card stat"><b>${fmt(stats.total_players)}</b><span>registered players</span></div>
    </div>
    <div class="card" style="margin-top:16px">${connectSteps()}</div>`;
}

function connectSteps() {
  return `<h2>How to play on o!ily</h2>
    <ol class="steps">
      <li>${me ? "You have an account." : `<a href="/register" data-link>Create an o!ily account</a> (or register from inside osu!).`}</li>
      <li>Find your <b>osu!.exe</b> (osu!stable). Right-click your desktop, choose <b>New</b> then <b>Shortcut</b>, and paste the path to osu!.exe followed by the server option, for example:<br><code>"C:\\Users\\YOU\\AppData\\Local\\osu!\\osu!.exe" -devserver ${SERVER_DOMAIN}</code></li>
      <li>Name the shortcut <b>o!ily</b>. Opening osu! normally still goes to the official servers; only this shortcut goes to o!ily.</li>
      <li>Open the shortcut, log in with your o!ily account, and play. Turn on <b>Relax</b> to land on the relax leaderboard.</li>
    </ol>
    <p class="muted" style="margin-bottom:0">osu!lazer support is coming later.</p>`;
}
async function connect() {
  document.title = "How to play · o!ily";
  app.innerHTML = `<div class="card">${connectSteps()}</div>`;
}

async function about() {
  document.title = "About · o!ily";
  app.innerHTML = `<div class="card">
    <h2>About o!ily</h2>
    <p>o!ily is a community osu! private server made by ilystudios, built around relax. It is not affiliated with or endorsed by ppy Pty Ltd. osu! and its logo are trademarks of ppy Pty Ltd. Please support the official game at <a href="https://osu.ppy.sh" target="_blank" rel="noopener">osu.ppy.sh</a>.</p>
    <h3>Rules (short version)</h3>
    <ul>
      <li>No cheats, no third-party aim or timing tools, no replay editing or stealing.</li>
      <li>One account per person.</li>
      <li>Be decent in chat. Staff can warn, remove scores, restrict or ban.</li>
      <li>You must be 13 or older to play.</li>
    </ul>
    <h3>Credits</h3>
    <p>Server: <a href="https://github.com/osuAkatsuki/bancho.py" target="_blank" rel="noopener">bancho.py</a> by the Akatsuki team (MIT licence). Beatmap data and downloads come from public mirrors; beatmaps belong to their creators. Flags from flagcdn.com.</p>
  </div>`;
}

// leaderboard page; state lives in the url: ?board=stable-rx&rs=0&sort=pp&page=1&country=us
async function leaderboard() {
  document.title = "Leaderboard · o!ily";
  const q = new URLSearchParams(location.search);
  const board = boardFor(q.get("board") || "stable-rx");
  let rs = Number(q.get("rs") || 0);
  if (!board.lazer && board.modes[rs] == null) rs = 0;
  const sort = SORTS.some(([k]) => k === q.get("sort")) ? q.get("sort") : "pp";
  const page = Math.max(1, Number(q.get("page") || 1));
  const country = (q.get("country") || "").toLowerCase();

  const set = (changes) => {
    const n = new URLSearchParams({ board: board.key, rs, sort, page, ...(country ? { country } : {}), ...changes });
    if (!n.get("country")) n.delete("country");
    navigate(`/leaderboard?${n}`);
  };

  const head = `
    <div class="row" style="margin-bottom:16px"><h1 style="margin:0">Leaderboard</h1></div>
    <div class="row" style="margin-bottom:12px">
      <div class="pills">${BOARDS.map((b) => `<button data-board="${b.key}" class="${b.key === board.key ? "on" : ""}">${b.label}</button>`).join("")}</div>
    </div>`;

  if (board.lazer) {
    app.innerHTML = `${head}<div class="card center"><h2>${board.label}: coming soon</h2>
      <p class="muted">The ${board.label}${board.note ? ` (${board.note})` : ""} leaderboard opens when o!ily's lazer server launches. Stable and Stable Relax are live now.</p></div>`;
    bindBoardPills(set);
    return;
  }

  const mode = board.modes[rs];
  const params = new URLSearchParams({ sort, page, page_size: 50 });
  if (country) params.set("country", country);
  const rows = (await api(`/leaderboards/${mode}?${params}`)).data;

  app.innerHTML = `${head}
    <div class="row" style="margin-bottom:16px">
      <div class="pills">${RULESETS.map((name, i) => `<button data-rs="${i}" ${board.modes[i] == null ? "disabled" : ""} class="${i === rs ? "on" : ""}">${name}</button>`).join("")}</div>
      <div class="spacer"></div>
      <select id="sort" aria-label="Sort by">${SORTS.map(([k, l]) => `<option value="${k}" ${k === sort ? "selected" : ""}>${l}</option>`).join("")}</select>
      <input id="country" placeholder="Country (e.g. US)" maxlength="2" value="${esc(country.toUpperCase())}" style="width:150px">
    </div>
    <div class="card" style="padding:8px 12px">
      ${rows.length ? `<div class="table-wrap"><table>
        <thead><tr><th>#</th><th>Player</th><th class="num">Performance</th><th class="num">Accuracy</th><th class="num">Plays</th><th class="num">Ranked score</th><th class="num">SS</th><th class="num">S</th><th class="num">A</th></tr></thead>
        <tbody>${rows.map((r) => `<tr>
          <td class="rank">#${r.rank}</td>
          <td><a class="player" href="/u/${r.player_id}" data-link>${flag(r.country)}${r.clan_tag ? `<span class="muted">[${esc(r.clan_tag)}]</span>` : ""}${esc(r.name)}</a></td>
          <td class="num pp">${ppFmt(r.pp)}</td>
          <td class="num">${pct(r.acc)}</td>
          <td class="num">${fmt(r.plays)}</td>
          <td class="num">${fmt(r.rscore)}</td>
          <td class="num">${fmt(r.xh_count + r.x_count)}</td>
          <td class="num">${fmt(r.sh_count + r.s_count)}</td>
          <td class="num">${fmt(r.a_count)}</td>
        </tr>`).join("")}</tbody></table></div>` : `<div class="empty">No players on this leaderboard yet. Set a score to be first!</div>`}
    </div>
    <div class="row" style="justify-content:center;margin-top:16px">
      <button class="btn btn-small" id="prev" ${page <= 1 ? "disabled" : ""}>Previous</button>
      <span class="muted">Page ${page}</span>
      <button class="btn btn-small" id="next" ${rows.length < 50 ? "disabled" : ""}>Next</button>
    </div>`;

  bindBoardPills(set);
  for (const b of app.querySelectorAll("[data-rs]")) b.onclick = () => set({ rs: b.dataset.rs, page: 1 });
  $("#sort").onchange = (e) => set({ sort: e.target.value, page: 1 });
  $("#country").onchange = (e) => set({ country: e.target.value.trim().toLowerCase().slice(0, 2), page: 1 });
  $("#prev").onclick = () => set({ page: page - 1 });
  $("#next").onclick = () => set({ page: page + 1 });
}
function bindBoardPills(set) {
  for (const b of app.querySelectorAll("[data-board]")) b.onclick = () => set({ board: b.dataset.board, rs: 0, page: 1 });
}

function scoreRow(s, showPlayer = false) {
  const bm = s.beatmap;
  const title = bm ? `${bm.artist} - ${bm.title} [${bm.version}]` : "Unknown map";
  return `<div class="score-row">
    ${bm ? `<img class="thumb" src="${cover(bm.set_id)}" alt="" loading="lazy" onerror="this.style.visibility='hidden'">` : `<div class="thumb"></div>`}
    <div class="info">
      ${bm ? `<a class="title" href="/b/${bm.id}" data-link>${esc(title)}</a>` : `<span class="title">${esc(title)}</span>`}
      <div class="sub">${grade(s.grade)} · ${pct(s.acc)} · ${fmt(s.max_combo)}x · ${s.nmiss} miss${s.nmiss === 1 ? "" : "es"} · ${ago(s.play_time)}
        ${showPlayer && s.player ? ` · <a href="/u/${s.player.id}" data-link>${esc(s.player.name)}</a>` : ""}</div>
    </div>
    <div class="right"><div class="pp">${ppFmt(s.pp)}</div><div class="mods">${mods(s.mods)}</div></div>
  </div>`;
}

async function profile(idOrName) {
  const q = new URLSearchParams(location.search);
  const board = boardFor(q.get("board") || "stable-rx");
  const player = (await api(`/players/${encodeURIComponent(decodeURIComponent(idOrName))}`)).data;
  document.title = `${player.name} · o!ily`;

  const stableBoards = BOARDS.filter((b) => !b.lazer);
  const b = board.lazer ? stableBoards[1] : board;
  let rs = Number(q.get("rs") || 0);
  if (b.modes[rs] == null) rs = 0;
  const mode = b.modes[rs];

  const [stats, best, recent, status] = await Promise.all([
    api(`/players/${player.id}/stats/${mode}`).then((r) => r.data).catch(() => null),
    api(`/players/${player.id}/scores?scope=best&mode=${mode}&limit=50`).then((r) => r.data).catch(() => []),
    api(`/players/${player.id}/scores?scope=recent&mode=${mode}&limit=10`).then((r) => r.data).catch(() => []),
    api(`/players/${player.id}/status`).then((r) => r.data).catch(() => null),
  ]);

  const staff = player.priv & (1 << 14) ? "Developer" : player.priv & (1 << 13) ? "Admin" : player.priv & (1 << 12) ? "Moderator" : player.priv & (1 << 11) ? "Nominator" : "";
  const set = (changes) => navigate(`/u/${player.id}?${new URLSearchParams({ board: b.key, rs, ...changes })}`);

  app.innerHTML = `
    <div class="card">
      <div class="profile-head">
        ${avatar(player.id, 110)}
        <div>
          <h1 style="margin-bottom:6px">${flag(player.country)} ${esc(player.name)}</h1>
          <div class="row">
            ${staff ? `<span class="badge">${staff}</span>` : ""}
            <span class="muted">${status ? `<span class="dot"></span>Online now` : `Last seen ${ago(player.latest_activity)}`}</span>
            <span class="muted">· Joined ${new Date(player.creation_time * 1000).toLocaleDateString("en-US", { month: "long", year: "numeric" })}</span>
          </div>
        </div>
      </div>
    </div>
    <div class="row" style="margin:16px 0">
      <div class="pills">${stableBoards.map((x) => `<button data-board="${x.key}" class="${x.key === b.key ? "on" : ""}">${x.label}</button>`).join("")}</div>
      <div class="pills">${RULESETS.map((name, i) => `<button data-rs="${i}" ${b.modes[i] == null ? "disabled" : ""} class="${i === rs ? "on" : ""}">${name}</button>`).join("")}</div>
    </div>
    ${stats ? `<div class="grid grid-4">
      <div class="card stat"><b class="pp">${ppFmt(stats.pp)}</b><span>performance</span></div>
      <div class="card stat"><b>${stats.rank ? `#${fmt(stats.rank)}` : "–"}</b><span>global rank${stats.country_rank ? ` · #${fmt(stats.country_rank)} ${esc(player.country.toUpperCase())}` : ""}</span></div>
      <div class="card stat"><b>${pct(stats.acc)}</b><span>accuracy</span></div>
      <div class="card stat"><b>${fmt(stats.plays)}</b><span>plays · ${hours(stats.playtime)}</span></div>
    </div>` : ""}
    <div class="grid grid-2" style="margin-top:16px">
      <div class="card"><h3>Best scores</h3>${best.length ? best.map((s) => scoreRow(s)).join("") : `<div class="empty">No scores yet.</div>`}</div>
      <div class="card"><h3>Recent plays</h3>${recent.length ? recent.map((s) => scoreRow(s)).join("") : `<div class="empty">No recent plays.</div>`}</div>
    </div>`;

  for (const x of app.querySelectorAll("[data-board]")) x.onclick = () => set({ board: x.dataset.board, rs: 0 });
  for (const x of app.querySelectorAll("[data-rs]")) x.onclick = () => set({ rs: x.dataset.rs });
}

async function beatmap(id) {
  const q = new URLSearchParams(location.search);
  const map = (await api(`/maps/${Number(id)}`)).data;
  document.title = `${map.artist} - ${map.title} · o!ily`;
  // relax leaderboards only exist for osu!, taiko and catch maps; mania maps use the Stable board
  const board = boardFor(q.get("board") || (map.mode === 3 ? "stable" : "stable-rx"));
  const b = board.lazer ? boardFor("stable") : board;
  const mode = b.modes[map.mode] ?? b.modes[0];
  const scores = (await api(`/maps/${map.id}/scores?mode=${mode}&limit=50`).catch(() => ({ data: [] }))).data;
  const mins = `${Math.floor(map.total_length / 60)}:${String(map.total_length % 60).padStart(2, "0")}`;

  app.innerHTML = `
    <div class="cover" style="background-image:url('${cover(map.set_id, "cover")}')">
      <div class="cover-text">
        <h1 style="margin-bottom:4px">${esc(map.artist)} - ${esc(map.title)}</h1>
        <div class="row"><span>[${esc(map.version)}] mapped by <b>${esc(map.creator)}</b></span><span class="badge">${STATUS[map.status] || "Unknown"}</span></div>
      </div>
    </div>
    <div class="card" style="margin-top:16px">
      <div class="chips">
        <span class="chip">★ ${map.diff.toFixed(2)}</span><span class="chip">${mins}</span><span class="chip">${Math.round(map.bpm)} BPM</span>
        <span class="chip">CS ${map.cs}</span><span class="chip">AR ${map.ar}</span><span class="chip">OD ${map.od}</span><span class="chip">HP ${map.hp}</span>
        <span class="chip">${fmt(map.max_combo)}x max combo</span><span class="chip">${fmt(map.plays)} plays</span>
      </div>
    </div>
    <div class="row" style="margin:16px 0">
      <div class="pills">${BOARDS.filter((x) => !x.lazer).map((x) => `<button data-board="${x.key}" ${x.modes[map.mode] == null ? "disabled" : ""} class="${x.key === b.key ? "on" : ""}">${x.label}</button>`).join("")}</div>
    </div>
    <div class="card" style="padding:8px 12px">
      ${scores.length ? `<div class="table-wrap"><table>
        <thead><tr><th>#</th><th>Player</th><th class="num">pp</th><th class="num">Accuracy</th><th class="num">Combo</th><th class="num">Misses</th><th>Mods</th><th>Grade</th><th class="num">When</th></tr></thead>
        <tbody>${scores.map((s, i) => `<tr>
          <td class="rank">#${i + 1}</td>
          <td><a class="player" href="/u/${s.player.id}" data-link>${flag(s.player.country)}${esc(s.player.name)}</a></td>
          <td class="num pp">${ppFmt(s.pp)}</td><td class="num">${pct(s.acc)}</td><td class="num">${fmt(s.max_combo)}x</td>
          <td class="num">${s.nmiss}</td><td class="mods">${mods(s.mods)}</td><td>${grade(s.grade)}</td><td class="num muted">${ago(s.play_time)}</td>
        </tr>`).join("")}</tbody></table></div>` : `<div class="empty">No scores on this map yet.</div>`}
    </div>`;
  for (const x of app.querySelectorAll("[data-board]")) x.onclick = () => navigate(`/b/${map.id}?board=${x.dataset.board}`);
}

async function login() {
  document.title = "Log in · o!ily";
  if (me) return navigate(`/u/${me.id}`);
  app.innerHTML = `<div class="card">
    <h2 class="center">Log in to o!ily</h2>
    <form class="form" id="f">
      <label>Username<input name="username" autocomplete="username" required></label>
      <label>Password<input name="password" type="password" autocomplete="current-password" required></label>
      <div id="msg"></div>
      <button class="btn btn-accent" type="submit">Log in</button>
      <p class="muted center" style="margin:0">No account? <a href="/register" data-link>Sign up</a></p>
    </form></div>`;
  $("#f").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      me = (await api("/sessions", { method: "POST", body: JSON.stringify({ username: f.get("username"), password: f.get("password") }) })).data;
      renderAccount();
      navigate(`/u/${me.id}`);
    } catch (err) { $("#msg").innerHTML = `<div class="error">${esc(err.message)}</div>`; }
  };
}

async function register() {
  document.title = "Sign up · o!ily";
  if (me) return navigate("/connect");
  app.innerHTML = `<div class="card">
    <h2 class="center">Create your o!ily account</h2>
    <p class="muted center">This account is only for o!ily. It is separate from your osu! account.</p>
    <form class="form" id="f">
      <label>Username<input name="username" autocomplete="username" minlength="2" maxlength="15" required></label>
      <label>Email<input name="email" type="email" autocomplete="email" required></label>
      <label>Password<input name="password" type="password" autocomplete="new-password" minlength="8" maxlength="32" required></label>
      <label style="display:flex;gap:8px;align-items:flex-start;font-weight:400"><input type="checkbox" required style="margin-top:4px"> I am 13 or older and agree to the <a href="/about" data-link>rules</a>.</label>
      <div id="msg"></div>
      <button class="btn btn-accent" type="submit">Sign up</button>
    </form></div>`;
  $("#f").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const creds = { username: f.get("username"), password: f.get("password") };
    try {
      await api("/accounts", { method: "POST", body: JSON.stringify({ ...creds, email: f.get("email") }) });
      me = (await api("/sessions", { method: "POST", body: JSON.stringify(creds) })).data;
      renderAccount();
      navigate("/connect");
    } catch (err) { $("#msg").innerHTML = `<div class="error">${esc(err.message)}</div>`; }
  };
}

// ---------- player search ----------
let searchTimer;
$("#search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  const box = $("#search-results");
  const q = e.target.value.trim();
  if (q.length < 2) { box.hidden = true; return; }
  searchTimer = setTimeout(async () => {
    try {
      const found = (await api(`/players/search?q=${encodeURIComponent(q)}`)).data.slice(0, 8);
      box.innerHTML = found.length ? found.map((p) => `<a href="/u/${p.id}" data-link>${esc(p.name)}</a>`).join("") : `<div class="loading" style="padding:10px">No players found</div>`;
      box.hidden = false;
    } catch { box.hidden = true; }
  }, 250);
});
document.addEventListener("click", (e) => { if (!e.target.closest(".nav-search")) $("#search-results").hidden = true; });

loadSession();
render();
