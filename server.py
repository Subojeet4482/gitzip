#!/usr/bin/env python3
"""GitZip Bot entry point:  python server.py   (Render: Start Command = python server.py)"""
import os
import signal
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gitzip import VERSION, background, jobs, web  # noqa: E402
from gitzip.config import DATA_DIR  # noqa: E402


def main():
    port = int(os.environ.get("PORT", "10000"))
    resumed = jobs.runner.resume_pending()
    jobs.runner.start()
    background.start_all()
    srv = web.make_server("0.0.0.0", port)
    signal.signal(signal.SIGTERM, lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
    print("=" * 60, flush=True)
    print("GitZip Bot v%s listening on port %d" % (VERSION, port), flush=True)
    print("Data folder: %s%s" % (DATA_DIR, "" if os.environ.get("DATA_DIR") else "  (temporary on Render free plan)"), flush=True)
    if resumed:
        print("Resumed %d unfinished upload job(s)" % resumed, flush=True)
    if web.PASSWORD_GENERATED:
        print("LOGIN PASSWORD (APP_PASSWORD env var not set): %s" % web.PASSWORD, flush=True)
    print("Keep-alive URL:", background.keepalive_url() or "(none - set RENDER_EXTERNAL_URL or Settings)", flush=True)
    print("=" * 60, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("Shutting down…", flush=True)
        srv.server_close()


if __name__ == "__main__":
    main()
