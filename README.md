# LTS Release Dashboard

A live internal web dashboard for tracking Jira tickets across the LTS (LEAP Strato) release pipeline — automatically organised by Bitbucket repository, with no manual tagging required.

## What it does

- **Ready for Live Release** — shows all tickets in the "Ready for Live Release" Jira status, split into columns by repo (Strato Worker, WebAPI, Internal Web) based on actual Bitbucket commit data
- **Release Blockers** — tickets that have commits in one of the tracked repos but are NOT yet ready for release (Open, In Development, Under Review, etc.)
- **Per-repo alarm** — highlights any repo column with 5 or more tickets waiting
- **Ticket age** — shows how long each ticket has been open since its created date
- **Auto-refresh** — page refreshes automatically every 15 minutes; manual refresh button also available

## How repo detection works

The dashboard calls Jira's dev-status API for each ticket to read the actual Bitbucket branch/commit data attached to it. It does not rely on Jira components or labels — a ticket appears in a repo column only if a developer has made a commit referencing that ticket from that repo.

Tracked repos:
| Bitbucket slug | Dashboard label |
|---|---|
| `strato-worker` | ⚙️ Strato Worker |
| `strato-webapi` | 🌐 WebAPI |
| `strato-internal-web` | 🖥️ Internal Web |

## Running locally

**Requirements:** Python 3.9+

```bash
# Clone the repo
git clone https://bitbucket.org/leapsoftwaredevelopments/lts-release-dashboard.git
cd lts-release-dashboard

# Install dependencies
pip install -r requirements.txt

# Create a .env file (never commit this)
cp .env.example .env
# Fill in your Jira credentials in .env

# Run the Flask dev server
python app.py
# Visit http://localhost:5000
```

## Environment variables

| Variable | Description |
|---|---|
| `JIRA_EMAIL` | Your Jira account email (e.g. `name@leapdev.io`) |
| `JIRA_API_TOKEN` | Jira API token — generate at [id.atlassian.com/manage-profile/security/api-tokens](https://id.atlassian.com/manage-profile/security/api-tokens) |
| `CACHE_TTL_SECONDS` | Optional. How often the dashboard refreshes data. Default: `900` (15 min) |
| `RELEASE_ALARM` | Optional. Ticket count that triggers the per-repo alarm. Default: `5` |

## Deployment

The app is a standard Python WSGI app. It runs with Gunicorn:

```
gunicorn --bind=0.0.0.0:8000 --timeout=120 --workers=2 app:app
```

The `Procfile` is included for platform deployments (Azure App Service, Render, Heroku, etc.).

Set `JIRA_EMAIL` and `JIRA_API_TOKEN` as environment variables / app settings on the hosting platform — never hardcode them.

## File structure

```
app.py              # Flask web app — serves the live dashboard
generate_dashboard.py  # Local runner — generates a static HTML file instead
requirements.txt    # Python dependencies
Procfile            # Gunicorn start command for platform deployment
```

## Tech stack

- **Python / Flask** — web server
- **Gunicorn** — production WSGI server
- **Jira REST API v3** — ticket data (`/rest/api/3/search/jql`)
- **Jira Dev Status API** — Bitbucket commit/branch data per ticket
- **ThreadPoolExecutor** — parallel API calls for performance
- **In-memory cache** — avoids hammering Jira on every page load
