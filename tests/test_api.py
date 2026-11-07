"""End-to-end API test: real server process + fake GitHub + fake Inception. Run: python3 tests/test_api.py"""
import http.cookiejar
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import mock_services as ms  # noqa: E402

for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
    os.environ.pop(k, None)
os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1,localhost"

ms.serve(ms.GitHubHandler, 9101)
ms.serve(ms.AIHandler, 9102)
TMP = tempfile.mkdtemp(prefix="gz-api-")
PORT = 8099
env = dict(os.environ, DATA_DIR=TMP, PORT=str(PORT), APP_PASSWORD="secret123", GITHUB_API_URL="http://127.0.0.1:9101", GZ_PACE="0")
proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "server.py")], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
BASE = "http://127.0.0.1:%d" % PORT
jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   ", name)
    else:
        FAIL += 1
        print("  FAIL ", name, extra)


def api(method, path, body=None, raw=None, headers=None, expect_error=False):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    h = {"X-GZ": "1"}
    if raw is None and body is not None:
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=h)
    try:
        with opener.open(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def wait(cond, timeout=40):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            v = cond()
        except (urllib.error.URLError, ConnectionError):
            v = None
        if v:
            return v
        time.sleep(0.15)
    return None


try:
    assert wait(lambda: api("GET", "/healthz")[0] == 200 if proc.poll() is None else False, 15), "server did not start"
    print("\n== auth")
    s, j = api("GET", "/api/staging")
    check("API needs login", s == 401)
    s, j = api("POST", "/api/login", {"password": "wrong"})
    check("wrong password rejected", s == 401)
    s, j = api("POST", "/api/login", {"password": "secret123"})
    check("login ok", s == 200)
    s, j = api("GET", "/api/session")
    check("session cookie works", j.get("authed") is True)
    req = urllib.request.Request(BASE + "/api/staging", data=b"{}", method="POST")
    try:
        urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar)).open(req)
        csrf = False
    except urllib.error.HTTPError as e:
        csrf = e.code == 403
    check("POST without X-GZ header blocked (CSRF)", csrf)
    r = urllib.request.urlopen(BASE + "/")
    check("UI served", r.status == 200 and b"GitZip" in r.read())
    r = urllib.request.Request(BASE + "/healthz", method="HEAD")
    check("HEAD /healthz (uptime pingers)", urllib.request.urlopen(r).status == 200)

    print("\n== settings")
    s, j = api("POST", "/api/settings", {"github_token": "good-token", "ai_key": "inc-good", "ai_base_url": "http://127.0.0.1:9102/v1/chat/completions/",
                                         "ai_model": "mercury-3", "default_private": False})
    check("settings saved, URL normalised", j["settings"]["ai_base_url"] == "http://127.0.0.1:9102/v1", j["settings"]["ai_base_url"])
    check("secrets never returned", "good-token" not in json.dumps(j) and j["settings"]["github_token"]["set"] is True)
    s, j = api("POST", "/api/settings/test-github", {})
    check("github test shows login", j.get("login") == "tester", j)
    s, j = api("POST", "/api/settings/test-ai", {})
    check("AI test ok", j.get("ok") is True, j)
    s, j = api("GET", "/api/models")
    check("model list", "mercury-3" in j["models"], j)
    api("POST", "/api/settings", {"github_token": "bad"})
    s, j = api("POST", "/api/settings/test-github", {})
    check("bad token -> friendly message", s == 502 and "token" in j["error"].lower(), j)
    api("POST", "/api/settings", {"github_token": "good-token"})

    print("\n== chunked upload over HTTP (with a retried chunk)")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for i in range(30):
            z.writestr("proj/mod%02d.py" % i, ("x = %d\n" % i) * 100)
        z.writestr("proj/data.bin", os.urandom(50_000))
    data = buf.getvalue()
    s, up = api("POST", "/api/upload/init", {"name": "proj.zip", "size": len(data)})
    uid, off = up["uid"], 0
    chunk = 20000
    first = True
    while off < len(data):
        piece = data[off:off + chunk]
        s, j = api("PUT", "/api/upload/%s?offset=%d" % (uid, off), raw=piece, headers={"Content-Type": "application/octet-stream"})
        assert s == 200, (s, j)
        if first:   # client lost the response and re-sends the same chunk
            s2, j2 = api("PUT", "/api/upload/%s?offset=%d" % (uid, off), raw=piece, headers={"Content-Type": "application/octet-stream"})
            check("duplicate chunk -> 409 with server offset", s2 == 409 and j2["offset"] == off + len(piece), (s2, j2))
            first = False
        off = j["offset"]
    s, st = api("POST", "/api/upload/%s/finish" % uid, {})
    sid = st["id"]
    st = wait(lambda: (lambda d: d if d["status"] != "extracting" else None)(api("GET", "/api/staging/" + sid)[1]))
    check("unzipped in background", st and st["status"] == "ready" and st["counts"]["upload"] == 31, st and (st["status"], st.get("error")))
    check("top folder stripped", st["strip"] == "proj/")
    s, st2 = api("POST", "/api/staging/%s/options" % sid, {"ignore": "*.bin"})
    check("options change recomputes", st2["counts"]["upload"] == 30, st2["counts"])
    s, st2 = api("POST", "/api/staging/%s/options" % sid, {"ignore": ""})

    print("\n== compare + job (browser can close: only polls)")
    s, c = api("POST", "/api/compare", {"staging_id": sid, "repo": "tester/proj"})
    check("compare on missing repo", c["repo_exists"] is False and c["new"] == 31, c)
    s, job = api("POST", "/api/jobs", {"staging_id": sid, "repo": "proj", "create_repo": True, "private": True})
    jid = job["id"]
    j = wait(lambda: (lambda d: d if d["status"] not in ("queued", "running") else None)(api("GET", "/api/jobs/" + jid)[1]))
    check("job done", j and j["status"] == "done" and j["verified"], j and (j["status"], j["error"]))
    check("log is available", "Poora ho gaya" in j["log"], j["log"][-300:])
    s, files = None, None
    req = urllib.request.Request("http://127.0.0.1:9101/__mock/files", data=json.dumps({"full": "tester/proj"}).encode(), method="POST")
    files = json.loads(urllib.request.urlopen(req).read())
    check("31 files on GitHub", len(files) == 31 + 0 or len(files) == 32, len(files))
    s, j2 = api("POST", "/api/jobs/%s/resume" % jid, {})
    j2 = wait(lambda: (lambda d: d if d["status"] not in ("queued", "running") else None)(api("GET", "/api/jobs/" + jid)[1]))
    check("resume of finished job = no-op upload", j2["status"] == "done" and j2["counts"]["todo"] == 0, j2["counts"])
    s, ls = api("GET", "/api/repos/tester/proj/ls?path=")
    check("repo browser lists entries", any(e["name"] == "mod00.py" for e in ls["entries"]), ls)
    s, d = api("POST", "/api/repos/tester/proj/delete", {"paths": ["mod00.py", "mod01.py"], "confirm": "yes"})
    check("delete in one commit", s == 200 and d["deleted"] == 2, d)

    print("\n== slash commands (no AI needed)")
    s, r = api("POST", "/api/chat", {"messages": [{"role": "user", "content": "/staging"}], "active_staging": sid})
    check("/staging", sid in r["messages"][0]["content"], r)
    s, r = api("POST", "/api/chat", {"messages": [{"role": "user", "content": "/compare tester/proj"}], "active_staging": sid})
    check("/compare", "same" in r["messages"][0]["content"], r)

    print("\n== AI chat with tools + approval card")
    s, r = api("POST", "/api/chat", {"messages": [{"role": "user", "content": "hello"}], "active_staging": sid})
    check("plain reply", "Namaste" in r["messages"][-1]["content"], r)
    s, r = api("POST", "/api/chat", {"messages": [{"role": "user", "content": "isko upload kar do"}], "active_staging": sid})
    ev = r["events"]
    check("AI called list_staging then start_upload", [e["tool"] for e in ev] == ["list_staging", "start_upload"], ev)
    ap = ev[-1].get("approval")
    check("upload waits for approval (nothing started yet)", ap and ap["status"] == "pending", ev)
    check("tool messages are in OpenAI format", r["messages"][0].get("tool_calls") and r["messages"][1]["role"] == "tool", r["messages"][:2])
    s, jl = api("GET", "/api/jobs")
    n_before = len(jl["jobs"])
    s, a = api("POST", "/api/approvals/" + ap["id"], {"approve": True})
    check("approve starts job", a["status"] == "approved" and a["job_id"], a)
    j = wait(lambda: (lambda d: d if d["status"] not in ("queued", "running") else None)(api("GET", "/api/jobs/" + a["job_id"])[1]))
    check("AI-started job finished", j and j["status"] == "done", j and j["error"])
    check("system prompt carried staging context", "ATTACHED" in ms.AIHandler.calls[-1]["messages"][0]["content"] or "st_" in ms.AIHandler.calls[-1]["messages"][0]["content"])
    check("tokens never sent to AI", "good-token" not in json.dumps(ms.AIHandler.calls))

    print("\n== AI errors are readable")
    api("POST", "/api/settings", {"ai_key": "wrong"})
    s, r = api("POST", "/api/chat", {"messages": [{"role": "user", "content": "hello"}]})
    check("bad AI key message", "key" in r.get("error", "").lower(), r)
    api("POST", "/api/settings", {"ai_key": "inc-good"})

    print("\n== housekeeping")
    s, st = api("GET", "/api/status")
    check("status endpoint", st["runner_alive"] is True and st["github"] and st["ai"], st)
    s, _ = api("DELETE", "/api/staging/" + sid)
    check("delete staging", s == 200)
    s, j = api("POST", "/api/login", {"password": "secret123"})
    for _ in range(5):
        api("POST", "/api/login", {"password": "nope"})
finally:
    proc.terminate()
    try:
        out = proc.communicate(timeout=5)[0].decode()
    except Exception:
        out = ""
    if "Traceback" in out:
        print("\n--- server log (errors) ---\n" + out[-2500:])
    shutil.rmtree(TMP, ignore_errors=True)

print("\n%d passed, %d failed" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
