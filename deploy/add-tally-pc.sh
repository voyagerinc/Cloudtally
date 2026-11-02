#!/usr/bin/env bash
# Allows one Tally PC to open its reverse tunnel to this server.
#
#   sudo bash deploy/add-tally-pc.sh "ssh-ed25519 AAAA... tally-office-pc" 9000
#
# The key may only forward 127.0.0.1:<port> on this server back to that PC's Tally: no shell, no
# other ports. Give each Tally PC its own port (9000, 9001, ...) and use that port in the app's
# connector (host 127.0.0.1).
set -euo pipefail
KEY=${1:?usage: add-tally-pc.sh "<public key>" <port>}
PORT=${2:?usage: add-tally-pc.sh "<public key>" <port>}
case "$PORT" in ''|*[!0-9]*) echo "Port must be a number"; exit 1;; esac
case "$KEY" in ssh-ed25519\ *|ssh-rsa\ *|ecdsa-sha2-*) ;; *) echo "That does not look like an SSH public key"; exit 1;; esac

FILE=/home/tallytunnel/.ssh/authorized_keys
LINE="restrict,port-forwarding,permitlisten=\"127.0.0.1:$PORT\",command=\"/bin/false\" $KEY"
if grep -qF "$KEY" "$FILE"; then
  grep -vF "$KEY" "$FILE" > "$FILE.new" && mv "$FILE.new" "$FILE"   # replace, e.g. when the port changes
fi
echo "$LINE" >> "$FILE"
chown tallytunnel:tallytunnel "$FILE"
chmod 600 "$FILE"
echo "Added. When the tunnel is up, Tally answers on this server at 127.0.0.1:$PORT:"
echo "  curl -s http://127.0.0.1:$PORT    # expect: <RESPONSE>TallyPrime Server is Running</RESPONSE>"
