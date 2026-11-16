#!/bin/sh
# The one command a pipeline's deploy key may run on the Docker host.
#
# Install it once beside the host's deploy.json (docs/API.md, "Deploying from the pipeline") and tie
# the pipeline's key to it in the login's ~/.ssh/authorized_keys:
#
#     restrict,command="/opt/openmontage/deploy" ssh-ed25519 AAAA... pipeline
#
# The pipeline then says "deploy sha256:<digest>". This script accepts exactly that, pulls that build
# of the repository named in deploy.json, takes the deploy helper out of the image and runs it. So
# the deploy logic arrives with every build, and this file stays as it is.
#
# Its output lands in a pipeline log that may be public: plain status lines only, and nothing that
# Docker or a settings file said.
set -eu
umask 077

root=$(cd "$(dirname "$0")" && pwd)
spec="$root/deploy.json"
request=${SSH_ORIGINAL_COMMAND:-$*}

digest=$(printf '%s\n' "$request" | sed -n 's/^deploy \(sha256:[0-9a-f]\{64\}\)$/\1/p')
if [ -z "$digest" ] || [ "$(printf '%s\n' "$request" | wc -l)" -ne 1 ]; then
    echo "refused: the request must be 'deploy sha256:<digest>'"
    exit 64
fi

repository=$(python3 -c 'import json, sys
value = json.load(open(sys.argv[1]))["repository"]
print(value if isinstance(value, str) else "")' "$spec" 2>/dev/null) || repository=
if ! printf '%s\n' "$repository" | grep -Eqx '[a-z0-9]+([._-][a-z0-9]+)*(:[0-9]+)?(/[a-z0-9]+([._-][a-z0-9]+)*)*'; then
    echo "refused: this host's deploy.json names no usable repository"
    exit 64
fi
image="$repository@$digest"

echo "pulling the build"
if ! docker pull -q "$image" >/dev/null 2>&1; then
    echo "failed: the build could not be pulled"
    exit 1
fi

mkdir -p "$root/releases"
release=$(mktemp -d "$root/releases/$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX")
if ! carrier=$(docker create --entrypoint /bin/true "$image" 2>/dev/null); then
    echo "failed: the build could not be opened"
    exit 1
fi
trap 'docker rm -v "$carrier" >/dev/null 2>&1 || true' EXIT
if ! docker cp "$carrier:/app/docker/deploy/deploy.py" "$release/deploy.py" >/dev/null 2>&1; then
    echo "failed: this build carries no deploy helper"
    exit 1
fi

python3 "$release/deploy.py" --spec "$spec" --image "$image" --release "$release"
