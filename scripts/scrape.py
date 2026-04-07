#!/usr/bin/env python3
"""
Rink Schedule Scraper
Fetches ice rink pages, uses Gemini to extract public skating sessions,
expands recurring events into concrete dated instances for the next 3 weeks,
and writes a subscribable .ics calendar file.
"""

import os
import json
import time
import hashlib
import logging
from datetime import datetime, timedelta, timezone, date
from pathlib import Path

from google import genai
from playwright.sync_api import sync_playwright
from icalendar import Calendar, Event
from dateutil import parser as dateparser

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# How many days ahead to expand recurring events into the calendar
LOOKAHEAD_DAYS = 21

# ── Rink definitions ──────────────────────────────────────────────────────────
RINKS = [
    {
        "name": "Champlin Ice Forum",
        "city": "Champlin",
        "url": "https://ci.champlin.mn.us/calendar.aspx?CID=25",
        "wait_for": "table, .fc-event, .calendar",
    },
    {
        "name": "Brooklyn Park Ice Arena",
        "city": "Brooklyn Park",
        "url": "https://www.brooklynpark.org/our-facilities/ice-arenas/ice-rink-schedules/",
        "wait_for": "table, iframe, .schedule",
    },
    {
        "name": "Maple Grove Community Center",
        "city": "Maple Grove",
        "url": "https://www.maplegrovemn.gov/274/Ice-arena",
        "wait_for": "table, .calendar, .schedule",
    },
    {
        "name": "Rogers Ice Arena",
        "city": "Rogers",
        "url": "https://www.rogersmn.gov/ice-arena",
        "wait_for": ".fc-event, table, .calendar",
    },
    {
        "name": "Elk River Arena",
        "city": "Elk River",
        "url": "https://www.elkrivermn.gov/1827/Skating",
        "wait_for": "table, .calendar, .fc-event",
    },
    {
        "name": "Anoka Ice Arena",
        "city": "Anoka",
        "url": "https://www.ci.anoka.mn.us/262/Ice-Arena",
        "wait_for": "table, .calendar",
    },
    {
        "name": "Coon Rapids Ice Center",
        "city": "Coon Rapids",
        "url": "https://www.coonrapidsmn.gov/532/Ice-Center",
        "wait_for": "table, .calendar, .schedule",
    },
]

# Day name → Python weekday number (Monday=0)
WEEKDAY_MAP = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def build_prompt(today_str: str) -> str:
    return f"""You are an expert at extracting ice rink public skating schedules from HTML.

Today's date is {today_str}. Extract ALL open skate, stick & puck, and open hockey sessions
from the HTML. Focus on sessions from today onward.

Return ONLY a valid JSON array — no markdown fences, no explanation. Each item:
{{
  "title": "event name as shown on the page",
  "type": "open_skate" | "stick_and_puck" | "open_hockey" | "other",
  "dates": ["YYYY-MM-DD", ...],
  "recurring_days": ["Monday", "Wednesday", ...],
  "start_time": "HH:MM",
  "end_time": "HH:MM",
  "cost": "e.g. $5 adults, $4 youth or empty string",
  "notes": "age limits, equipment required, registration needed, etc. or empty string",
  "season_start": "YYYY-MM-DD or null",
  "season_end": "YYYY-MM-DD or null"
}}

Rules:
- Include ONLY public sessions: open skate, stick & puck, open hockey.
  Skip team practices, lessons, tournaments, private rentals.
- Times must be 24-hour format (e.g. 13:30 not 1:30 PM).
- If specific dates are listed on the page, put them in "dates" as YYYY-MM-DD.
- If the schedule says things like "every Saturday" or "Tuesdays and Thursdays",
  put those day names in "recurring_days" and leave "dates" as [].
- A single event entry can have BOTH dates and recurring_days if the page shows both.
- If season start/end dates are visible, include them; otherwise use null.
- Only include events from {today_str} onward — skip past dates.
- If no relevant events are found, return [].
- Never include markdown, code fences, or any text outside the JSON array.
"""


def fetch_html(url: str, wait_selector: str, timeout: int = 20000) -> str:
    """Render the page with Playwright and return the body HTML."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        )
        page.goto(url, wait_until="networkidle", timeout=timeout)
        try:
            page.wait_for_selector(wait_selector, timeout=6000)
        except Exception:
            pass  # best-effort wait
        html = page.inner_html("body")
        browser.close()
    return html


def parse_events(client: genai.Client, html: str, rink_name: str, today_str: str) -> list[dict]:
    """Ask Gemini to extract events from raw HTML."""
    truncated = html[:60_000]
    prompt = build_prompt(today_str) + f"\n\nRink: {rink_name}\n\nHTML:\n{truncated}"
    response = client.models.generate_content(
        model="gemini-2.5-pro-exp-03-25",
        contents=prompt,
    )
    raw = response.text.strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning("JSON parse error for %s: %s", rink_name, e)
        log.debug("Raw response: %s", raw[:500])
        return []


def expand_recurring(ev: dict, today: date, lookahead: int) -> list[date]:
    """
    Given an event with recurring_days, return all matching dates
    from today through today+lookahead days, respecting season bounds.
    """
    target_weekdays = {
        WEEKDAY_MAP[d.lower()]
        for d in ev.get("recurring_days", [])
        if d.lower() in WEEKDAY_MAP
    }
    if not target_weekdays:
        return []

    window_start = today
    window_end = today + timedelta(days=lookahead)

    season_start = ev.get("season_start")
    season_end = ev.get("season_end")
    if season_start:
        try:
            window_start = max(window_start, dateparser.parse(season_start).date())
        except Exception:
            pass
    if season_end:
        try:
            window_end = min(window_end, dateparser.parse(season_end).date())
        except Exception:
            pass

    dates = []
    current = window_start
    while current <= window_end:
        if current.weekday() in target_weekdays:
            dates.append(current)
        current += timedelta(days=1)
    return dates


def make_uid(rink: str, title: str, date_str: str, start: str) -> str:
    key = f"{rink}|{title}|{date_str}|{start}"
    return hashlib.md5(key.encode()).hexdigest() + "@rink-calendar"


def parse_time(time_str: str, date_str: str) -> datetime | None:
    try:
        return dateparser.parse(f"{date_str} {time_str}")
    except Exception:
        return None


def events_to_ical(all_events: list[dict], today: date, lookahead: int) -> bytes:
    """Convert extracted event dicts into concrete dated .ics events."""
    cal = Calendar()
    cal.add("prodid", "-//Twin Cities Rink Calendar//EN")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", "Twin Cities Ice Rink Sessions")
    cal.add("x-wr-caldesc",
            "Open skate, stick & puck, and open hockey — updated every 4 hours")
    cal.add("x-wr-timezone", "America/Chicago")
    cal.add("refresh-interval;value=duration", "PT4H")

    type_labels = {
        "open_skate":     "Skating - Open Skate",
        "stick_and_puck": "Skating - Stick & Puck",
        "open_hockey":    "Skating - Open Hockey",
        "other":          "Skating - Public Ice",
    }

    now = datetime.now(tz=timezone.utc)
    added = 0

    for ev in all_events:
        rink      = ev.get("rink", "Unknown Rink")
        title     = ev.get("title", "Ice Session")
        etype     = ev.get("type", "other")
        label     = type_labels.get(etype, "Skating - Public Ice")
        summary   = f"{label} @ {rink}"
        start_str = ev.get("start_time", "00:00")
        end_str   = ev.get("end_time",   "00:00")

        cost  = ev.get("cost", "")
        notes = ev.get("notes", "")
        desc_parts = [title]
        if cost:
            desc_parts.append(f"Cost: {cost}")
        if notes:
            desc_parts.append(notes)
        description = "\n".join(desc_parts)

        # Collect all concrete dates for this event
        concrete_dates: list[date] = []

        # 1. Specific dates listed on the page
        for date_str in ev.get("dates", []):
            try:
                d = dateparser.parse(date_str).date()
                if d >= today:
                    concrete_dates.append(d)
            except Exception:
                continue

        # 2. Expand recurring days into the lookahead window
        concrete_dates += expand_recurring(ev, today, lookahead)

        # Deduplicate and sort
        concrete_dates = sorted(set(concrete_dates))

        for d in concrete_dates:
            date_str = d.strftime("%Y-%m-%d")
            dt_start = parse_time(start_str, date_str)
            dt_end   = parse_time(end_str,   date_str)
            if not dt_start or not dt_end:
                continue

            ical_ev = Event()
            ical_ev.add("uid",         make_uid(rink, title, date_str, start_str))
            ical_ev.add("summary",     summary)
            ical_ev.add("description", description)
            ical_ev.add("dtstart",     dt_start)
            ical_ev.add("dtend",       dt_end)
            ical_ev.add("dtstamp",     now)
            ical_ev.add("location",    rink)
            cal.add_component(ical_ev)
            added += 1

    log.info("Calendar built with %d concrete events", added)
    return cal.to_ical()


def main():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise SystemExit("GEMINI_API_KEY environment variable not set.")

    client = genai.Client(api_key=api_key)
    output_dir = Path(os.environ.get("OUTPUT_DIR", "."))
    output_dir.mkdir(parents=True, exist_ok=True)

    today     = date.today()
    today_str = today.strftime("%Y-%m-%d")
    log.info("Starting scrape — today is %s, lookahead %d days", today_str, LOOKAHEAD_DAYS)

    all_events: list[dict] = []
    errors: list[str] = []

    for rink in RINKS:
        log.info("Fetching  %s ...", rink["name"])
        try:
            html = fetch_html(rink["url"], rink["wait_for"])
            log.info("  Parsing  %s  (%d chars) ...", rink["name"], len(html))
            events = parse_events(client, html, rink["name"], today_str)
            for ev in events:
                ev["rink"] = rink["name"]
                ev["city"] = rink["city"]
            all_events.extend(events)
            log.info("  ✓  %d raw event entries found", len(events))
        except Exception as exc:
            log.error("  ✗  %s failed: %s", rink["name"], exc)
            errors.append(f"{rink['name']}: {exc}")
        time.sleep(3)

    # Write .ics
    ics_bytes = events_to_ical(all_events, today, LOOKAHEAD_DAYS)
    ics_path  = output_dir / "rink-schedule.ics"
    ics_path.write_bytes(ics_bytes)
    log.info("Wrote %s", ics_path)

    # Write JSON summary with concrete event counts
    rink_counts = {}
    for ev in all_events:
        rink_name = ev.get("rink", "Unknown")
        specific  = [d for d in ev.get("dates", []) if d >= today_str]
        recurring = expand_recurring(ev, today, LOOKAHEAD_DAYS)
        rink_counts[rink_name] = rink_counts.get(rink_name, 0) + len(specific) + len(recurring)

    summary = {
        "generated_at":          datetime.now(tz=timezone.utc).isoformat(),
        "today":                 today_str,
        "lookahead_days":        LOOKAHEAD_DAYS,
        "total_calendar_events": sum(rink_counts.values()),
        "errors":                errors,
        "rinks": [
            {
                "name":            r["name"],
                "calendar_events": rink_counts.get(r["name"], 0),
            }
            for r in RINKS
        ],
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    log.info("Done. total_calendar_events=%d  errors=%d",
             summary["total_calendar_events"], len(errors))

    if errors:
        log.warning("Errors:\n%s", "\n".join(errors))


if __name__ == "__main__":
    main()
