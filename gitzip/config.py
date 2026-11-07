"""Paths, environment and the persistent settings store."""
import json
import os
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DATA_DIR") or (ROOT / "data")).resolve()
STAGING_DIR = DATA_DIR / "staging"
JOBS_DIR = DATA_DIR / "jobs"
TMP_DIR = DATA_DIR / "tmp"
STATIC_DIR = ROOT / "static"
for _d in (DATA_DIR, STAGING_DIR, JOBS_DIR, TMP_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def _num(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


GITHUB_API = (os.environ.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
PACE = _num("GZ_PACE", 0.8)                    # seconds between content-creating GitHub calls
MAX_UPLOAD_BYTES = int(_num("MAX_UPLOAD_MB", 300) * 1048576)     # one zip / one file
MAX_EXTRACT_BYTES = int(_num("MAX_EXTRACT_MB", 1000) * 1048576)  # all files of one zip
MAX_FILE_BYTES = int(_num("MAX_FILE_MB", 50) * 1048576)          # single file inside the repo
STAGING_TTL_HOURS = _num("STAGING_TTL_HOURS", 72)
DEFAULT_AI_BASE = "https://api.inceptionlabs.ai/v1"
DEFAULT_AI_MODEL = "mercury-3"

DEFAULTS = {
    "github_token": "",
    "ai_key": "",
    "ai_base_url": DEFAULT_AI_BASE,
    "ai_model": DEFAULT_AI_MODEL,
    "ai_reasoning": "low",
    "ai_confirm_uploads": True,
    "default_repo": "",
    "default_branch": "",
    "default_private": True,
    "commit_message": "Upload via GitZip",
    "skip_junk": True,
    "chunk_files": 100,
    "keepalive_enabled": True,
    "keepalive_url": "",
}
SECRETS = {"github_token": "GITHUB_TOKEN", "ai_key": "INCEPTION_API_KEY"}
ENV_FALLBACK = {"ai_base_url": "AI_BASE_URL", "ai_model": "AI_MODEL"}


class Settings:
    """JSON-file backed settings. Secrets may also come from env vars (they survive Render restarts)."""

    def __init__(self, path=None):
        self.path = Path(path) if path else DATA_DIR / "settings.json"
        self.lock = threading.RLock()
        self.data = {}
        self._load()

    def _load(self):
        try:
            self.data = json.loads(self.path.read_text("utf-8"))
        except Exception:
            self.data = {}

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2), "utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)

    def get(self, key):
        with self.lock:
            v = self.data.get(key)
            if v in (None, ""):
                env = SECRETS.get(key) or ENV_FALLBACK.get(key)
                if env and os.environ.get(env):
                    return os.environ[env].strip()
                return DEFAULTS[key]
            return v

    def source(self, key):
        with self.lock:
            if self.data.get(key) not in (None, ""):
                return "saved"
            env = SECRETS.get(key) or ENV_FALLBACK.get(key)
            if env and os.environ.get(env):
                return "env"
            return "none"

    def update(self, patch):
        """Apply a partial update; unknown keys are ignored. Returns list of changed keys."""
        changed = []
        with self.lock:
            for k, v in (patch or {}).items():
                if k not in DEFAULTS:
                    continue
                d = DEFAULTS[k]
                if isinstance(d, bool):
                    v = bool(v)
                elif isinstance(d, int):
                    try:
                        v = max(10, min(500, int(v)))
                    except (TypeError, ValueError):
                        continue
                else:
                    v = str(v if v is not None else "").strip()
                    if k == "ai_base_url":
                        v = v.rstrip("/")
                        for tail in ("/chat/completions", "/models"):
                            if v.endswith(tail):
                                v = v[: -len(tail)]
                        if v and not v.startswith(("http://", "https://")):
                            v = "https://" + v
                    if k == "default_repo":
                        v = v.strip("/ ")
                    if k == "keepalive_url":
                        v = v.rstrip("/")
                if self.data.get(k) != v:
                    self.data[k] = v
                    changed.append(k)
            if changed:
                self._save()
        return changed

    def public(self):
        """Settings for the UI: secrets are never sent back, only a hint."""
        out = {}
        with self.lock:
            for k in DEFAULTS:
                if k in SECRETS:
                    v = self.get(k)
                    out[k] = {"set": bool(v), "hint": ("…" + v[-4:]) if v else "", "source": self.source(k)}
                else:
                    out[k] = self.get(k)
        return out


settings = Settings()
