# Notion → Google Drive backup

Automatically back up your whole Notion workspace to a Google Shared Drive as
plain Markdown (`.md`) files, on a schedule, using GitHub Actions. You don't
need a server, and your computer doesn't need to be on.

- **Incremental:** after the first full backup, each run only exports pages
  edited since the last run, so a typical run takes a few minutes.
- **Readable anywhere:** one `.md` file per page, in folders that mirror your
  Notion structure. Database fields go at the top of each file. Easy for
  people and AI tools (Gemini, Claude, NotebookLM) to read.
- **Safe:** it never deletes anything from Drive. Pages deleted in Notion stay
  in the backup.

## What you need

- A Notion workspace where you can create an integration (a "connection").
- **Google Workspace** (a paid Google account). The backup uses a *Shared
  Drive*, which personal @gmail.com accounts don't have.
- A Google Cloud project, which is free for this use.
- A GitHub account. The free plan's 2,000 Actions minutes a month is plenty.

## Setup in brief

1. **Notion:** create an internal integration with read access. Copy its token.
   Under *Content access*, share every top-level page you want backed up.
2. **Google Cloud:** create a project, enable the **Google Drive API**, create a
   **service account**, and download a **JSON key** for it.
3. **Google Drive:** create a **Shared Drive**. Add the service account's email
   as a **Content manager**. Copy the ID from the end of its URL.
4. **GitHub:** click **Use this template** → **Create a new repository**. Make it
   **Private**.
5. In your new repo, go to **Settings → Secrets and variables → Actions** and
   add three repository secrets:

   | Secret | Value |
   |---|---|
   | `NOTION_API_KEY` | the Notion integration token (`ntn_…`) |
   | `GDRIVE_FOLDER_ID` | the Shared Drive (or folder) ID |
   | `GDRIVE_SERVICE_ACCOUNT_JSON` | the entire contents of the JSON key file |

6. Go to **Actions → Notion backup → Run workflow**. The first run is the full
   backup. It takes roughly 1 hour per 6,000 pages. Large workspaces may need
   a second run to finish, and that happens automatically on the next
   schedule.

It then runs once a day. To change how often, edit the `cron:` line in
`.github/workflows/backup.yml`.

## Running it on your own computer (optional)

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env          # fill in NOTION_API_KEY and GDRIVE_FOLDER_ID
mkdir secrets                 # put the JSON key at secrets/service-account.json

.venv/bin/python main.py --dry-run --limit 25   # test: 25 pages to ./dist, no upload
.venv/bin/python main.py                        # export changes + upload to Drive
```

| Option | What it does |
|---|---|
| `--dry-run` | export to `./dist` only, never touch Drive |
| `--limit N` | export at most N pages (for testing) |
| `--time-budget M` | stop starting new pages after M minutes |
| `--upload-only` | finish an interrupted upload |
| `--full` | re-export every page |

`.env`, `secrets/` and `dist/` are git-ignored. Never commit keys.

## Output layout

```
_backup_index.md                  start here: every top-level page and database
_manifest.json                    bookkeeping for incremental runs (don't edit)
Projects (3e92ee0d).md            a page
Projects (3e92ee0d)/              its subpages and databases
    Tasks (c59dfa98).md           database index: a table of every entry
    Tasks (c59dfa98)/             one file per database entry
```

**Not included:** files and images uploaded into Notion (listed by name only),
and comments.

## Troubleshooting

| Symptom | Fix |
|---|---|
| A page is missing | Share its top-level parent with the integration (Notion → integration → *Content access*). |
| `Service account key creation is disabled` | Your Google org blocks keys. An org admin can turn off the `iam.disableServiceAccountKeyCreation` and `iam.managed.disableServiceAccountKeyCreation` policies **for this project only**. |
| `storageQuotaExceeded` | You used a normal My Drive folder. Service accounts can only write to a **Shared Drive**. |
| Drive `404 File not found` | The folder ID has extra text from the URL (`?usp=…`), or the service account isn't a member of the Shared Drive. |
| Website says "Failed to add secret" | Add the secrets with GitHub's CLI instead: `gh secret set NAME -R owner/repo`. |
| `refusing to allow an OAuth App to create or update workflow` | Your git login lacks the `workflow` permission. Run `gh auth refresh -s workflow`. |

## Code

| Module | Job |
|---|---|
| `main.py` | Command line: ties export and upload together |
| `notion_backup/config.py` | Reads `.env` / environment variables |
| `notion_backup/notion_api.py` | Notion client with rate limiting and retries |
| `notion_backup/markdown.py` | Notion blocks and properties → Markdown |
| `notion_backup/notion_exporter.py` | Discovery, folder tree, incremental export |
| `notion_backup/drive_uploader.py` | Mirrors the files into Google Drive |
