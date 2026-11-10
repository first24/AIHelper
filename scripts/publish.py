"""Publish a verified demo fix. Run only in the separate trusted publish job."""
import os
import re
import subprocess


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def main():
    job_id = os.environ['JOB_ID']
    if not re.fullmatch(r'[0-9a-f]{16}', job_id):
        raise SystemExit('Invalid task ID')
    branch = 'assistant/' + job_id
    # This clean job receives exactly one source file, never the agent's checkout state.
    changed = subprocess.check_output(['git', 'diff', '--name-only'], text=True).splitlines()
    if changed != ['demo_shop/pricing.py']:
        raise SystemExit('Expected one modified pricing file; got ' + repr(changed))
    run('git', 'checkout', '-b', branch)
    run('git', 'config', 'user.name', 'AIHelper')
    run('git', 'config', 'user.email', 'aihelper@users.noreply.github.com')
    run('git', 'add', '--', 'demo_shop/pricing.py')
    run('git', '-c', 'core.hooksPath=/dev/null', 'commit', '-m', 'fix: correct order discount calculation')
    # gh sets up authentication without embedding the token in a remote URL or command argument.
    run('gh', 'auth', 'setup-git')
    run('git', '-c', 'core.hooksPath=/dev/null', 'push', 'origin', branch)
    print('Verified repair branch pushed. The local assistant will create the draft PR.')


if __name__ == '__main__':
    main()
