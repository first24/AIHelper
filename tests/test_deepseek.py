import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from assistant.agent import DeepSeek


class DeepSeekContractTests(unittest.TestCase):
    def test_native_tool_request_and_response(self):
        result={'choices':[{'finish_reason':'tool_calls','message':{'content':None,'tool_calls':[
            {'id':'call-1','type':'function','function':{'name':'list_todos','arguments':'{}'}}]}}]}
        captured=[]
        def respond(request,timeout):
            captured.append(request)
            return io.BytesIO(json.dumps(result).encode())
        with patch('assistant.agent.urlopen',side_effect=respond):
            response=DeepSeek(key='fake-key').complete([{'role':'user','content':'查看待办'}])
        request=captured[0]
        payload=json.loads(request.data)
        self.assertEqual(request.full_url,'https://api.deepseek.com/chat/completions')
        self.assertEqual(request.get_header('Authorization'),'Bearer fake-key')
        self.assertNotIn('fake-key',json.dumps(payload))
        self.assertEqual(payload['thinking'],{'type':'disabled'})
        self.assertIn('tools',payload)
        self.assertEqual(response['tool_calls'][0]['function']['name'],'list_todos')

    def test_truncated_output_never_returns_tool_to_executor(self):
        result={'choices':[{'finish_reason':'length','message':{'content':'partial','tool_calls':[]}}]}
        with patch('assistant.agent.urlopen',return_value=io.BytesIO(json.dumps(result).encode())):
            with self.assertRaisesRegex(RuntimeError,'截断'):
                DeepSeek(key='fake').complete([{'role':'user','content':'hello'}])

    def test_balance_error_is_sanitized(self):
        error=HTTPError('https://api.deepseek.com/chat/completions',402,'provider-private-body',{},None)
        with patch('assistant.agent.urlopen',side_effect=error):
            with self.assertRaisesRegex(RuntimeError,'余额不足') as caught:
                DeepSeek(key='private-key').complete([{'role':'user','content':'hello'}])
        self.assertNotIn('private',str(caught.exception))

    def test_invalid_provider_json_is_reported(self):
        with patch('assistant.agent.urlopen',return_value=io.BytesIO(b'not json')):
            with self.assertRaisesRegex(RuntimeError,'无效 JSON'):
                DeepSeek(key='fake').complete([{'role':'user','content':'hello'}])
