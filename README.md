# 🏒 Twin Cities Rink Schedule Calendar

Automatically scrapes open skate and stick & puck sessions from 7 Twin Cities ice rinks every morning and publishes a subscribable `.ics` calendar file.

**Rinks covered:**
- Champlin Ice Forum
- Brooklyn Park Ice Arena
- Maple Grove Community Center
- Rogers Ice Arena
- Elk River Arena
- Anoka Ice Arena
- Coon Rapids Ice Center

---

## How it works

Every day at 7:00 AM Central, a GitHub Actions job:
1. Visits each rink's schedule page using a real browser (Playwright/Chromium)
2. Sends the page HTML to Claude, which extracts all public ice sessions
3. Writes a `.ics` calendar file and commits it back to this repo
4. Your calendar app re-fetches the file (every 12 hours by default)

---

## Setup — step by step

### 1. Create a GitHub account (if you don't have one)
Go to [github.com](https://github.com) and sign up. It's free.

### 2. Create a new repository
- Click the **+** in the top-right → **New repository**
- Name it something like `rink-calendar`
- Set it to **Public** (required for the free calendar subscription URL)
- Click **Create repository**

### 3. Upload these files
Click **uploading an existing file** on the new repo page, then upload:
```
requirements.txt
scripts/scrape.py
.github/workflows/scrape.yml
public/rink-schedule.ics
```
Make sure to recreate the folder structure:
- `scripts/scrape.py` goes in a folder called `scripts`
- `.github/workflows/scrape.yml` goes in `.github/workflows/`
- `public/rink-schedule.ics` goes in a folder called `public`

### 4. Get an Anthropic API key
- Go to [console.anthropic.com](https://console.anthropic.com)
- Sign up or log in → **API Keys** → **Create Key**
- Copy the key (starts with `sk-ant-...`)
- You'll need a small amount of credit — each daily run costs roughly **$0.05–0.15** using claude-haiku

### 5. Add the API key to GitHub Secrets
- In your repo, go to **Settings** → **Secrets and variables** → **Actions**
- Click **New repository secret**
- Name: `ANTHROPIC_API_KEY`
- Value: paste your key
- Click **Add secret**

### 6. Enable GitHub Pages (to serve the .ics file)
- In your repo, go to **Settings** → **Pages**
- Under **Source**, select **Deploy from a branch**
- Branch: `main`, Folder: `/public`
- Click **Save**
- After a minute, your calendar URL will be:
  ```
  https://YOUR-GITHUB-USERNAME.github.io/rink-calendar/rink-schedule.ics
  ```

### 7. Subscribe to the calendar

**Google Calendar:**
1. Open [calendar.google.com](https://calendar.google.com)
2. Click **+** next to "Other calendars" → **From URL**
3. Paste your `.ics` URL from step 6
4. Click **Add calendar**

**Apple Calendar (iPhone/Mac):**
1. Go to **Settings** → **Calendar** → **Accounts** → **Add Account** → **Other**
2. Tap **Add Subscribed Calendar**
3. Paste the URL and tap **Next** → **Save**

---

## Running it manually

To trigger a scrape right now without waiting for the schedule:
1. Go to your repo on GitHub
2. Click the **Actions** tab
3. Click **Scrape Rink Schedules** in the left sidebar
4. Click **Run workflow** → **Run workflow**

Watch the logs to see it work in real time.

---

## Troubleshooting

**The calendar is empty / no events showing**
- Check the Actions tab for errors. Click the latest run → `scrape` job to see logs.
- Some rink sites block automated requests. The scraper uses a real browser to work around this, but some sites may still fail.

**A specific rink isn't showing events**
- That rink's website may have changed its URL or schedule format.
- Open an issue or edit `scripts/scrape.py` — the `RINKS` list at the top is easy to update.

**I want to add another rink**
Add an entry to the `RINKS` list in `scripts/scrape.py`:
```python
{
    "name": "My Rink",
    "city": "City Name",
    "url": "https://...",
    "wait_for": "table, .calendar",
},
```

---

## Cost estimate

| Component | Cost |
|-----------|------|
| GitHub Actions | Free (2,000 min/month included) |
| Anthropic API (claude-haiku, 7 rinks/day) | ~$1.50–4/month |
| GitHub Pages | Free |

Total: **under $5/month**, usually closer to $2.

---

## Files

```
├── .github/
│   └── workflows/
│       └── scrape.yml          # GitHub Actions schedule
├── scripts/
│   └── scrape.py               # Main scraper + parser
├── public/
│   ├── rink-schedule.ics       # ← subscribe to this URL
│   └── summary.json            # Last run stats (auto-generated)
└── requirements.txt
```
