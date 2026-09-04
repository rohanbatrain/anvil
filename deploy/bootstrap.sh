#!/usr/bin/env bash
# One-time VPS preparation for the Anvil console.
#
# Run once, as root, on a fresh Ubuntu 22.04 or 24.04 server:
#   curl -fsSL https://raw.githubusercontent.com/rohanbatrain/anvil/main/deploy/bootstrap.sh | bash
# or, having cloned:
#   sudo bash deploy/bootstrap.sh
#
# Idempotent: safe to run again after a change.

set -Eeuo pipefail

DOMAIN="${ANVIL_DOMAIN:-anvil.rohanbatra.in}"
REPO="${ANVIL_REPO:-https://github.com/rohanbatrain/anvil.git}"
APP_USER=anvil
APP_ROOT=/opt/anvil
DEPLOY_USER="${ANVIL_DEPLOY_USER:-deploy}"

log() { printf '\n\033[1;33m==>\033[0m %s\n' "$*"; }
die() { printf '\n\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root"

log "Packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
  python3 python3-venv python3-dev build-essential \
  git curl ca-certificates ufw fail2ban \
  debian-keyring debian-archive-keyring apt-transport-https \
  unattended-upgrades

log "Caddy"
if ! command -v caddy >/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  apt-get update -qq
  apt-get install -y -qq caddy
fi

log "Service account"
# No shell and no home: this account exists to own a process, not to be used.
id -u "$APP_USER" &>/dev/null || useradd --system --no-create-home --shell /usr/sbin/nologin "$APP_USER"

log "Deploy account"
# Separate from root and from the service account. CI authenticates as this
# user, and it can do exactly two privileged things (see sudoers below).
if ! id -u "$DEPLOY_USER" &>/dev/null; then
  useradd --create-home --shell /bin/bash "$DEPLOY_USER"
fi
install -d -m 700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "/home/$DEPLOY_USER/.ssh"
touch "/home/$DEPLOY_USER/.ssh/authorized_keys"
chown "$DEPLOY_USER:$DEPLOY_USER" "/home/$DEPLOY_USER/.ssh/authorized_keys"
chmod 600 "/home/$DEPLOY_USER/.ssh/authorized_keys"

log "Directories"
install -d -m 755 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$APP_ROOT" "$APP_ROOT/releases"
install -d -m 750 -o "$APP_USER"    -g "$APP_USER"    /var/lib/anvil
install -d -m 750 -o root -g "$APP_USER" /etc/anvil
install -d -m 755 -o caddy -g caddy /var/log/caddy

log "Environment file"
# Created empty and locked down. Populated by hand, never by this script and
# never from the repository -- a secret that passes through git is compromised.
if [[ ! -f /etc/anvil/anvil.env ]]; then
  cat > /etc/anvil/anvil.env <<'ENVEOF'
# Anvil, public demonstration instance.
#
# This file holds every credential the instance uses. It is mode 640, owned by
# root, readable only by the anvil service account. It is never in git, never in
# a container image, and never in the systemd unit (which is world-readable).
#
# TEST MODE ONLY. anvil/core/config.py refuses to start against a key that does
# not begin rzp_test_, so this instance cannot be pointed at production Razorpay
# even by mistake. A leaked test key can create test orders; it cannot move
# anybody's money.
#
# Rotate all of these after the demonstration period.
ANVIL_MODE=live
ANVIL_ENV=demo
ANVIL_LOG_FORMAT=json
ANVIL_LOG_LEVEL=INFO
ANVIL_SEED=20260902
ANVIL_PUBLIC_BASE_URL=https://anvil.rohanbatra.in

# Gate on the console. Share with reviewers; it protects the demonstration.
ANVIL_CONSOLE_USERNAME=reviewer
ANVIL_CONSOLE_PASSWORD=CHANGE_ME

# Razorpay, test mode. Dashboard -> Account & Settings -> API Keys.
ANVIL_RAZORPAY_KEY_ID=
ANVIL_RAZORPAY_KEY_SECRET=
# You choose this value; paste the same one into the dashboard webhook.
ANVIL_RAZORPAY_WEBHOOK_SECRET=

# Anthropic. Set a spend limit on this key in the Anthropic console -- it is the
# only credential here that can cost real money if abused.
ANVIL_ANTHROPIC_API_KEY=
ENVEOF
fi
chown root:"$APP_USER" /etc/anvil/anvil.env
chmod 640 /etc/anvil/anvil.env

log "First checkout"
if [[ ! -d "$APP_ROOT/repo/.git" ]]; then
  git clone --depth 50 "$REPO" "$APP_ROOT/repo"
  chown -R "$DEPLOY_USER:$DEPLOY_USER" "$APP_ROOT/repo"
fi

log "Virtualenv"
if [[ ! -x "$APP_ROOT/venv/bin/python" ]]; then
  python3 -m venv "$APP_ROOT/venv"
fi
"$APP_ROOT/venv/bin/pip" install --quiet --upgrade pip
chown -R "$DEPLOY_USER:$DEPLOY_USER" "$APP_ROOT/venv"

log "systemd unit"
install -m 644 "$APP_ROOT/repo/deploy/anvil.service" /etc/systemd/system/anvil.service
systemctl daemon-reload
systemctl enable anvil >/dev/null

log "Deploy privileges"
# The deploy user may restart and inspect this one service and nothing else.
# Passwordless sudo scoped to two commands is the difference between a
# compromised CI token restarting a service and owning the box.
cat > /etc/sudoers.d/anvil-deploy <<SUDOEOF
$DEPLOY_USER ALL=(root) NOPASSWD: /bin/systemctl restart anvil, /bin/systemctl status anvil, /usr/bin/systemctl restart anvil, /usr/bin/systemctl status anvil
SUDOEOF
chmod 440 /etc/sudoers.d/anvil-deploy
visudo -cf /etc/sudoers.d/anvil-deploy >/dev/null || die "sudoers fragment is invalid"

log "Caddy site"
install -m 644 "$APP_ROOT/repo/deploy/Caddyfile" /etc/caddy/Caddyfile
sed -i "s/anvil\.rohanbatra\.in/$DOMAIN/g" /etc/caddy/Caddyfile
systemctl enable caddy >/dev/null

log "Firewall"
ufw --force reset >/dev/null
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow OpenSSH >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
# Port 8000 is deliberately NOT opened. The application binds loopback only.
ufw --force enable >/dev/null

log "SSH hardening"
cat > /etc/ssh/sshd_config.d/99-anvil.conf <<'SSHEOF'
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
X11Forwarding no
MaxAuthTries 3
SSHEOF
sshd -t || die "sshd config invalid; not restarting"
systemctl reload ssh 2>/dev/null || systemctl reload sshd

log "Unattended security upgrades"
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'AUEOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
AUEOF

systemctl enable --now fail2ban >/dev/null 2>&1 || true

cat <<DONEEOF

$(printf '\033[1;32mBootstrap complete.\033[0m')

Three things left, in this order:

  1. Fill in the credentials and the console password:
       sudo sed -i "s/CHANGE_ME/\$(openssl rand -base64 24 | tr -d '/+=')/" /etc/anvil/anvil.env
       sudo nano /etc/anvil/anvil.env     # the four ANVIL_RAZORPAY_/ANTHROPIC values
       sudo grep CONSOLE_PASSWORD /etc/anvil/anvil.env      # note it down

     Test-mode Razorpay keys only. The application refuses to start against a
     rzp_live_ key, so a mistake here fails loudly rather than quietly.

  2. Add the CI public key so GitHub can deploy:
       echo '<ssh-ed25519 AAAA... key>' >> /home/$DEPLOY_USER/.ssh/authorized_keys

  3. Point DNS at this server, then deploy:
       A     $DOMAIN   ->  $(curl -s4 --max-time 5 ifconfig.me || echo '<this server IPv4>')
       sudo -u $DEPLOY_USER bash $APP_ROOT/repo/deploy/deploy.sh

  Caddy will obtain the certificate once DNS resolves. Until then it will
  retry and log the failure, which is the expected state, not a fault.

DONEEOF
