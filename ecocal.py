"""
calendar.py — Economic calendar event detection
Fetches today's events and determines trading restrictions
"""

import requests
from datetime import datetime, timedelta
import pytz
import config

ET = pytz.timezone("America/New_York")

# Cache events so we only fetch once per day
_cached_events = None
_cache_date    = None


def fetch_todays_events():
    """
    Fetch today's economic events from ForexFactory
    Falls back to empty list if unavailable
    Returns list of event dicts
    """
    global _cached_events, _cache_date

    today = datetime.now(ET).date()

    # Return cache if already fetched today
    if _cache_date == today and _cached_events is not None:
        return _cached_events

    events = []

    try:
        # ForexFactory JSON feed (free, no auth needed)
        url      = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        response = requests.get(url, timeout=10)

        if response.status_code == 200:
            data = response.json()
            for item in data:
                try:
                    event_time = datetime.strptime(
                        item["date"], "%Y-%m-%dT%H:%M:%S%z"
                    ).astimezone(ET)

                    if event_time.date() != today:
                        continue
                    if item.get("impact", "") not in ["High", "Medium"]:
                        continue

                    events.append({
                        "title":  item.get("title", ""),
                        "time":   event_time,
                        "impact": item.get("impact", ""),
                        "tier":   _classify_event(item.get("title", ""))
                    })
                except Exception:
                    continue

            print(f"📅 Loaded {len(events)} economic events for today")

    except Exception as e:
        print(f"⚠️ Could not fetch calendar: {e} — trading without calendar")

    _cached_events = events
    _cache_date    = today
    return events


def _classify_event(title):
    """Classify event into tier 1, 2, or 3"""
    title_upper = title.upper()

    for keyword in config.TIER1_EVENTS:
        if keyword.upper() in title_upper:
            return 1

    for keyword in config.TIER2_EVENTS:
        if keyword.upper() in title_upper:
            return 2

    return 3


def get_current_restriction():
    """
    Check if current time falls within a news window
    Returns dict with restriction details
    """
    now    = datetime.now(ET)
    events = fetch_todays_events()

    for event in events:
        event_time = event["time"]
        tier       = event["tier"]

        if tier == 1:
            pre_min  = config.TIER1_PRE_MINUTES
            post_min = config.TIER1_POST_MINUTES
        elif tier == 2:
            pre_min  = config.TIER2_PRE_MINUTES
            post_min = config.TIER2_POST_MINUTES
        else:
            continue

        window_start = event_time - timedelta(minutes=pre_min)
        window_end   = event_time + timedelta(minutes=post_min)

        if window_start <= now <= window_end:
            return {
                "restricted":   True,
                "tier":         tier,
                "event":        event["title"],
                "action":       "FULL_STOP" if tier == 1 else "REDUCE_SIZE",
                "risk_factor":  0.0 if tier == 1 else config.TIER2_RISK_FACTOR,
                "until":        window_end.strftime("%H:%M")
            }

    return {
        "restricted":  False,
        "tier":        0,
        "event":       None,
        "action":      "NORMAL",
        "risk_factor": 1.0
    }


def print_todays_schedule():
    """Print all today's events to terminal"""
    events = fetch_todays_events()

    if not events:
        print("📅 No high-impact events today")
        return

    print("\n📅 Today's Economic Calendar:")
    print("─" * 45)
    for e in sorted(events, key=lambda x: x["time"]):
        tier_label = "🔴 TIER 1" if e["tier"] == 1 else "🟡 TIER 2"
        time_str   = e["time"].strftime("%I:%M %p ET")
        print(f"   {tier_label} | {time_str} | {e['title']}")
    print("─" * 45 + "\n")


def next_event_info():
    """Get the next upcoming event today"""
    now    = datetime.now(ET)
    events = fetch_todays_events()

    future = [e for e in events if e["time"] > now and e["tier"] in [1, 2]]
    if not future:
        return None

    next_e   = min(future, key=lambda x: x["time"])
    mins_away = int((next_e["time"] - now).total_seconds() / 60)

    return {
        "title":     next_e["title"],
        "time":      next_e["time"].strftime("%I:%M %p ET"),
        "tier":      next_e["tier"],
        "mins_away": mins_away
    }
