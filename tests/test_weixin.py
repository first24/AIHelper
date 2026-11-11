import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from assistant.agent import Assistant
from assistant.core import Config
from assistant.mail import Mailer
from assistant.state import State, user_zone
from assistant.weixin import WeixinAPI, WeixinChannel, WeixinError, api_origin, load_account, write_private_json


class WeixinTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.folder.name) / 'assistant.sqlite3')
        self.state = State(self.db_path)
        self.config = Config(repo='first24/AIHelper', users=frozenset({'owner'}), db=self.db_path)
        self.account = {'bot_token': 'test-token', 'ilink_bot_id': 'bot',
                        'ilink_user_id': 'peer', 'baseurl': 'https://ilinkai.weixin.qq.com'}
        write_private_json(Path(self.folder.name) / 'weixin/account.json', self.account)
        self.channel = WeixinChannel(self.config, self.state)
        self.chat = self.channel.chat_id(self.account)

    def tearDown(self):
        self.state.db.close()
        self.folder.cleanup()

    def update(self, text='/提醒', **extra):
        message = {'message_id': '18446744073709551615', 'from_user_id': 'peer',
                   'to_user_id': 'bot', 'message_type': 1, 'message_state': 2,
                   'context_token': 'private-context',
                   'item_list': [{'type': 1, 'text_item': {'text': text}}]}
        message.update(extra)
        return {'ret': 0, 'msgs': [message], 'get_updates_buf': 'cursor-1'}

    def test_authenticated_wire_format_and_error_redaction(self):
        api = WeixinAPI(token='test-token')
        with patch.object(api.opener, 'open', return_value=io.BytesIO(b'{"ret":0,"msgs":[]}')) as mocked:
            api.request('ilink/bot/getupdates', {'get_updates_buf': ''})
        request = mocked.call_args.args[0]
        self.assertEqual(request.get_header('Authorization'), 'Bearer test-token')
        payload = json.loads(request.data)
        self.assertEqual(payload['base_info']['bot_agent'], 'AIHelper/2.1')
        self.assertTrue(payload['base_info']['channel_version'].startswith('3.0.0/'))
        with patch.object(api.opener, 'open', return_value=io.BytesIO(b'{"ret":-14,"errmsg":"test-token"}')):
            with self.assertRaises(WeixinError) as caught:
                api.request('ilink/bot/getupdates', {})
        self.assertEqual(caught.exception.code, -14)
        self.assertNotIn('test-token', str(caught.exception))

    def test_api_origin_rejects_untrusted_redirects(self):
        for url in ('http://ilinkai.weixin.qq.com', 'https://evil.example',
                    'https://ilinkai.weixin.qq.com.evil.example',
                    'https://user@ilinkai.weixin.qq.com', 'https://ilinkai.weixin.qq.com/path'):
            with self.assertRaises(ValueError):
                api_origin(url)

    def test_account_load_keeps_credentials_local(self):
        self.assertEqual(load_account(self.db_path), self.account)
        self.assertFalse((Path(self.folder.name) / 'weixin/account.tmp').exists())

    def test_only_scanning_owner_and_direct_user_messages_accepted(self):
        for extra in ({'from_user_id': 'stranger'}, {'message_type': 2},
                      {'group_id': 'group'}, {'to_user_id': 'another-bot'}, {'message_state': 1}):
            self.channel.ingest(self.account, self.update(**extra))
        self.assertEqual(self.state.rows('SELECT * FROM weixin_inbox'), [])

    def test_restart_recovers_pending_cursor_and_deduplicates_message(self):
        self.channel.ingest(self.account, self.update())
        self.state.db.close()
        self.state = State(self.db_path)
        self.channel = WeixinChannel(self.config, self.state)
        self.channel.drain(self.account)
        self.channel.ingest(self.account, self.update())
        self.channel.drain(self.account)
        self.assertEqual(len(self.state.rows('SELECT * FROM messages_v2')), 1)
        self.assertEqual(self.state.rows('SELECT cursor FROM weixin_sync')[0]['cursor'], 'cursor-1')
        self.assertEqual(self.state.rows('SELECT token FROM weixin_context')[0]['token'], 'private-context')

    def test_full_queue_preserves_pending_message_until_capacity_available(self):
        for index in range(30):
            self.state.enqueue('busy-' + str(index), 'owner', 'feishu-chat', '/提醒')
        self.channel.ingest(self.account, self.update())
        self.assertFalse(self.channel.drain(self.account))
        self.assertEqual(len(self.state.rows('SELECT * FROM weixin_inbox')), 1)
        self.state.execute("UPDATE messages_v2 SET status='ready'")
        self.assertTrue(self.channel.drain(self.account))
        self.assertEqual(self.state.rows('SELECT * FROM weixin_inbox'), [])

    def test_rejecting_bad_batch_does_not_advance_cursor(self):
        with self.assertRaises(WeixinError):
            self.channel.ingest(self.account, {'msgs': [None], 'get_updates_buf': 'bad'})
        self.assertEqual(self.state.rows('SELECT * FROM weixin_sync'), [])

    def test_bad_media_and_long_text_return_actionable_reply_without_model(self):
        for message, expected in ((self.update(item_list=[{'type': 1, 'text_item': None}]), '文字消息'),
                                  (self.update(text='字' * 6001, message_id='long'), '6000')):
            self.channel.ingest(self.account, message)
            self.channel.drain(self.account)
            replies = []
            assistant = Assistant(self.config, self.state, None, Mailer(self.state),
                                  lambda *args: replies.append(args), user_zone())
            assistant.tick()
            self.assertIn(expected, replies[-1][1])

    def test_text_tool_call_reminder_and_shared_todo_reach_weixin(self):
        self.state.execute('INSERT INTO todos VALUES(?,?,?,?,?)', ('t1', 'owner', '已从飞书添加', '', 0))
        self.channel.ingest(self.account, self.update(text='一分钟后提醒我喝水'))
        self.channel.drain(self.account)
        class Model:
            def __init__(self):
                self.count = 0
            def complete(self, messages):
                self.count += 1
                if self.count == 1:
                    return {'role': 'assistant', 'content': None, 'tool_calls': [{
                        'id': 'c1', 'type': 'function', 'function': {'name': 'create_reminder',
                        'arguments': json.dumps({'text': '喝水', 'at': '2026-11-11T12:01:00+08:00'})}}]}
                return {'role': 'assistant', 'content': '完成'}
        sent = []
        from datetime import datetime
        now = datetime.fromisoformat('2026-11-11T12:00:00+08:00').timestamp()
        assistant = Assistant(self.config, self.state, Model(), Mailer(self.state),
                              lambda *args: sent.append(args), user_zone(), clock=lambda: now)
        assistant.tick()
        self.state.deliver(lambda *args: sent.append(args), now + 61, user_zone())
        self.assertTrue(all(reply[0] == self.chat for reply in sent))
        self.assertIn('喝水', sent[-1][1])
        self.assertEqual(self.state.rows('SELECT owner FROM schedules')[0]['owner'], 'owner')
        assistant.receive('lookup', 'owner', self.chat, '/待办')
        assistant.tick()
        self.assertIn('已从飞书添加', sent[-1][1])

    def test_partial_send_retry_skips_completed_parts_and_reuses_client_id(self):
        self.channel.ingest(self.account, self.update())
        sent = []
        def send(path, body, **kwargs):
            sent.append(body['msg'])
            if len(sent) == 2:
                raise WeixinError('temporary error')
            return {'ret': 0}
        with patch('assistant.weixin.WeixinAPI.request', side_effect=send):
            with self.assertRaises(WeixinError):
                self.channel.send(self.chat, '字' * 4000, 'reply')
            self.channel.send(self.chat, '字' * 4000, 'reply')
        self.assertEqual(len(sent), 3)
        self.assertEqual(sent[1]['client_id'], sent[2]['client_id'])
        self.assertEqual(sent[0]['context_token'], 'private-context')
        self.assertEqual(len(self.state.rows('SELECT * FROM weixin_sent')), 2)

    def test_rebinding_cannot_deliver_to_previous_account(self):
        with self.assertRaises(WeixinError):
            self.channel.send('weixin:old-account:peer', 'private result', 'key')

    def test_multiple_owners_require_explicit_mapping(self):
        config = Config(repo='first24/AIHelper', users=frozenset({'owner', 'other'}), db=self.db_path)
        with patch.dict(os.environ, {'WEIXIN_OWNER_OPEN_ID': ''}):
            with self.assertRaises(ValueError):
                WeixinChannel(config, self.state)


if __name__ == '__main__':
    unittest.main()
