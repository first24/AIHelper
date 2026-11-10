import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from assistant.core import Config, Engine, Store


class FakeGitHub:
    def __init__(self):
        self.dispatched = []
        self.result = None
        self.pr = None
        self.fail_dispatch = False
        self.pr_creations = 0

    def dispatch(self, job):
        self.dispatched.append(job['id'])
        if self.fail_dispatch:
            raise TimeoutError('response lost')

    def find_run(self, job):
        return self.result

    def run(self, run_id):
        return self.result

    def find_pr(self, job):
        return self.pr

    def create_pr(self, job, run):
        self.pr_creations += 1
        return self.pr


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.config = Config(repo='first24/AIHelper', users=frozenset({'owner'}))
        self.store = Store(':memory:')
        self.github = FakeGitHub()
        self.messages = []
        self.engine = Engine(self.config, self.store, self.github, lambda chat, text, key: self.messages.append((chat, text, key)))

    def receive(self, mid='m1', sender='owner', text='/修复 演示'):
        self.engine.receive(mid, sender, 'chat1', text)

    def complete(self, conclusion='success'):
        self.github.result = {'id': 42, 'status': 'completed', 'conclusion': conclusion, 'html_url': 'https://github.com/example/run/42'}

    def test_unknown_user_cannot_dispatch(self):
        self.receive(sender='stranger')
        self.engine.tick()
        self.assertEqual(self.github.dispatched, [])
        self.assertEqual(self.messages, [])

    def test_duplicate_events_dispatch_once(self):
        self.receive()
        self.receive()
        self.engine.tick()
        self.engine.tick()
        self.assertEqual(len(self.github.dispatched), 1)
        self.assertEqual(len(self.messages), 1)

    def test_full_flow(self):
        self.receive()
        self.engine.tick()
        self.complete()
        self.github.pr = 'https://github.com/first24/AIHelper/pull/1'
        self.engine.tick()
        self.engine.tick()
        job = self.store.get(self.github.dispatched[0])
        self.assertEqual(job['status'], 'done')
        self.assertEqual(job['notified'], 1)
        self.assertIn(self.github.pr, self.messages[-1][1])

    def test_bot_creates_pr_after_verified_branch(self):
        self.receive()
        self.engine.tick()
        self.complete()
        self.github.create_pr = lambda job, run: 'https://github.com/example/pr/new'
        self.engine.tick()
        self.assertEqual(self.store.get(self.github.dispatched[0])['status'], 'done')

    def test_failed_workflow_does_not_create_pr(self):
        self.receive()
        self.engine.tick()
        self.complete('failure')
        self.engine.tick()
        self.assertEqual(self.github.pr_creations, 0)

    def test_failed_tests_never_report_success(self):
        self.receive()
        self.engine.tick()
        self.complete('failure')
        self.engine.tick()
        self.engine.tick()
        self.assertEqual(self.store.get(self.github.dispatched[0])['status'], 'failed')
        self.assertNotIn('修复完成', self.messages[-1][1])

    def test_success_without_pr_is_failure(self):
        self.receive()
        self.engine.tick()
        self.complete()
        self.engine.tick()
        self.assertEqual(self.store.get(self.github.dispatched[0])['status'], 'failed')

    def test_ambiguous_dispatch_does_not_duplicate(self):
        self.github.fail_dispatch = True
        self.receive()
        self.engine.tick()
        self.engine.tick()
        self.assertEqual(len(self.github.dispatched), 1)
        self.complete()
        self.github.pr = 'https://github.com/example/pr/1'
        self.engine.tick()
        self.assertEqual(self.store.get(self.github.dispatched[0])['status'], 'done')

    def test_notification_is_retried(self):
        self.receive()
        self.engine.tick()
        self.complete()
        self.github.pr = 'https://github.com/example/pr/1'
        self.engine.tick()
        original = self.engine.send
        self.engine.send = lambda *args: (_ for _ in ()).throw(TimeoutError())
        self.engine.tick()
        self.assertEqual(self.store.get(self.github.dispatched[0])['notified'], 0)
        self.engine.send = original
        self.engine.tick()
        self.assertEqual(self.store.get(self.github.dispatched[0])['notified'], 1)

    def test_status_private_to_task_owner(self):
        self.receive()
        job = self.store.pending()[0]
        config = replace(self.config, users=frozenset({'owner', 'other'}))
        self.engine.config = config
        self.engine.receive('status', 'other', 'chat2', '/状态 ' + job['id'])
        self.assertIn('未找到', self.messages[-1][1])

    def test_timeout(self):
        self.engine.config = replace(self.config, timeout=-1)
        self.receive()
        self.engine.tick()
        self.engine.tick()
        self.assertEqual(self.store.get(self.github.dispatched[0])['status'], 'failed')

    def test_restart_resumes_waiting_without_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'state.db')
            first = Store(path)
            engine = Engine(self.config, first, self.github, self.engine.send)
            engine.receive('durable', 'owner', 'chat', '/修复 演示')
            engine.tick()
            first.conn.close()
            second = Store(path)
            engine = Engine(self.config, second, self.github, self.engine.send)
            engine.tick()
            self.assertEqual(len(self.github.dispatched), 1)
            second.conn.close()

    def test_backlog_limit(self):
        for index in range(6):
            self.receive(mid=str(index))
        self.assertEqual(len(self.store.pending()), 5)
        self.assertIn('已达', self.messages[-1][1])

    def test_only_one_dispatched_task_at_a_time(self):
        self.receive(mid='first')
        self.receive(mid='second')
        self.engine.tick()
        self.assertEqual(len(self.github.dispatched), 1)
        self.complete()
        self.github.pr = 'https://github.com/example/pr/1'
        self.engine.tick()
        self.assertEqual(len(self.github.dispatched), 2)

    def test_missing_credentials_fail_closed(self):
        with self.assertRaises(ValueError):
            self.config.validate()
