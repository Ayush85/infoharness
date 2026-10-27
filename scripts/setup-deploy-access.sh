#!/usr/bin/env bash
# One-time: let GitHub Actions deploy to this server over SSH.
#
#   sudo bash scripts/setup-deploy-access.sh            # run on the droplet
#
# Creates a 'deploy' user with its own SSH key, Docker access and passwordless sudo, then prints the
# three values to paste into GitHub (Settings -> Secrets and variables -> Actions).
# The private key is shown once and then deleted from the server. Treat it as root access:
# revoke any time with  sudo rm /home/deploy/.ssh/authorized_keys
set -euo pipefail
trap 'echo "xx setup-deploy-access.sh failed at line $LINENO: $BASH_COMMAND" >&2' ERR
[[ $EUID -eq 0 ]] || { echo "Run with sudo." >&2; exit 1; }
say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

# Earlier versions could exit early and leave a generated key in /tmp; remove those leftovers.
for d in /tmp/tmp.*; do
  [[ -f "$d/key" && -f "$d/key.pub" ]] && grep -q "github-actions-deploy@" "$d/key.pub" 2>/dev/null && rm -rf "$d"
done

USER_NAME=deploy
DEPLOY_PATH="${DEPLOY_PATH:-/opt/infoharness}"

say "User '$USER_NAME' (docker access, passwordless sudo)"
id "$USER_NAME" >/dev/null 2>&1 || useradd --create-home --shell /bin/bash "$USER_NAME"
if getent group docker >/dev/null; then usermod -aG docker "$USER_NAME"; fi
echo "$USER_NAME ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/90-deploy
chmod 440 /etc/sudoers.d/90-deploy
visudo -cf /etc/sudoers.d/90-deploy >/dev/null

if ! command -v rsync >/dev/null; then say "Installing rsync"; apt-get update -qq && apt-get install -y -qq rsync; fi
mkdir -p "$DEPLOY_PATH"
chown -R "$USER_NAME:$USER_NAME" "$DEPLOY_PATH"   # an existing checkout here is taken over by the deploy user

home="/home/$USER_NAME"
install -d -m 700 -o "$USER_NAME" -g "$USER_NAME" "$home/.ssh"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
say "New SSH key for GitHub Actions (replaces any previous one)"
ssh-keygen -q -t ed25519 -N "" -C "github-actions-deploy@$(hostname)" -f "$tmp/key"
touch "$home/.ssh/authorized_keys"
sed -i '/github-actions-deploy@/d' "$home/.ssh/authorized_keys"
cat "$tmp/key.pub" >> "$home/.ssh/authorized_keys"
chown "$USER_NAME:$USER_NAME" "$home/.ssh/authorized_keys"
chmod 600 "$home/.ssh/authorized_keys"

host_ip="$(curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')"
# sshd -T fails on Ubuntu's socket-activated SSH, so fall back to the config files.
ssh_port="$( { sshd -T 2>/dev/null || true; } | awk '/^port /{print $2; exit}')"
if [[ -z "$ssh_port" ]]; then
  ssh_port="$( { cat /etc/ssh/sshd_config /etc/ssh/sshd_config.d/*.conf 2>/dev/null || true; } | awk 'tolower($1)=="port"{print $2; exit}')"
fi
ssh_port="${ssh_port:-22}"
# Pin this server's host keys so the workflow refuses to talk to an impostor.
entry="$host_ip"; [[ "$ssh_port" == 22 ]] || entry="[$host_ip]:$ssh_port"
known=""
for f in /etc/ssh/ssh_host_ed25519_key.pub /etc/ssh/ssh_host_rsa_key.pub; do
  if [[ -f "$f" ]]; then known+="$entry $(cut -d' ' -f1,2 "$f")"$'\n'; fi
done
known="${known%$'\n'}"

cat <<EOF

=====================================================================
 Add these in GitHub: repo -> Settings -> Secrets and variables -> Actions
=====================================================================

 SECRETS (tab "Secrets" -> New repository secret)

 DEPLOY_HOST         = $host_ip
 DEPLOY_PORT         = $ssh_port
 DEPLOY_KNOWN_HOSTS  = (the lines between the markers)
-----BEGIN KNOWN HOSTS-----
$known
-----END KNOWN HOSTS-----
 DEPLOY_SSH_KEY      = (everything between and including the BEGIN/END lines)
$(cat "$tmp/key")

 VARIABLES (tab "Variables" -> New repository variable)

 DEPLOY_DOMAIN       = infoharness.com
 DEPLOY_EMAIL        = your email (for HTTPS certificates)
 DEPLOY_PATH         = $DEPLOY_PATH
 DEPLOY_ARGS         = (optional, e.g. --proxy none   if nginx already owns 80/443)

Then: repo -> Actions -> "Deploy" -> Run workflow. Later deploys run on every push to main.
EOF

echo
echo "The private key was deleted from this server. If you lose it, just run this script again."
