#!/usr/bin/env python3
"""
Shadow Scheduler
Auto-creates observation ("shadow") events on a manager's calendar when tracked
people hold meetings with tracked counterparts, walking them through a
multi-stage lifecycle defined in YAML.

Usage:
    python shadow_scanner.py --config config/<name>.yaml
    python shadow_scanner.py --config config/<name>.yaml --dry-run
"""

import argparse
import json
import logging
import os
import re
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

REPO_DIR = Path(__file__).resolve().parent
CLIENT_SECRETS_PATH = REPO_DIR / "credentials.json"
TOKEN_PATH = REPO_DIR / "token.json"
SCOPES = ["https://www.googleapis.com/auth/calendar"]
BASIL_COLOR_ID = "10"
SHADOW_EMOJI = "🔍"
SHADOW_CONFLICT_MINUTES = 45


# ---------------------------------------------------------------------------
# Slack notifications (optional)
# ---------------------------------------------------------------------------

def _slack_post(text, slack_user_id=None):
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token or not slack_user_id:
        return
    payload = json.dumps({
        "channel": slack_user_id,
        "text": text,
        "mrkdwn": True,
    }).encode()
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            body = json.loads(resp.read())
            if not body.get("ok"):
                log.warning(f"Slack API error: {body.get('error')}")
    except urllib.error.URLError as e:
        log.warning(f"Slack notification failed: {e}")


# ---------------------------------------------------------------------------
# Config & state
# ---------------------------------------------------------------------------

def load_config(config_path):
    with open(config_path) as f:
        return yaml.safe_load(f)


def load_state(state_path):
    if Path(state_path).exists():
        with open(state_path) as f:
            return json.load(f)
    return {"shadowed_events": {}}


def save_state(state_path, state):
    with open(state_path, "w") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------------------
# Google Calendar auth
# ---------------------------------------------------------------------------

def get_credentials():
    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    elif not creds or not creds.valid:
        if not CLIENT_SECRETS_PATH.exists():
            log.error(
                f"OAuth client secrets not found at {CLIENT_SECRETS_PATH}. "
                f"See README for how to create one."
            )
            raise SystemExit(1)
        flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRETS_PATH), SCOPES)
        creds = flow.run_local_server(port=0)
    TOKEN_PATH.write_text(creds.to_json())
    return creds


def build_calendar_service():
    creds = get_credentials()
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


# ---------------------------------------------------------------------------
# Stage detection (config-driven)
# ---------------------------------------------------------------------------

def build_stage_matcher(config):
    """Returns (detect_fn, labels) from the config's stages block.

    Stages are defined in YAML as a list of {id, label, keywords}. Matching is
    case-insensitive. If no stage keyword matches, default_stage (if set) is
    used; otherwise the event is skipped.
    """
    stages = config.get("stages", [])
    default_stage = config.get("default_stage")
    labels = {s["id"]: s.get("label", s["id"].title()) for s in stages}
    # Sort stages by keyword specificity (longer keywords first) so "provider
    # exam" wins over a stage whose keyword is just "exam".
    ordered = []
    for stage in stages:
        for kw in stage.get("keywords", []):
            ordered.append((kw.lower(), stage["id"]))
    ordered.sort(key=lambda t: -len(t[0]))

    def _match(text):
        t = (text or "").lower()
        for kw, sid in ordered:
            if kw in t:
                return sid
        return None

    def detect(event_summary, event_description=""):
        sid = _match(event_summary)
        if sid:
            return sid
        for line in (event_description or "").splitlines():
            if line.strip().lower().startswith("event name:"):
                sid = _match(line)
                if sid:
                    return sid
                break
        return default_stage

    return detect, labels


# ---------------------------------------------------------------------------
# Calendar operations
# ---------------------------------------------------------------------------

def get_coach_events(service, coach_email, client_name, days_ahead=60):
    now = datetime.now(timezone.utc)
    time_max = now + timedelta(days=days_ahead)
    try:
        result = service.events().list(
            calendarId=coach_email,
            q=client_name,
            timeMin=now.isoformat(),
            timeMax=time_max.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        ).execute()
        return result.get("items", [])
    except HttpError as e:
        log.warning(f"Could not access {coach_email}: {e}")
        return []


def event_already_shadowed(state, coach_email, source_event_id):
    return f"{coach_email}::{source_event_id}" in state.get("shadowed_events", {})


def mark_event_shadowed(state, coach_email, source_event_id, shadow_event_id, stage, client_name):
    state.setdefault("shadowed_events", {})[f"{coach_email}::{source_event_id}"] = {
        "shadow_event_id": shadow_event_id,
        "stage": stage,
        "client_name": client_name,
        "created_at": datetime.now().isoformat(),
    }


def get_scheduled_shadows(service, manager_email, days_ahead=60):
    now = datetime.now(timezone.utc)
    time_max = now + timedelta(days=days_ahead)
    try:
        result = service.events().list(
            calendarId=manager_email,
            q=SHADOW_EMOJI,
            timeMin=now.isoformat(),
            timeMax=time_max.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        ).execute()
        shadows = []
        for event in result.get("items", []):
            start_str = event.get("start", {}).get("dateTime")
            if start_str:
                summary = event.get("summary", "")
                coach = _extract_coach_from_shadow(summary)
                shadows.append((datetime.fromisoformat(start_str), coach))
        return shadows
    except HttpError as e:
        log.warning(f"Could not fetch shadow events: {e}")
        return []


def _extract_coach_from_shadow(summary):
    """Extract coach name from '🔍 Shadow: Client + Coach — Stage'."""
    match = re.search(r'\+ (.+?) — ', summary)
    return match.group(1) if match else "unknown"


def find_shadow_conflict(start_str, scheduled_shadows):
    if not start_str:
        return None, None
    start = datetime.fromisoformat(start_str)
    for shadow_start, shadow_coach in scheduled_shadows:
        if abs((start - shadow_start).total_seconds()) < SHADOW_CONFLICT_MINUTES * 60:
            return shadow_start, shadow_coach
    return None, None


def coach_sort_key(entry, shadow_state):
    """Sort so coaches with no prior shadow run first, then oldest shadow."""
    coach_email = entry["coach_email"]
    coach_name = entry["coach_name"]
    last_shadow = ""
    for key, val in shadow_state.get("shadowed_events", {}).items():
        if key.startswith(f"{coach_email}::"):
            created = val.get("created_at", "")
            if created > last_shadow:
                last_shadow = created
    return (last_shadow, coach_name)


def create_shadow_event(service, manager_calendar_id, coach_name, client_name,
                        stage, stage_label, source_event, tz, dry_run=False,
                        slack_user_id=None):
    start = source_event.get("start", {})
    end = source_event.get("end", {})
    zoom_link = source_event.get("location", "")
    description = source_event.get("description", "")
    pw_match = re.search(r"Password:\s*(\d+)", description)
    zoom_pw = pw_match.group(1) if pw_match else ""
    summary = f"{SHADOW_EMOJI} Shadow: {client_name} + {coach_name} — {stage_label}"
    body = {
        "summary": summary,
        "description": (
            f"Lifecycle Shadow — {stage_label}\n"
            f"Coach: {coach_name}\n"
            f"Client: {client_name}\n"
            + (f"\nZoom: {zoom_link}\nPassword: {zoom_pw}" if zoom_link else "")
        ),
        "location": zoom_link,
        "start": start,
        "end": end,
        "colorId": BASIL_COLOR_ID,
        "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 10}]},
    }
    if dry_run:
        log.info(f"[DRY RUN] Would create: {summary}")
        return "dry-run-id"
    try:
        event = service.events().insert(calendarId=manager_calendar_id, body=body).execute()
        log.info(f"Created: {summary}")

        start_dt_str = start.get("dateTime", "")
        if start_dt_str:
            dt_local = datetime.fromisoformat(start_dt_str).astimezone(tz)
            date_str = dt_local.strftime("%b %d")
            time_str = dt_local.strftime("%-I:%M %p %Z")
        else:
            date_str = start.get("date", "TBD")
            time_str = ""
        slack_msg = (
            f"🔍 *Shadow scheduled* — {stage_label} with *{coach_name}* + {client_name}\n"
            f"📅 {date_str} at {time_str}\n"
            f"Added to your calendar."
        )
        _slack_post(slack_msg, slack_user_id)

        return event["id"]
    except HttpError as e:
        log.error(f"Failed to create event: {e}")
        return None


# ---------------------------------------------------------------------------
# Main scan loop
# ---------------------------------------------------------------------------

def run(config, state, dry_run=False):
    manager_email = config["manager_email"]
    slack_user_id = config.get("slack_user_id")
    wh = config.get("working_hours", {})
    min_hour = wh.get("start_hour", 9)
    max_hour = wh.get("end_hour", 20)
    tz_name = wh.get("timezone", "America/Chicago")
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        log.warning(f"Unknown timezone {tz_name!r}; falling back to America/Chicago")
        tz = ZoneInfo("America/Chicago")
    detect_stage, stage_labels = build_stage_matcher(config)

    service = build_calendar_service()
    new_shadows = 0

    scheduled_shadows = get_scheduled_shadows(service, manager_email)
    log.info(f"Loaded {len(scheduled_shadows)} existing shadow(s) for conflict detection")

    active_entries = [e for e in config.get("tracked_clients", []) if e.get("active", True)]
    active_entries.sort(key=lambda e: coach_sort_key(e, state))

    for entry in active_entries:
        coach_email = entry["coach_email"]
        coach_name = entry["coach_name"]
        client_name = entry["client_name"]
        log.info(f"Scanning {coach_name} for {client_name}...")

        events = get_coach_events(service, coach_email, client_name)
        had_conflicts = False
        any_scheduled = False

        for event in events:
            event_id = event["id"]
            stage = detect_stage(event.get("summary", ""), event.get("description", ""))
            if not stage or event_already_shadowed(state, coach_email, event_id):
                continue
            stage_label = stage_labels.get(stage, stage.title())
            start_dt_str = event.get("start", {}).get("dateTime")
            if start_dt_str:
                start_dt_local = datetime.fromisoformat(start_dt_str).astimezone(tz)
                local_hour = start_dt_local.hour
                if not (min_hour <= local_hour <= max_hour):
                    log.info(
                        f"  Skipping out-of-hours "
                        f"({start_dt_local.strftime('%-I:%M %p %Z')})"
                    )
                    continue

                conflict_time, conflict_coach = find_shadow_conflict(
                    start_dt_str, scheduled_shadows
                )
                if conflict_time:
                    had_conflicts = True
                    event_time_str = start_dt_local.strftime("%-I:%M %p %Z")
                    conflict_time_str = conflict_time.astimezone(tz).strftime("%-I:%M %p %Z")
                    log.info(
                        f"  Skipped {coach_name} at {event_time_str} "
                        f"(conflicts with {conflict_coach} shadow at {conflict_time_str})"
                    )
                    continue

            shadow_id = create_shadow_event(
                service, manager_email, coach_name, client_name, stage, stage_label,
                event, tz, dry_run, slack_user_id=slack_user_id
            )
            if shadow_id:
                mark_event_shadowed(state, coach_email, event_id, shadow_id, stage, client_name)
                if start_dt_str:
                    scheduled_shadows.append(
                        (datetime.fromisoformat(start_dt_str), coach_name)
                    )
                new_shadows += 1
                any_scheduled = True

        if had_conflicts and not any_scheduled:
            log.warning(
                f"  {coach_name} needs manual scheduling — "
                f"all upcoming events conflict with existing shadows"
            )

    log.info(f"Done. {new_shadows} new shadow(s) created.")
    if new_shadows == 0:
        _slack_post("✅ Shadow scan complete — no new sessions detected.", slack_user_id)
    return new_shadows


def main():
    parser = argparse.ArgumentParser(description="Shadow Scheduler — auto-shadow your team's meetings")
    parser.add_argument("--config", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    config = load_config(args.config)
    manager_id = config.get("manager_id", Path(args.config).stem)
    state_path = str(REPO_DIR / "state" / f"{manager_id}.json")
    state = load_state(state_path)
    try:
        run(config, state, dry_run=args.dry_run)
    finally:
        if not args.dry_run:
            save_state(state_path, state)


if __name__ == "__main__":
    main()
