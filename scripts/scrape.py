#!/usr/bin/env python3
"""
Rink Schedule Scraper
Fetches ice rink pages, uses Gemini to extract public skating sessions,
and writes a subscribable .ics calendar file.
"""

import os
import json
import time
import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path

import google.generativeai as genai
from playwright.sync_api import sync_playwright
from icalendar import Calendar, Event
from dateutil import parser as dateparser
from dateutil.rrule import rrulestr

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

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

# ── Gemini prompt ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are an expert at extracting ice rink public skating schedules from HTML.

Extract ALL open skate, stick & puck, and open hockey sessions from the HTML provided.

Return ONLY a valid JSON array — no markdown fences, no explanation. Each item:
{
  "title": "event name as shown on the page",
  "type": "open_skate" | "stick_and_puck" | "open_hockey" | "other",
  "dates": ["YYYY-MM-DD", ...],          // specific dates if listed
  "recurring_days": ["Monday", ...],     // if it repeats on certain weekdays
  "start_time": "HH:MM",                // 24-hour format
  "end_time": "HH:MM",                  // 24-hour format
  "cost": "e.g. $5 adults, $4 youth",
  "notes": "age limits, equipment required, registration needed, etc.",
  "season_start": "YYYY-MM-DD or null",
  "season_end": "YYYY-MM-DD or null"
}

Rules:
- Include ONLY public sessions (open skate, stick & puck, open hockey). Skip team practices,
  lessons, tournaments, and private rentals.
- Times must be 24-hour format (e.g. 13:30, not 1:30 PM).
- If only recurring days are known (no specific dates), leave "dates" as [] and fill
  "recurring_days".
- If no relevant events are found, return [].
- Never include markdown, code fences, or any text outside the JSON array.
"""


def fetch_html(url: str, wait_selector: str, timeout: int = 15000) -> str:
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
            page.wait_for_selector(wait_selector, timeout=5000)
        except Exception:
            pass  # best-effort wait
        html = page.inner_html("body")
        browser.close()
    return html


def parse_events(model: genai.GenerativeModel, html: str, rink_name: str) -> list[dict]:
    """Ask Gemini to extract events from raw HTML."""
    truncated = html[:60_000]  # stay well within context window
    prompt = f"{SYSTEM_PROMPT}\n\nRink: {rink_name}\n\nHTML:\n{truncated}"
    response = model.generate_content(prompt)
    raw = response.text.strip()
    # Strip accidental markdown fences
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning("JSON parse error for %s: %s", rink_name, e)
        return []


def make_uid(rink: str, title: str, date: str, start: str) -> str:
    key = f"{rink}|{title}|{date}|{start}"
    return hashlib.md5(key.encode()).hexdigest() + "@rink-calendar"


def events_to_ical(all_events: list[dict]) -> bytes:
    """Convert extracted event dicts into a .ics calendar."""
    cal = Calendar()
    cal.add("prodid", "-//Twin Cities Rink Calendar//EN")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", "Twin Cities Ice Rink Sessions")
    cal.add("x-wr-caldesc",
            "Open skate, stick & puck, and open hockey sessions from 7 Twin Cities rinks")
    cal.add("x-wr-timezone", "America/Chicago")
    cal.add("refresh-interval;value=duration", "PT12H")

    type_labels = {
        "open_skate": "Open Skate",
        "stick_and_puck": "Stick & Puck",
        "open_hockey": "Open Hockey",
        "other": "Public Ice",
    }

    now = datetime.now(tz=timezone.utc)

    for ev in all_events:
        rink = ev.get("rink", "Unknown Rink")
        title = ev.get("title", "Ice Session")
        etype = ev.get("type", "other")
        label = type_labels.get(etype, "Public Ice")
        summary = f"{label} – {rink}"

        start_str = ev.get("start_time", "00:00")
        end_str = ev.get("end_time", "00:00")

        cost = ev.get("cost", "")
        notes = ev.get("notes", "")
        desc_parts = [title]
        if cost:
            desc_parts.append(f"Cost: {cost}")
        if notes:
            desc_parts.append(notes)
        description = "\n".join(desc_parts)

        # ── Specific dated events ────────────────────────────────────────────
        for date_str in ev.get("dates", []):
            try:
                dt = dateparser.parse(f"{date_str} {start_str}")
                dt_end = dateparser.parse(f"{date_str} {end_str}")
            except Exception:
                continue

            ical_ev = Event()
            ical_ev.add("uid", make_uid(rink, title, date_str, start_str))
            ical_ev.add("summary", summary)
            ical_ev.add("description", description)
            ical_ev.add("dtstart", dt)
            ical_ev.add("dtend", dt_end)
            ical_ev.add("dtstamp", now)
            ical_ev.add("location", rink)
            cal.add_component(ical_ev)

        # ── Recurring weekly events ──────────────────────────────────────────
        days_map = {
            "monday": "MO", "tuesday": "TU", "wednesday": "WE",
            "thursday": "TH", "friday": "FR", "saturday": "SA", "sunday": "SU",
        }
        rec_days = [
            days_map[d.lower()]
            for d in ev.get("recurring_days", [])
            if d.lower() in days_map
        ]
        if rec_days:
            season_start = ev.get("season_start")
            season_end = ev.get("season_end")

            try:
                dtstart = dateparser.parse(
                    f"{season_start or datetime.now().strftime('%Y-%m-%d')} {start_str}"
                )
                dtend_time = dateparser.parse(
                    f"{season_start or datetime.now().strftime('%Y-%m-%d')} {end_str}"
                )
            except Exception:
                continue

            ical_ev = Event()
            ical_ev.add("uid", make_uid(rink, title, ",".join(rec_days), start_str))
            ical_ev.add("summary", summary)
            ical_ev.add("description", description)
            ical_ev.add("dtstart", dtstart)
            ical_ev.add("dtend", dtend_time)
            ical_ev.add("dtstamp", now)
            ical_ev.add("location", rink)

            rrule: dict = {"freq": "weekly", "byday": rec_days}
            if season_end:
                try:
                    rrule["until"] = dateparser.parse(season_end)
                except Exception:
                    pass
            ical_ev.add("rrule", rrule)
            cal.add_component(ical_ev)

    return cal.to_ical()


def main():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise SystemExit("GEMINI_API_KEY environment variable not set.")

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-1.5-flash")  # free tier, fast, generous limits

    output_dir = Path(os.environ.get("OUTPUT_DIR", "."))
    output_dir.mkdir(parents=True, exist_ok=True)

    all_events: list[dict] = []
    errors: list[str] = []

    for rink in RINKS:
        log.info("Fetching  %s ...", rink["name"])
        try:
            html = fetch_html(rink["url"], rink["wait_for"])
            log.info("  Parsing  %s  (%d chars) ...", rink["name"], len(html))
            events = parse_events(model, html, rink["name"])
            for ev in events:
                ev["rink"] = rink["name"]
                ev["city"] = rink["city"]
            all_events.extend(events)
            log.info("  ✓  %d events found", len(events))
        except Exception as exc:
            log.error("  ✗  %s failed: %s", rink["name"], exc)
            errors.append(f"{rink['name']}: {exc}")
        time.sleep(2)  # be polite between requests

    # Write .ics
    ics_path = output_dir / "rink-schedule.ics"
    ics_path.write_bytes(events_to_ical(all_events))
    log.info("Wrote %s  (%d total events)", ics_path, len(all_events))

    # Write JSON summary (useful for debugging / a future web dashboard)
    summary = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "total_events": len(all_events),
        "errors": errors,
        "rinks": [
            {
                "name": r["name"],
                "event_count": sum(1 for e in all_events if e.get("rink") == r["name"]),
            }
            for r in RINKS
        ],
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    log.info("Done. %d errors.", len(errors))

    if errors:
        log.warning("Errors encountered:\n%s", "\n".join(errors))


if __name__ == "__main__":
    main()
