"""Background threads: keep-alive self ping, job-runner watchdog, disk janitor."""
import os
import random
import shutil
import threading
import time
import urllib.request

from . import VERSION, jobs, staging, uploads
from .config import JOBS_DIR, STAGING_TTL_HOURS, settings

START = time.time()
KA = {"last_ok": None, "last_try": None, "error": "", "pings": 0, "url": ""}


def keepalive_url():
    return (settings.get("keepalive_url") or os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("KEEPALIVE_URL") or "").rstrip("/")


def ping_once():
    url = keepalive_url()
    KA["url"] = url
    if not url:
        KA["error"] = "URL nahi mila (Render par RENDER_EXTERNAL_URL apne aap hota hai)"
        return False
    KA["last_try"] = time.time()
    try:
        req = urllib.request.Request(url + "/healthz", headers={"User-Agent": "GitZip-KeepAlive/" + VERSION})
        with urllib.request.urlopen(req, timeout=25) as r:
            r.read(64)
        KA.update(last_ok=time.time(), error="", pings=KA["pings"] + 1)
        return True
    except Exception as e:
        KA["error"] = str(e)[:160]
        return False


def _keepalive_loop():
    time.sleep(20)
    while True:
        try:
            if settings.get("keepalive_enabled") and keepalive_url():
                ok = ping_once()
                time.sleep(random.uniform(240, 300) if ok else 45)   # Render sleeps after 15 min idle
            else:
                KA["url"] = keepalive_url()
                time.sleep(60)
        except Exception:
            time.sleep(60)


def _watchdog_loop():
    while True:
        try:
            if not jobs.runner.alive():
                jobs.runner.start()
        except Exception:
            pass
        time.sleep(20)


def _janitor_loop():
    while True:
        try:
            staging.cleanup_old(STAGING_TTL_HOURS, jobs.runner.busy_staging())
            uploads.cleanup()
            cutoff = time.time() - 30 * 86400
            for j in jobs.store.all():
                if j["status"] not in jobs.ACTIVE and j["created"] < cutoff:
                    jobs.store.delete(j["id"])
        except Exception:
            pass
        time.sleep(1800)


def start_all():
    for fn, name in ((_keepalive_loop, "keepalive"), (_watchdog_loop, "watchdog"), (_janitor_loop, "janitor")):
        threading.Thread(target=fn, name=name, daemon=True).start()


def status():
    try:
        du = shutil.disk_usage(str(JOBS_DIR))
        free_mb = du.free >> 20
    except OSError:
        free_mb = None
    return {"version": VERSION, "uptime": int(time.time() - START), "runner_alive": jobs.runner.alive(),
            "running_job": jobs.runner.current, "queued": jobs.runner.q.qsize(), "free_mb": free_mb,
            "keepalive": dict(KA, enabled=bool(settings.get("keepalive_enabled")), url=keepalive_url()),
            "on_render": bool(os.environ.get("RENDER")), "ephemeral": not os.environ.get("DATA_DIR")}
