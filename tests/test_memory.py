"""Offline behavior tests. Synthetic data only; semantic tests use a fake provider."""
from dataclasses import replace
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from assistant.agent import Assistant
from assistant.core import Config
from assistant.mail import Mailer
from assistant.memory_context import ContextBuilder, TokenBudget, preview_result
from assistant.memory_jobs import MemoryJobs
from assistant.memory_search import MemorySearch, time_range
from assistant.memory_types import BudgetExceeded, MemoryConflict, MemorySettings, ProviderContextLimit, SUMMARY_FIELDS, encoded, valid_protocol
from assistant.memory_admin import backup_database
from assistant.state import State, user_zone
from assistant.tools import SPECS, Tools


class Model:
    def __init__(self, responses=()):
        self.responses, self.calls = list(responses), []

    def complete(self, messages):
        self.calls.append(messages)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def call(name, args, identity='c1'):
    return {'role': 'assistant', 'content': None, 'tool_calls': [{'id': identity, 'type': 'function', 'function': {'name': name, 'arguments': encoded(args)}}]}


class SummaryModel:
    model = 'fake-summary'
    def __init__(self, callback=None):
        self.callback, self.calls = callback, []

    def complete(self, messages, tools=True):
        assert tools is False
        self.calls.append(messages)
        events = json.loads(messages[-1]['content'])
        value = {'schema_version': 1, 'source_refs': [events[0]['id']], **{k: [] for k in SUMMARY_FIELDS}}
        value['goals'] = [{'text': '合成测试目标', 'source_refs': [events[0]['id']]}]
        if self.callback:
            self.callback(value)
        return {'role': 'assistant', 'content': encoded(value)}


class Embedding:
    provider, model, version, dimension = 'fake', 'fake-v1', '1', 2
    def __init__(self, callback=None):
        self.callback = callback
        self.calls = []

    def embed(self, texts):
        self.calls.append(texts)
        if self.callback:
            self.callback()
        return [[1., 0.] if '咖啡' in text or '饮品' in text else [0., 1.] for text in texts]


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.state = State(':memory:')
        self.addCleanup(self.state.db.close)
        self.zone = user_zone()
        self.now = datetime.fromisoformat('2026-11-17T10:00:00+08:00').timestamp()
        self.state.memory.clock = lambda: self.now
        self.model = Model()
        self.sent = []
        self.assistant = Assistant(Config('example/test', users=frozenset({'owner'})), self.state, self.model, Mailer(self.state), lambda *a: self.sent.append(a), self.zone, clock=lambda: self.now)

    def send(self, text, responses=(), mid=None, chat='chat'):
        mid = mid or 'm' + str(len(self.state.rows('SELECT id FROM messages_v2')))
        self.model.responses.extend(responses)
        with patch('assistant.state.time.time', return_value=self.now):
            self.assistant.receive(mid, 'owner', chat, text)
        self.assistant.tick()
        return self.state.memory.turn('owner', chat, mid)

    def tools(self, mid='direct', chat='chat', request=''):
        tools = Tools(self.state, 'owner', chat, mid, self.zone, clock=lambda: self.now)
        tools.request_text = request
        return tools

    def history(self, request='查以前历史', chat='chat'):
        return MemorySearch(self.state, 'owner', chat, request, self.zone)

    def drain(self, jobs=None):
        jobs = jobs or MemoryJobs(self.state, clock=lambda: self.now)
        for _ in range(100):
            if not jobs.run_one():
                return
        self.fail('memory jobs did not converge')

    def eight_turns(self):
        for i in range(8):
            self.send('合成历史问题' + str(i), [{'role': 'assistant', 'content': '合成回复' + str(i)}])

    def test_additive_migration_idempotency_legacy_time_and_foreign_keys(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / 'legacy.db')
            with closing(sqlite3.connect(path)) as db:
                db.execute('CREATE TABLE turns(id INTEGER PRIMARY KEY,owner TEXT,chat TEXT,body TEXT)')
                db.execute('INSERT INTO turns VALUES(1,?,?,?)', ('owner', 'chat', encoded([{'role': 'user', 'content': '旧轮原文'}])))
                db.execute('CREATE TABLE preferences(owner TEXT,key TEXT,value TEXT,PRIMARY KEY(owner,key))')
                db.execute('INSERT INTO preferences VALUES(?,?,?)', ('owner', 'language', '中文'))
                db.commit()
            one = State(path)
            count = one.memory.stats()['events']
            event = one.rows("SELECT event_at FROM memory_events WHERE source_type='legacy_turn'")[0]
            self.assertIsNone(event['event_at'])
            self.assertEqual(one.rows('PRAGMA foreign_keys')[0]['foreign_keys'], 1)
            one.db.close()
            two = State(path)
            try:
                self.assertEqual(two.memory.stats()['events'], count)
                self.assertEqual(two.rows('SELECT value FROM preferences')[0]['value'], '中文')
                self.assertEqual(two.rows('SELECT body FROM turns')[0]['body'], encoded([{'role': 'user', 'content': '旧轮原文'}]))
                self.assertEqual(two.rows('PRAGMA foreign_key_check'), [])
            finally:
                two.db.close()

    def test_enqueue_archive_is_atomic_and_duplicate_input_not_archived_twice(self):
        with patch.object(self.state.memory, 'begin', side_effect=RuntimeError('archive failed')):
            with self.assertRaises(RuntimeError):
                self.state.enqueue('atomic', 'owner', 'chat', '合成')
        self.assertEqual(self.state.rows('SELECT * FROM messages_v2'), [])
        self.state.enqueue('one', 'owner', 'chat', '/待办')
        self.state.enqueue('one', 'owner', 'chat', '/待办')
        self.assertEqual(len(self.state.rows("SELECT * FROM memory_events WHERE kind='user'")), 1)

    def test_command_list_snapshot_reaches_followup_without_live_reordering(self):
        t = self.tools()
        first = t.execute('create_reminder', {'text': '第一条', 'at': '2026-11-18T09:00:00+08:00'})
        second = t.execute('create_reminder', {'text': '第二条', 'at': '2026-11-18T10:00:00+08:00'})
        self.send('/提醒')
        self.assertEqual(self.model.calls, [])
        newer=t.execute('create_reminder',{'text':'新插入的更早提醒','at':'2026-11-18T08:00:00+08:00'})
        self.send('把刚才第二条改到十一点', [call('update_reminder', {'id': second['id'], 'at': '2026-11-18T11:00:00+08:00'}), {'role': 'assistant', 'content': '已修改'}])
        content = encoded(self.model.calls[0])
        self.assertIn(first['id'], content)
        self.assertIn(second['id'], content)
        self.assertNotIn(newer['id'],content)
        self.assertLess(content.index(first['id']),content.index(second['id']))
        self.assertIn('command_result', encoded(self.state.rows('SELECT kind FROM memory_events')))
        self.assertEqual(self.state.rows('SELECT count(*) AS n FROM schedules')[0]['n'], 3)
        self.assertTrue(valid_protocol(self.model.calls[0][1:]))

    def test_over_six_turns_retained_but_recent_context_bounded(self):
        self.eight_turns()
        self.assertEqual(len(self.state.rows('SELECT * FROM memory_turns')), 8)
        self.assertNotIn('合成历史问题0', encoded(self.state.history('owner', 'chat')))
        hit = self.history().search('合成历史问题0')
        self.assertEqual(len(hit['hits']), 1)
        evidence = self.history().read(hit['hits'][0]['id'])
        self.assertIn('合成历史问题0', evidence['excerpt'])
        self.assertFalse(hit['semantic_available'])

    def test_parallel_tool_results_are_paired_and_failures_preserve_success(self):
        response = call('add_todo', {'title': '合成待办'})
        response['tool_calls'].extend(call('list_todos', {}, 'c2')['tool_calls'])
        turn = self.send('记录待办', [response, RuntimeError('model offline')])
        events = self.state.memory.events('owner', turn['id'])
        results = [e for e in events if e['kind'] == 'tool_result']
        self.assertEqual([e['tool_call_id'] for e in results], ['c1', 'c2'])
        self.assertEqual(results[0]['payload']['status'], 'succeeded')
        self.assertIn('合成待办', self.sent[-1][1])
        self.assertTrue(valid_protocol(self.state.history('owner', 'chat')))

    def test_recovery_marks_unknown_and_does_not_execute_or_replay_orphan(self):
        self.state.enqueue('crash', 'owner', 'chat', '合成操作')
        self.state.claim_message()
        self.state.memory.record('owner', 'chat', 'crash', 'model:0', 'assistant', 'assistant', call('add_todo', {'title': '未确认的操作'}))
        tools = self.tools('crash')
        tools.trace_call_id = 'c1'
        self.state.memory.tool_event('owner', 'chat', 'crash', 'tool:start', 'add_todo', {'title': '未确认的操作'}, call_id='c1')
        self.state.recover()
        row = self.state.memory.turn('owner', 'chat', 'crash')
        self.assertEqual(row['status'], 'interrupted')
        self.assertEqual(self.state.rows('SELECT * FROM todos'), [])
        history = self.state.history('owner', 'chat')
        self.assertTrue(valid_protocol(history))
        self.assertIn('结果需查询', encoded(history))

    def test_budget_includes_schemas_and_rechecks_after_tools(self):
        self.send('记录测试待办', [call('add_todo', {'title': '预算测试'}), {'role': 'assistant', 'content': '完成'}])
        budgets = [json.loads(r['budget_json']) for r in self.state.rows('SELECT budget_json FROM memory_request_stats ORDER BY step')]
        self.assertEqual(len(budgets), 2)
        self.assertGreater(budgets[1]['messages'], budgets[0]['messages'])
        self.assertGreater(budgets[0]['tools'], 0)
        self.assertEqual(budgets[0]['count_quality'], 'estimated')
        self.assertTrue(all(r['fits'] for r in budgets))
        self.state.memory_settings = replace(self.state.memory_settings, app_token_limit=1000)
        before = len(self.model.calls)
        self.send('再记一条', [])
        self.assertEqual(len(self.model.calls), before)
        self.assertIn('预算', self.sent[-1][1])
        self.assertEqual(len(self.state.rows('SELECT * FROM todos')), 1)

    def test_long_results_remain_valid_json_and_large_payloads_paged(self):
        result = {'ok': True, 'id': 'e_real', 'status': 'unknown', 'body': '合成正文' * 30000}
        compact = preview_result(result, 'ev_handle')
        self.assertEqual(compact['status'], 'unknown')
        self.assertEqual(compact['id'], 'e_real')
        self.assertTrue(compact['has_more'])
        self.assertLess(len(encoded(compact)), 7000)
        self.state.enqueue('big', 'owner', 'chat', '长结果')
        identity = self.state.memory.record('owner', 'chat', 'big', 'big', 'metadata', 'command_result', result)
        self.assertTrue(self.state.rows('SELECT * FROM memory_payloads'))
        first = self.history().read(identity)
        self.assertTrue(first['has_more'])
        self.assertEqual(first['next_offset'], 4000)
        second = self.history().read(identity, offset=4000)
        self.assertNotEqual(first['excerpt'], second['excerpt'])

    def test_scope_id_guessing_time_and_cleared_epochs(self):
        turn = self.send('合成敏感记录', [{'role': 'assistant', 'content': '收到'}])
        event = self.state.memory.events('owner', turn['id'])[0]
        with self.assertRaises(ValueError):
            MemorySearch(self.state, 'other', 'chat', '查以前历史', self.zone).read(event['id'], scope='owner_history')
        with self.assertRaises(ValueError):
            self.history('你好', 'other-chat').read(event['id'], scope='owner_history')
        self.send('/清空对话')
        self.assertNotIn('合成敏感记录', encoded(self.state.history('owner', 'chat')))
        self.assertEqual(self.history().search('合成敏感记录', scope='owner_history')['hits'], [])
        with self.assertRaises(ValueError):
            self.history().search('合成敏感记录', scope='owner_history', include_cleared=True)
        hits = self.history('查已清空历史').search('合成敏感记录', scope='owner_history', include_cleared=True)
        self.assertTrue(hits['hits'])
        start, end, info = time_range('昨天', self.zone, self.now)
        self.assertEqual(datetime.fromtimestamp(start, self.zone).isoformat(), '2026-11-16T00:00:00+08:00')
        self.assertEqual(end - start, 86400)
        a, b, _ = time_range('上周', self.zone, self.now)
        self.assertEqual(datetime.fromtimestamp(a, self.zone).isoformat(), '2026-11-09T00:00:00+08:00')
        self.assertEqual(b - a, 7 * 86400)
        self.assertTrue(time_range('前几天', self.zone, self.now)[2]['fuzzy'])

    def test_explicit_memory_version_conflict_expiry_and_delete(self):
        turn = self.send('记住我喜欢咖啡', [call('save_memory', {'logical_key': 'drink', 'text': '我喜欢咖啡'}), {'role': 'assistant', 'content': '保存'}])
        items = self.state.memory.list_items('owner', 'chat')
        self.assertEqual(len(items), 1)
        first = items[0]
        with self.assertRaises(MemoryConflict):
            self.state.memory.save_item('owner', 'chat', 'x', 'drink', '茶', expected_version=0, identity=first['id'])
        with self.assertRaises(ValueError):
            self.tools(request='这次简单说').execute('save_memory', {'logical_key': 'style', 'text': '简单'})
        self.send('改成我喜欢茶', [call('update_memory', {'id': first['id'], 'expected_version': first['version'], 'text': '我喜欢茶'}), {'role': 'assistant', 'content': '更新'}])
        second = self.state.memory.list_items('owner', 'chat')[0]
        self.assertEqual(second['version'], 2)
        self.assertEqual(second['supersedes_id'], first['id'])
        self.drain()
        self.send('/遗忘记忆 ' + second['id'])
        self.assertEqual(self.state.memory.list_items('owner', 'chat', include_history=True), [])
        self.assertEqual(self.history().search('我喜欢咖啡', scope='owner_history')['hits'], [])
        self.assertNotIn('我喜欢茶', encoded(self.state.history('owner', 'chat')))
        self.assertFalse(self.state.rows("SELECT * FROM memory_source_revisions WHERE content_json LIKE '%我喜欢%'"))

    def test_note_delete_invalidates_derived_copies_and_exclusion_survives_reindex(self):
        turn = self.send('记一条笔记：合成秘密花园', [call('save_note', {'title': '测试笔记', 'body': '合成秘密花园'}), {'role': 'assistant', 'content': '保存'}])
        note = self.state.rows('SELECT id FROM notes')[0]['id']
        self.send('/查看笔记 ' + note)
        self.drain()
        self.assertTrue(self.history().search('合成秘密花园')['hits'])
        self.tools(request='停止用于记忆 '+note).execute('exclude_note_from_memory', {'id': note})
        self.assertEqual(self.history().search('合成秘密花园')['hits'], [])
        self.assertEqual(self.tools().get_note(note)['note']['body'], '合成秘密花园')
        self.send('/查看笔记 '+note)
        self.assertIn('合成秘密花园',self.sent[-1][1])
        self.assertNotIn('合成秘密花园',encoded(self.state.history('owner','chat')))
        self.assertEqual(self.history().search('合成秘密花园')['hits'],[])
        with self.state.transaction() as db:
            self.state.memory.enqueue_job(db, 'owner', 'index_source', 'note:' + note, 1)
        self.drain()
        self.assertEqual(self.history().search('合成秘密花园')['hits'], [])
        self.send('/删除笔记 ' + note)
        self.assertEqual(self.state.rows('SELECT * FROM notes'), [])
        self.assertNotIn('合成秘密花园', encoded(self.state.rows('SELECT payload_json FROM memory_events')))

    def test_preferences_versions_source_unknown_and_one_turn_exception(self):
        tools = self.tools(request='记住我喜欢简短回答')
        tools.execute('remember_preference', {'key': 'brief_style', 'value': '简短'})
        with self.assertRaises(MemoryConflict):
            self.tools('m2', request='记住我喜欢详细回答').execute('remember_preference', {'key': 'brief_style', 'value': '详细', 'expected_version': 0})
        with self.assertRaises(ValueError):
            self.tools('m3', request='这次简单说').execute('remember_preference', {'key': 'brief_style', 'value': '简单', 'expected_version': 1})
        self.tools('m4', request='记住我喜欢详细回答').execute('remember_preference', {'key': 'brief_style', 'value': '详细', 'expected_version': 1})
        self.assertEqual(self.state.rows('SELECT value FROM preferences')[0]['value'], '详细')
        versions = self.tools(request='以前的偏好').execute('get_preference_history', {'key': 'brief_style'})['versions']
        self.assertEqual([v['version'] for v in versions], [2, 1])

    def test_summary_complete_range_failure_and_revision_race(self):
        self.state.memory_settings = replace(self.state.memory_settings, summary=True)
        self.eight_turns()
        model = SummaryModel()
        self.drain(MemoryJobs(self.state, model, clock=lambda: self.now))
        rows = self.state.rows("SELECT * FROM memory_summaries WHERE status='active'")
        self.assertEqual(len(rows), 1)
        self.assertTrue(model.calls)
        messages, manifest, _ = ContextBuilder(self.state).build('owner', 'chat', '可信规则', [{'role': 'user', 'content': '新问题'}], SPECS)
        self.assertIn('旧摘要资料', encoded(messages))
        # New session revision during generation must never activate the old snapshot.
        self.state.execute("UPDATE memory_summaries SET status='invalid'")
        with self.state.transaction() as db:
            session = self.state.memory.session(db, 'owner', 'chat')
            self.state.memory.enqueue_job(db, 'owner', 'summary', session['id'], session['revision'] + 1)
        model = SummaryModel(lambda value: self.state.enqueue('concurrent', 'owner', 'chat', '并发消息'))
        jobs = MemoryJobs(self.state, model, clock=lambda: self.now)
        for _ in range(20):
            jobs.run_one()
            if model.calls:
                break
        self.assertEqual(self.state.rows("SELECT * FROM memory_summaries WHERE status='active'"), [])

    def test_summary_invalid_ids_and_delete_during_generation_rejected(self):
        self.state.memory_settings = replace(self.state.memory_settings, summary=True)
        self.eight_turns()
        def invented(value):
            value['entities'] = [{'text': 'r_invented', 'source_refs': value['source_refs']}]
        self.drain(MemoryJobs(self.state, SummaryModel(invented), clock=lambda: self.now))
        self.assertFalse(self.state.rows("SELECT * FROM memory_summaries WHERE status='active'"))
        self.assertTrue(self.state.rows("SELECT * FROM memory_jobs WHERE status='pending' AND error_code='ValueError'"))

    def test_semantic_fake_fusion_model_change_and_failure_fallback(self):
        self.state.memory_settings = replace(self.state.memory_settings, semantic_search=True)
        self.send('记住我喜欢咖啡', [call('save_memory', {'logical_key': 'drink', 'text': '我喜欢咖啡'}), {'role': 'assistant', 'content': '保存'}])
        provider = Embedding()
        self.drain(MemoryJobs(self.state, embedding_provider=provider, clock=lambda: self.now))
        result = self.history().search('饮品')
        self.assertTrue(result['semantic_available'])
        self.assertTrue(result['hits'])
        self.assertTrue(self.state.rows('SELECT * FROM memory_embeddings'))
        provider.model = 'other-space'
        self.assertEqual(self.history().search('饮品')['hits'], [])
        with patch.object(provider, 'embed', side_effect=RuntimeError('offline')):
            result = self.history().search('咖啡')
        self.assertFalse(result['semantic_available'])
        self.assertTrue(result['hits'])

    def test_read_tools_are_not_effect_cached_and_keyword_query_is_literal(self):
        self.send('合成100%_\\测试', [{'role': 'assistant', 'content': '收到'}])
        tools = self.tools(request='查以前历史')
        self.assertTrue(tools.execute('search_history', {'query': '%_\\'})['hits'])
        before = len(self.state.rows('SELECT * FROM effects'))
        tools.execute('search_history', {'query': '没有匹配'})
        self.assertEqual(len(self.state.rows('SELECT * FROM effects')), before)

    def test_session_deletion_preview_revision_and_business_retention(self):
        self.send('合成会话内容', [call('add_todo', {'title': '保留业务待办'}), {'role': 'assistant', 'content': '完成'}])
        self.send('/会话记录')
        identity = re.search(r'ms_[0-9a-f]+', self.sent[-1][1])[0]
        revision = int(re.search(r'预览版本 (\d+)', self.sent[-1][1])[1])
        self.send(f'/删除会话记录 {identity} {revision}')
        self.assertIn('已清理', self.sent[-1][1])
        self.assertNotIn('合成会话内容', encoded(self.state.rows('SELECT payload_json FROM memory_events')))
        self.assertEqual(len(self.state.rows('SELECT * FROM todos')), 1)
        self.assertNotEqual(self.state.memory.current_session('owner', 'chat')['id'], identity)

    def test_preference_forget_removes_authority_history_and_effect_copies(self):
        self.send('记住我的签名是合成签名', [call('remember_preference', {'key':'email_signature','value':'合成签名'}), {'role':'assistant','content':'收到'}])
        self.send('以前的签名偏好',[call('get_preference_history',{'key':'email_signature'}),{'role':'assistant','content':'合成签名'}])
        self.drain()
        self.send('删除这条记忆 pref:email_signature', [call('forget_memory', {'id':'pref:email_signature'})])
        self.assertFalse(self.state.rows('SELECT * FROM preferences'))
        self.assertNotIn('合成签名', encoded(self.state.rows('SELECT content_json FROM memory_source_revisions')))
        self.assertNotIn('合成签名', encoded(self.state.rows('SELECT result FROM effects')))
        self.assertNotIn('合成签名', encoded(self.state.rows('SELECT text,reply FROM messages_v2')))
        self.assertNotIn('合成签名', encoded(self.state.rows('SELECT text FROM memory_segments')))
        self.assertFalse(self.history().search('合成签名',scope='owner_history')['hits'])

    def test_owner_memories_cross_chats_session_memories_do_not(self):
        one=self.state.memory.save_item('owner','chat','manual','shared','合成共享事实')
        two=self.state.memory.save_item('owner','chat','manual','private','合成会话事实',scope='session')
        other=self.state.memory.list_items('owner','other-chat')
        self.assertEqual([i['id'] for i in other],[one['id']])
        with self.assertRaises(ValueError):
            self.state.memory.get_item('stranger','chat',one['id'])
        with self.assertRaises(ValueError):
            self.state.memory.get_item('owner','other-chat',two['id'])

    def test_retention_expiry_payload_truth_and_unaffected_memory(self):
        first=self.send('需要到期的历史', [{'role':'assistant','content':'过去'}])
        self.state.execute('UPDATE memory_turns SET received_at=? WHERE id=?',(self.now-91*86400,first['id']))
        self.state.enqueue('payload','owner','chat','大正文')
        identity=self.state.memory.record('owner','chat','payload','data','metadata','command_result',{'body':'合成正文'*30000})
        self.state.execute('UPDATE memory_payloads SET created_at=?',(self.now-8*86400,))
        item=self.state.memory.save_item('owner','chat','direct','temp','短暂有效',valid_until=self.now+1)
        preserved=self.state.memory.save_item('owner','chat','direct','keep','长期保留')
        self.now+=2
        with self.assertRaises(ValueError):
            self.history('你好').read(item['id'])
        MemoryJobs(self.state,clock=lambda:self.now).maintain()
        self.assertEqual(self.state.memory.events('owner',first['id']),[])
        self.assertIn('payload_retention_expired',self.history().read(identity)['excerpt'])
        self.assertEqual([i['id'] for i in self.state.memory.list_items('owner','chat')],[preserved['id']])

    def test_source_version_check_prevents_stale_read_and_deleted_index_commit(self):
        tools=self.tools()
        note=tools.execute('save_note',{'title':'版本测试','body':'合成咖啡'})['id']
        evidence=self.history().read(note)
        self.tools('edit').execute('update_note',{'id':note,'body':'合成红茶'})
        with self.assertRaises(ValueError):
            self.history().read(note,expected_version=evidence['version'])
        self.state.memory_settings=replace(self.state.memory_settings,semantic_search=True)
        def remove():
            if self.state.rows('SELECT id FROM notes WHERE id=?',(note,)):
                self.tools('delete').execute('delete_note',{'id':note})
        provider=Embedding(remove)
        self.drain(MemoryJobs(self.state,embedding_provider=provider,clock=lambda:self.now))
        self.assertFalse(self.state.rows("SELECT * FROM memory_segments WHERE status='active' AND source_id=?",(note,)))
        self.assertFalse(self.state.rows('SELECT * FROM memory_embeddings'))

    def test_summary_generation_cannot_commit_after_source_deletion(self):
        self.state.memory_settings=replace(self.state.memory_settings,summary=True)
        self.send('记一条合成笔记', [call('save_note',{'title':'摘要来源','body':'合成被删资料'}),{'role':'assistant','content':'收到'}])
        note=self.state.rows('SELECT id FROM notes')[0]['id']
        for i in range(7):
            self.send('后续合成问题'+str(i),[{'role':'assistant','content':'收到'}])
        def remove(value):
            if self.state.rows('SELECT id FROM notes WHERE id=?',(note,)):
                self.tools('delete').execute('delete_note',{'id':note})
        model=SummaryModel(remove)
        self.drain(MemoryJobs(self.state,model,clock=lambda:self.now))
        # A later job may summarize the unaffected second turn, never the deleted first.
        for summary in self.state.rows("SELECT * FROM memory_summaries WHERE status='active'"):
            self.assertGreater(summary['from_seq'],1)
            self.assertNotIn('合成被删资料',summary['content_json'])
        self.assertEqual(self.history().search('合成被删资料')['hits'],[])

    def test_independent_deletion_ledger_applied_to_old_backup_before_use(self):
        with tempfile.TemporaryDirectory() as folder:
            live=Path(folder)/'live.db'
            snapshot=Path(folder)/'older.db'
            one=State(str(live))
            note=Tools(one,'owner','chat','seed',self.zone).execute('save_note',{'title':'备份测试','body':'合成需要遗忘的正文'})['id']
            backup_database(live,snapshot)
            Tools(one,'owner','chat','delete',self.zone).execute('delete_note',{'id':note})
            ledger=Path(folder)/'memory-deletions.jsonl'
            self.assertNotIn('合成需要遗忘的正文',ledger.read_text(encoding='utf-8'))
            one.db.close()
            restored=State(str(snapshot))
            try:
                self.assertEqual(restored.rows('SELECT * FROM notes'),[])
                self.assertNotIn('合成需要遗忘的正文',encoded(restored.rows('SELECT content_json FROM memory_source_revisions')))
                self.assertEqual(restored.memory.apply_deletion_ledger(),0)
                self.assertFalse(restored.rows('PRAGMA foreign_key_check'))
            finally:
                restored.db.close()

    def test_provider_overflow_retries_only_context_not_successful_action(self):
        self.send('记录合成待办',[call('add_todo',{'title':'仅创建一次'}),ProviderContextLimit('limit'),{'role':'assistant','content':'完成'}])
        self.assertEqual(len(self.state.rows('SELECT * FROM todos')),1)
        self.assertEqual(len(self.model.calls),3)
        self.assertTrue(valid_protocol(self.model.calls[-1][1:]))
        self.assertIn('仅创建一次',encoded(self.model.calls[-1]))
        self.assertTrue(self.state.rows("SELECT * FROM memory_request_stats WHERE budget_json LIKE '%provider_context_limit%'"))

    def test_codex_handoff_is_opt_in_rechecked_and_contains_no_other_history(self):
        from assistant.codex_jobs import CodexJobs
        self.send('无关的合成私人资料',[{'role':'assistant','content':'收到'}])
        turn=self.send('必要的合成任务约束',[{'role':'assistant','content':'收到'}])
        event=self.state.memory.events('owner',turn['id'])[0]['id']
        with patch.dict('os.environ',{'CODEX_PROJECTS_JSON':'{"aihelper":"Test"}'}):
            queue=CodexJobs(self.assistant.config,self.state,clock=lambda:self.now)
            with self.assertRaises(ValueError):
                queue.submit('owner','chat','not-authorized','检查项目',evidence_id=event)
            job=queue.submit('owner','chat','selected','引用证据 '+event+' 检查项目',evidence_id=event)
            claimed=queue.claim('hp-local',['aihelper'])['job']
            self.assertIn('必要的合成任务约束',claimed['prompt'])
            self.assertNotIn('无关的合成私人资料',claimed['prompt'])
            queue.finish('hp-local',job['id'],claimed['lease'],'done','Synthetic result')
            next_job=queue.submit('owner','chat','selected-again','引用证据 '+event+' 检查项目',evidence_id=event)
            with self.state.transaction() as db:
                self.state.memory.tombstone(db,'owner','turn',turn['id'])
                self.state.memory._invalidate_turns(db,'owner',[turn['id']],True)
            claimed=queue.claim('hp-local',['aihelper'])['job']
            self.assertEqual(claimed['id'],next_job['id'])
            self.assertNotIn('必要的合成任务约束',claimed['prompt'])
            self.assertIn('背景已失效',claimed['prompt'])

    def test_scan_limit_reports_partial_and_latest_source_filter_is_permission_first(self):
        self.eight_turns()
        self.state.memory_settings=replace(self.state.memory_settings,scan_limit=2)
        result=self.history().search('合成')
        self.assertEqual(result['coverage'],'partial')
        self.assertLessEqual(len(result['hits']),2)

    def test_summary_rejects_claiming_success_from_failed_tool(self):
        from assistant.memory_types import validate_summary
        event={'id':'ev_test','kind':'tool_result','payload':{'status':'failed','result':{'ok':False}}}
        value={'schema_version':1,'source_refs':['ev_test'],**{k:[] for k in SUMMARY_FIELDS}}
        value['observed_actions']=[{'text':'已完成','status':'succeeded','source_refs':['ev_test']}]
        with self.assertRaises(ValueError):
            validate_summary(value,[event])

    def test_already_queued_followups_move_into_new_epoch_after_reset(self):
        self.send('旧周期合成资料',[{'role':'assistant','content':'收到'}])
        self.assistant.receive('reset','owner','chat','/清空对话')
        self.assistant.receive('after-reset','owner','chat','新周期第一问')
        self.assistant.receive('after-reset-two','owner','chat','新周期第二问')
        self.model.responses.extend([{'role':'assistant','content':'新周期第一答'},{'role':'assistant','content':'新周期第二答'}])
        self.assistant.tick()
        self.assistant.tick()
        self.assistant.tick()
        active=self.state.memory.current_session('owner','chat')
        self.assertEqual(self.state.memory.turn('owner','chat','after-reset')['session_id'],active['id'])
        self.assertNotIn('旧周期合成资料',encoded(self.model.calls[-1]))
        self.assertIn('新周期第一问',encoded(self.model.calls[-1]))

    def test_summary_watermark_never_spans_unselected_queued_inputs(self):
        self.state.memory_settings=replace(self.state.memory_settings,summary=True)
        for i in range(8):
            self.assistant.receive('queued-'+str(i),'owner','chat','批量排队'+str(i))
            self.model.responses.append({'role':'assistant','content':'合成回复'})
        for _ in range(8): self.assistant.tick()
        self.drain(MemoryJobs(self.state,SummaryModel(),clock=lambda:self.now))
        self.assertFalse(self.state.rows("SELECT * FROM memory_summaries WHERE status='active'"))
        for i in range(6): self.send('后续消息'+str(i),[{'role':'assistant','content':'收到'}])
        self.drain(MemoryJobs(self.state,SummaryModel(),clock=lambda:self.now))
        summaries=self.state.rows("SELECT * FROM memory_summaries WHERE status='active'")
        self.assertTrue(summaries)
        for summary in summaries:
            events=self.state.rows('SELECT id,turn_id FROM memory_events WHERE session_id=? AND seq>=? AND seq<=?',(summary['session_id'],summary['from_seq'],summary['upto_seq']))
            refs={r['source_id'] for r in self.state.rows("SELECT source_id FROM memory_sources WHERE derived_kind='summary' AND derived_id=?",(summary['id'],))}
            self.assertEqual({e['id'] for e in events},refs)

    def test_effect_cache_is_owner_and_chat_scoped(self):
        args={'title':'合成待办'}
        one=self.tools('same','chat').execute('add_todo',args)
        other=Tools(self.state,'stranger','chat','same',self.zone,clock=lambda:self.now).execute('add_todo',args)
        third=self.tools('same','other-chat').execute('add_todo',args)
        self.assertEqual(len({one['id'],other['id'],third['id']}),3)

    def test_archive_and_pending_jobs_survive_reopen_without_legacy_duplication(self):
        with tempfile.TemporaryDirectory() as folder:
            path=str(Path(folder)/'restart.db')
            first=State(path)
            first.enqueue('persist','owner','chat','合成重启记录')
            turn=first.memory.turn('owner','chat','persist')
            with first.transaction() as db:
                first.memory.finish(db,turn['id'],'合成结果')
                db.execute("UPDATE messages_v2 SET status='ready',reply='合成结果' WHERE id='persist'")
            count=first.memory.stats()['events']
            first.db.close()
            second=State(path)
            try:
                self.assertEqual(second.memory.stats()['events'],count)
                self.assertTrue(MemorySearch(second,'owner','chat','查以前历史',self.zone).search('合成重启记录')['hits'])
                jobs=MemoryJobs(second)
                while jobs.run_one(): pass
                self.assertTrue(second.rows("SELECT * FROM memory_segments WHERE status='active'"))
                self.assertFalse(second.rows('PRAGMA foreign_key_check'))
            finally:
                second.db.close()

    def test_hidden_reasoning_is_never_archived_and_unknown_mail_remains_unknown(self):
        response=call('list_todos',{})
        response['reasoning_content']='hidden-not-for-storage'
        response['tool_calls'][0]['reasoning_content']='also-hidden'
        self.send('查询待办',[response,{'role':'assistant','content':'完成','reasoning_content':'third-hidden'}])
        self.assertNotIn('hidden',encoded(self.state.rows('SELECT payload_json FROM memory_events')))
        self.state.enqueue('send-status','owner','chat','合成发送命令')
        self.state.memory.tool_event('owner','chat','send-status','send:result','explicit_send_email',{'id':'e_synthetic'}, {'status':'unknown','receipt':'结果需核查'})
        turn=self.state.memory.turn('owner','chat','send-status')
        result=[e for e in self.state.memory.events('owner',turn['id']) if e['kind']=='command_result'][0]
        self.assertEqual(result['payload']['status'],'unknown')

    def test_corrupt_deletion_ledger_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'test.db'
            state=State(str(path))
            state.db.close()
            (Path(folder)/'memory-deletions.jsonl').write_text('{broken}\n',encoding='utf-8')
            with self.assertRaises(ValueError):
                State(str(path))

    def test_derived_job_lease_and_embedding_retry_preserve_keyword_fallback(self):
        self.tools().execute('save_note',{'title':'合成索引','body':'合成咖啡'})
        jobs=MemoryJobs(self.state,clock=lambda:self.now)
        first=jobs.claim()
        self.assertIsNotNone(first)
        self.assertIsNone(jobs.claim())
        self.now+=181
        second=jobs.claim()
        self.assertEqual(second['id'],first['id'])
        self.assertNotEqual(second['lease_token'],first['lease_token'])
        self.state.execute("UPDATE memory_jobs SET status='pending',lease_until=0 WHERE id=?",(first['id'],))
        self.state.memory_settings=replace(self.state.memory_settings,semantic_search=True)
        provider=Embedding()
        jobs=MemoryJobs(self.state,embedding_provider=provider,clock=lambda:self.now)
        with patch.object(provider,'embed',side_effect=RuntimeError('provider unavailable')):
            jobs.run_one()
        self.assertTrue(self.state.rows("SELECT * FROM memory_segments WHERE status='active'"))
        self.assertTrue(self.state.rows("SELECT * FROM memory_jobs WHERE status='pending' AND error_code='EmbeddingUnavailable'"))
        self.assertTrue(self.history().search('咖啡')['hits'])
        self.now+=3601
        self.drain(jobs)
        self.assertTrue(self.state.rows('SELECT * FROM memory_embeddings'))

    def test_ambiguous_forget_requires_selection_and_old_unaffected_events_still_read(self):
        one=self.state.memory.save_item('owner','chat','direct','one','合成事实一')
        two=self.state.memory.save_item('owner','chat','direct','two','合成事实二')
        self.send('/记忆')
        with self.assertRaises(ValueError):
            self.tools(request='忘记刚才那个').execute('forget_memory',{'id':one['id']})
        self.assertEqual(len(self.state.memory.list_items('owner','chat')),2)
        self.send('/遗忘记忆 '+one['id'])
        self.assertEqual([i['id'] for i in self.state.memory.list_items('owner','chat')],[two['id']])
        self.assertTrue(self.history().search('合成事实二')['hits'])

    def test_search_never_uses_its_current_question_as_historical_evidence(self):
        self.send('以前说过合成不存在的地址吗',[call('search_history',{'query':'合成不存在的地址','scope':'owner_history'}),{'role':'assistant','content':'没有可确认的历史证据'}])
        tool=json.loads(self.model.calls[-1][-1]['content'])
        self.assertEqual(tool['hits'],[])

    def test_many_long_memories_use_bounded_previews_without_blocking_requests(self):
        for i in range(20):
            self.state.memory.save_item('owner','chat','direct','long-'+str(i),'合成长期资料'*500)
        messages,manifest,metrics=ContextBuilder(self.state).build('owner','chat','可信规则',[{'role':'user','content':'今天有什么安排'}],SPECS)
        self.assertTrue(metrics['fits'])
        self.assertLessEqual(len(manifest['source_refs']),5)
        self.assertIn('full_read_handle',encoded(messages))
        self.assertIn('has_more_memories',encoded(messages))


if __name__ == '__main__':
    unittest.main()
