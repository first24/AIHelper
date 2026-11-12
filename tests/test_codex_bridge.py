from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

from assistant.codex_jobs import CodexJobs
from assistant.core import Config
from assistant.local_codex import Worker,clean_env,redact
from assistant.state import State


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.environment=patch.dict(os.environ,{'CODEX_PROJECTS_JSON':'{"aihelper":"AIHelper"}','CODEX_RUNNER_ID':'hp-local'})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.state=State(':memory:')
        self.addCleanup(self.state.db.close)
        self.now=1000
        self.service=CodexJobs(Config('first24/AIHelper',users=frozenset({'owner'})),self.state,clock=lambda:self.now)

    def submit(self,message='message'):
        return self.service.submit('owner','weixin:owner',message,'Review the project')['id']

    def claim(self):
        return self.service.claim('hp-local',['aihelper'])['job']

    def test_request_owner_registry_and_idempotency(self):
        id=self.submit()
        self.assertEqual(id,self.submit())
        self.assertEqual(len(self.state.rows('SELECT * FROM codex_jobs')),1)
        with self.assertRaises(ValueError): self.service.get('other',id)
        with self.assertRaises(ValueError): self.service.submit('other','chat','id','Task')
        with self.assertRaises(ValueError): self.service.submit('owner','chat','id','Task','unknown')
        self.assertFalse(self.service.connection()['online'])

    def test_one_claim_lease_check_and_durable_finish(self):
        id=self.submit()
        job=self.claim()
        self.assertEqual(job['id'],id)
        self.assertIsNone(self.claim())
        with self.assertRaises(ValueError): self.service.heartbeat('hp-local',id,'wrong')
        self.service.heartbeat('hp-local',id,job['lease'],session_id='12345678-1234-1234-1234-123456789abc')
        self.service.finish('hp-local',id,job['lease'],'done','Done')
        self.service.finish('hp-local',id,job['lease'],'done','Done')
        self.assertEqual(self.service.get('owner',id)['status'],'done')
        self.assertEqual(len(self.state.rows("SELECT * FROM deliveries WHERE id LIKE '%:finish'")),1)
        self.assertNotIn('lease',json.dumps(self.service.get('owner',id)))

    def test_cancel_running_kills_at_next_heartbeat(self):
        id=self.submit()
        job=self.claim()
        self.service.cancel('owner',id)
        self.assertTrue(self.service.heartbeat('hp-local',id,job['lease'])['stop'])
        self.service.finish('hp-local',id,job['lease'],'done','Work retained')
        self.assertEqual(self.service.get('owner',id)['status'],'canceled')

    def test_offline_queued_and_lease_expiry_never_reexecute(self):
        id=self.submit()
        self.now+=10000
        self.service.expire()
        self.assertEqual(self.service.get('owner',id)['status'],'queued')
        job=self.claim()
        self.now+=181
        self.service.expire()
        self.assertEqual(self.service.get('owner',id)['status'],'interrupted')
        self.assertIsNone(self.claim())
        self.service.finish('hp-local',id,job['lease'],'failed','Recovered result')
        self.assertEqual(self.service.get('owner',id)['status'],'failed')

    def test_continue_requires_completed_owned_session(self):
        id=self.submit()
        with self.assertRaises(ValueError): self.service.submit('owner','chat','next','Continue',parent_id=id)
        job=self.claim()
        self.service.finish('hp-local',id,job['lease'],'done','Done',session_id='12345678-1234-1234-1234-123456789abc',workdir='C:/tasks/'+id)
        next=self.service.submit('owner','chat','next','Continue',parent_id=id)
        self.assertEqual(self.claim()['session_id'],'12345678-1234-1234-1234-123456789abc')
        with self.assertRaises(ValueError): self.service.submit('owner','chat','third','Continue',parent_id=id)

    def test_pending_survives_database_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path=str(Path(directory)/'db.sqlite3')
            first=State(path)
            CodexJobs(self.service.config,first).submit('owner','chat','persist','Test')
            first.db.close()
            second=State(path)
            try:
                self.assertIsNotNone(CodexJobs(self.service.config,second).claim('hp-local',['aihelper'])['job'])
            finally:
                second.db.close()


class WorkerTests(unittest.TestCase):
    def test_environment_does_not_inherit_service_credentials(self):
        with patch.dict(os.environ,{'GH_TOKEN':'secret','SMTP_AUTH_CODE':'secret','DEEPSEEK_API_KEY':'secret','CODEX_THREAD_ID':'original','USERPROFILE':'C:/test'}):
            environment=clean_env()
        for key in ('GH_TOKEN','SMTP_AUTH_CODE','DEEPSEEK_API_KEY','CODEX_THREAD_ID'):
            self.assertNotIn(key,environment)
        self.assertEqual(environment['USERPROFILE'],'C:/test')

    def test_redaction_and_cli_arguments(self):
        self.assertNotIn('ghp_abcdef123456',redact('value ghp_abcdef123456'))
        worker=Worker.__new__(Worker)
        worker.settings={'codex_executable':'C:/codex.exe'}
        command=worker.command({'workdir':'C:/tasks/one','session_id':''})
        self.assertIn('workspace-write',command)
        self.assertNotIn('--dangerously-bypass-approvals-and-sandbox',command)
        self.assertEqual(command[-1],'-')
        self.assertNotIn('user prompt',command)

    def test_parent_directory_cannot_escape_task_root(self):
        with tempfile.TemporaryDirectory() as directory:
            worker=Worker.__new__(Worker)
            worker.tasks=Path(directory)/'tasks'
            worker.jobs=Path(directory)/'jobs'
            worker.jobs.mkdir()
            parent=worker.jobs/'cx_parent'
            parent.mkdir()
            (parent/'record.json').write_text(json.dumps({'workdir':directory,'project':'aihelper','session_id':'session'}))
            with self.assertRaises(ValueError):
                worker.workdir({'parent_id':'cx_parent','project':'aihelper','session_id':'session'}, {})

    def test_recovered_results_are_reported_without_executing(self):
        with tempfile.TemporaryDirectory() as directory:
            worker=Worker.__new__(Worker)
            worker.jobs=Path(directory)
            folder=worker.jobs/'cx_job'
            folder.mkdir()
            record={'id':'cx_job','state':'running','lease':'test','workdir':'C:/task'}
            (folder/'record.json').write_text(json.dumps(record))
            worker.report=Mock()
            worker.recover()
            self.assertEqual(worker.report.call_args.args[1]['status'],'failed')
            self.assertIn('没有自动重跑',worker.report.call_args.args[1]['result'])


if __name__=='__main__':
    unittest.main()
