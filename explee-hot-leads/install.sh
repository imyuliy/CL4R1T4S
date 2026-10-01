#!/usr/bin/env bash
# Installs (or updates) the Explee hot leads -> email sync on a Linux server.
#
#   sudo bash install.sh              install or update
#   sudo bash install.sh --uninstall  remove everything again
#
# Answers can be given up front as environment variables instead of typing them:
# EXPLEE_API_KEY, SMTP_USER, SMTP_PASSWORD, MAIL_TO, BACKFILL (yes/no).
set -euo pipefail

NAME="explee-hot-leads"
SERVICE_USER="explee-leads"
APP_DIR="/opt/$NAME"
CONF_DIR="/etc/$NAME"
CONF_FILE="$CONF_DIR/config.env"
STATE_DIR="/var/lib/$NAME"
BIN="/usr/local/bin/$NAME"
SOURCE_URL="${SOURCE_URL:-https://raw.githubusercontent.com/imyuliy/CL4R1T4S/refs/heads/claude/explee-hot-leads-crm-fisit0/explee-hot-leads}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
die()  { printf '\n\033[31mStopped: %s\033[0m\n' "$*" >&2; exit 1; }

ask() {  # ask VAR "question" [default] [secret]
  local var="$1" question="$2" default="${3:-}" secret="${4:-}" reply=""
  if [ -n "${!var:-}" ]; then return; fi
  [ -r /dev/tty ] || die "no keyboard available; set $var as an environment variable instead"
  while [ -z "$reply" ]; do
    if [ -n "$default" ]; then printf '%s [%s]: ' "$question" "$default" >/dev/tty
    else printf '%s: ' "$question" >/dev/tty; fi
    if [ -n "$secret" ]; then IFS= read -r -s reply </dev/tty; echo >/dev/tty
    else IFS= read -r reply </dev/tty; fi
    reply="${reply:-$default}"
  done
  printf -v "$var" '%s' "$reply"
}

yes_no() {  # yes_no "question" default(y/n)  -> exit status
  local reply=""
  printf '%s [%s]: ' "$1" "$([ "$2" = y ] && echo Y/n || echo y/N)" >/dev/tty
  IFS= read -r reply </dev/tty || true
  reply="${reply:-$2}"
  case "$reply" in [Yy]*) return 0 ;; *) return 1 ;; esac
}

[ "$(id -u)" -eq 0 ] || die "please run with sudo:  sudo bash install.sh"

if [ "${1:-}" = "--uninstall" ]; then
  say "Removing $NAME"
  systemctl disable --now "$NAME.timer" 2>/dev/null || true
  rm -f "/etc/systemd/system/$NAME.service" "/etc/systemd/system/$NAME.timer" "/etc/cron.d/$NAME"
  systemctl daemon-reload 2>/dev/null || true
  rm -rf "$APP_DIR" "$CONF_DIR" "$STATE_DIR" "$BIN"
  userdel "$SERVICE_USER" 2>/dev/null || true
  ok "removed"
  exit 0
fi

say "1/5  Checking this server"
command -v python3 >/dev/null || die "python3 is missing. Install it with:  sudo apt install -y python3"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))' \
  || die "python3 is too old (need 3.8 or newer): $(python3 --version)"
ok "$(python3 --version)"
id "$SERVICE_USER" >/dev/null 2>&1 \
  || useradd --system --no-create-home --home-dir "$STATE_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
ok "service user $SERVICE_USER"

say "2/5  Installing the program"
mkdir -p "$APP_DIR"
if [ -f "$HERE/explee_hot_leads.py" ]; then
  install -m 0755 "$HERE/explee_hot_leads.py" "$APP_DIR/explee_hot_leads.py"
else
  tmp="$(mktemp)"
  if command -v curl >/dev/null; then curl -fsSL "$SOURCE_URL/explee_hot_leads.py" -o "$tmp"
  else wget -qO "$tmp" "$SOURCE_URL/explee_hot_leads.py"; fi
  python3 -m py_compile "$tmp" || die "the downloaded program is damaged; please try again"
  install -m 0755 "$tmp" "$APP_DIR/explee_hot_leads.py"
  rm -f "$tmp"
fi
cat >"$BIN" <<EOF
#!/bin/sh
# Runs explee-hot-leads as its own user so file ownership stays right.
if [ "\$(id -u)" -ne 0 ]; then echo "please use: sudo $NAME \$*" >&2; exit 1; fi
exec runuser -u $SERVICE_USER -- /usr/bin/env python3 $APP_DIR/explee_hot_leads.py "\$@"
EOF
chmod 0755 "$BIN"
install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$STATE_DIR"
ok "installed in $APP_DIR"

say "3/5  Settings"
keep=no
if [ -f "$CONF_FILE" ]; then
  if yes_no "Settings already exist. Keep them?" y; then keep=yes; ok "kept $CONF_FILE"; fi
fi
if [ "$keep" = no ]; then
  echo
  echo "  Paste your Explee API key (it starts with sk_explee_). Nothing shows while you paste;"
  echo "  that is normal. Press Enter after pasting."
  ask EXPLEE_API_KEY "  Explee API key" "" secret
  echo
  echo "  The emails are sent through a Gmail account. You need a Gmail 'app password' for it:"
  echo "  https://myaccount.google.com/apppasswords  (create one named 'explee', copy the 16 letters)"
  ask SMTP_USER "  Gmail address that sends the emails"
  ask SMTP_PASSWORD "  Gmail app password (16 letters)" "" secret
  ask MAIL_TO "  Send the hot leads to which address?" "$SMTP_USER"
  if [ -z "${BACKFILL:-}" ]; then
    if yes_no "  Also email the hot leads from the past 7 days, right now?" y; then BACKFILL=yes; else BACKFILL=no; fi
  fi
  case "$BACKFILL" in [Yy]*) lookback=7 ;; *) lookback=0 ;; esac

  install -d -m 0750 -o root -g "$SERVICE_USER" "$CONF_DIR"
  (
  umask 027
  {
    echo "# Settings for $NAME. After changing this file nothing needs restarting."
    printf 'EXPLEE_API_KEY=%s\n' "$EXPLEE_API_KEY"
    printf 'EXPLEE_PROJECT_ID=%s\n' "${EXPLEE_PROJECT_ID:-42547}"
    printf 'SMTP_HOST=%s\n' "${SMTP_HOST:-smtp.gmail.com}"
    printf 'SMTP_PORT=%s\n' "${SMTP_PORT:-587}"
    printf 'SMTP_USER=%s\n' "$SMTP_USER"
    printf 'SMTP_PASSWORD=%s\n' "$SMTP_PASSWORD"
    printf 'MAIL_TO=%s\n' "$MAIL_TO"
    printf 'FIRST_RUN_LOOKBACK_DAYS=%s\n' "$lookback"
    printf 'TIMEZONE=%s\n' "${TIMEZONE:-Europe/Amsterdam}"
  } >"$CONF_FILE"
  )
  chown root:"$SERVICE_USER" "$CONF_FILE"
  chmod 0640 "$CONF_FILE"
  ok "saved to $CONF_FILE (only root and the service can read it)"
fi

say "4/5  Testing (Explee connection + a test email)"
if ! "$BIN" test; then
  echo
  echo "  The test failed (see the FAIL line above). Fix the setting and run this installer again,"
  echo "  or send the FAIL line to whoever helps you."
  yes_no "  Turn on the 10-minute schedule anyway?" n || die "not scheduled; nothing else was changed"
fi

say "5/5  Scheduling every 10 minutes"
if command -v systemctl >/dev/null && [ -d /run/systemd/system ]; then
  cat >"/etc/systemd/system/$NAME.service" <<EOF
[Unit]
Description=Email new Explee hot leads
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=$SERVICE_USER
Group=$SERVICE_USER
ExecStart=/usr/bin/env python3 $APP_DIR/explee_hot_leads.py run
TimeoutStartSec=15min
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=$STATE_DIR
EOF
  cat >"/etc/systemd/system/$NAME.timer" <<EOF
[Unit]
Description=Check Explee for new hot leads every 10 minutes

[Timer]
OnCalendar=*:0/10
AccuracySec=30s
Persistent=true

[Install]
WantedBy=timers.target
EOF
  rm -f "/etc/cron.d/$NAME"
  systemctl daemon-reload
  systemctl enable --now "$NAME.timer" >/dev/null
  systemctl start "$NAME.service" || true   # first check right now
  ok "running every 10 minutes (systemd timer)"
  log_hint="sudo journalctl -u $NAME -n 50"
else
  echo "*/10 * * * * $SERVICE_USER /usr/bin/env python3 $APP_DIR/explee_hot_leads.py run >>$STATE_DIR/run.log 2>&1" \
    >"/etc/cron.d/$NAME"
  chmod 0644 "/etc/cron.d/$NAME"
  "$BIN" run || true
  ok "running every 10 minutes (cron)"
  log_hint="sudo tail -n 50 $STATE_DIR/run.log"
fi
echo
"$BIN" status | sed 's/^/  /'

say "Done. New Explee hot leads now arrive by email within about 10 minutes."
echo "  Check on it:      sudo $NAME status"
echo "  See the log:      $log_hint"
echo "  Change settings:  sudo nano $CONF_FILE"
echo "  Remove it:        sudo bash install.sh --uninstall"
