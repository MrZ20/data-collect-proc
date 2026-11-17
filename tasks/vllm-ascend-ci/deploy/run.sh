#!/bin/sh
set -eu

deploy_dir=${DEPLOY_DIR:-/opt/data-collect-proc}
image=${IMAGE:-data-collect-vllm-ascend-ci:0.1.4}
if [ "$#" -eq 0 ]; then
    set -- collect
fi

exec docker run --rm --read-only --tmpfs /tmp \
    --mount "type=bind,src=$deploy_dir/config.yaml,dst=/config.yaml,readonly" \
    --mount "type=bind,src=$deploy_dir/state/vllm-ascend-ci,dst=/state" \
    --mount "type=bind,src=$deploy_dir/secrets/github_token,dst=/run/secrets/github_token,readonly" \
    -e GITHUB_TOKEN_FILE=/run/secrets/github_token \
    "$image" --config /config.yaml "$@"
