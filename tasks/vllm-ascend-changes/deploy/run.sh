#!/bin/sh
set -eu
deploy_dir=${DEPLOY_DIR:-/opt/data-collect-proc}
image=${IMAGE:-data-collect-vllm-ascend-changes:0.1.0}
exec docker run --rm --read-only --tmpfs /tmp \
    --mount "type=bind,src=$deploy_dir/state/vllm-ascend-changes,dst=/state" \
    --mount "type=bind,src=$deploy_dir/secrets/github_token,dst=/run/secrets/github_token,readonly" \
    -e GITHUB_TOKEN_FILE=/run/secrets/github_token "$image" "$@"
