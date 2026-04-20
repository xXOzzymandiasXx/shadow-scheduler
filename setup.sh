#!/usr/bin/env bash
#
# Shadow Scheduler — Setup
# Run once to configure everything for a new manager.
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BOLD='\033[1m'
NC='\033[0m'

step()  { echo -e "\n${GREEN}${BOLD}[$1]${NC} $2"; }
warn()  { echo -e "${YELLOW}!${NC} $1"; }
fail()  { echo -e "${RED}ERROR:${NC} $1"; exit 1; }
ok()    { echo -e "  ${GREEN}done${NC}"; }

echo -e "${BOLD}"
echo "========================================="
echo "  Shadow Scheduler — Setup"
echo "========================================="
echo -e "${NC}"

# ------------------------------------------------------------------
# 1. Check prerequisites
# ------------------------------------------------------------------
step "1/6" "Checking prerequisites..."

command -v python3 >/dev/null || fail "python3 not found. Install it first."
command -v pip3 >/dev/null    || fail "pip3 not found. Install it first."
echo "  python3: $(python3 --version 2>&1)"

# ------------------------------------------------------------------
# 2. Install Python dependencies
# ------------------------------------------------------------------
step "2/6" "Installing Python dependencies..."
pip3 install --user -q -r "$REPO_DIR/requirements.txt"
ok

# ------------------------------------------------------------------
# 3. Check Google OAuth credentials
# ------------------------------------------------------------------
step "3/6" "Checking Google OAuth credentials..."

if [ ! -f "$REPO_DIR/credentials.json" ]; then
    echo ""
    echo -e "  ${RED}Missing:${NC} $REPO_DIR/credentials.json"
    echo ""
    echo "  You need a Google OAuth Desktop-app credentials file."
    echo "  See the README section 'Creating Google OAuth credentials'."
    echo ""
    read -p "  Press Enter once credentials.json is in the repo root (or Ctrl-C to exit)... "
    [ -f "$REPO_DIR/credentials.json" ] || fail "Still not found. Exiting."
fi
echo "  credentials.json: found"

# ------------------------------------------------------------------
# 4. Collect manager details
# ------------------------------------------------------------------
step "4/6" "Tell me about yourself..."
echo ""

read -p "  Your full name (e.g. Alex Johnson): " MANAGER_NAME
[ -z "$MANAGER_NAME" ] && fail "Name is required."

DEFAULT_ID=$(echo "$MANAGER_NAME" | awk '{print tolower($1)}')
read -p "  Manager ID [$DEFAULT_ID]: " MANAGER_ID
MANAGER_ID="${MANAGER_ID:-$DEFAULT_ID}"

read -p "  Your work email (e.g. alex.johnson@company.com): " MANAGER_EMAIL
[ -z "$MANAGER_EMAIL" ] && fail "Email is required."

echo ""
echo "  Slack DM notifications are optional. Leave blank to skip."
echo "  To find your Slack member ID: profile → ⋯ → 'Copy member ID'"
echo ""
read -p "  Your Slack member ID (or blank to skip): " SLACK_USER_ID

SLACK_BOT_TOKEN=""
if [ -n "$SLACK_USER_ID" ]; then
    echo ""
    echo "  Paste your Slack bot token (xoxb-...). See README for setup."
    read -p "  Slack bot token (or blank to skip): " SLACK_BOT_TOKEN
fi

# ------------------------------------------------------------------
# 5. Collect coaches and generate config
# ------------------------------------------------------------------
step "5/6" "Add the people you want to track..."
echo ""
echo "  Enter each coach/rep/teammate's name + email. Type 'done' when finished."
echo ""

COACHES=()
COACH_EMAILS=()
i=1
while true; do
    read -p "  Person $i name (or 'done'): " CNAME
    [ "$CNAME" = "done" ] || [ "$CNAME" = "DONE" ] && break
    [ -z "$CNAME" ] && continue
    read -p "  Person $i email: " CEMAIL
    [ -z "$CEMAIL" ] && { warn "Skipping — no email provided."; continue; }
    COACHES+=("$CNAME")
    COACH_EMAILS+=("$CEMAIL")
    i=$((i + 1))
done

NUM_COACHES=${#COACHES[@]}
[ "$NUM_COACHES" -eq 0 ] && fail "You need at least one person to track."
echo ""
echo "  $NUM_COACHES person(s) added."

CONFIG_FILE="$REPO_DIR/config/${MANAGER_ID}.yaml"
cat > "$CONFIG_FILE" << YAMLEOF
# Shadow Scheduler — Manager Config
# Manager: $MANAGER_NAME

manager_id: $MANAGER_ID
manager_name: $MANAGER_NAME
manager_email: $MANAGER_EMAIL
slack_user_id: $SLACK_USER_ID

working_hours:
  start_hour: 9
  end_hour: 20
  utc_offset_hours: -5  # CT default; edit for your timezone

# Default stage definitions (health-coaching example). Edit these to match
# your own lifecycle — see config/template.yaml for the full spec.
stages:
  - id: intake
    label: "S1: Intake"
    keywords: ["intake"]
  - id: lab_review
    label: "S2: Lab Review"
    keywords: ["lab review", "lab"]
  - id: provider_exam
    label: "S3: Provider Exam"
    keywords: ["provider exam", "patient exam"]

default_stage: intake

tracked_clients:
  # Add entries as you start shadowing. Example:
  #
  # - coach_name: ${COACHES[0]:-Coach Name}
  #   coach_email: ${COACH_EMAILS[0]:-coach@example.com}
  #   client_name: Client Full Name
  #   started_at_stage: intake
  #   active: true
YAMLEOF

echo "  Config: $CONFIG_FILE"
mkdir -p "$REPO_DIR/state" "$REPO_DIR/logs"

# ------------------------------------------------------------------
# 6. Install LaunchAgent
# ------------------------------------------------------------------
step "6/6" "Installing LaunchAgent..."

LA_DIR="$HOME/Library/LaunchAgents"
mkdir -p "$LA_DIR"

PLIST="$LA_DIR/com.shadow-scheduler.${MANAGER_ID}.plist"
cat > "$PLIST" << PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.shadow-scheduler.${MANAGER_ID}</string>

  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>${REPO_DIR}/shadow_scanner.py</string>
    <string>--config</string>
    <string>${REPO_DIR}/config/${MANAGER_ID}.yaml</string>
  </array>

  <key>StartCalendarInterval</key>
  <array>
    <dict>
      <key>Hour</key>
      <integer>6</integer>
      <key>Minute</key>
      <integer>0</integer>
    </dict>
    <dict>
      <key>Hour</key>
      <integer>18</integer>
      <key>Minute</key>
      <integer>0</integer>
    </dict>
  </array>

  <key>StandardOutPath</key>
  <string>${REPO_DIR}/logs/scanner.log</string>

  <key>StandardErrorPath</key>
  <string>${REPO_DIR}/logs/scanner.err</string>

  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>SLACK_BOT_TOKEN</key>
    <string>${SLACK_BOT_TOKEN}</string>
  </dict>
</dict>
</plist>
PLISTEOF

launchctl load "$PLIST" 2>/dev/null || true
echo "  Scanner: installed (runs at 6 AM + 6 PM daily)"

# ------------------------------------------------------------------
# Done
# ------------------------------------------------------------------
echo ""
echo -e "${GREEN}${BOLD}========================================="
echo "  Setup complete!"
echo "=========================================${NC}"
echo ""
echo -e "  ${BOLD}To add your first tracked client:${NC}"
echo "    Edit $CONFIG_FILE and add an entry under tracked_clients:"
echo ""
echo "    - coach_name: Coach Full Name"
echo "      coach_email: coach@example.com"
echo "      client_name: Client Full Name"
echo "      started_at_stage: intake"
echo "      active: true"
echo ""
echo -e "  ${BOLD}To test right now (opens browser for Google OAuth on first run):${NC}"
echo "    python3 $REPO_DIR/shadow_scanner.py --config $CONFIG_FILE --dry-run"
echo ""
echo -e "  ${BOLD}Tail the logs:${NC}"
echo "    tail -f $REPO_DIR/logs/scanner.log"
echo ""
