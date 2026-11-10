"""Publish a verified demo fix. Run only in the separate trusted publish job."""
import os
import re
import subprocess
import tempfile
from pathlib import Path


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
    body = f'''修复订单折扣金额计算，使用原始回归测试验证。

- 任务编号：{job_id}
- 验证：shop_tests 以及助理服务测试全部通过
- 执行记录：{os.environ['RUN_URL']}

请审查代码后手动合并。
'''
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'pr-body.md'
        path.write_text(body, encoding='utf-8')
        run('gh', 'pr', 'create', '--draft', '--base', os.environ['BASE_BRANCH'], '--head', branch,
            '--title', 'fix: 修复订单折扣金额计算', '--body-file', str(path))


if __name__ == '__main__':
    main()
