"""HTTP layer: static UI + JSON API (stdlib only, no dependencies)."""
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import VERSION, ai, background, jobs, staging, uploads
from .config import DATA_DIR, STATIC_DIR, settings
from .ghclient import GH, GitHubError, UserError, friendly, list_dir

mimetypes.add_type("application/manifest+json", ".webmanifest")
mimetypes.add_type("text/javascript", ".js")

# ----------------------------------------------------------------------------- auth
def _load_password():
    pw = os.environ.get("APP_PASSWORD", "").strip()
    if pw:
        return pw, False
    p = DATA_DIR / "auth.json"
    try:
        return json.loads(p.read_text("utf-8"))["password"], True
    except (OSError, ValueError, KeyError):
        pw = secrets.token_urlsafe(9)
        try:
            p.write_text(json.dumps({"password": pw}), "utf-8")
            os.chmod(p, 0o600)
        except OSError:
            pass
        return pw, True


PASSWORD, PASSWORD_GENERATED = _load_password()
SECRET = (os.environ.get("SECRET_KEY") or hashlib.sha256(("gz7|" + PASSWORD).encode()).hexdigest()).encode()
FAILS = {}
COOKIE = "gz_s"


def _sign(s):
    return hmac.new(SECRET, s.encode(), hashlib.sha256).hexdigest()[:40]


def make_session():
    exp = str(int(time.time()) + 30 * 86400)
    return exp + "." + _sign(exp)


def valid_session(v):
    try:
        exp, sig = v.split(".", 1)
        return int(exp) > time.time() and hmac.compare_digest(sig, _sign(exp))
    except (ValueError, AttributeError):
        return False


# ----------------------------------------------------------------------------- handler
class ApiError(Exception):
    def __init__(self, status, msg, extra=None):
        super().__init__(msg)
        self.status, self.msg, self.extra = status, msg, extra or {}


ROUTES = []


def route(method, pattern):
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), fn))
        return fn
    return deco


CSP = ("default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "GitZip/" + VERSION
    timeout = 120

    def log_message(self, fmt, *args):
        pass

    # ---- plumbing
    def setup(self):
        super().setup()
        self._consumed = False

    def cookie(self, name):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return ""

    def ip(self):
        return (self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[0].strip()

    def secure(self):
        return (self.headers.get("X-Forwarded-Proto") or "").lower() == "https"

    def body_json(self, limit=6_000_000):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ApiError(413, "Body bahut bada hai")
        raw = self.rfile.read(n) if n else b""
        self._consumed = True
        if not raw:
            return {}
        try:
            j = json.loads(raw)
        except ValueError:
            raise ApiError(400, "JSON galat hai")
        return j if isinstance(j, dict) else {}

    def send_bytes(self, status, data, ctype, headers=None, head=False):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Referrer-Policy", "same-origin")
        if not self._consumed and int(self.headers.get("Content-Length") or 0) > 0:
            self.send_header("Connection", "close")
            self.close_connection = True
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if not head:
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def send_json(self, status, obj, headers=None, head=False):
        h = {"Cache-Control": "no-store"}
        h.update(headers or {})
        self.send_bytes(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", h, head)

    # ---- dispatch
    def do_GET(self):
        self.dispatch("GET")

    def do_HEAD(self):
        self.dispatch("HEAD")

    def do_POST(self):
        self.dispatch("POST")

    def do_PUT(self):
        self.dispatch("PUT")

    def do_DELETE(self):
        self.dispatch("DELETE")

    def dispatch(self, method):
        head = method == "HEAD"
        url = urlsplit(self.path)
        self.q = {k: v[0] for k, v in parse_qs(url.query).items()}
        path = url.path
        try:
            if path == "/healthz":
                return self.send_json(200, {"ok": True, "v": VERSION}, head=head)
            if not path.startswith("/api/"):
                return self.static(path, head)
            if method not in ("GET", "HEAD") and self.headers.get("X-GZ") != "1":
                raise ApiError(403, "Missing X-GZ header")
            open_routes = ("/api/login", "/api/session", "/api/logout")
            if path not in open_routes and not valid_session(self.cookie(COOKIE)):
                raise ApiError(401, "Login karo")
            m_ = "GET" if head else method
            for rm, rx, fn in ROUTES:
                if rm == m_:
                    m = rx.match(path)
                    if m:
                        res = fn(self, *m.groups())
                        if res is not None:
                            status, obj, *hdr = res
                            self.send_json(status, obj, hdr[0] if hdr else None, head)
                        return
            raise ApiError(404, "Not found")
        except ApiError as e:
            self.send_json(e.status, dict({"error": e.msg}, **e.extra), head=head)
        except GitHubError as e:
            self.send_json(502, {"error": friendly(e)}, head=head)
        except ai.AIError as e:
            self.send_json(200, {"error": str(e), "ai_error": True}, head=head)
        except (UserError, ValueError) as e:
            self.send_json(400, {"error": str(e)}, head=head)
        except KeyError as e:
            self.send_json(404, {"error": str(e.args[0]) if e.args else "Not found"}, head=head)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except Exception:
            sys.stderr.write("API error on %s %s\n%s\n" % (method, path, traceback.format_exc()))
            self.send_json(500, {"error": "Server error — logs dekho"}, head=head)

    def static(self, path, head):
        if path in ("", "/"):
            path = "/index.html"
        rel = path.lstrip("/")
        target = os.path.normpath(os.path.join(str(STATIC_DIR), rel))
        if not target.startswith(str(STATIC_DIR) + os.sep) or not os.path.isfile(target):
            return self.send_bytes(404, b"Not found", "text/plain", head=head)
        with open(target, "rb") as f:
            data = f.read()
        ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith(("json", "javascript", "xml")):
            ctype += "; charset=utf-8"
        self.send_bytes(200, data, ctype, {"Cache-Control": "no-cache"}, head)


# ----------------------------------------------------------------------------- auth routes
@route("POST", "/api/login")
def api_login(h):
    ip = h.ip()
    n, until = FAILS.get(ip, (0, 0))
    gn, guntil = FAILS.get("*", (0, 0))      # global limit too: X-Forwarded-For can be spoofed
    if max(until, guntil) > time.time():
        raise ApiError(429, "Bahut galat try. %d sec baad dobara karo." % (max(until, guntil) - time.time()))
    pw = str(h.body_json().get("password", ""))
    if hmac.compare_digest(pw.encode(), PASSWORD.encode()):
        FAILS.pop(ip, None)
        FAILS.pop("*", None)
        ck = "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax%s" % (COOKIE, make_session(), 30 * 86400, "; Secure" if h.secure() else "")
        return 200, {"ok": True}, {"Set-Cookie": ck}
    n += 1
    gn += 1
    FAILS[ip] = (n, time.time() + 60 if n >= 5 else 0)
    FAILS["*"] = (gn, time.time() + 60 if gn >= 15 else 0)
    time.sleep(0.6)
    raise ApiError(401, "Password galat hai")


@route("POST", "/api/logout")
def api_logout(h):
    h.body_json()
    return 200, {"ok": True}, {"Set-Cookie": "%s=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax" % COOKIE}


@route("GET", "/api/session")
def api_session(h):
    return 200, {"authed": valid_session(h.cookie(COOKIE)), "version": VERSION}


@route("GET", "/api/status")
def api_status(h):
    s = background.status()
    s["github"] = bool(settings.get("github_token"))
    s["ai"] = bool(settings.get("ai_key"))
    return 200, s


# ----------------------------------------------------------------------------- settings
@route("GET", "/api/settings")
def api_settings(h):
    return 200, settings.public()


@route("POST", "/api/settings")
def api_settings_save(h):
    changed = settings.update(h.body_json())
    return 200, {"changed": changed, "settings": settings.public()}


@route("POST", "/api/settings/test-github")
def api_test_github(h):
    tok = str(h.body_json().get("token") or "").strip() or settings.get("github_token")
    if not tok:
        raise ApiError(400, "Pehle token daalo")
    me, hdrs = GH(tok).me()
    repos = GH(tok).list_repos(1)
    return 200, {"ok": True, "login": me["login"], "scopes": hdrs.get("x-oauth-scopes") or "fine-grained / no scope header",
                 "repos_visible": len(repos), "rate_remaining": hdrs.get("x-ratelimit-remaining")}


@route("POST", "/api/settings/test-ai")
def api_test_ai(h):
    h.body_json()
    return 200, ai.test_ai()


@route("GET", "/api/models")
def api_models(h):
    return 200, {"models": ai.list_models()}


@route("POST", "/api/keepalive/ping")
def api_ping(h):
    h.body_json()
    ok = background.ping_once()
    return 200, {"ok": ok, "keepalive": background.KA}


# ----------------------------------------------------------------------------- uploads
@route("POST", "/api/upload/init")
def api_up_init(h):
    b = h.body_json()
    return 200, uploads.init(b.get("name", ""), b.get("size", 0), b.get("staging_id"), b.get("rel"))


@route("GET", "/api/upload/(up_[0-9a-f]+)")
def api_up_offset(h, uid):
    return 200, {"offset": uploads.offset(uid)}


@route("PUT", "/api/upload/(up_[0-9a-f]+)")
def api_up_chunk(h, uid):
    n = int(h.headers.get("Content-Length") or 0)
    if n <= 0 or n > 16 * 1048576:
        raise ApiError(413, "Chunk size galat")
    try:
        size = uploads.write_chunk(uid, int(h.q.get("offset", "-1")), h.rfile, n)
    except uploads.Conflict as c:
        h._consumed = True
        raise ApiError(409, "offset mismatch", {"offset": c.size})
    h._consumed = True
    return 200, {"offset": size}


@route("POST", "/api/upload/(up_[0-9a-f]+)/finish")
def api_up_finish(h, uid):
    b = h.body_json()
    try:
        st = uploads.finish(uid, b.get("staging_id"))
    except uploads.Conflict as c:
        raise ApiError(409, "Upload poora nahi hua", {"offset": c.size})
    return 200, st


# ----------------------------------------------------------------------------- staging
def _stage_detail(sid, limit=1500):
    m = staging.get(sid)
    if not m:
        raise ApiError(404, "Staging nahi mili")
    if m["status"] != "ready":
        return dict(m, files_list=[], counts=None)
    eff = staging.effective(sid)
    rows = [{"path": f["path"], "size": f["size"], "skip": f["skip"]} for f in eff["files"][:limit]]
    return dict(m, files_list=rows, truncated=len(eff["files"]) > limit, counts=eff["counts"], strip=eff["strip"])


@route("GET", "/api/staging")
def api_stage_list(h):
    return 200, {"staging": staging.list_all()}


@route("POST", "/api/staging")
def api_stage_new(h):
    return 200, staging.create(str(h.body_json().get("name") or "Files"))


@route("GET", r"/api/staging/(st_\d+_[0-9a-f]+)")
def api_stage_get(h, sid):
    return 200, _stage_detail(sid)


@route("POST", r"/api/staging/(st_\d+_[0-9a-f]+)/options")
def api_stage_opts(h, sid):
    staging.set_options(sid, h.body_json())
    return 200, _stage_detail(sid)


@route("POST", r"/api/staging/(st_\d+_[0-9a-f]+)/recount")
def api_stage_recount(h, sid):
    h.body_json()
    staging.recount(sid)
    return 200, _stage_detail(sid)


@route("DELETE", r"/api/staging/(st_\d+_[0-9a-f]+)")
def api_stage_del(h, sid):
    if sid in jobs.runner.busy_staging():
        raise ApiError(409, "Is zip ka upload chal raha hai — pehle job cancel karo.")
    staging.delete(sid)
    return 200, {"ok": True}


@route("GET", r"/api/staging/(st_\d+_[0-9a-f]+)/file")
def api_stage_file(h, sid):
    try:
        txt, cut = staging.read_text(sid, staging.dest_to_rel(sid, h.q.get("path", "")))
    except FileNotFoundError:
        raise ApiError(404, "File nahi mili")
    return 200, {"text": txt, "truncated": cut}


@route("PUT", r"/api/staging/(st_\d+_[0-9a-f]+)/file")
def api_stage_file_put(h, sid):
    b = h.body_json()
    staging.write_text(sid, staging.dest_to_rel(sid, b.get("path", "")), str(b.get("content", "")))
    return 200, _stage_detail(sid)


# ----------------------------------------------------------------------------- repos
@route("GET", "/api/repos")
def api_repos(h):
    rs = GH(settings.get("github_token")).list_repos()
    return 200, {"repos": [{"full_name": r["full_name"], "private": r["private"], "default_branch": r["default_branch"],
                            "can_push": (r.get("permissions") or {}).get("push", True), "archived": r.get("archived", False),
                            "url": r.get("html_url", "")} for r in rs]}


@route("POST", "/api/repos")
def api_repo_new(h):
    b = h.body_json()
    name = str(b.get("name", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise ApiError(400, "Repo naam me sirf letters, numbers, - _ . allowed hain")
    gh = GH(settings.get("github_token"))
    r = gh.create_repo(name, bool(b.get("private", True)), str(b.get("description", "")), gh.me()[0]["login"])
    return 200, {"full_name": r["full_name"], "url": r.get("html_url", "")}


@route("GET", r"/api/repos/([^/]+/[^/]+)/ls")
def api_repo_ls(h, full):
    gh = GH(settings.get("github_token"))
    br = h.q.get("branch") or gh.get_repo(full)["default_branch"]
    return 200, {"repo": full, "branch": br, "path": h.q.get("path", ""), "entries": list_dir(gh, full, br, h.q.get("path", ""))}


@route("POST", r"/api/repos/([^/]+/[^/]+)/delete")
def api_repo_delete(h, full):
    b = h.body_json()
    if b.get("confirm") != "yes":
        raise ApiError(400, "Confirm chahiye")
    return 200, jobs.delete_paths(full, b.get("branch", ""), [str(p) for p in b.get("paths", [])][:500])


@route("POST", "/api/compare")
def api_compare(h):
    b = h.body_json()
    return 200, jobs.compare(b.get("staging_id", ""), b.get("repo", ""), b.get("branch", ""))


# ----------------------------------------------------------------------------- jobs
@route("GET", "/api/jobs")
def api_jobs(h):
    return 200, {"jobs": [jobs.store.snapshot(j["id"], 0) for j in jobs.store.all()[:60]]}


@route("POST", "/api/jobs")
def api_job_new(h):
    b = h.body_json()
    return 200, jobs.start_job(b.get("staging_id", ""), b.get("repo", ""), b.get("branch", ""), b.get("message", ""),
                               bool(b.get("create_repo")), bool(b.get("private", True)))


@route("GET", r"/api/jobs/(j_\d+_[0-9a-f]+)")
def api_job_get(h, jid):
    j = jobs.store.snapshot(jid)
    if not j:
        raise ApiError(404, "Job nahi mila")
    text, nxt = jobs.store.read_log(jid, int(h.q.get("since", "0") or 0))
    j["log"], j["log_next"] = text, nxt
    return 200, j


@route("POST", r"/api/jobs/(j_\d+_[0-9a-f]+)/cancel")
def api_job_cancel(h, jid):
    h.body_json()
    return 200, {"ok": jobs.runner.cancel(jid)}


@route("POST", r"/api/jobs/(j_\d+_[0-9a-f]+)/resume")
def api_job_resume(h, jid):
    h.body_json()
    ok = jobs.runner.resume(jid)
    if not ok:
        raise ApiError(409, "Ye job abhi resume nahi ho sakta")
    return 200, {"ok": True}


@route("DELETE", r"/api/jobs/(j_\d+_[0-9a-f]+)")
def api_job_del(h, jid):
    j = jobs.store.get(jid)
    if j and j["status"] in jobs.ACTIVE:
        raise ApiError(409, "Pehle cancel karo")
    jobs.store.delete(jid)
    return 200, {"ok": True}


# ----------------------------------------------------------------------------- AI
@route("POST", "/api/chat")
def api_chat(h):
    b = h.body_json(2_000_000)
    msgs = b.get("messages")
    if not isinstance(msgs, list) or not msgs:
        raise ApiError(400, "messages chahiye")
    return 200, ai.run_chat(msgs, b.get("active_staging"))


@route("GET", r"/api/approvals/(ap_[0-9a-f]+)")
def api_ap_get(h, aid):
    ap = ai.get_approval(aid)
    if not ap:
        raise ApiError(404, "Approval nahi mila (server restart?)")
    return 200, ap


@route("POST", r"/api/approvals/(ap_[0-9a-f]+)")
def api_ap_decide(h, aid):
    return 200, ai.decide(aid, bool(h.body_json().get("approve")))


# ----------------------------------------------------------------------------- server
class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128


def make_server(host, port):
    return Server((host, port), Handler)
