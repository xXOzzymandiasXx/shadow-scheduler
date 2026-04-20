# Shadow Scheduler

Auto-schedule observation ("shadow") events on a manager's calendar when people on their team hold meetings with tracked counterparts, walking through a multi-stage lifecycle defined in YAML.

If you manage people who have recurring meetings with a specific set of clients/reports/candidates/patients, and you want to sit in on those meetings at defined lifecycle stages without manually chasing calendars — this tool does that.

## Use cases

- **Health coach manager** shadowing coaches through intake → lab review → provider exam
- **Sales manager** observing reps through discovery → demo → close
- **Clinical supervisor** sitting in on new therapists through intake → assessment → treatment planning
- **Engineering lead** shadowing a new hire's first rounds of interviews
- **Onboarding buddy** joining a new employee's first N meetings

## How it works

1. You list the people you want to track in `config/<your-id>.yaml` — each entry says *"watch this person's calendar for meetings with this counterpart."*
2. You define your lifecycle stages in the same file — each stage has keywords the scanner uses to auto-detect which stage a meeting represents.
3. A LaunchAgent runs the scanner twice a day (6 AM + 6 PM).
4. When it finds a tracked meeting, it creates a matching "shadow" event on your calendar with the meeting's Zoom link, at the same time, color-coded basil green.
5. State is persisted so the same event never gets shadowed twice.
6. Conflict detection prevents double-booking yourself within 45 minutes of an existing shadow.
7. Optional Slack DM when a shadow is scheduled.

## Setup

### 1. Clone and install

```bash
git clone https://github.com/xXOzzymandiasXx/shadow-scheduler.git
cd shadow-scheduler
```

### 2. Create Google OAuth credentials

Shadow Scheduler talks to Google Calendar, so it needs a Google OAuth client. This is a one-time 5-minute thing.

1. Go to [console.cloud.google.com](https://console.cloud.google.com/)
2. Create a new project (or pick an existing one)
3. APIs & Services → **Library** → search "Google Calendar API" → Enable
4. APIs & Services → **OAuth consent screen** → External → fill in app name + your email → Save (add yourself as a test user if prompted)
5. APIs & Services → **Credentials** → Create Credentials → **OAuth client ID** → Application type: **Desktop app** → Create
6. Download the JSON, rename it to `credentials.json`, drop it in the repo root

### 3. (Optional) Slack setup

If you want DM notifications when shadows are scheduled:

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → Create New App → From scratch
2. Name it, pick your workspace
3. OAuth & Permissions → Bot Token Scopes → add `chat:write` and `im:write`
4. Install to Workspace → copy the `xoxb-...` token
5. In your workspace, find your Slack member ID: profile → ⋯ → Copy member ID

### 4. Run setup

```bash
./setup.sh
```

It'll ask for your name, email, Slack info (optional), and the people you're tracking. Then it drops a config file in `config/<your-id>.yaml` and installs a LaunchAgent so the scanner runs automatically at 6 AM and 6 PM.

### 5. First run

```bash
python3 shadow_scanner.py --config config/<your-id>.yaml --dry-run
```

Opens a browser for Google OAuth consent on first run. After that, `token.json` is cached and all future runs are headless. `--dry-run` prints what it *would* schedule without actually creating calendar events.

## Adding tracked clients

Edit your `config/<your-id>.yaml`:

```yaml
tracked_clients:
  - coach_name: Jane Smith
    coach_email: jane.smith@company.com
    client_name: Acme Corp — John Doe
    started_at_stage: intake
    active: true
```

The scanner searches Jane's calendar for events containing "Acme Corp — John Doe" in the title or description, classifies them by stage (via the keywords you defined), and schedules matching shadows on your calendar.

## Stages

Stages are fully configurable. Default example uses a health-coaching lifecycle, but you can define anything:

```yaml
stages:
  - id: discovery
    label: "Discovery Call"
    keywords: ["discovery", "intro call"]
  - id: demo
    label: "Product Demo"
    keywords: ["demo", "product walkthrough"]
  - id: close
    label: "Close"
    keywords: ["contract review", "close"]

default_stage: discovery  # optional: used when no keywords match
```

Matching is case-insensitive and applies to event titles first, then the "Event Name:" line in descriptions (useful for Zoom-style invites). Longer keywords win over shorter ones so `"provider exam"` matches before a stage keyed on `"exam"`.

## Configuration reference

| Field | Required | Description |
|---|---|---|
| `manager_id` | yes | Short slug used for filenames (`alex`, `jane`) |
| `manager_name` | yes | Your full name |
| `manager_email` | yes | Your work email (Calendar ID to write shadows to) |
| `slack_user_id` | no | Slack member ID for DM notifications |
| `working_hours.timezone` | no | IANA name (e.g. `America/Chicago`, `Europe/London`). Used for out-of-hours filtering and all log/Slack display times. Default: `America/Chicago` |
| `working_hours.start_hour` | no | Earliest local hour for shadow events (default 9) |
| `working_hours.end_hour` | no | Latest local hour (default 20) |
| `stages` | yes | List of `{id, label, keywords}` |
| `default_stage` | no | Stage id to assume when no keywords match; omit to skip |
| `tracked_clients` | yes | List of `{coach_name, coach_email, client_name, active}` |

## Environment variables

| Var | Used by | Notes |
|---|---|---|
| `SLACK_BOT_TOKEN` | Slack notifications | Only read if `slack_user_id` is set in config. Set via your LaunchAgent's `EnvironmentVariables` block, which `setup.sh` does for you. |

## Files the scanner creates

- `token.json` — cached Google OAuth refresh token (auto-created after first browser consent)
- `state/<manager_id>.json` — tracks which source events have been shadowed (prevents duplicates)
- `logs/scanner.log` + `scanner.err` — LaunchAgent stdout/stderr

All are gitignored.

## Running manually

```bash
python3 shadow_scanner.py --config config/<your-id>.yaml           # live
python3 shadow_scanner.py --config config/<your-id>.yaml --dry-run # test
```

## Troubleshooting

**"Could not access <email>": the scanner can't read that person's calendar.** They need to share it with you (Google Calendar → Settings → Share with specific people → Make changes to events or See all event details). The scanner uses your Google account, so it can only see calendars you personally have access to.

**"OAuth client secrets not found": you're missing `credentials.json`.** See Setup step 2.

**"This app isn't verified" warning during OAuth consent:** expected for personal-use OAuth clients. Click "Advanced" → "Go to <app name> (unsafe)" → Continue. For a workspace-internal deployment, set your OAuth consent screen user type to "Internal" in GCP Console.

**Scanner isn't running on schedule:** check `launchctl list | grep shadow-scheduler`. To restart: `launchctl unload ~/Library/LaunchAgents/com.shadow-scheduler.<your-id>.plist && launchctl load ~/Library/LaunchAgents/com.shadow-scheduler.<your-id>.plist`.

## License

MIT — see [LICENSE](LICENSE).
