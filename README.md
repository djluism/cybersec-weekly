# SmallTeam Shield — Automated Weekly Micro-Enterprise Cybersecurity Newsletter

An automated weekly cybersecurity newsletter website tailored specifically for **micro-enterprises (1–5 employees)** with no dedicated IT staff.

Every **Monday at 6:00 AM**, a GitHub Actions workflow runs `scripts/generate_newsletter.py`, which:
1. **Scrapes & Parses RSS Feeds** from:
   - **CISA Cybersecurity Advisories** (`https://www.cisa.gov/cybersecurity-advisories/all.xml`)
   - **Krebs on Security** (`https://krebsonsecurity.com/feed/`)
   - **BleepingComputer** (`https://www.bleepingcomputer.com/feed/`)
2. **Filters Out Enterprise Corporate Noise** using **Gemini 4 Argon** (`gemini-4-argon` via the `google-genai` SDK), discarding alerts that only apply to large enterprises or data centers (SCADA/ICS PLCs, BGP routing, Kubernetes clusters, VMware vCenter, Citrix NetScaler, F5 BIG-IP, SAP/Oracle ERP, etc.).
3. **Generates Plain-English 2–3 Sentence Summaries** optimized for non-technical 1–5 person teams, paired with a single concrete 5-minute action step.
4. **Builds the Static Dark-Mode Website (`index.html`)** with Tailwind CSS, featuring the **current week in a top Hero Block** and stacking **older weeks chronologically beneath it**.

---

## Project Structure

- `index.html` — Built static website (HTML + Tailwind CSS dark mode aesthetic) ready for GitHub Pages or any static host.
- `templates/index.html.j2` — Jinja2 HTML template with top Hero Block, Noise Filter Telemetry panel, interactive team action checkboxes, source/severity/keyword filtering, and chronological archive stack.
- `data/weeks.json` — Structured weekly newsletter archive powering the static build.
- `scripts/generate_newsletter.py` — RSS scraper, Gemini 4 Argon curation agent, 2–3 sentence summary enforcer, and static HTML generator.
- `.github/workflows/weekly-newsletter.yml` — Automated GitHub Actions workflow scheduled for every Monday at 6:00 AM (`cron: '0 6 * * 1'`).
- `requirements.txt` — Python dependencies (`google-genai`, `feedparser`, `requests`, `jinja2`, `python-dateutil`).

---

## Setup & Usage

### 1. Local Execution

Install dependencies:
```bash
pip install -r requirements.txt
```

Run live RSS ingestion + Gemini 4 Argon curation:
```bash
export GEMINI_API_KEY="your-gemini-api-key"
python scripts/generate_newsletter.py
```

Re-render `index.html` from existing `data/weeks.json` without calling the API:
```bash
python scripts/generate_newsletter.py --render-only
```

### 2. GitHub Actions Automation (Every Monday at 6:00 AM)

1. In your GitHub repository, navigate to **Settings → Secrets and variables → Actions** and add:
   - `GEMINI_API_KEY`: Your Google Gemini API key.
2. Enable **GitHub Pages** under **Settings → Pages** and set the source to **GitHub Actions**.
3. The workflow in `.github/workflows/weekly-newsletter.yml` will run automatically every Monday at `06:00` (`0 6 * * 1`) or on-demand via the **Actions → Run workflow** button.
