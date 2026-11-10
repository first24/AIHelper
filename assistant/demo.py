"""Offline transport simulation; the regression tests and local git commit are real."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .core import Config, Engine, Store


def demo():
    root = Path(__file__).resolve().parents[1]
    report = {"mode": "offline simulation", "real_feishu": False, "real_github": False,
              "real_model_call": False, "messages": []}
    with tempfile.TemporaryDirectory(prefix='aihelper-demo-') as directory:
        checkout = Path(directory)
        for name in ('demo_shop', 'shop_tests'):
            shutil.copytree(root / name, checkout / name, ignore=shutil.ignore_patterns('__pycache__'))

        def command(*args, check=True):
            return subprocess.run(args, cwd=checkout, check=check, capture_output=True, text=True,
                                  encoding='utf-8', errors='replace',
                                  env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUTF8': '1'})

        command('git', 'init', '-b', 'main')
        command('git', 'config', 'user.name', 'AIHelper local demo')
        command('git', 'config', 'user.email', 'demo@example.invalid')
        command('git', 'add', '.')
        command('git', '-c', 'core.hooksPath=NUL' if os.name == 'nt' else 'core.hooksPath=/dev/null', 'commit', '-m', 'demo: intentional pricing bug')

        class LocalGitHub:
            def dispatch(self, job):
                before = command(sys.executable, '-m', 'unittest', 'discover', '-s', 'shop_tests', '-v', check=False)
                report['before'] = {'returncode': before.returncode, 'log': before.stdout + before.stderr}
                if before.returncode == 0:
                    raise RuntimeError('Expected the demo regression to fail')
                branch = 'assistant/' + job['id']
                command('git', 'checkout', '-b', branch)
                source = checkout / 'demo_shop/pricing.py'
                old = '(amount - (Decimal("1") - rate) * 10)'
                text = source.read_text(encoding='utf-8')
                if old not in text:
                    raise RuntimeError('Demo fixture changed')
                # Fixture patch, NOT an AI call. Live workflow uses Codex with DeepSeek.
                source.write_text(text.replace(old, '(amount * rate)').replace(
                    '    # Intentional demo bug: subtracting the rate instead of multiplying it.\n', ''), encoding='utf-8')
                after = command(sys.executable, '-m', 'unittest', 'discover', '-s', 'shop_tests', '-v', check=False)
                report['after'] = {'returncode': after.returncode, 'log': after.stdout + after.stderr}
                if after.returncode:
                    raise RuntimeError('Fixture repair did not pass regression tests')
                command('git', 'add', 'demo_shop/pricing.py')
                command('git', '-c', 'core.hooksPath=NUL' if os.name == 'nt' else 'core.hooksPath=/dev/null', 'commit', '-m', 'fix: correct discount calculation')
                report['branch'] = branch
                report['commit'] = command('git', 'rev-parse', 'HEAD').stdout.strip()
                report['diff'] = command('git', 'show', '--format=', 'HEAD').stdout

            def find_run(self, job):
                return {'id': 1, 'status': 'completed', 'conclusion': 'success',
                        'html_url': 'https://example.invalid/offline-run'}

            def run(self, run_id):
                return self.find_run(None)

            def find_pr(self, job):
                return 'https://example.invalid/offline-pr (本地模拟链接)'

        def send(chat, text, key):
            report['messages'].append({'chat': chat, 'text': text})
            print('[飞书通道模拟] ' + text)

        store = Store(':memory:')
        config = Config(repo='first24/AIHelper', users=frozenset({'demo-user'}))
        engine = Engine(config, store, LocalGitHub(), send)
        engine.receive('offline-demo-message', 'demo-user', 'demo-chat', '/修复 演示')
        for _ in range(3):
            engine.tick()
        job = store.get(report['branch'].split('/')[1])
        if job['status'] != 'done' or not job['notified']:
            raise RuntimeError('Offline flow did not complete')
        report['job_status'] = job['status']
        (root / 'demo-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        (root / 'demo-report.md').write_text(
            '# 本地演练结果\n\n模式：离线模拟。未连接真实飞书、GitHub 或模型 API。\n\n'
            '真实执行：复现错误 → 应用预设补丁 → 运行原始回归测试 → 创建本地 Git 分支和提交。\n\n'
            f"- 修复前测试退出码：{report['before']['returncode']}\n"
            f"- 修复后测试退出码：{report['after']['returncode']}（5 项通过）\n"
            f"- 分支：`{report['branch']}`\n- 本地提交：`{report['commit']}`\n"
            '- GitHub 工作流、PR 和飞书消息：使用模拟通道。线上版通过 Codex Action 调用 DeepSeek 修复。\n\n'
            '## 代码差异\n\n```diff\n' + report['diff'] + '\n```\n\n'
            '## 修复后测试记录\n\n```text\n' + report['after']['log'] + '\n```\n', encoding='utf-8')
        print('离线演练通过；完整记录：demo-report.md / demo-report.json')
