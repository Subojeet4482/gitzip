"""Browser test of the real UI (Chromium, phone viewport) against real server + fake GitHub/AI.
Run: python3 tests/ui_test.py [screenshot_dir]"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import mock_services as ms  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
    os.environ.pop(k, None)
os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1,localhost"
SHOTS = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="gz-shots-")
os.makedirs(SHOTS, exist_ok=True)

ms.serve(ms.GitHubHandler, 9101)
ms.serve(ms.AIHandler, 9102)
TMP = tempfile.mkdtemp(prefix="gz-ui-")
PORT = 8098
env = {k: v for k, v in os.environ.items() if k not in ("GITHUB_TOKEN", "INCEPTION_API_KEY")}
env.update(DATA_DIR=TMP, PORT=str(PORT), APP_PASSWORD="secret123", GITHUB_API_URL="http://127.0.0.1:9101", GZ_PACE="0")
proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "server.py")], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
BASE = "http://127.0.0.1:%d" % PORT
PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   ", name)
    else:
        FAIL += 1
        print("  FAIL ", name, extra)


for _ in range(60):
    try:
        urllib.request.urlopen(BASE + "/healthz", timeout=1)
        break
    except Exception:
        time.sleep(0.25)

buf = io.BytesIO()
with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr("my-app-main/README.md", "# My app\n")
    z.writestr("my-app-main/src/main.py", "print('hello')\n" * 20)
    z.writestr("my-app-main/src/util.py", "x = 1\n")
    z.writestr("my-app-main/node_modules/a/b.js", "junk")
    z.writestr("my-app-main/logo.bin", os.urandom(3000))
zip_path = os.path.join(TMP, "my-app-main.zip")
open(zip_path, "wb").write(buf.getvalue())

errors = []
try:
    with sync_playwright() as pw:
        br = pw.chromium.launch()
        ctx = br.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=1, is_mobile=True, has_touch=True, bypass_csp=True)
        page = ctx.new_page()
        page.on("console", lambda m: errors.append("console: " + m.text) if m.type == "error" and "401" not in m.text else None)  # 401 = the wrong-password attempt on purpose
        page.on("pageerror", lambda e: errors.append("pageerror: " + str(e)))
        page.on("dialog", lambda d: d.accept())

        def shot(name):
            page.screenshot(path=os.path.join(SHOTS, name + ".png"))

        print("\n== login")
        page.goto(BASE)
        page.wait_for_selector("#lpw")
        shot("01-login")
        page.fill("#lpw", "wrong")
        page.click("#lgo")
        page.wait_for_function("document.querySelector('#lerr').textContent.length > 0")
        check("wrong password message", "galat" in page.inner_text("#lerr"))
        page.fill("#lpw", "secret123")
        page.click("#lgo")
        page.wait_for_selector("#drop")
        check("home shown after login", True)
        check("no-token hint visible", "token abhi set nahi" in page.inner_text("#view"))

        print("\n== settings: GitHub + AI")
        page.goto(BASE + "/#/settings")
        page.wait_for_selector("#gTok")
        page.fill("#gTok", "good-token")
        page.click("#gSave")
        page.wait_for_function("document.querySelector('#gRes').textContent.includes('Login')")
        check("github test result", "tester" in page.inner_text("#gRes"), page.inner_text("#gRes"))
        page.fill("#aKey", "inc-good")
        page.fill("#aBase", "http://127.0.0.1:9102/v1")
        page.click("#aSave")
        page.wait_for_function("/chal raha|✗/.test(document.querySelector('#aRes').textContent)", timeout=15000)
        check("AI test result", "chal raha" in page.inner_text("#aRes"), page.inner_text("#aRes"))
        shot("02-settings")

        print("\n== add zip from the browser")
        page.goto(BASE + "/#/")
        page.wait_for_selector("#drop")
        page.set_input_files("#pickZip", zip_path)
        page.wait_for_selector("[data-s]", timeout=20000)
        check("zip listed", "my-app-main" in page.inner_text("#stl"))
        shot("03-home")
        page.click("[data-s]")
        page.wait_for_selector("#sStats")
        txt = page.inner_text("#sStats")
        check("stats: 4 files, junk skipped", "4" in txt and "skip" in txt, txt)
        check("top folder stripped note", "my-app-main/" in page.inner_text("#oStripInfo"))
        paths = page.inner_text("#sFiles")
        check("files list has stripped paths", "src/main.py" in paths and "my-app-main/src" not in paths, paths)
        shot("04-stage")
        page.fill("#oPrefix", "backend")
        page.wait_for_function("document.querySelector('#sFiles').innerText.includes('backend/src/main.py')", timeout=5000)
        check("destination folder applied live", True)
        page.fill("#oPrefix", "")
        page.wait_for_function("!document.querySelector('#sFiles').innerText.includes('backend/')", timeout=5000)

        print("\n== compare + upload (job runs on server)")
        page.wait_for_function("document.querySelector('#uRepo').options.length >= 1")
        page.select_option("#uRepo", "__new__")
        page.fill("#uName", "ui-demo")
        page.click("#uCmp")
        page.wait_for_function("document.querySelector('#uRes').innerText.includes('nayi')")
        check("compare shows new repo", "exist nahi" in page.inner_text("#uRes"), page.inner_text("#uRes"))
        page.click("#uGo")
        page.wait_for_selector("#mOk")
        page.click("#mOk")
        page.wait_for_selector("#jPill")
        page.wait_for_function("document.querySelector('#jPill').innerText.includes('done')", timeout=30000)
        check("job finished in UI", True)
        check("log visible", "Poora ho gaya" in page.inner_text("#jLog"), page.inner_text("#jLog"))
        shot("05-job-done")
        files = json.loads(urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:9101/__mock/files", data=json.dumps({"full": "tester/ui-demo"}).encode(), method="POST")).read())
        check("files really on 'GitHub'", set(files) == {"README.md", "src/main.py", "src/util.py", "logo.bin"}, sorted(files))

        print("\n== second upload of same zip = nothing re-sent")
        page.goto(BASE + "/#/")
        page.wait_for_selector("[data-s]")
        page.click("[data-s]")
        page.wait_for_selector("#uRepo")
        page.wait_for_function("document.querySelector('#uRepo').options.length > 1", timeout=10000)
        page.select_option("#uRepo", "tester/ui-demo")
        page.click("#uCmp")
        page.wait_for_function("document.querySelector('#uRes').innerText.includes('same')")
        res = page.inner_text("#uRes")
        check("compare: all same", "0 nayi" in res.replace("\n", " ") or "0" in res, res)
        shot("06-compare")

        print("\n== AI chat: tools + approval card")
        page.goto(BASE + "/#/ai")
        page.wait_for_selector("#cIn")
        page.fill("#cIn", "isko upload kar do")
        page.click("#cSend")
        page.wait_for_selector(".ap [data-yes]", timeout=20000)
        check("approval card shown", True)
        shot("07-ai-approval")
        page.click(".ap [data-yes]")
        page.wait_for_function("document.querySelector('.ap .pill') && document.querySelector('.ap .pill').innerText.includes('done')", timeout=30000)
        check("approved job completed from chat", True)
        page.fill("#cIn", "/help")
        page.click("#cSend")
        page.wait_for_function("document.querySelector('#msgs').innerText.includes('Commands')")
        check("slash command answered locally", True)
        shot("08-ai-done")

        print("\n== jobs, repos, dark mode, layout")
        page.goto(BASE + "/#/jobs")
        page.wait_for_selector(".job")
        check("jobs listed", page.locator(".job").count() >= 2)
        shot("09-jobs")
        page.goto(BASE + "/#/repos")
        page.wait_for_selector("[data-r]")
        check("repos listed", "tester/ui-demo" in page.inner_text("#rl"))
        page.click("[data-r]")
        page.wait_for_selector("[data-e]")
        shot("10-repo")
        page.evaluate("localStorage.setItem('zg_dark','true')")
        page.goto(BASE + "/#/")
        page.reload()
        page.wait_for_selector("#drop")
        shot("11-dark-home")
        sw = page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        check("no horizontal scroll on phone", sw)
        br.close()
finally:
    proc.terminate()
    out = proc.communicate(timeout=5)[0].decode()
    if "Traceback" in out:
        print("\n--- server log ---\n" + out[-2500:])
    shutil.rmtree(TMP, ignore_errors=True)

check("no JS console/page errors", not errors, errors)
print("\n%d passed, %d failed  (screenshots: %s)" % (PASS, FAIL, SHOTS))
sys.exit(1 if FAIL else 0)
