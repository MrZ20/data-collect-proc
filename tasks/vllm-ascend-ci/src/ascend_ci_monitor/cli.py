"""Command line entry points for discovery and one-shot collection."""

import argparse
import fcntl
import json
import logging
import os
from pathlib import Path

import yaml

from .collector import collect, sync_policy
from .discovery import discover
from .github import GitHub
from .policy import Policy
from .publish import publish
from .state import State

DEFAULTS = {
    'source_repository': 'vllm-project/vllm-ascend',
    'data_repository': 'MrZ20/data_repo',
    'config_branch': 'main',
    'policy_path': 'configs/vllm-ascend-ci/workflows.yaml',
    'data_branch': 'data/vllm-ascend-ci',
    'state_dir': './state/vllm-ascend-ci',
    'initial_hours': 1,
    'recheck_days': 7,
}


def token_file(explicit, kind: str) -> str:
    path = explicit or os.environ.get(f'GITHUB_{kind}_TOKEN_FILE') or os.environ.get('GITHUB_TOKEN_FILE')
    if not path:
        raise ValueError('Set --token-file or GITHUB_TOKEN_FILE (or separate READ/WRITE token files)')
    token = Path(path).read_text(encoding='utf-8').strip()
    if not token or any(character.isspace() for character in token):
        raise ValueError('Token file must contain one token, without surrounding instructions')
    return token


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Collect runtime CI failures for agent analysis')
    parser.add_argument('--config', type=Path)
    parser.add_argument('--token-file', type=Path, help='Shared read/write GitHub token file')
    sub = parser.add_subparsers(dest='command', required=True)
    discovery = sub.add_parser('discover', help='Merge actual runtime jobs/steps into a local policy')
    discovery.add_argument('--output', type=Path, required=True)
    discovery.add_argument('--samples', type=int, default=2)
    validation = sub.add_parser('validate-policy')
    validation.add_argument('path', type=Path)
    collection = sub.add_parser('collect', help='Collect once and publish; no background scheduler')
    collection.add_argument('--policy', type=Path, help='Use a local policy instead of syncing the private repository')
    collection.add_argument('--no-upload', action='store_true', help='Collect into the local outbox only')
    sub.add_parser('publish', help='Retry the local outbox without collecting new runs')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    try:
        if args.command == 'validate-policy':
            policy = Policy.load(args.path)
            print(f'Valid policy: {policy.repository}')
            return 0
        config = dict(DEFAULTS)
        if args.config:
            supplied = yaml.safe_load(args.config.read_text(encoding='utf-8'))
            if not isinstance(supplied, dict):
                raise ValueError('Deployment config must be a mapping')
            unknown = set(supplied) - set(DEFAULTS)
            if unknown:
                raise ValueError(f'Unknown deployment config keys: {sorted(unknown)}')
            config.update(supplied)
        if config['initial_hours'] <= 0 or config['recheck_days'] < 0:
            raise ValueError('initial_hours must be positive; recheck_days cannot be negative')
        source = GitHub(token_file(args.token_file, 'READ'))
        if args.command == 'discover':
            if args.samples < 1:
                raise ValueError('--samples must be positive')
            existing = yaml.safe_load(args.output.read_text(encoding='utf-8')) if args.output.exists() else None
            data = discover(source, config['source_repository'], existing, args.samples)
            Policy.from_dict(data)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            temporary = args.output.with_suffix('.tmp')
            temporary.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding='utf-8')
            temporary.replace(args.output)
            print(json.dumps({'workflows': len(data['workflows']), 'api_requests': source.requests}))
            return 0
        writer = GitHub(token_file(args.token_file, 'WRITE'))
        state_dir = Path(config['state_dir'])
        state_dir.mkdir(parents=True, exist_ok=True)
        with (state_dir / 'collector.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                logging.info('Another collection is running; skipping this invocation')
                return 0
            with State(state_dir / 'state.sqlite3') as state:
                if args.command == 'publish':
                    result = {'published': publish(writer, config['data_repository'], config['data_branch'], state)}
                else:
                    policy = sync_policy(writer, config, state_dir, args.policy)
                    result = collect(source, writer, config, policy, state, upload=not args.no_upload)
                result.update(api_requests=source.requests + writer.requests,
                              read_remaining=source.remaining, write_remaining=writer.remaining)
                print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, RuntimeError, ValueError, yaml.YAMLError) as exc:
        logging.error('%s', exc)
        return 1
