import base64
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

from assistant.core import Config
from assistant.state import State,user_zone
from assistant.tools import Tools
from assistant.connected import ConnectedServices
from assistant.calendar import Calendar,scopes
from assistant.inbox import Inbox
from assistant.subscriptions import Subscriptions,InboxWatch
from assistant.web import public_url,parse_feed,text_from_html,fetch_public
from assistant.project_patch import validate_changes,safe_path
from assistant.projects import ProjectRepairs


ZONE=user_zone()
NOW=datetime(2026,11,11,9,tzinfo=ZONE).timestamp()
CONFIG=Config(repo='first24/AIHelper',users=frozenset({'owner'}),token='test-token')


class WebTests(unittest.TestCase):
    def test_private_dns_and_mixed_dns_blocked(self):
        for addresses in (['127.0.0.1'],['10.0.0.1'],['169.254.169.254'],['8.8.8.8','192.168.0.1'],['::1']):
            with self.subTest(addresses=addresses),patch('assistant.web.socket.getaddrinfo',return_value=[(2,1,6,'',(ip,443)) for ip in addresses]):
                with self.assertRaises(ValueError):
                    public_url('https://example.com/path')

    def test_unicode_url_normalized(self):
        with patch('assistant.web.socket.getaddrinfo',return_value=[(2,1,6,'',('8.8.8.8',443))]):
            parsed,_=public_url('https://例子.com/中文 空格?q=关键词&x=1')
            self.assertTrue(parsed.hostname.startswith('xn--'))
            self.assertIn('%E4%B8%AD',parsed.path)
            self.assertIn('&x=1',parsed.query)
            parsed.geturl().encode('ascii')

    def test_redirect_cannot_reach_internal(self):
        response=Mock(status=302)
        response.getheader.return_value='http://127.0.0.1/private'
        connection=Mock()
        connection.getresponse.return_value=response
        with patch('assistant.web.http.client.HTTPConnection',return_value=connection),patch('assistant.web.socket.create_connection'),patch('assistant.web.socket.getaddrinfo',side_effect=[[(2,1,6,'',('8.8.8.8',80))],[(2,1,6,'',('127.0.0.1',80))]]):
            with self.assertRaises(ValueError):
                fetch_public('http://example.com')
            self.assertEqual(connection.request.call_count,1)

    def test_feed_and_html(self):
        rss=b'<rss><channel><item><title>News</title><link>/story</link><description>&lt;p&gt;Fact&lt;/p&gt;</description></item></channel></rss>'
        self.assertEqual(parse_feed(rss,'https://example.com')[0]['url'],'https://example.com/story')
        atom=b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>A</title><id>1</id><link href="https://example.com/a"/></entry></feed>'
        self.assertEqual(len(parse_feed(atom,'https://example.com')[0]['id']),64)
        with self.assertRaises(ValueError):
            parse_feed(b'<!DOCTYPE x><rss/>','https://example.com')
        title,text=text_from_html('<title>T</title><nav>Menu</nav><p>Fact</p><script>Bad</script>')
        self.assertEqual((title,text),('T','Fact'))


class SubscriptionTests(unittest.TestCase):
    def setUp(self):
        self.state=State(':memory:')
        self.now=NOW
        self.service=Subscriptions(self.state,ZONE,clock=lambda:self.now)

    def create(self):
        return self.service.create('owner','weixin:chat','News','search','Python',datetime.fromtimestamp(NOW+60,ZONE).isoformat(),'daily','abc')['id']

    def test_due_snapshot_and_persistent_outbox(self):
        id=self.create()
        self.now+=61
        result={'results':[{'title':'A','url':'https://example.com/a','summary':'Fact'}]}
        with patch('assistant.subscriptions.web_search',return_value=result):
            self.service.tick()
            self.assertEqual(len(self.state.rows('SELECT * FROM deliveries')),1)
            self.now+=86400
            self.service.tick()
            self.assertEqual(len(self.state.rows('SELECT * FROM deliveries')),1)
        self.service.manage('owner',id,'pause')
        self.assertEqual(self.state.rows('SELECT status FROM deliveries')[0]['status'],'canceled')
        with self.assertRaises(ValueError):
            self.service.manage('other',id,'resume')

    def test_cancel_during_network_prevents_notification(self):
        id=self.create()
        self.now+=61
        def cancel(*args):
            self.service.manage('owner',id,'cancel')
            return {'results':[{'title':'A','url':'https://example.com/a'}]}
        with patch('assistant.subscriptions.web_search',side_effect=cancel):
            self.service.tick()
        self.assertFalse(self.state.rows('SELECT * FROM deliveries'))

    def test_page_baseline_and_failure_backoff(self):
        with patch('assistant.subscriptions.public_url'),patch('assistant.subscriptions.read_page',return_value={'fingerprint':'a','text':'A'}):
            id=self.service.create('owner','chat','Page','page','https://example.com',datetime.fromtimestamp(NOW+1,ZONE).isoformat(),'once','page')['id']
        self.now+=2
        with patch('assistant.subscriptions.read_page',side_effect=RuntimeError('Unavailable')):
            self.service.tick()
        row=self.state.rows('SELECT * FROM subscriptions WHERE id=?',(id,))[0]
        self.assertEqual(row['attempts'],1)
        self.assertGreater(row['retry_at'],self.now)
        self.assertFalse(self.state.rows('SELECT * FROM deliveries'))


class IMAPMock:
    def __init__(self,*args,**kwargs):
        self.calls=[]
    def login(self,*args): pass
    def select(self,folder,readonly=False):
        assert readonly
        return 'OK',[]
    def response(self,key): return key,[b'8']
    def logout(self): pass
    def uid(self,method,*args):
        self.calls.append((method,args))
        if method=='search': return 'OK',[b'1 2']
        if 'BODY.PEEK[]' in args[-1]:
            return 'OK',[(b'UID 2',b'From: person@example.com\r\nSubject: Hello\r\nMessage-ID: <a@example.com>\r\n\r\nBody text')]
        return 'OK',[(b'UID 2 RFC822.SIZE 200',b'From: person@example.com\r\nSubject: Hello\r\nMessage-ID: <a@example.com>\r\n\r\n')]


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.client=IMAPMock()
        self.inbox=Inbox(CONFIG,factory=lambda *a,**k:self.client)
        self.inbox.user='test@qq.com'
        self.inbox.password='local-test-only'

    def test_reads_use_peek_and_uidvalidity(self):
        messages=self.inbox.list('owner')['messages']
        self.assertEqual(messages[0]['id'],'m_8_2')
        message=self.inbox.read('owner','m_8_2')
        self.assertEqual(message['body'],'Body text')
        self.assertTrue(all('PEEK' in args[-1] for method,args in self.client.calls if method=='fetch'))
        self.assertFalse(any(method=='store' for method,args in self.client.calls))
        with self.assertRaises(ValueError): self.inbox.read('owner','m_7_2')
        with self.assertRaises(ValueError): self.inbox.list('other')

    def test_reply_draft_preserves_thread_without_sending(self):
        state=State(':memory:')
        id=self.inbox.reply_draft(state,'owner','chat','m_8_2','Thank you','reply')
        row=state.draft(id,'owner')
        self.assertEqual(row['status'],'draft')
        self.assertEqual(row['in_reply_to'],'<a@example.com>')
        self.assertEqual(row['recipient'],'person@example.com')

    def test_watch_baseline_and_cancel(self):
        state=State(':memory:')
        watch=InboxWatch(state,self.inbox,clock=lambda:NOW)
        watch.set('owner','weixin:chat',True)
        self.assertEqual(state.rows('SELECT last_uid FROM inbox_watch')[0]['last_uid'],2)
        state.execute('UPDATE inbox_watch SET last_uid=1,next_check=0')
        watch.tick()
        self.assertEqual(len(state.rows('SELECT * FROM deliveries')),1)
        watch.set('owner','chat',False)
        self.assertEqual(state.rows('SELECT status FROM deliveries')[0]['status'],'canceled')


class CalendarTests(unittest.TestCase):
    def test_conflicts_and_slots(self):
        cal=Calendar(CONFIG,ZONE,clock=lambda:NOW)
        start=datetime.fromtimestamp(NOW+3600,ZONE)
        end=start+timedelta(hours=3)
        event={'id':'evt','title':'Busy','start':(start+timedelta(hours=1)).isoformat(),'end':(start+timedelta(hours=2)).isoformat(),'busy':True}
        with patch.object(cal,'events',return_value={'calendar_id':'calendar','events':[event]}),patch.object(cal,'call') as api:
            result=cal.create('owner','Test',start.isoformat(),end.isoformat(),'','key')
            self.assertFalse(result['ok'])
            api.assert_not_called()
            self.assertEqual(len(cal.slots('owner',start.isoformat(),end.isoformat(),30)['slots']),2)
        with self.assertRaises(ValueError): cal.window(end.isoformat(),start.isoformat())

    def test_refresh_is_bound_to_owner_and_saved_privately(self):
        with tempfile.TemporaryDirectory() as directory:
            cal=Calendar(replace(CONFIG,db=str(Path(directory)/'db.sqlite3'),app_id='app'),ZONE,clock=lambda:NOW)
            path=cal.path('owner')
            path.parent.mkdir()
            path.write_text(json.dumps({'open_id':'other','client_id':'app','access_token':'test','expires_at':NOW+10000}))
            with self.assertRaises(ValueError): cal.token('owner')
            path.write_text(json.dumps({'open_id':'owner','client_id':'app','access_token':'test','refresh_token':'refresh','expires_at':0}))
            with patch('assistant.calendar.token_request',return_value={'access_token':'new','expires_at':NOW+10000,'client_id':'app'}):
                self.assertEqual(cal.token('owner'),'new')
            self.assertEqual(json.loads(path.read_text())['refresh_token'],'refresh')

    def test_write_permissions_are_explicit(self):
        service=ConnectedServices(CONFIG,State(':memory:'),ZONE)
        with patch.dict(os.environ,{'CALENDAR_ACCESS':'read'}):
            self.assertNotIn('create',scopes())
            with self.assertRaises(ValueError):
                service.execute('delete_calendar_event',{'id':'evt'},'owner','chat','msg','effect')
        with patch.dict(os.environ,{'CALENDAR_ACCESS':'write'}):
            self.assertIn('create',scopes())
            self.assertNotIn('delete',scopes())


class ProjectTests(unittest.TestCase):
    def test_source_paths_and_artifact_limits(self):
        for name in ('../a.py','.git/config','.github/workflows/a.yml','a/.env','/tmp/a','a\\b','a//b'):
            with self.subTest(name=name),self.assertRaises(ValueError): safe_path(name)
        change={'path':'assistant/a.py','content':base64.b64encode(b'print(1)').decode()}
        self.assertEqual(validate_changes([change])[0]['bytes'],b'print(1)')
        with self.assertRaises(ValueError): validate_changes([change,change])
        with self.assertRaises(ValueError): validate_changes([dict(change,mode='120000')])

    def test_submit_durable_idempotency_and_scope(self):
        state=State(':memory:')
        github=Mock()
        github.call.return_value={'sha':'a'*40}
        repairs=ProjectRepairs(CONFIG,state,clock=lambda:NOW,github_factory=lambda c:github)
        with patch.dict(os.environ,{'PROJECT_BASE_REF':'feature/connected-assistant'}):
            one=repairs.submit('owner','chat','message','Fix bug')
            two=repairs.submit('owner','chat','message','Fix bug')
        self.assertEqual(one['id'],two['id'])
        self.assertEqual(len(state.rows('SELECT * FROM project_jobs')),1)
        with self.assertRaises(ValueError): repairs.status('other',one['id'])
        with self.assertRaises(ValueError): repairs.submit('owner','chat','other','Fix','unknown/repo')

    def test_reject_mismatched_verified_artifact_before_publish(self):
        repairs=ProjectRepairs(CONFIG,State(':memory:'))
        with self.assertRaises(ValueError):
            repairs.publish({'id':'abc','repository':'first24/AIHelper','base_sha':'a'*40,'ref':'main'}, {'job_id':'other'})

    def test_project_failure_creates_one_outbox_item(self):
        state=State(':memory:')
        repairs=ProjectRepairs(CONFIG,state)
        job={'id':'job','owner':'owner','chat':'weixin:chat'}
        repairs.finish(job,'failed','Failure')
        repairs.finish(job,'failed','Failure')
        self.assertEqual(len(state.rows('SELECT * FROM deliveries')),1)


if __name__=='__main__':
    unittest.main()
