#!/bin/sh
# Runs first inside an agent box. The box has no network of its own, so its loopback ports are
# forwarded to the job's gateway (Claude) and web proxy sockets; then this becomes the program.
set -u
SOCKETS=/run/openmontage
socat TCP-LISTEN:"$OM_BOX_GATEWAY_PORT",bind=127.0.0.1,reuseaddr,fork UNIX-CONNECT:"$SOCKETS/gateway.sock" &
socat TCP-LISTEN:"$OM_BOX_WEB_PORT",bind=127.0.0.1,reuseaddr,fork UNIX-CONNECT:"$SOCKETS/web.sock" &

# Wait (at most two seconds) until both listen, so the program's first request finds them.
gateway=$(printf ':%04X ' "$OM_BOX_GATEWAY_PORT")
web=$(printf ':%04X ' "$OM_BOX_WEB_PORT")
tries=0
while [ "$tries" -lt 200 ]; do
  listening=$(awk '$4 == "0A" {print ":" substr($2, index($2, ":") + 1) " "}' /proc/net/tcp)
  case "$listening" in *"$gateway"*) case "$listening" in *"$web"*) break ;; esac ;; esac
  tries=$((tries + 1))
  sleep 0.01
done
exec "$@"
