/* GitZip Bot UI — talks to the Python server; uploads keep running there even if this tab is closed. */
(function () {
  "use strict";

  // ============================================================ helpers
  var $ = function (s, el) { return (el || document).querySelector(s); };
  var $$ = function (s, el) { return Array.prototype.slice.call((el || document).querySelectorAll(s)); };
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) { return ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]; }); }
  function ls(k, d) { try { var v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } }
  function lsSet(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { } }
  function bytes(n) { n = n || 0; if (n < 1024) return n + " B"; if (n < 1048576) return (n / 1024).toFixed(1) + " KB"; if (n < 1073741824) return (n / 1048576).toFixed(1) + " MB"; return (n / 1073741824).toFixed(2) + " GB"; }
  function ago(ts) { if (!ts) return ""; var s = Math.max(0, Date.now() / 1000 - ts); if (s < 60) return "abhi"; if (s < 3600) return Math.floor(s / 60) + " min pehle"; if (s < 86400) return Math.floor(s / 3600) + " ghante pehle"; return Math.floor(s / 86400) + " din pehle"; }
  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  function debounce(fn, ms) { var t; return function () { var a = arguments, c = this; clearTimeout(t); t = setTimeout(function () { fn.apply(c, a); }, ms); }; }
  function base(p) { var a = p.split("/"); return a[a.length - 1]; }
  function toast(msg) { var t = $("#toast"); t.textContent = msg; t.classList.add("show"); clearTimeout(t._t); t._t = setTimeout(function () { t.classList.remove("show"); }, 2800); }

  var IC = {};
  (function (P) { Object.keys(P).forEach(function (k) { IC[k] = '<svg viewBox="0 0 24 24"><path d="' + P[k] + '"/></svg>'; }); })({
    folder: "M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z",
    file: "M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z M14 3v5h5",
    zip: "M4 4h16v4H4z M5 8v12h14V8 M10 12h4",
    lock: "M6 11h12v9H6z M8 11V8a4 4 0 0 1 8 0v3",
    globe: "M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20z M2 12h20 M12 2c3 3 4.5 6.5 4.5 10S15 19 12 22c-3-3-4.5-6.5-4.5-10S9 5 12 2z",
    plus: "M12 5v14 M5 12h14", send: "M5 12h14 M13 6l6 6-6 6", clip: "M21 12.5l-8.5 8.5a5.5 5.5 0 0 1-8-8L13 4.5a3.7 3.7 0 0 1 5.3 5.3l-8.5 8.5a1.8 1.8 0 0 1-2.6-2.6L14.5 8",
    check: "M5 12l5 5 9-10", warn: "M12 4l9 16H3z M12 10v4 M12 17h.01"
  });

  var S = { authed: false, status: null, settings: null, staging: [], repos: null, uploads: [], rid: 0, timers: [], back: null, dismiss: null,
    active: ls("gz_active", ""), down: false };
  function ApiErr(status, msg, data) { var e = new Error(msg); e.status = status; e.data = data || {}; return e; }

  // ============================================================ api
  async function api(method, path, body, o) {
    o = o || {};
    var opt = { method: method, headers: { "X-GZ": "1" }, credentials: "same-origin" };
    if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
    var r;
    try { r = await fetch(path, opt); } catch (e) { S.down = true; updateBadge(); throw ApiErr(0, "Server se connect nahi ho paya (internet band ya server so raha hai)."); }
    S.down = false;
    var data = {}; try { data = await r.json(); } catch (e) { }
    if (r.status === 401 && !o.noAuth) { S.authed = false; showLogin(); throw ApiErr(401, data.error || "Login karo", data); }
    if (!r.ok) throw ApiErr(r.status, data.error || ("Error " + r.status), data);
    return data;
  }

  // ============================================================ sheets
  function sheet(html) { $("#sheetInner").innerHTML = '<div class="grab"></div>' + html; $("#sheet").classList.add("open"); }
  function shut() { S.dismiss = null; $("#sheet").classList.remove("open"); }
  function closeSheet() { $("#sheet").classList.remove("open"); if (S.dismiss) { var d = S.dismiss; S.dismiss = null; d(null); } }
  $("#sheet").addEventListener("click", function (e) { if (e.target.id === "sheet") closeSheet(); });
  function menu(title, items) {
    sheet("<h3>" + esc(title) + "</h3>" + items.map(function (m, i) { return '<button class="btn block mi' + (m[2] ? " danger" : "") + '" data-m="' + i + '">' + m[0] + "</button>"; }).join(""));
    $$("[data-m]", $("#sheetInner")).forEach(function (b) { b.onclick = function () { shut(); items[+b.dataset.m][1](); }; });
  }
  // ask({title,msg,fields:[{k,l,v,p,type,opts}],ok,danger}) -> Promise<{k:value}|null>
  function ask(o) {
    return new Promise(function (res) {
      sheet("<h3>" + esc(o.title || "") + "</h3>" + (o.msg ? '<div class="muted">' + o.msg + "</div>" : "") +
        (o.fields || []).map(function (f) {
          var lab = f.l ? "<label>" + esc(f.l) + "</label>" : "";
          if (f.opts) return lab + '<select data-f="' + f.k + '">' + f.opts.map(function (x) { return "<option" + (x === f.v ? " selected" : "") + ">" + esc(x) + "</option>"; }).join("") + "</select>";
          if (f.type === "textarea") return lab + '<textarea data-f="' + f.k + '" rows="6" spellcheck="false" style="white-space:pre-wrap">' + esc(f.v || "") + "</textarea>";
          return lab + '<input type="' + (f.type || "text") + '" data-f="' + f.k + '" value="' + esc(f.v || "") + '" placeholder="' + esc(f.p || "") + '" autocapitalize="none" autocomplete="off" spellcheck="false">';
        }).join("") +
        '<div class="row" style="margin-top:14px"><button class="btn" id="mNo" style="flex:1">Cancel</button><button class="btn ' + (o.danger ? "danger" : "primary") + '" id="mOk" style="flex:1">' + esc(o.ok || "OK") + "</button></div>");
      S.dismiss = res;
      $("#mNo").onclick = closeSheet;
      $("#mOk").onclick = function () { var v = {}; $$("[data-f]", $("#sheetInner")).forEach(function (i) { v[i.dataset.f] = i.tagName === "TEXTAREA" ? i.value : i.value.trim(); }); shut(); res(v); };
      var f = $("#sheetInner input,#sheetInner textarea"); if (f) setTimeout(function () { f.focus(); }, 80);
    });
  }
  function confirmSheet(title, msg, ok, danger) { return ask({ title: title, msg: msg, ok: ok, danger: danger }); }

  // ============================================================ theme + badge
  function applyDark() { var d = ls("zg_dark", null); if (d === null) d = !!(window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches); document.documentElement.classList.toggle("dark", !!d); }
  applyDark();
  function updateBadge() {
    var b = $("#netBadge"), st = S.status;
    if (!navigator.onLine) { b.textContent = "offline"; b.className = "badge off"; }
    else if (S.down) { b.textContent = "server?"; b.className = "badge off"; }
    else if (st && (st.running_job || st.queued)) { b.textContent = "uploading"; b.className = "badge run"; }
    else { b.textContent = "online"; b.className = "badge"; }
  }
  window.addEventListener("online", updateBadge); window.addEventListener("offline", updateBadge);
  function startGlobalPoll() {
    setInterval(function () { if (!S.authed || document.hidden) return; api("GET", "/api/status").then(function (s) { S.status = s; updateBadge(); }).catch(function () { }); }, 12000);
  }

  // ============================================================ navigation
  function clearTimers() { S.timers.forEach(clearTimeout); S.timers = []; }
  function later(fn, ms, rid) { S.timers.push(setTimeout(function () { if (rid === S.rid) fn(); }, ms)); }
  function nav(h) { if (location.hash === h) route(); else location.hash = h; }
  var view = $("#view"), titleEl = $("#title"), backBtn = $("#backBtn");
  backBtn.onclick = function () { if (S.back) S.back(); else nav("#/"); };
  $("#settingsBtn").onclick = function () { nav("#/settings"); };
  $$("[data-tab]").forEach(function (b) { b.onclick = function () { nav(b.dataset.tab === "home" ? "#/" : "#/" + b.dataset.tab); }; });
  window.addEventListener("hashchange", route);

  var TAB = { home: "home", stage: "home", ai: "ai", jobs: "jobs", job: "jobs", repos: "repos", repo: "repos" };
  function chrome(screen, title, back) {
    titleEl.textContent = title;
    S.back = back || null;
    backBtn.style.display = back ? "" : "none";
    var tab = TAB[screen];
    $("#tabs").classList.toggle("off", !tab);
    $$("#tabs button").forEach(function (b) { b.classList.toggle("on", b.dataset.tab === tab); });
    view.classList.remove("in"); void view.offsetWidth; view.classList.add("in");
    window.scrollTo(0, 0);
  }
  function route() {
    if (!S.authed) return;
    clearTimers(); var rid = ++S.rid;
    S.uploads = S.uploads.filter(function (u) { return u.status === "up" || u.status === "unzip"; });
    var raw = (location.hash || "#/").slice(2), qs = "", qi = raw.indexOf("?");
    if (qi > -1) { qs = raw.slice(qi + 1); raw = raw.slice(0, qi); }
    var parts = raw.split("/").map(function (x) { try { return decodeURIComponent(x); } catch (e) { return x; } }), q = new URLSearchParams(qs);
    var name = parts[0] || "home";
    var run = { home: function () { return homeView(rid); }, stage: function () { return stageView(parts[1], rid); }, jobs: function () { return jobsView(rid); },
      job: function () { return jobView(parts[1], rid); }, ai: function () { return aiView(rid); }, repos: function () { return reposView(rid); },
      repo: function () { return repoView(parts[1] + "/" + parts[2], q.get("p") || "", rid); }, settings: function () { return settingsView(rid); } }[name] || function () { return homeView(rid); };
    Promise.resolve().then(run).catch(function (e) { if (rid === S.rid && e.status !== 401) view.innerHTML = '<div class="card"><div class="err">' + esc(e.message) + '</div><button class="btn block" id="retryBtn">Retry</button></div>', $("#retryBtn").onclick = route; });
  }

  // ============================================================ login / boot
  function showLogin() {
    clearTimers(); S.rid++;
    $("#tabs").classList.add("off"); backBtn.style.display = "none"; titleEl.textContent = "GitZip Bot";
    view.innerHTML = '<div class="login card"><div class="logo"><svg viewBox="0 0 24 24"><path d="M6 11h12v9H6z M8 11V8a4 4 0 0 1 8 0v3"/></svg></div><h2>GitZip Bot</h2><div class="muted">Password daalo (Render → Environment → <b>APP_PASSWORD</b>)</div>' +
      '<input type="password" id="lpw" placeholder="Password" autocomplete="current-password"><div class="err" id="lerr"></div><button class="btn primary block" id="lgo">Login</button></div>';
    async function go() {
      $("#lerr").textContent = "";
      try { await api("POST", "/api/login", { password: $("#lpw").value }, { noAuth: true }); S.authed = true; await afterLogin(); }
      catch (e) { $("#lerr").textContent = e.message; }
    }
    $("#lgo").onclick = go; $("#lpw").onkeydown = function (e) { if (e.key === "Enter") go(); }; setTimeout(function () { $("#lpw").focus(); }, 50);
  }
  async function afterLogin() {
    var r = await Promise.all([api("GET", "/api/settings"), api("GET", "/api/status")]);
    S.settings = r[0]; S.status = r[1]; updateBadge();
    await restoreBackup();
    if (!S.poller) { S.poller = true; startGlobalPoll(); }
    route();
  }
  async function restoreBackup() {
    var b = ls("gz_backup", null); if (!b || !ls("gz_backup_on", true)) return;
    var patch = {};
    if (b.github_token && !S.settings.github_token.set) patch.github_token = b.github_token;
    if (b.ai_key && !S.settings.ai_key.set) patch.ai_key = b.ai_key;
    if (Object.keys(patch).length) {
      try { var r = await api("POST", "/api/settings", patch); S.settings = r.settings; S.status = await api("GET", "/api/status"); toast("Server restart ke baad tokens wapas set ho gaye ✓"); } catch (e) { }
    }
  }
  async function boot() {
    view.innerHTML = '<div class="empty"><i class="spin"></i>Server se connect ho raha hai…<br><span class="muted">Render free plan par so raha ho to 30-60 sec lagte hain</span></div>';
    for (var i = 0; i < 40; i++) {
      try { var s = await api("GET", "/api/session", undefined, { noAuth: true }); S.authed = !!s.authed; break; }
      catch (e) { await sleep(3000); }
    }
    if (!S.authed) return showLogin();
    try { await afterLogin(); } catch (e) { if (e.status !== 401) view.innerHTML = '<div class="card"><div class="err">' + esc(e.message) + "</div></div>"; }
  }

  // ============================================================ uploading files to the server (resumable, chunked)
  function xhrPut(url, blob, onprog) {
    return new Promise(function (res, rej) {
      var x = new XMLHttpRequest(); x.open("PUT", url); x.timeout = 120000;
      x.setRequestHeader("X-GZ", "1"); x.setRequestHeader("Content-Type", "application/octet-stream");
      x.upload.onprogress = function (e) { if (onprog && e.lengthComputable) onprog(e.loaded); };
      x.onload = function () { var d = {}; try { d = JSON.parse(x.responseText); } catch (e) { } if (x.status >= 200 && x.status < 300) res(d); else rej(ApiErr(x.status, d.error || "Error " + x.status, d)); };
      x.onerror = function () { rej(ApiErr(0, "Network error")); }; x.ontimeout = function () { rej(ApiErr(0, "Timeout")); };
      x.send(blob);
    });
  }
  async function uploadOne(file, o, onprog) {
    var init = await api("POST", "/api/upload/init", { name: file.name, size: file.size, staging_id: o.stagingId || null, rel: o.rel || null });
    var off = 0, fails = 0;
    while (off < file.size) {
      var end = Math.min(off + init.chunk, file.size);
      try {
        var r = await xhrPut("/api/upload/" + init.uid + "?offset=" + off, file.slice(off, end), function (n) { onprog(off + n); });
        off = r.offset; fails = 0;
      } catch (e) {
        if (e.status === 409 && e.data && typeof e.data.offset === "number") { off = e.data.offset; continue; }   // resync with server
        if (e.status === 401) { showLogin(); throw e; }
        if ([400, 403, 413, 507].indexOf(e.status) > -1 || ++fails > 6) throw e;
        await sleep(1000 * fails);
      }
      onprog(off);
    }
    for (var t = 0; ; t++) {
      try { return await api("POST", "/api/upload/" + init.uid + "/finish", { staging_id: o.stagingId || null }); }
      catch (e) { if (t > 3 || e.status === 400 || e.status === 404) throw e; await sleep(1500); }
    }
  }
  function paintUploads() {
    var box = $("#ups"); if (!box) return;
    box.innerHTML = S.uploads.filter(function (u) { return u.status !== "gone"; }).map(function (u) {
      var pct = u.total ? Math.min(100, u.sent / u.total * 100) : 0;
      var msg = u.status === "up" ? "Server par bhej raha hoon… " + pct.toFixed(0) + "%" : u.status === "unzip" ? "Extract ho raha hai… " + (u.msg || "") : u.status === "done" ? "✓ Tayyar" : "✗ " + esc(u.msg);
      return '<div class="card" style="padding:12px"><div class="name">' + esc(u.name) + '</div><div class="bar sm"><i style="width:' + (u.status === "up" ? pct : 100) + '%"></i></div><div class="muted ' + (u.status === "err" ? "err" : "") + '">' + msg + "</div></div>";
    }).join("");
  }
  async function waitReady(u, sid) {
    u.status = "unzip";
    for (;;) {
      var d = await api("GET", "/api/staging/" + sid);
      if (d.status === "ready") { u.status = "done"; return d; }
      if (d.status === "error") throw ApiErr(400, d.error || "Extract fail");
      u.msg = d.progress && d.progress.total ? d.progress.done + "/" + d.progress.total : ""; paintUploads(); await sleep(900);
    }
  }
  window.addEventListener("beforeunload", function (e) { if (S.uploads.some(function (u) { return u.status === "up"; })) { e.preventDefault(); e.returnValue = ""; } });

  // Adds zips (each becomes a staging area) or loose files / a folder (one staging area). Returns staging id.
  async function addFiles(list, kind) {
    list = Array.prototype.slice.call(list); if (!list.length) return null;
    var last = null;
    try {
      if (kind === "zip") {
        for (var i = 0; i < list.length; i++) {
          var f = list[i], u = { name: f.name, total: f.size, sent: 0, status: "up", msg: "" }; S.uploads.push(u); paintUploads();
          try {
            var st = await uploadOne(f, {}, function (n) { u.sent = n; paintUploads(); });
            last = (await waitReady(u, st.id)).id;
          } catch (e) { u.status = "err"; u.msg = e.message; toast(f.name + ": " + e.message); }
          paintUploads();
        }
      } else {
        var first = list[0], label = first.webkitRelativePath ? first.webkitRelativePath.split("/")[0] : (list.length === 1 ? first.name : "Files " + new Date().toLocaleTimeString());
        var u2 = { name: label + " (" + list.length + " files)", total: list.reduce(function (a, f) { return a + f.size; }, 0), sent: 0, status: "up", msg: "" }; S.uploads.push(u2); paintUploads();
        var sg = await api("POST", "/api/staging", { name: label }), done = 0;
        try {
          for (var k = 0; k < list.length; k++) {
            var ff = list[k]; if (!ff.size) { continue; }
            await uploadOne(ff, { stagingId: sg.id, rel: ff.webkitRelativePath || ff.name }, function (n) { u2.sent = done + n; paintUploads(); });
            done += ff.size; u2.sent = done;
          }
          await api("POST", "/api/staging/" + sg.id + "/recount", {}); u2.status = "done"; last = sg.id;
        } catch (e) { u2.status = "err"; u2.msg = e.message; toast(e.message); }
        paintUploads();
      }
    } finally { await refreshStaging(); }
    if (last) { S.active = last; lsSet("gz_active", last); }
    return last;
  }
  async function refreshStaging() { try { S.staging = (await api("GET", "/api/staging")).staging; S.stagingLoaded = true; } catch (e) { } if ($("#stl")) paintStagingList(); }
  // file inputs are wired once; pick() only opens the dialog (S.pickCb = what to do with the result)
  [["#pickZip", "zip"], ["#pickDir", "loose"], ["#pickFiles", "loose"]].forEach(function (x) {
    var inp = $(x[0]);
    inp.onchange = async function () {
      var files = Array.prototype.slice.call(inp.files), cb = S.pickCb; inp.value = ""; S.pickCb = null;
      if (!files.length) return;
      var sid = await addFiles(files, x[1]);
      if (sid && cb) cb(sid);
      else if (sid) toast("Add ho gaya ✓ — list se kholo aur GitHub par bhejo");
    };
  });
  function pick(kind) {
    S.pickCb = S.pickDone; S.pickDone = null;
    $(kind === "zip" ? "#pickZip" : kind === "dir" ? "#pickDir" : "#pickFiles").click();
  }

  // ============================================================ HOME
  function paintStagingList() {
    var box = $("#stl"); if (!box) return;
    if (!S.stagingLoaded) { box.innerHTML = '<div class="empty"><i class="spin"></i></div>'; return; }
    if (!S.staging.length) { box.innerHTML = '<div class="empty"><span class="big-i">📦</span>Abhi kuch add nahi hai.<br>Upar se zip ya files add karo.</div>'; return; }
    box.innerHTML = '<div class="list">' + S.staging.map(function (m, i) {
      var sub = m.status === "ready" ? m.files + " files · " + bytes(m.bytes) + " · " + ago(m.created) : m.status === "extracting" ? "Extract ho raha hai… " + (m.progress && m.progress.total ? m.progress.done + "/" + m.progress.total : "") : "❌ " + esc(m.error);
      return '<div class="item" data-s="' + m.id + '" style="--i:' + i + '"><div class="ico zip">' + IC.zip + '</div><div class="meta"><div class="name">' + esc(m.name) + '</div><div class="sub">' + sub + "</div></div>" +
        (S.active === m.id ? '<span class="pill">active</span>' : "") + "</div>";
    }).join("") + "</div>";
    $$("[data-s]", box).forEach(function (el) { el.onclick = function () { nav("#/stage/" + el.dataset.s); }; });
  }
  async function homeView(rid) {
    chrome("home", "GitZip Bot");
    var st = S.status || {}, s = S.settings || {}, notes = "";
    if (!(s.github_token && s.github_token.set)) notes += '<div class="note warn">GitHub token abhi set nahi hai. <b><a href="#/settings" style="color:inherit">Settings</a></b> me daalo, tabhi upload hoga.</div>';
    if (!(s.ai_key && s.ai_key.set)) notes += '<div class="note">AI use karna ho to Settings me Inception API key daalo (100M free tokens). Bina AI ke bhi sab kuch manual chalta hai.</div>';
    view.innerHTML = notes +
      '<div class="drop" id="drop"><div class="big">📦</div><b>Zip ya files add karo</b><div class="muted">Server par extract hoke GitHub par upload hogi.<br>Zip server tak pahunch jaye to tab band kar sakte ho.</div>' +
      '<div class="row"><button class="btn primary" id="aZip">📦 Zip</button><button class="btn" id="aFiles">📄 Files</button><button class="btn" id="aDir">📁 Folder</button></div></div>' +
      '<div id="ups" style="margin-top:12px"></div>' +
      '<h2 class="sec">Tumhare zips / files</h2><div id="stl"></div>' +
      '<h2 class="sec">Server</h2><div class="card" id="srv"></div>';
    $("#aZip").onclick = function () { pick("zip"); }; $("#aFiles").onclick = function () { pick("files"); }; $("#aDir").onclick = function () { pick("dir"); };
    var dz = $("#drop");
    ["dragenter", "dragover"].forEach(function (ev) { dz.addEventListener(ev, function (e) { e.preventDefault(); dz.classList.add("over"); }); });
    ["dragleave", "drop"].forEach(function (ev) { dz.addEventListener(ev, function (e) { e.preventDefault(); dz.classList.remove("over"); }); });
    dz.addEventListener("drop", function (e) {
      var fl = Array.prototype.slice.call(e.dataTransfer.files || []); if (!fl.length) return;
      addFiles(fl, fl.every(function (f) { return /\.zip$/i.test(f.name); }) ? "zip" : "loose");
    });
    paintUploads(); paintStagingList(); paintServer();
    async function tick() {
      try {
        var r = await Promise.all([api("GET", "/api/staging"), api("GET", "/api/status")]);
        if (rid !== S.rid) return; S.staging = r[0].staging; S.stagingLoaded = true; S.status = r[1]; updateBadge(); paintStagingList(); paintServer();
      } catch (e) { }
      var busy = S.staging.some(function (m) { return m.status === "extracting"; });
      later(tick, busy ? 1200 : 6000, rid);
    }
    tick();
  }
  function paintServer() {
    var b = $("#srv"), st = S.status; if (!b || !st) return;
    var ka = st.keepalive || {};
    b.innerHTML = '<div class="kv"><span>Runner: <b>' + (st.runner_alive ? "✅ chal raha" : "❌ band") + "</b></span><span>Uptime: <b>" + Math.floor(st.uptime / 60) + " min</b></span><span>Free disk: <b>" + (st.free_mb == null ? "?" : st.free_mb + " MB") + "</b></span>" +
      "<span>Keep-alive: <b>" + (ka.last_ok ? "✅ " + ago(ka.last_ok) : ka.enabled ? (ka.url ? "⏳ pehla ping" : "⚠ URL nahi") : "off") + "</b></span><span>v" + esc(st.version) + "</span></div>" +
      (st.ephemeral ? '<div class="muted" style="margin-top:8px">⚠ Data folder temporary hai: Render restart par zips/jobs mit sakte hain, par GitHub par jo commit ho chuka wo safe hai (dobara upload karoge to skip ho jayega).</div>' : "");
  }

  // ============================================================ STAGE (one zip / folder, options, upload)
  async function loadRepos(force) {
    if (S.repos && !force) return S.repos;
    S.repos = (await api("GET", "/api/repos")).repos;
    return S.repos;
  }
  var SKIP = { junk: "junk", ignored: "ignore", too_big: "bahut badi", invalid: "invalid" };
  function slug(n) { return String(n || "project").toLowerCase().replace(/\.zip$/, "").replace(/[^a-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "") || "project"; }
  function sw(id, label, sub, on) { return '<div class="sw"><div><strong>' + label + '</strong><div class="muted">' + sub + '</div></div><label class="switch"><input type="checkbox" id="' + id + '"' + (on ? " checked" : "") + '><span class="slider"></span></label></div>'; }

  async function stageView(id, rid) {
    chrome("stage", "Zip", function () { nav("#/"); });
    view.innerHTML = '<div class="empty"><i class="spin"></i>Loading…</div>';
    var d;
    try { d = await api("GET", "/api/staging/" + id); } catch (e) { toast(e.message); return nav("#/"); }
    if (rid !== S.rid) return;
    titleEl.textContent = d.name;
    var delBtn = function () {
      $("#sDel").onclick = async function () {
        if (!(await confirmSheet("Delete?", "<b>" + esc(d.name) + "</b> server se hat jayega (GitHub par jo upload ho chuka wo rahega).", "Delete", true))) return;
        try { await api("DELETE", "/api/staging/" + id); if (S.active === id) { S.active = ""; lsSet("gz_active", ""); } await refreshStaging(); nav("#/"); } catch (e) { toast(e.message); }
      };
    };
    if (d.status === "extracting") {
      view.innerHTML = '<div class="card"><h2>Extract ho raha hai…</h2><div class="bar big"><i style="width:' + (d.progress && d.progress.total ? d.progress.done / d.progress.total * 100 : 5) + '%"></i></div><div class="muted">' + (d.progress && d.progress.total ? d.progress.done + " / " + d.progress.total : "") + "</div></div>";
      later(function () { stageView(id, rid); }, 1000, rid); return;
    }
    if (d.status === "error") {
      view.innerHTML = '<div class="card"><div class="note bad">❌ ' + esc(d.error) + '</div><button class="btn danger block" id="sDel">🗑 Delete</button></div>'; delBtn(); return;
    }
    S.stage = d; S.active = id; lsSet("gz_active", id);
    var o = d.options, s = S.settings || {};
    view.innerHTML =
      '<div class="card"><div id="sStats"></div><div class="btnrow"><button class="btn" id="sAI">✨ AI se karao</button><button class="btn danger" id="sDel">🗑 Delete</button></div></div>' +
      '<div class="card"><h2>Options</h2><label>Top folder hatao (zip ke andar sirf ek folder ho to)</label>' +
      '<div class="seg" id="oStrip"><button data-v="auto">Auto</button><button data-v="on">Haan</button><button data-v="off">Nahi</button></div><div class="muted" id="oStripInfo" style="margin-top:6px"></div>' +
      sw("oJunk", "Junk skip karo", "node_modules, __pycache__, .DS_Store, __MACOSX …", o.skip_junk) +
      sw("oEmpty", "Khaali folders bhi", "Har khaali folder me .gitkeep banega", o.empty_dirs) +
      '<label>Repo ke andar folder (khali = root me)</label><input type="text" id="oPrefix" value="' + esc(o.prefix) + '" placeholder="e.g. backend" autocapitalize="none">' +
      '<label>Ignore patterns (har line me ek)</label><textarea id="oIgnore" rows="3" placeholder="*.log&#10;build/&#10;secrets.txt" style="white-space:pre-wrap">' + esc(o.ignore) + "</textarea></div>" +
      '<div class="card" id="sUp"><h2>GitHub par bhejo</h2><div class="muted" id="uHint"></div><label>Repository</label><select id="uRepo"><option>Loading…</option></select>' +
      '<div id="uNew"><label>Naye repo ka naam</label><input type="text" id="uName" value="' + esc(slug(d.name)) + '" autocapitalize="none"><label>Visibility</label><select id="uVis"><option value="private">private</option><option value="public">public</option></select></div>' +
      '<label>Branch (khali = default branch)</label><input type="text" id="uBranch" value="' + esc(s.default_branch || "") + '" placeholder="main" autocapitalize="none">' +
      '<label>Commit message</label><input type="text" id="uMsg" value="' + esc(s.commit_message || "Upload via GitZip") + '">' +
      '<div class="btnrow"><button class="btn" id="uCmp">🔍 Compare</button><button class="btn primary" id="uGo">🚀 Upload</button></div><div class="cmp" id="uRes"></div>' +
      '<div class="muted" style="margin-top:8px">Upload server par chalta hai: tab band karo, phone lock karo — chalta rahega. Dobara bhejoge to sirf badli hui files jayengi.</div></div>' +
      '<h2 class="sec">Files</h2><div class="tools"><input type="text" id="fq" placeholder="Search files"><button class="icon-btn" id="fNew" title="New file">' + IC.plus + '</button></div><div class="list fl" id="sFiles"></div>';
    delBtn();
    $("#sAI").onclick = function () { nav("#/ai"); };

    function paintStats() {
      var D = S.stage, c = D.counts, r = c.reasons || {}, why = Object.keys(r).map(function (k) { return (SKIP[k] || k) + " " + r[k]; }).join(", ");
      $("#sStats").innerHTML = '<div style="font-size:15px"><b>' + c.upload + "</b> files upload hongi · " + bytes(c.bytes) + '</div><div class="muted">' + (c.skipped ? c.skipped + " skip (" + why + ")" : "Kuch skip nahi") + " · zip me total " + D.files + "</div>" +
        (D.notes && D.notes.length ? '<details class="how"><summary>Extract notes (' + D.notes.length + ")</summary><div class=\"muted\">" + D.notes.map(esc).join("<br>") + "</div></details>" : "");
      $$("#oStrip button").forEach(function (b) { b.classList.toggle("on", b.dataset.v === D.options.strip_top); });
      $("#oStripInfo").textContent = D.strip ? "'" + D.strip + "' folder hata diya — andar ki files root me jayengi." : "Koi top folder nahi hataya.";
    }
    function paintFiles() {
      var q = ($("#fq").value || "").toLowerCase(), all = S.stage.files_list.filter(function (f) { return !q || f.path.toLowerCase().indexOf(q) > -1; }), rows = all.slice(0, 400);
      $("#sFiles").innerHTML = rows.length ? rows.map(function (f, i) {
        return '<div class="item' + (f.skip ? " skipped" : "") + '" data-f="' + i + '"><div class="meta"><div class="name">' + esc(f.path) + '</div><div class="sub">' + bytes(f.size) + "</div></div>" + (f.skip ? '<span class="tag">' + (SKIP[f.skip] || f.skip) + "</span>" : "") + "</div>";
      }).join("") + (all.length > rows.length ? '<div class="empty">…aur ' + (all.length - rows.length) + " files (search use karo)</div>" : "") : '<div class="empty">Koi file nahi</div>';
      $$("[data-f]", $("#sFiles")).forEach(function (el) { el.onclick = function () { openEditor(rows[+el.dataset.f].path); }; });
    }
    paintStats(); paintFiles();
    $("#fq").oninput = paintFiles;

    var pending = {}, flush = debounce(async function () {
      var p = pending; pending = {};
      try { S.stage = await api("POST", "/api/staging/" + id + "/options", p); paintStats(); paintFiles(); $("#uRes").innerHTML = ""; } catch (e) { toast(e.message); }
    }, 500);
    function saveOpts(p) { Object.keys(p).forEach(function (k) { pending[k] = p[k]; }); flush(); }
    $$("#oStrip button").forEach(function (b) { b.onclick = function () { saveOpts({ strip_top: b.dataset.v }); $$("#oStrip button").forEach(function (x) { x.classList.toggle("on", x === b); }); }; });
    $("#oJunk").onchange = function () { saveOpts({ skip_junk: this.checked }); };
    $("#oEmpty").onchange = function () { saveOpts({ empty_dirs: this.checked }); };
    $("#oPrefix").oninput = function () { saveOpts({ prefix: this.value }); };
    $("#oIgnore").oninput = function () { saveOpts({ ignore: this.value }); };

    async function openEditor(path) {
      var r; try { r = await api("GET", "/api/staging/" + id + "/file?path=" + encodeURIComponent(path)); } catch (e) { return toast(e.message); }
      var out = await ask({ title: base(path), msg: r.truncated ? "⚠ File badi hai — sirf shuru ka hissa dikh raha hai, save mat karo." : "", fields: [{ k: "t", type: "textarea", v: r.text }], ok: "Save" });
      if (!out || r.truncated) return;
      try { S.stage = await api("PUT", "/api/staging/" + id + "/file", { path: path, content: out.t }); paintStats(); paintFiles(); toast("Saved ✓"); } catch (e) { toast(e.message); }
    }
    $("#fNew").onclick = async function () {
      var r = await ask({ title: "Nayi file", fields: [{ k: "n", l: "Path (repo ke andar)", p: "README.md" }], ok: "Banao" });
      if (!r || !r.n) return;
      try { S.stage = await api("PUT", "/api/staging/" + id + "/file", { path: r.n, content: "" }); paintStats(); paintFiles(); openEditor(r.n); } catch (e) { toast(e.message); }
    };

    // ---- upload card
    function target() {
      var v = $("#uRepo").value, isNew = v === "__new__", name = isNew ? $("#uName").value.trim() : v;
      return { repo: name, create: isNew, priv: $("#uVis").value === "private", branch: $("#uBranch").value.trim(), message: $("#uMsg").value.trim() };
    }
    function syncNew() { $("#uNew").style.display = $("#uRepo").value === "__new__" ? "" : "none"; $("#uRes").innerHTML = ""; }
    $("#uRepo").onchange = syncNew;
    loadRepos(true).then(function (list) {   // always fresh: repos get created by jobs / on github.com
      if (rid !== S.rid) return;
      var ok = list.filter(function (r) { return r.can_push && !r.archived; }), def = (S.settings || {}).default_repo;
      $("#uRepo").innerHTML = '<option value="__new__">➕ Naya repo banao…</option>' + ok.map(function (r) { return '<option value="' + esc(r.full_name) + '">' + (r.private ? "🔒 " : "🌐 ") + esc(r.full_name) + "</option>"; }).join("");
      $("#uRepo").value = def && ok.some(function (r) { return r.full_name === def; }) ? def : (ok.length && S.lastRepo && ok.some(function (r) { return r.full_name === S.lastRepo; }) ? S.lastRepo : "__new__");
      $("#uVis").value = (S.settings || {}).default_private === false ? "public" : "private"; syncNew();
    }).catch(function (e) {
      $("#uRepo").innerHTML = '<option value="__new__">➕ Naya repo banao…</option>'; syncNew();
      $("#uHint").innerHTML = '<span class="err">Repos load nahi hue: ' + esc(e.message) + "</span>";
    });
    $("#uCmp").onclick = async function () {
      var t = target(); if (!t.repo) return toast("Repo ka naam daalo");
      $("#uRes").innerHTML = '<i class="spin" style="margin:6px 0"></i>';
      try {
        var c = await api("POST", "/api/compare", { staging_id: id, repo: t.repo, branch: t.branch });
        $("#uRes").innerHTML = (c.repo_exists ? "" : "🆕 Repo abhi exist nahi karta — banega. ") + "<b>" + c.repo + "</b> @" + esc(c.branch || "default") + "<br>🆕 <b>" + c.new + "</b> nayi · ✏️ <b>" + c.changed + "</b> badli · ✅ <b>" + c.same + "</b> pehle se same (skip)" +
          (c.new_sample && c.new_sample.length ? '<div class="muted">' + c.new_sample.slice(0, 4).map(esc).join(", ") + (c.new < 5 ? "" : " …") + "</div>" : "") + (c.warning ? '<div class="err">' + esc(c.warning) + "</div>" : "");
      } catch (e) { $("#uRes").innerHTML = '<span class="err">' + esc(e.message) + "</span>"; }
    };
    $("#uGo").onclick = async function () {
      var t = target(); if (!t.repo) return toast("Repo ka naam daalo");
      if (!(S.settings.github_token && S.settings.github_token.set)) { toast("Pehle GitHub token daalo"); return nav("#/settings"); }
      if (t.create && !/^[A-Za-z0-9_.-]+$/.test(t.repo)) return toast("Repo naam me sirf letters, numbers, - _ . allowed hain");
      var D = S.stage;
      if (!(await confirmSheet("Upload shuru karein?", "<b>" + D.counts.upload + " files</b> → <b>" + esc(t.repo) + "</b>" + (t.branch ? " @" + esc(t.branch) : "") + (t.create ? "<br>🆕 Naya " + (t.priv ? "private" : "public") + " repo banega" : "") + "<br>Sirf nayi/badli hui files jayengi. Server par chalega — tab band kar sakte ho.", "🚀 Upload"))) return;
      try {
        var j = await api("POST", "/api/jobs", { staging_id: id, repo: t.repo, branch: t.branch, message: t.message, create_repo: t.create, private: t.priv });
        S.lastRepo = t.repo; nav("#/job/" + j.id);
      } catch (e) { toast(e.message); }
    };
  }

  // ============================================================ JOBS
  var PILL = { queued: ["queued", "gray"], running: ["running", "run"], done: ["done ✓", ""], partial: ["partial", "bad"], failed: ["failed", "bad"], cancelled: ["cancelled", "gray"] };
  function pill(st) { var p = PILL[st] || [st, "gray"]; return '<span class="pill ' + p[1] + '">' + p[0] + "</span>"; }
  function isActive(j) { return j.status === "queued" || j.status === "running"; }
  function pctOf(j) { var c = j.counts; if (j.status === "done") return 100; return c.todo ? Math.min(100, c.done / c.todo * 100) : 0; }
  var PHASE = { connect: "GitHub se connect ho raha hai…", scan: "Files check ho rahi hain…", upload: "Upload chal raha hai", verify: "GitHub par verify ho raha hai…", done: "" };

  async function jobsView(rid) {
    chrome("jobs", "Jobs");
    view.innerHTML = '<div class="empty"><i class="spin"></i>Loading…</div>';
    async function tick() {
      var r; try { r = await api("GET", "/api/jobs"); } catch (e) { if (rid === S.rid && e.status !== 401) { if (!$$("[data-j]").length) view.innerHTML = '<div class="card"><div class="err">' + esc(e.message) + "</div></div>"; later(tick, 4000, rid); } return; }
      if (rid !== S.rid) return;
      var js = r.jobs;
      view.innerHTML = js.length ? js.map(function (j) {
        var c = j.counts;
        return '<div class="card job" data-j="' + j.id + '"><div class="top"><div class="name">' + esc(j.repo) + "</div>" + pill(j.status) + '</div><div class="sub">' + esc(j.staging_name || "") + " · " + ago(j.created) + "</div>" +
          '<div class="bar sm"><i style="width:' + pctOf(j) + '%"></i></div><div class="muted">' + (isActive(j) ? esc(j.note || PHASE[j.phase] || "") + " · " : "") + c.done + "/" + c.todo + " bheji · " + c.same + " same" + (c.failed ? " · ❌ " + c.failed : "") + "</div></div>";
      }).join("") : '<div class="empty"><span class="big-i">🚀</span>Abhi koi upload job nahi.<br>Home se zip add karke upload karo.</div>';
      $$("[data-j]").forEach(function (el) { el.onclick = function () { nav("#/job/" + el.dataset.j); }; });
      later(tick, js.some(isActive) ? 1500 : 6000, rid);
    }
    await tick();
  }

  async function jobView(id, rid) {
    chrome("job", "Upload job", function () { nav("#/jobs"); });
    view.innerHTML = '<div class="card"><div class="top" style="display:flex;justify-content:space-between;gap:8px"><div class="name" id="jRepo"></div><span id="jPill"></span></div><div class="muted" id="jPhase" style="margin-top:4px"></div>' +
      '<div class="bar big"><i id="jBar"></i></div><div class="chips" id="jChips"></div><ul class="errlist" id="jErr"></ul><div class="btnrow" id="jBtns"></div></div>' +
      '<div class="note" id="jNote" style="display:none">✅ Ye server par chal raha hai — tab band karo, phone lock karo, tab bhi chalta rahega.</div>' +
      '<h2 class="sec">Log</h2><pre class="log" id="jLog"></pre>';
    var since = 0, pre = $("#jLog"), lastBtns = "";
    function paint(j) {
      var c = j.counts, act = isActive(j);
      $("#jRepo").textContent = j.repo + (j.branch ? " @" + j.branch : ""); $("#jPill").innerHTML = pill(j.status);
      $("#jPhase").innerHTML = esc(act ? (j.note || PHASE[j.phase] || "") + (j.phase === "upload" && j.chunks > 1 ? " (commit " + j.chunk + "/" + j.chunks + ")" : "") : (j.error || (j.status === "done" ? "Sab files GitHub par verify ho gayi ✓" : "")));
      $("#jPhase").className = "muted" + (j.status === "failed" || j.status === "partial" ? " err" : "");
      $("#jBar").style.width = pctOf(j) + "%"; $("#jNote").style.display = act ? "" : "none";
      $("#jChips").innerHTML = '<span class="chip"><b>' + c.done + "</b> / " + c.todo + " bheji</span>" + '<span class="chip">✅ <b>' + c.same + "</b> same</span>" + '<span class="chip">⏭ <b>' + c.skipped + "</b> skip</span>" + (c.failed ? '<span class="chip" style="color:var(--danger)">❌ <b>' + c.failed + "</b></span>" : "") + (j.bytes_total ? '<span class="chip">' + bytes(j.bytes_done) + " / " + bytes(j.bytes_total) + "</span>" : "") +
        (j.repo_url ? '<a class="chip add" href="' + esc(j.repo_url) + '" target="_blank" rel="noopener">GitHub ↗</a>' : "") + (j.head && j.repo_url ? '<a class="chip add" href="' + esc(j.repo_url + "/commit/" + j.head) + '" target="_blank" rel="noopener">last commit ↗</a>' : "");
      $("#jErr").innerHTML = (j.errors || []).map(function (e) { return "<li><b>" + esc(e.path) + "</b> — " + esc(e.error) + "</li>"; }).join("");
      var b = [];
      if (act) b.push('<button class="btn danger" id="jCancel">⏹ Cancel</button>');
      else { b.push('<button class="btn primary" id="jResume">' + (j.status === "done" ? "🔄 Dobara sync" : "▶ Resume / Retry") + '</button>'); b.push('<button class="btn danger" id="jDel">🗑 Hatao</button>'); }
      var bh = b.join(""); if (bh === lastBtns) return; lastBtns = bh;
      $("#jBtns").innerHTML = bh;
      if ($("#jCancel")) $("#jCancel").onclick = async function () { try { await api("POST", "/api/jobs/" + id + "/cancel", {}); toast("Cancel ho raha hai…"); } catch (e) { toast(e.message); } };
      if ($("#jResume")) $("#jResume").onclick = async function () { try { await api("POST", "/api/jobs/" + id + "/resume", {}); toast("Resume shuru ✓"); } catch (e) { toast(e.message); } };
      if ($("#jDel")) $("#jDel").onclick = async function () { if (!(await confirmSheet("Job hatao?", "Sirf record hatega; GitHub par kuch nahi badlega.", "Hatao", true))) return; try { await api("DELETE", "/api/jobs/" + id); nav("#/jobs"); } catch (e) { toast(e.message); } };
    }
    async function tick() {
      var j;
      try { j = await api("GET", "/api/jobs/" + id + "?since=" + since); }
      catch (e) { if (e.status === 404) { toast("Job nahi mila"); return nav("#/jobs"); } if (rid === S.rid) { $("#jPhase").textContent = e.message; later(tick, 4000, rid); } return; }
      if (rid !== S.rid) return;
      if (j.log_next < since) pre.textContent = "";
      var near = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
      if (j.log) { pre.textContent += j.log; if (near) pre.scrollTop = pre.scrollHeight; }
      since = j.log_next; paint(j);
      later(tick, isActive(j) ? 1000 : 5000, rid);
    }
    tick();
  }

  // ============================================================ AI CHAT
  var CH = ls("gz_chat", null) || { msgs: [], ui: [] };
  function saveChat() { CH.ui = CH.ui.slice(-120); CH.msgs = CH.msgs.slice(-80); lsSet("gz_chat", CH); }
  function inl(t) {
    var s = esc(t);
    s = s.replace(/`([^`\n]+)`/g, "<code>$1</code>").replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>").replace(/(https?:\/\/[^\s<)]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>');
    return s.replace(/\n/g, "<br>");
  }
  function md(t) { return String(t || "").split("```").map(function (p, i) { return i % 2 ? "<pre>" + esc(p.replace(/^\w*\n/, "")) + "</pre>" : inl(p); }).join(""); }
  function summaryHtml(s) {
    s = s || {};
    return "<b>" + esc(s.repo || "?") + "</b>" + (s.branch ? " @" + esc(s.branch) : "") + (s.create_repo ? " · 🆕 naya " + (s.private ? "private" : "public") + " repo" : "") +
      (s.new != null ? "<br>🆕 " + s.new + " nayi · ✏️ " + s.changed + " badli · ✅ " + s.same + " same (skip)" : "") + (s.note ? "<br><span class=\"muted\">" + esc(s.note) + "</span>" : "");
  }
  function itemHtml(it, i) {
    if (it.t === "u") return '<div class="b u">' + esc(it.text) + "</div>";
    if (it.t === "a") return '<div class="b a">' + md(it.text) + "</div>";
    if (it.t === "s") return '<div class="b s' + (it.bad ? " err" : "") + '">' + esc(it.text) + "</div>";
    if (it.t === "tool") return '<div class="tool' + (it.ok ? "" : " bad") + '">🔧 ' + esc(it.name) + (it.ok ? "" : " ✗") + "</div>";
    if (it.t === "ap") {
      var j = it.job, body = "";
      if (it.job_id) {
        body = j ? pill(j.status) + '<div class="bar sm"><i style="width:' + pctOf(j) + '%"></i></div><div class="muted">' + j.counts.done + "/" + j.counts.todo + " bheji · " + j.counts.same + " same" + (j.error ? '<br><span class="err">' + esc(j.error) + "</span>" : "") + "</div>" : '<div class="muted">Job shuru ho raha hai…</div>';
        body += '<div class="btnrow"><button class="btn" data-open="' + it.job_id + '">Details →</button></div>';
      } else if (it.status === "pending") body = '<div class="btnrow"><button class="btn primary" data-ap="' + i + '" data-yes="1">✅ Approve &amp; upload</button><button class="btn" data-ap="' + i + '">Cancel</button></div>';
      else body = '<div class="muted">' + (it.status === "rejected" ? "❌ Cancel kiya" : "⌛ Expire ho gaya — AI se dobara bolo") + "</div>";
      return '<div class="ap"><h4>🚀 Upload ' + (it.status === "pending" ? "confirm karo" : "") + "</h4>" + (it.summary && it.summary.repo ? summaryHtml(it.summary) : "") + body + "</div>";
    }
    return "";
  }
  async function pollChatJobs(rid) {
    var items = CH.ui.filter(function (x) { return x.t === "ap" && x.job_id && !(x.job && !isActive(x.job)); });
    if (!items.length || S.chatPolling === rid) return;
    S.chatPolling = rid;
    try {
      var js = (await api("GET", "/api/jobs")).jobs, m = {}; js.forEach(function (j) { m[j.id] = j; });
      items.forEach(function (x) { if (m[x.job_id]) x.job = m[x.job_id]; });
      if (rid !== S.rid) return; saveChat(); paintChat(true);
    } catch (e) { }
    later(function () { S.chatPolling = 0; pollChatJobs(rid); }, 1800, rid);
  }
  function paintChat(keepPoll) {
    var box = $("#msgs"); if (!box) return;
    var near = window.innerHeight + window.scrollY >= document.body.scrollHeight - 160;
    box.innerHTML = CH.ui.map(itemHtml).join("") + (S.chatBusy ? '<div class="typing">AI kaam kar raha hai</div>' : "");
    $$("[data-ap]", box).forEach(function (b) { b.onclick = function () { decide(CH.ui[+b.dataset.ap], !!b.dataset.yes); }; });
    $$("[data-open]", box).forEach(function (b) { b.onclick = function () { nav("#/job/" + b.dataset.open); }; });
    if (near || S.chatBusy) window.scrollTo(0, document.body.scrollHeight);
    if (!keepPoll) pollChatJobs(S.rid);
    var sg = $("#sugg"); if (sg) sg.style.display = CH.ui.length ? "none" : "";
  }
  async function decide(item, yes) {
    try {
      var r = await api("POST", "/api/approvals/" + item.id, { approve: yes });
      item.status = r.status; item.job_id = r.job_id || "";
      var note = yes ? "✅ Approve kiya — upload job " + r.job_id + " shuru ho gaya." : "❌ Upload cancel kiya.";
      CH.msgs.push({ role: "user", content: note }); CH.ui.push({ t: "s", text: note }); saveChat(); paintChat();
    } catch (e) { toast(e.message); if (e.status === 404) { item.status = "expired"; saveChat(); paintChat(); } }
  }
  async function sendChat(text) {
    text = (text || "").trim(); if (!text || S.chatBusy) return;
    if (text === "/clear") { CH = { msgs: [], ui: [] }; saveChat(); return paintChat(); }
    CH.msgs.push({ role: "user", content: text }); CH.ui.push({ t: "u", text: text }); S.chatBusy = true; saveChat(); paintChat();
    try {
      var r = await api("POST", "/api/chat", { messages: CH.msgs, active_staging: S.active });
      if (r.error) throw ApiErr(200, r.error, r);
      var ei = 0;
      r.messages.forEach(function (m) {
        CH.msgs.push({ role: m.role, content: m.content, tool_calls: m.tool_calls, tool_call_id: m.tool_call_id });
        if (m.role !== "assistant") return;
        if (m.content) CH.ui.push({ t: "a", text: m.content });
        (m.tool_calls || []).forEach(function () {
          var ev = r.events[ei++]; if (!ev) return;
          CH.ui.push({ t: "tool", name: ev.tool, ok: ev.ok });
          if (ev.approval) CH.ui.push({ t: "ap", id: ev.approval.id, summary: ev.approval.summary, status: ev.approval.status, job_id: ev.approval.job_id });
          else if (ev.job_id) CH.ui.push({ t: "ap", id: "", summary: {}, status: "approved", job_id: ev.job_id });
        });
      });
    } catch (e) { CH.ui.push({ t: "s", bad: true, text: "❌ " + e.message + (/key|Settings/i.test(e.message) ? " (Settings → AI)" : "") }); }
    S.chatBusy = false; saveChat(); paintChat();
  }
  async function aiView(rid) {
    chrome("ai", "AI Assistant");
    var hasKey = S.settings && S.settings.ai_key && S.settings.ai_key.set;
    view.innerHTML = (hasKey ? "" : '<div class="note warn">AI key set nahi hai — <a href="#/settings" style="color:inherit"><b>Settings → AI</b></a> me Inception key daalo. Tab tak slash commands chalte hain: <code>/help</code></div>') +
      '<div class="chatwrap"><div class="msgs" id="msgs"></div><div class="sugg" id="sugg"></div><div id="ups"></div>' +
      '<div class="composer"><div class="attach" id="att"></div><div class="box"><button class="icon-btn" id="cAtt" title="Zip attach karo">' + IC.clip + '</button><textarea id="cIn" rows="1" placeholder="Kuch bhi bolo… ya /help"></textarea><button class="icon-btn send" id="cSend">' + IC.send + "</button></div></div></div>";
    $("#sugg").innerHTML = ["📦 Mujhe zip GitHub par upload karna hai", "📚 /repos", "📊 Last upload ka status batao", "/help"].map(function (t) { return '<button class="btn" data-q="' + esc(t.replace(/^\S+ (?=\/)/, "")) + '">' + esc(t) + "</button>"; }).join("");
    $$("[data-q]").forEach(function (b) { b.onclick = function () { sendChat(b.dataset.q.replace(/^📦 |^📊 /, "")); }; });
    function paintAtt() {
      var m = S.staging.filter(function (x) { return x.id === S.active; })[0];
      $("#att").innerHTML = m ? "📎 <b>" + esc(m.name) + "</b> · " + m.files + ' files <button class="chip add" id="attX">✕</button>' : "";
      if (m) $("#attX").onclick = function () { S.active = ""; lsSet("gz_active", ""); paintAtt(); };
    }
    await refreshStaging(); if (rid !== S.rid) return; paintAtt();
    paintUploads(); paintChat();
    var ta = $("#cIn");
    ta.oninput = function () { ta.style.height = "auto"; ta.style.height = Math.min(140, ta.scrollHeight) + "px"; };
    ta.onkeydown = function (e) { if (e.key === "Enter" && !e.shiftKey && !/Mobi|Android|iPhone/i.test(navigator.userAgent)) { e.preventDefault(); $("#cSend").click(); } };
    $("#cSend").onclick = function () { var v = ta.value; ta.value = ""; ta.style.height = "auto"; sendChat(v); };
    $("#cAtt").onclick = function () {
      S.pickDone = async function (sid) {
        await refreshStaging(); paintAtt(); var m = S.staging.filter(function (x) { return x.id === sid; })[0] || { name: "zip", files: "?" };
        sendChat('Maine "' + m.name + '" attach kiya (' + m.files + " files). Isko GitHub par upload karna hai.");
      };
      pick("zip");
    };
  }

  // ============================================================ REPOS
  async function reposView(rid) {
    chrome("repos", "Repos");
    view.innerHTML = '<div class="empty"><i class="spin"></i>Loading…</div>';
    var list;
    try { list = await loadRepos(true); } catch (e) { if (rid === S.rid) view.innerHTML = '<div class="card"><div class="err">' + esc(e.message) + '</div><button class="btn block" onclick="location.hash=\'#/settings\'">Settings</button></div>'; return; }
    if (rid !== S.rid) return;
    view.innerHTML = '<div class="tools"><input type="text" id="rq" placeholder="Search ' + list.length + ' repos" autocapitalize="none"></div><div id="rl"></div><div class="fabs"><button class="fab" id="rAdd" title="New repo">' + IC.plus + "</button></div>";
    function rp() {
      var q = $("#rq").value.toLowerCase(), l = list.filter(function (r) { return r.full_name.toLowerCase().indexOf(q) > -1; });
      $("#rl").innerHTML = '<div class="list">' + (l.length ? l.map(function (r, i) { return '<div class="item" data-r="' + i + '"><div class="ico">' + (r.private ? IC.lock : IC.globe) + '</div><div class="meta"><div class="name">' + esc(r.full_name) + '</div><div class="sub">' + esc(r.default_branch) + " · " + (r.private ? "private" : "public") + (r.can_push ? "" : " · read-only") + "</div></div></div>"; }).join("") : '<div class="empty"><span class="big-i">🐙</span>Koi repo nahi mila</div>') + "</div>";
      $$("[data-r]").forEach(function (el) { el.onclick = function () { nav("#/repo/" + l[+el.dataset.r].full_name); }; });
    }
    $("#rq").oninput = rp; rp();
    $("#rAdd").onclick = async function () {
      var r = await ask({ title: "Naya repo", fields: [{ k: "n", l: "Naam", p: "my-project" }, { k: "v", l: "Visibility", opts: ["private", "public"] }], ok: "Banao" });
      if (!r || !r.n) return;
      try { var c = await api("POST", "/api/repos", { name: r.n, private: r.v !== "public" }); toast("Repo ban gaya: " + c.full_name); reposView(rid); } catch (e) { toast(e.message); }
    };
  }
  async function repoView(full, path, rid) {
    var parent = path.replace(/[^\/]+\/?$/, "").replace(/\/$/, "");
    chrome("repo", path ? base(path) : full.split("/")[1], function () { nav(path ? "#/repo/" + full + (parent ? "?p=" + encodeURIComponent(parent) : "") : "#/repos"); });
    view.innerHTML = '<div class="empty"><i class="spin"></i>Loading…</div>';
    var r;
    try { r = await api("GET", "/api/repos/" + full + "/ls?path=" + encodeURIComponent(path)); } catch (e) { if (rid === S.rid) view.innerHTML = '<div class="card"><div class="err">' + esc(e.message) + "</div></div>"; return; }
    if (rid !== S.rid) return;
    var sel = null;
    view.innerHTML = '<div class="crumbs">' + esc(full + " @ " + r.branch + " / " + path) + '</div><div class="tools"><input type="text" id="eq" placeholder="Search this folder"><button class="icon-btn" id="eSel" title="Select">☑</button><a class="icon-btn" href="https://github.com/' + esc(full) + '" target="_blank" rel="noopener" title="GitHub" style="text-decoration:none;color:inherit">↗</a></div><div id="el"></div><div class="selbar" id="eb"></div>';
    function paint() {
      var q = $("#eq").value.toLowerCase(), l = r.entries.filter(function (x) { return !q || x.name.toLowerCase().indexOf(q) > -1; });
      $("#el").innerHTML = '<div class="list">' + (l.length ? l.map(function (x, i) {
        var p = (path ? path + "/" : "") + x.name;
        return '<div class="item' + (sel && sel[p] ? " picked" : "") + '" data-e="' + i + '">' + (sel ? '<span class="ck"></span>' : "") + '<div class="ico ' + (x.type === "dir" ? "dir" : "") + '">' + (x.type === "dir" ? IC.folder : IC.file) + '</div><div class="meta"><div class="name">' + esc(x.name) + '</div><div class="sub">' + (x.type === "dir" ? "Folder" : bytes(x.size)) + "</div></div></div>";
      }).join("") : '<div class="empty"><span class="big-i">📂</span>Khaali</div>') + "</div>";
      $$("[data-e]", $("#el")).forEach(function (el) {
        var x = l[+el.dataset.e], p = (path ? path + "/" : "") + x.name;
        el.onclick = function () { if (sel) { if (sel[p]) delete sel[p]; else sel[p] = 1; paint(); } else if (x.type === "dir") nav("#/repo/" + full + "?p=" + encodeURIComponent(p)); else window.open("https://github.com/" + full + "/blob/" + encodeURIComponent(r.branch) + "/" + p.split("/").map(encodeURIComponent).join("/"), "_blank"); };
      });
      $("#eSel").classList.toggle("on", !!sel);
      var b = $("#eb"); b.classList.toggle("show", !!sel); if (!sel) { b.innerHTML = ""; return; }
      b.innerHTML = '<div class="sbt"><b>' + Object.keys(sel).length + ' selected</b><span><button id="eX">✕</button></span></div><div class="sba"><button class="d" id="eDel">🗑 Delete selected</button></div>';
      $("#eX").onclick = function () { sel = null; paint(); };
      $("#eDel").onclick = async function () {
        var ps = Object.keys(sel); if (!ps.length) return;
        if (!(await confirmSheet("Delete? (1/2)", "<b>" + ps.length + " item(s)</b> repo se hatenge (ek commit me).", "Aage", true))) return;
        if (!(await confirmSheet("Pakka? (2/2)", "Branch <b>" + esc(r.branch) + "</b> par abhi commit hoga. Git history me purana version rahega.", "Haan, delete", true))) return;
        try { var d = await api("POST", "/api/repos/" + full + "/delete", { paths: ps, branch: r.branch, confirm: "yes" }); toast(d.deleted + " file(s) delete ✓"); repoView(full, path, rid); } catch (e) { toast(e.message); }
      };
    }
    $("#eq").oninput = paint; $("#eSel").onclick = function () { sel = sel ? null : {}; paint(); }; paint();
  }

  // ============================================================ SETTINGS
  async function settingsView(rid) {
    chrome("settings", "Settings", function () { nav("#/"); });
    var r = await Promise.all([api("GET", "/api/settings"), api("GET", "/api/status")]);
    if (rid !== S.rid) return;
    S.settings = r[0]; S.status = r[1]; var s = S.settings, st = S.status, ka = st.keepalive || {};
    function hint(x) { return x.set ? "✓ Set hai (" + (x.source === "env" ? "environment variable" : "saved") + ") …" + esc(x.hint.replace("…", "")) : "Abhi set nahi hai"; }
    function opts(list, cur) { return list.map(function (v) { return '<option' + (v === cur ? " selected" : "") + ">" + esc(v) + "</option>"; }).join(""); }
    view.innerHTML =
      (st.ephemeral ? '<div class="note warn">⚠ Render free plan par server restart hone par settings mit jaati hain. Isliye tokens ko Render → Environment me <b>GITHUB_TOKEN</b> aur <b>INCEPTION_API_KEY</b> naam se bhi daalo, ya neeche "Is device par backup" on rakho.</div>' : "") +
      '<div class="card"><h2>GitHub token</h2><details class="how"><summary>Token kaise banaye?</summary><div class="muted">github.com → Settings → Developer settings → Personal access tokens.</div><ul class="perm"><li><b>Fine-grained:</b> Repository access → All repositories (ya jo repos chahiye)</li><li>Repository permissions → <b>Contents: Read and write</b></li><li>Repository permissions → <b>Metadata: Read-only</b></li><li>Naya repo banane ke liye: Account permissions → <b>Administration</b> (create repositories) / ya classic token me <b>repo</b> tick</li><li>Classic token: sirf <b>repo</b> tick karna kaafi hai</li></ul></details>' +
      '<label>Token</label><input type="password" id="gTok" placeholder="ghp_… / github_pat_…" autocomplete="off"><div class="muted" id="gHint" style="margin-top:6px">' + hint(s.github_token) + '</div><div class="btnrow"><button class="btn primary" id="gSave">Save &amp; test</button><button class="btn danger" id="gClear">Hatao</button></div><div class="muted" id="gRes" style="margin-top:8px"></div></div>' +
      '<div class="card"><h2>AI — Inception Labs</h2><div class="muted">Naye account par <b>100 million free tokens</b> (platform.inceptionlabs.ai → API Keys). Key sirf server par rehti hai, AI ko token/key kabhi nahi dikhti.</div>' +
      '<label>API key</label><input type="password" id="aKey" placeholder="sk_…" autocomplete="off"><div class="muted" id="aHint" style="margin-top:6px">' + hint(s.ai_key) + "</div>" +
      '<label>Base URL</label><input type="text" id="aBase" value="' + esc(s.ai_base_url) + '" autocapitalize="none"><label>Model</label><select id="aModel">' + opts([s.ai_model], s.ai_model) + '</select>' +
      '<label>Soch ki gehraai (reasoning)</label><select id="aReason">' + opts(["instant", "low", "medium", "high"], s.ai_reasoning) + "</select>" +
      sw("aConf", "Upload se pehle mujhse puchho", "AI upload start karne se pehle chat me Approve button dikhayega (safe)", s.ai_confirm_uploads) +
      '<div class="btnrow"><button class="btn primary" id="aSave">Save &amp; test AI</button></div><div class="muted" id="aRes" style="margin-top:8px"></div></div>' +
      '<div class="card"><h2>Upload defaults</h2><label>Default repo (owner/name)</label><input type="text" id="dRepo" value="' + esc(s.default_repo) + '" placeholder="myname/my-repo" autocapitalize="none"><label>Default branch (khali = repo ka default)</label><input type="text" id="dBranch" value="' + esc(s.default_branch) + '" placeholder="main" autocapitalize="none">' +
      '<label>Commit message</label><input type="text" id="dMsg" value="' + esc(s.commit_message) + '">' + sw("dPriv", "Naye repo default private", "", s.default_private) + sw("dJunk", "Junk skip (node_modules, __pycache__ …)", "Naye zips ke liye default", s.skip_junk) +
      '<label>Ek commit me max files</label><input type="number" id="dChunk" min="10" max="500" value="' + esc(s.chunk_files) + '"><div class="btnrow"><button class="btn primary" id="dSave">Save</button></div></div>' +
      '<div class="card"><h2>Keep-alive (server ko sone se roko)</h2><div class="muted">Server khud apne aap ko har ~5 min ping karta hai taaki Render free plan par so na jaye. Status: <b>' + (ka.last_ok ? "✅ last ping " + ago(ka.last_ok) : ka.error ? "⚠ " + esc(ka.error) : "⏳") + "</b></div>" +
      sw("kOn", "Self ping on", "", s.keepalive_enabled) + '<label>URL (khali = automatic)</label><input type="text" id="kUrl" value="' + esc(s.keepalive_url) + '" placeholder="https://mera-bot.onrender.com" autocapitalize="none"><div class="btnrow"><button class="btn" id="kPing">Ping abhi</button><button class="btn primary" id="kSave">Save</button></div><div class="muted" style="margin-top:8px">Extra safety: uptimerobot.com par free monitor bana ke <code>/healthz</code> URL har 5 min ping karwa sakte ho.</div></div>' +
      '<div class="card"><h2>Is device par</h2>' + sw("tDark", "Dark mode", "", document.documentElement.classList.contains("dark")) + sw("tBack", "Tokens ka backup is device par", "Server restart ke baad tokens apne aap wapas set ho jayenge (sirf tumhare browser me)", ls("gz_backup_on", true)) +
      '<div class="btnrow"><button class="btn danger" id="oOut">Logout</button></div></div>' +
      '<div class="card"><h2>About</h2><div class="kv"><span>GitZip Bot <b>v' + esc(st.version) + "</b></span><span>Uptime <b>" + Math.floor(st.uptime / 60) + " min</b></span><span>Disk free <b>" + (st.free_mb == null ? "?" : st.free_mb + " MB") + "</b></span><span>Runner <b>" + (st.runner_alive ? "OK" : "DOWN") + "</b></span></div></div>";

    async function save(p) { var x = await api("POST", "/api/settings", p); S.settings = x.settings; return x; }
    function backup(k, v) { var b = ls("gz_backup", {}); if (v) b[k] = v; else delete b[k]; lsSet("gz_backup", b); }
    $("#gSave").onclick = async function () {
      var v = $("#gTok").value.trim(), out = $("#gRes"); out.className = "muted"; out.textContent = "Check ho raha hai…";
      try {
        if (v) { await save({ github_token: v }); if (ls("gz_backup_on", true)) backup("github_token", v); $("#gTok").value = ""; }
        var t = await api("POST", "/api/settings/test-github", {});
        out.className = "ok"; out.textContent = "✓ Login: " + t.login + " · " + t.repos_visible + "+ repos dikh rahe · scopes: " + t.scopes; $("#gHint").textContent = hint(S.settings.github_token).replace(/<[^>]+>/g, "");
        S.repos = null;
      } catch (e) { out.className = "err"; out.textContent = "✗ " + e.message; }
    };
    $("#gClear").onclick = async function () { if (!(await confirmSheet("Token hatao?", "Server se GitHub token hat jayega.", "Hatao", true))) return; await save({ github_token: "" }); backup("github_token", ""); toast("Token hata diya"); settingsView(rid); };
    api("GET", "/api/models").then(function (m) { var cur = $("#aModel"); if (!cur || rid !== S.rid) return; var sel = cur.value; cur.innerHTML = opts(m.models.indexOf(sel) > -1 ? m.models : [sel].concat(m.models), sel); }).catch(function () { });
    $("#aConf").onchange = function () { save({ ai_confirm_uploads: this.checked }).catch(function (e) { toast(e.message); }); };
    $("#aSave").onclick = async function () {
      var v = $("#aKey").value.trim(), out = $("#aRes"); out.className = "muted"; out.textContent = "Test ho raha hai…";
      try {
        var p = { ai_base_url: $("#aBase").value, ai_model: $("#aModel").value, ai_reasoning: $("#aReason").value }; if (v) p.ai_key = v;
        await save(p); if (v) { if (ls("gz_backup_on", true)) backup("ai_key", v); $("#aKey").value = ""; }
        var t = await api("POST", "/api/settings/test-ai", {});
        if (t.error) throw ApiErr(200, t.error); out.className = "ok"; out.textContent = "✓ AI chal raha hai (" + t.model + ", " + t.ms + " ms)"; $("#aHint").textContent = hint(S.settings.ai_key).replace(/<[^>]+>/g, "");
      } catch (e) { out.className = "err"; out.textContent = "✗ " + e.message; }
    };
    $("#dSave").onclick = async function () {
      try { await save({ default_repo: $("#dRepo").value, default_branch: $("#dBranch").value, commit_message: $("#dMsg").value, default_private: $("#dPriv").checked, skip_junk: $("#dJunk").checked, chunk_files: $("#dChunk").value }); toast("Saved ✓"); } catch (e) { toast(e.message); }
    };
    $("#kSave").onclick = async function () { try { await save({ keepalive_enabled: $("#kOn").checked, keepalive_url: $("#kUrl").value }); toast("Saved ✓"); } catch (e) { toast(e.message); } };
    $("#kPing").onclick = async function () { try { var p = await api("POST", "/api/keepalive/ping", {}); toast(p.ok ? "Ping ✓" : "Ping fail: " + (p.keepalive.error || "")); } catch (e) { toast(e.message); } };
    $("#tDark").onchange = function () { lsSet("zg_dark", this.checked); applyDark(); };
    $("#tBack").onchange = function () { lsSet("gz_backup_on", this.checked); if (!this.checked) lsSet("gz_backup", {}); toast(this.checked ? "Backup on (agli baar token save karoge tab)" : "Backup hata diya"); };
    $("#oOut").onclick = async function () { try { await api("POST", "/api/logout", {}, { noAuth: true }); } catch (e) { } S.authed = false; showLogin(); };
  }

  if ("serviceWorker" in navigator) addEventListener("load", function () { navigator.serviceWorker.register("sw.js").catch(function () { }); });
  boot();
})();
