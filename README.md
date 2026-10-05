# Junior Wingman (Job Hunter)

Self-hosted, open-source (MIT) job search assistant. It keeps searching for open
positions that fit your CV, writes a tailored application letter for each one,
and sends it **only after you approve it**. It also watches your Gmail inbox to
keep an application tracker up to date (status, contact phone number, the
salary expectation you gave).

> 🇭🇺 Magyar összefoglaló lent: [Gyors indítás magyarul](#gyors-indítás-magyarul)

## How it works

```
CV + LinkedIn/Glassdoor PDF export ──► AI: search keywords
                                         │
  every 6 h: Remotive · Arbeitnow · RemoteOK · JSearch (LinkedIn/Indeed/Glassdoor via Google Jobs)
                                         │
                keyword prefilter ──► AI match score (0-100) ──► "Suggested jobs"
                                         │ score ≥ your minimum
                                         ▼
           AI cover letter (HU for jobs in Hungary, EN for remote) ──► "Approval queue"
                                         │ you click Approve
               ┌─────────────────────────┴───────────────────────┐
     posting has an apply email                       no apply email
     → sent from your Gmail with CV attached          → open posting, paste letter, mark applied
               └─────────────────────────┬───────────────────────┘
                                         ▼
     every 30 min: Gmail sync → AI classifies replies → tracker status
     (received / interview / offer / rejected) + contact phone
```

### Why no direct LinkedIn / Glassdoor automation?

Their terms of service forbid bots that log in, scrape, or auto-apply, and they
ban accounts that do it. Job Hunter therefore **never logs in to job portals**.
Your profiles are read from the PDF export you upload, and their listings are
reached legally through the JSearch aggregator (Google for Jobs).

## Quick start (Docker, local)

1. Install Docker Desktop and enable *Settings → General → Start Docker Desktop when you sign in*.
2. `cp .env.example .env` and set at least `SECRET_KEY` and `ANTHROPIC_API_KEY`
   (or `LLM_PROVIDER=ollama`).
3. `docker compose up -d --build`
4. Open http://localhost:8000 → **Profile** → upload your CV.

Because of `restart: unless-stopped` the app comes back automatically after a
reboot. You never need to start or stop it by hand.

## Gmail setup (one time, free)

1. https://console.cloud.google.com → new project → *APIs & Services → Library* → enable **Gmail API**.
2. *OAuth consent screen*: External, add yourself as a **test user**.
3. *Credentials → Create credentials → OAuth client ID → Web application*.
   Authorized redirect URI: `http://localhost:8000/gmail/callback` (or `https://<your-domain>/gmail/callback`).
4. Put the client ID and secret into `.env`, then `docker compose up -d`, and go to **Settings → Connect**.

Scopes: `gmail.readonly` (status tracking) and `gmail.send` (sending approved applications).
The token stays in your local SQLite database (`./data`).

## LinkedIn profile sync (official API, no export needed)

EEA/Swiss members can connect LinkedIn through the official
[Member Data Portability (Member) API](https://learn.microsoft.com/en-us/linkedin/dma/member-data-portability/member-data-portability-member/)
(EU Digital Markets Act). The app pulls your profile, positions, education, skills,
certifications, languages, projects and job-seeker preferences **weekly**, and
imports the applications you made on LinkedIn (e.g. Easy Apply) into the tracker.

1. Create an app at https://www.linkedin.com/developers/apps/new. As the company page you **must** select
   *Member Data Portability (Member) Default Company*.
2. On the app's *Products* tab, request **Member Data Portability API (Member)**.
3. In the [OAuth Token Generator](https://www.linkedin.com/developers/tools/oauth), select the app, choose the scope
   `r_dma_portability_self_serve`, and allow access.
4. Paste the token on **Settings → LinkedIn**. When it expires the dashboard tells you; generate a new one the same way.

Glassdoor, Indeed and Profession.hu offer no candidate-profile API. For them, upload a PDF/text export once (it is stored).

## Job sources

| Source | Key | Coverage |
|---|---|---|
| Remotive | none | remote jobs |
| Arbeitnow | none | EU (mostly DE) and remote |
| RemoteOK | none | remote jobs (attribution link in footer) |
| JSearch (RapidAPI) | `JSEARCH_API_KEY`, free tier ~200 req/month | LinkedIn, Indeed, Glassdoor, Profession.hu listings, Hungary + remote |

To add a source, subclass `JobSource` in `app/sources/` and append it to `ALL_SOURCES`.

## AI providers

- `LLM_PROVIDER=claude` (default) uses `CLAUDE_MODEL` for letters and `CLAUDE_SCORING_MODEL`
  for bulk scoring/classification. Set the scoring model to `claude-haiku-4-5` to cut cost.
  Server-side refusal fallbacks are on (`CLAUDE_FALLBACKS=true`).
- `LLM_PROVIDER=ollama` runs a local model (`OLLAMA_URL`, `OLLAMA_MODEL`). Free, but lower quality.

Cost control: only jobs that pass a keyword prefilter are scored, at most
`MAX_LLM_SCORES_PER_RUN` per run, and at most `MAX_DRAFTS_PER_RUN` letters are
pre-written per run.

## Cloud deployment (free tier)

Recommended: **Oracle Cloud Always Free** ARM VM (free indefinitely, enough for this app).

```bash
# on the VM (Ubuntu), after installing Docker
git clone https://github.com/Fonyi03/Junior-Wingman.git && cd Junior-Wingman
cp .env.example .env   # BASE_URL=https://jobs.example.com, APP_PASSWORD=..., SECRET_KEY=...
DOMAIN=jobs.example.com docker compose -f deploy/docker-compose.cloud.yml up -d --build
```

Caddy provides HTTPS automatically. Open ports 80/443 in the VM's security list.
Point a DNS record (or a free DuckDNS name) at the VM. **Always set `APP_PASSWORD`
in the cloud**, because the app holds your CV and Gmail access.

## Development

```bash
pip install -r requirements-dev.txt
DATA_DIR=./data uvicorn app.main:app --reload
pytest -q
```

Stack: FastAPI, Jinja2 templates with a small hand-written design system (`app/static/app.css`, light/dark),
SQLModel/SQLite, APScheduler, Anthropic SDK, Gmail API.

Demo data for UI work (never touches a non-empty database):

```bash
DATA_DIR=/tmp/demo python -m scripts.demo_data
```

The repository ships Anthropic's open-source design skills in `.claude/skills/` (frontend-design,
design-critique, design-system, accessibility-review, ux-copy; Apache 2.0). Claude Code picks them up
automatically when working on this project.

---

## Gyors indítás magyarul

1. Telepítsd a Docker Desktopot, és kapcsold be: *Settings → General → Start Docker Desktop when you sign in*.
2. Másold le a `.env.example` fájlt `.env` néven. Állítsd be benne a `SECRET_KEY` és az `ANTHROPIC_API_KEY` értékét (vagy `LLM_PROVIDER=ollama`).
3. Futtasd: `docker compose up -d --build`
4. Nyisd meg a http://localhost:8000 címet, és a **Profil** oldalon töltsd fel az önéletrajzodat. A LinkedIn-profilodat is feltöltheted: Profil → Továbbiak → Mentés PDF-ként.
5. A **Beállítások** oldalon csatlakoztasd a Gmailt (lásd fent a *Gmail setup* részt).
6. Ugyanott a LinkedInt is bekötheted a hivatalos API-n keresztül, export nélkül (lásd a *LinkedIn profile sync* részt). Ez EU-s felhasználóknak érhető el. A profilod ezután hetente frissül, és a LinkedInen beadott jelentkezéseid is bekerülnek a trackerbe.

Az app 6 óránként keres, a talált állásokat pontozza, és megírja a motivációs leveleket: magyar állásnál magyarul, remote-nál angolul. A levelek a **Jóváhagyásra vár** oldalra kerülnek. Semmit nem küld el a jóváhagyásod nélkül. A **Jelentkezéseim** oldalon látod a jelentkezések státuszát, a kapcsolattartó telefonszámát és a megadott bérigényt. Ezeket a Gmail-szinkron 30 percenként frissíti.
