# GitZip Bot v7 — zip → GitHub, AI ke saath, Render par 24×7

Zip (ya files / folder) do → server par extract hota hai → GitHub repo me upload hota hai.
**Upload server par chalta hai**, isliye tab band karo, phone lock karo, app kill karo — upload chalta rehta hai.
Saath me **Inception Labs AI** (Mercury, 100M free tokens) hai jo pooch ke sab kuch kar deta hai — ya tum khud manual bhi kar sakte ho.

Sirf Python standard library use hoti hai — **koi package install nahi karna**.

---

## Purani problems → ab kya hua

| Purani problem | Ab |
|---|---|
| Tab/app band karte hi upload ruk jata tha | Upload **server par** chalta hai. Browser sirf dekhta hai. Wapas aao to progress wahi mil jata hai. |
| Poora dobara upload karo to har file dobara jaati thi | Har baar GitHub ke tree se **SHA compare** hota hai. Sirf **nayi/badli** files jaati hain, baaki skip. Resume/Retry/server-restart ke baad bhi wahi se aage. |
| Kabhi koi file upload nahi hoti thi, `404` aata tha | File path ab URL me jaata hi nahi (Git Data API). Aakhir me GitHub se **verify** hota hai ki har file pahunchi; jo nahi pahunchi wo auto-repair hoti hai. `.git` folder / galat paths apne aap skip. Khaali repo, naya repo (404 window), naya branch — sab handle. 404 ka asli reason (token ko repo access nahi) saaf Hindi/English me dikhta hai. |
| Network/GitHub atak jaye | Har call retry + back-off. 502/503, secondary rate limit, `Retry-After` — sab ke liye server khud ruk ke dobara try karta hai. |
| Mobile par bada zip upload fail | Browser → server upload **4 MB ke chunks** me hota hai; fail chunk apne aap dobara jaata hai (resume). |
| Render free par server so jata tha | Server khud ko har ~5 min ping karta hai (keep-alive). |
| Settings / token | Settings page me GitHub token + AI key; server par rehte hain (AI ko kabhi nahi dikhte). |

---

## Render par deploy (free plan)

1. Is folder ki saari files ko ek **naye GitHub repo** me daalo (pehli baar GitHub website → *Add file → Upload files*, ya purane GitZip se).
2. [render.com](https://render.com) → **New → Blueprint** → apna repo chuno → **Apply**.
   (Ya **New → Web Service**: Runtime *Python*, Build `pip install -r requirements.txt`, Start `python server.py`, Health Check Path `/healthz`.)
3. Service → **Environment** tab me `APP_PASSWORD` dikhega (Blueprint ne random bana diya). Ye hi tumhara login password hai. Chaaho to badal lo.
4. Service ka URL kholo (`https://xxxx.onrender.com`) → password daalo.
5. **Settings** (⚙️) me:
   * **GitHub token** daalo → *Save & test*.
   * **AI key** daalo (niche dekho) → *Save & test AI*.
6. **Free plan ka dhyan:** Render free par disk temporary hota hai — server restart/deploy par settings aur extract ki hui zips mit jaati hain. Isliye
   * Render → Environment me `GITHUB_TOKEN` aur `INCEPTION_API_KEY` bhi daal do (ye restart par bhi bachte hain), **ya**
   * Settings → "Tokens ka backup is device par" on rakho (default on hai) — restart ke baad app khulte hi tokens wapas set ho jaate hain.
   * Jo commit GitHub par ho chuka hai wo hamesha safe hai. Restart ke baad zip dobara add karke upload karoge to purani files skip hongi.

### GitHub token kaise banaye
GitHub → Settings → Developer settings → Personal access tokens.
* **Classic token:** sirf `repo` tick karo. (Sabse aasaan.)
* **Fine-grained:** Repository access chuno, permissions: **Contents → Read and write**, **Metadata → Read**. Naye repo banane ke liye **Administration → Read and write** bhi.
* Fine-grained token me repo select nahi hoga to GitHub **404** deta hai — yehi purane 404 ka sabse bada reason tha.

### AI key (Inception Labs)
* [platform.inceptionlabs.ai](https://platform.inceptionlabs.ai) par account banao → **API Keys** → key copy karo. Naye account par free tokens milte hain.
* Base URL: `https://api.inceptionlabs.ai/v1` (default set hai). Model: `mercury-3` (default), `mercury-2.5`, `mercury-2` — Settings me dropdown se badal sakte ho.
* AI sirf server se baat karta hai (browser se nahi), isliye CORS ka tension nahi.

---

## Use kaise kare

**Manual:** Home → 📦 Zip → zip chuno → list me tap → Options (top folder hatao, junk skip, ignore, repo ke andar folder) → Repo chuno ya naya banao → **Compare** (kitni nayi/badli/same) → **Upload** → Job screen par progress + log.

**AI se:** AI tab → 📎 se zip attach karo → AI pochega (kaunsa repo? naya ya purana? private/public? branch? folder?) → compare karke bataega → upload start karega.
Default me AI **upload se pehle tumhe "Approve & upload" button** dikhata hai (Settings me band kar sakte ho). AI ke paas delete ka tool hi nahi hai.

**Commands** (AI key ke bina bhi chalte hain): `/help`, `/staging`, `/files`, `/repos [text]`, `/compare <repo> [branch]`, `/upload <repo> [branch] [new] [public]`, `/jobs`, `/status`, `/retry [job]`, `/cancel [job]`, `/clear`.

**Repos tab:** repos list, naya repo, andar ki files dekhna, files/folders select karke ek commit me delete (2-step confirm).

---

## Environment variables (sab optional, `APP_PASSWORD` ke alawa)

| Name | Matlab |
|---|---|
| `APP_PASSWORD` | Login password. Set nahi kiya to random bana ke logs me print hota hai. |
| `GITHUB_TOKEN`, `INCEPTION_API_KEY` | Tokens (Settings me daale hue tokens in par **priority** lete hain). |
| `AI_BASE_URL`, `AI_MODEL` | AI defaults. |
| `DATA_DIR` | Data folder. Render paid **Disk** attach karo to uska mount path yahan do — tab sab kuch restart par bhi bachega. |
| `MAX_UPLOAD_MB` (300), `MAX_EXTRACT_MB` (1000), `MAX_FILE_MB` (50) | Limits. GitHub 100 MB se badi file allow hi nahi karta. |
| `STAGING_TTL_HOURS` (72) | Extract ki hui zips itne ghante baad apne aap hat jaati hain. |
| `KEEPALIVE_URL` | Render par zarurat nahi (`RENDER_EXTERNAL_URL` apne aap milta hai). |
| `SECRET_KEY` | Login cookie ka secret (optional). |

Extra safety ke liye [uptimerobot.com](https://uptimerobot.com) par free monitor bana ke `https://xxxx.onrender.com/healthz` har 5 min ping karwa sakte ho.

---

## Local chalana
```bash
python3 server.py            # http://localhost:10000 , password logs me print hoga
APP_PASSWORD=mypass PORT=8080 python3 server.py
```

## Tests (GitHub / AI ke nakli servers ke saath, internet nahi chahiye)
```bash
python3 tests/test_engine.py   # upload engine: resume, retry, 404, rate limit, empty repo, rebase ...
python3 tests/test_api.py      # poora server API + AI tools + approval
python3 tests/ui_test.py       # phone-size browser me UI (playwright chahiye)
```

## Limits (sach-sach)
* GitHub ka khud ka limit: ~500 content-creating requests/ghanta. Zyada binary files (images) wale bade zip me bot khud rukta hai aur wait ke baad chalta hai — job "rate limit wait" dikhata hai. Text files tree ke andar hi jaati hain, unke liye alag request nahi lagti.
* Render free par RAM 512 MB hai — 50 MB se badi single files default me skip hoti hain.
* Submodules / symlinks zip se upload nahi hote (skip, extract notes me dikhta hai).

## Structure
```
server.py            entry point
gitzip/              ghclient.py (GitHub, retries) · staging.py (zip, rules) · jobs.py (upload engine)
                     ai.py (Inception + tools + /commands) · web.py (HTTP, login) · background.py (keep-alive)
static/              UI (PWA)
tests/               mock GitHub / AI + tests
```
