"""Read public PR refs and check path names without checking out files."""

from pathlib import Path
import subprocess
import time

from watch_api import SOURCE

REMOTE = f'https://github.com/{SOURCE}.git'


def git(args, cwd=None, network=False, diff=False):
    for attempt in range(3 if network else 1):
        try:
            result = subprocess.run(['git', *args], cwd=cwd, capture_output=True, timeout=300)
            if result.returncode == 0 or (diff and result.returncode == 1):
                return result
            result.check_returncode()
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
            if not network or attempt == 2:
                raise RuntimeError(f'git {args[0]} failed: {exc.stderr[-500:] if exc.stderr else exc}') from exc
            time.sleep(2 ** attempt)


def head_refs():
    output = git(['ls-remote', '--exit-code', REMOTE, 'refs/pull/*/head'], network=True).stdout
    return {ref.split('/')[2]: sha for sha, ref in (line.split() for line in output.decode().splitlines())}


def path_matches(directory: Path, bases, paths, excludes):
    """Compare each head with its target branch without fetching file contents."""
    repo = directory / 'source.git'
    if not repo.exists():
        git(['init', '--bare', str(repo)])
    git(['config', 'remote.origin.url', REMOTE], repo)
    git(['config', 'remote.origin.promisor', 'true'], repo)
    git(['config', 'remote.origin.partialclonefilter', 'blob:none'], repo)
    results = {}
    numbers = list(bases)
    for start in range(0, len(numbers), 100):
        batch = numbers[start:start + 100]
        branches = set(bases[n] for n in batch)
        refs = [f'+refs/heads/{branch}:refs/watch/base/{branch}' for branch in branches]
        refs.extend(f'+refs/pull/{n}/head:refs/watch/pr/{n}' for n in batch)
        git(['fetch', '--no-tags', '--filter=blob:none', 'origin', *refs], repo, network=True)
        for n in batch:
            ref = f'refs/watch/pr/{n}'
            sha = git(['rev-parse', ref], repo).stdout.decode().strip()
            comparison = git(['diff', '--quiet', '--no-renames', f'refs/watch/base/{bases[n]}...' + ref, '--',
                              *(':(top,literal)' + path for path in paths),
                              *(':(top,exclude,literal)' + path for path in excludes)], repo, diff=True)
            results[n] = (sha, comparison.returncode == 1)
    return results
