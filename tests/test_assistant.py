import json
import smtplib
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from assistant.agent import Assistant
from assistant.core import Config
from assistant.mail import Mailer
from assistant.state import State, acquire_instance_lock, next_due, user_zone
from assistant.tools import Tools


class FakeModel:
    def __init__(self,responses):
        self.responses=list(responses)
        self.calls=[]

    def complete(self,messages):
        self.calls.append(messages)
        item=self.responses.pop(0)
        if isinstance(item,Exception):
            raise item
        return item


def call(name,args,id='call1'):
    return {'role':'assistant','content':None,'tool_calls':[{'id':id,'type':'function','function':{'name':name,'arguments':json.dumps(args)}}]}


class FakeSMTP:
    sent=[]
    error=None
    def __init__(self,*args,**kwargs):
        pass
    def login(self,*args):
        if self.error:
            raise self.error
    def send_message(self,message,**kwargs):
        self.sent.append(message)
        return {}
    def quit(self):
        pass
    def close(self):
        pass


class AssistantTests(unittest.TestCase):
    def setUp(self):
        self.state=State(':memory:')
        self.zone=user_zone('Asia/Shanghai')
        self.now=datetime(2026,11,11,10,0,tzinfo=self.zone).timestamp()
        self.clock=lambda:self.now
        self.tools=Tools(self.state,'owner','chat','msg',self.zone,clock=self.clock)
        self.config=Config(repo='first24/AIHelper',users=frozenset({'owner'}))
        self.sent=[]
        FakeSMTP.sent=[]
        FakeSMTP.error=None
        self.mailer=Mailer(self.state,user='owner@qq.com',password='fake-authorization',smtp_factory=FakeSMTP)

    def tearDown(self):
        self.state.db.close()

    def assistant(self,responses):
        model=FakeModel(responses)
        service=Assistant(self.config,self.state,model,self.mailer,lambda *args:self.sent.append(args),self.zone,clock=self.clock)
        return service,model

    def reminder(self,repeat='once',text='休息',at='2026-11-11T10:01:00+08:00'):
        return self.tools.execute('create_reminder',{'text':text,'at':at,'repeat':repeat})

    def test_reminder_persists_and_sends_once(self):
        task=self.reminder()
        self.state.deliver(lambda *args:self.sent.append(args),self.now+61,self.zone)
        self.state.deliver(lambda *args:self.sent.append(args),self.now+120,self.zone)
        self.assertEqual(len(self.sent),1)
        self.assertIn(task['id'],self.sent[0][1])

    def test_weekday_rolls_friday_to_monday(self):
        friday=datetime(2026,11,13,9,0,tzinfo=self.zone).timestamp()
        nxt=next_due(friday,friday+30,'weekdays',self.zone)
        self.assertEqual(datetime.fromtimestamp(nxt,self.zone).isoformat(),'2026-11-16T09:00:00+08:00')

    def test_weekly_skips_old_occurrences(self):
        old=datetime(2026,11,11,9,0,tzinfo=self.zone).timestamp()
        future=next_due(old,old+16*86400,'weekly',self.zone)
        self.assertGreater(future,old+16*86400)
        self.assertEqual(datetime.fromtimestamp(future,self.zone).weekday(),2)

    def test_cancel_prevents_notification(self):
        task=self.reminder()
        self.tools.execute('cancel_reminder',{'id':task['id']})
        self.state.deliver(lambda *args:self.sent.append(args),self.now+120,self.zone)
        self.assertEqual(self.sent,[])

    def test_cancel_pending_delivery_after_send_failure(self):
        task=self.reminder()
        self.state.deliver(lambda *a:(_ for _ in ()).throw(TimeoutError()),self.now+61,self.zone)
        self.tools.execute('cancel_reminder',{'id':task['id']})
        self.state.deliver(lambda *a:self.sent.append(a),self.now+1000,self.zone)
        self.assertEqual(self.sent,[])

    def test_retry_notification_keeps_deduplication_key(self):
        self.reminder()
        keys=[]
        def fail(*args):
            keys.append(args[2])
            raise TimeoutError()
        self.state.deliver(fail,self.now+61,self.zone)
        self.state.deliver(lambda *a:keys.append(a[2]),self.now+100,self.zone)
        self.assertEqual(keys[0],keys[1])

    def test_restart_keeps_schedule(self):
        with tempfile.TemporaryDirectory() as folder:
            path=str(Path(folder)/'state.db')
            one=State(path)
            Tools(one,'owner','chat','m',self.zone,clock=self.clock).execute('create_reminder',{'text':'测试','at':'2026-11-11T10:01:00+08:00'})
            one.db.close()
            two=State(path)
            two.recover()
            two.deliver(lambda *a:self.sent.append(a),self.now+61,self.zone)
            self.assertEqual(len(self.sent),1)
            two.db.close()

    def test_single_instance_lock_releases_cleanly(self):
        with tempfile.TemporaryDirectory() as folder:
            path=str(Path(folder)/'state.db')
            first=acquire_instance_lock(path)
            try:
                with self.assertRaises(RuntimeError):
                    acquire_instance_lock(path)
            finally:
                first.close()
            second=acquire_instance_lock(path)
            second.close()

    def test_accepted_mail_stays_sent_if_quit_fails(self):
        class QuitFailure(FakeSMTP):
            def quit(self):
                raise OSError('connection ended')
        draft_id=self.draft()
        mailer=Mailer(self.state,user='owner@qq.com',password='fake',smtp_factory=QuitFailure)
        self.assertIn('已接受',mailer.send_draft(draft_id,'owner'))
        self.assertEqual(self.state.draft(draft_id,'owner')['status'],'sent')

    def test_duplicate_tool_write_returns_same_task(self):
        first=self.reminder()
        second=self.reminder()
        self.assertEqual(first['id'],second['id'])
        self.assertEqual(len(self.tools.list_reminders()['reminders']),1)

    def test_other_owner_cannot_change_reminder(self):
        first=self.reminder()
        other=Tools(self.state,'other','chat2','m2',self.zone,clock=self.clock)
        with self.assertRaises(ValueError):
            other.execute('cancel_reminder',{'id':first['id']})
        self.assertEqual(other.list_reminders()['reminders'],[])

    def test_past_time_is_rejected(self):
        with self.assertRaises(ValueError):
            self.reminder(at='2026-11-10T10:00:00+08:00')

    def test_update_reminder_moves_due_time(self):
        first=self.reminder()
        self.tools.execute('update_reminder',{'id':first['id'],'at':'2026-11-11T11:00:00+08:00'})
        self.state.deliver(lambda *a:self.sent.append(a),self.now+120,self.zone)
        self.assertEqual(self.sent,[])
        self.assertIn('11:00:00',self.tools.list_reminders()['reminders'][0]['at'])

    def test_update_cancels_old_pending_daily_delivery(self):
        task=self.reminder(repeat='daily')
        self.state.deliver(lambda *a:(_ for _ in ()).throw(TimeoutError()),self.now+61,self.zone)
        self.tools.execute('update_reminder',{'id':task['id'],'at':'2026-11-12T11:00:00+08:00'})
        self.state.deliver(lambda *a:self.sent.append(a),self.now+1000,self.zone)
        self.assertEqual(self.sent,[])

    def test_daily_brief_is_built_from_current_todos(self):
        self.tools.execute('add_todo',{'title':'交报告'})
        self.tools.execute('schedule_daily_brief',{'at':'2026-11-11T10:01:00+08:00'})
        self.state.deliver(lambda *a:self.sent.append(a),self.now+61,self.zone)
        self.assertIn('交报告',self.sent[0][1])

    def test_todo_is_private_and_can_complete(self):
        result=self.tools.execute('add_todo',{'title':'交报告','due':'2026-11-13'})
        other=Tools(self.state,'other','chat','other',self.zone,clock=self.clock)
        self.assertEqual(other.list_todos()['todos'],[])
        with self.assertRaises(ValueError):
            other.execute('complete_todo',{'id':result['id']})
        self.tools.execute('complete_todo',{'id':result['id']})
        self.assertEqual(self.tools.list_todos()['todos'],[])

    def draft(self):
        return self.tools.execute('draft_email',{'to':'me@example.com','subject':'测试','body':'正文'})['id']

    def test_draft_does_not_send(self):
        self.draft()
        self.assertEqual(FakeSMTP.sent,[])

    def test_explicit_send_does_not_repeat(self):
        draft_id=self.draft()
        first=self.mailer.send_draft(draft_id,'owner')
        self.mailer.send_draft(draft_id,'owner')
        self.assertIn('已接受',first)
        self.assertEqual(len(FakeSMTP.sent),1)
        self.assertEqual(FakeSMTP.sent[0]['To'],'me@example.com')

    def test_wrong_owner_cannot_send_mail(self):
        draft_id=self.draft()
        with self.assertRaises(ValueError):
            self.mailer.send_draft(draft_id,'other')
        self.assertEqual(FakeSMTP.sent,[])

    def test_mail_timeout_is_unknown_and_not_retried(self):
        draft_id=self.draft()
        FakeSMTP.error=TimeoutError()
        self.mailer.send_draft(draft_id,'owner')
        self.assertEqual(self.state.draft(draft_id,'owner')['status'],'unknown')
        FakeSMTP.error=None
        self.mailer.send_draft(draft_id,'owner')
        self.assertEqual(FakeSMTP.sent,[])

    def test_mail_auth_failure_is_actionable(self):
        draft_id=self.draft()
        FakeSMTP.error=smtplib.SMTPAuthenticationError(535,b'bad auth')
        reply=self.mailer.send_draft(draft_id,'owner')
        self.assertIn('授权码',reply)
        self.assertEqual(self.state.draft(draft_id,'owner')['status'],'failed')

    def test_header_injection_rejected(self):
        with self.assertRaises(ValueError):
            self.tools.execute('draft_email',{'to':'a@example.com\r\nBcc: bad@example.com','subject':'hi','body':'x'})
        with self.assertRaises(ValueError):
            self.tools.execute('draft_email',{'to':'a@example.com','subject':'hi\nBcc: bad','body':'x'})

    def test_recovery_marks_inflight_mail_unknown(self):
        draft_id=self.draft()
        self.state.execute("UPDATE drafts SET status='sending' WHERE id=?",(draft_id,))
        self.state.recover()
        self.assertEqual(self.state.draft(draft_id,'owner')['status'],'unknown')

    def test_unknown_tool_and_extra_fields_are_rejected(self):
        with self.assertRaises(ValueError):
            self.tools.execute('execute_shell',{'cmd':'anything'})
        with self.assertRaises(ValueError):
            self.tools.execute('list_todos',{'owner':'someone-else'})
        with self.assertRaises(ValueError):
            self.tools.execute('list_todos',{'include_completed':'false'})

    def test_agent_uses_tool_results_and_deduplicates_event(self):
        service,model=self.assistant([call('add_todo',{'title':'交报告'}),{'role':'assistant','content':'完成'}])
        service.receive('event','owner','chat','记个待办')
        service.receive('event','owner','chat','记个待办')
        service.tick()
        service.tick()
        self.assertEqual(len(model.calls),2)
        self.assertEqual(len(self.tools.list_todos()['todos']),1)
        self.assertIn('t_',self.sent[0][1])

    def test_multiturn_uses_saved_history_and_actual_clock(self):
        service,model=self.assistant([call('create_reminder',{'text':'交报告','at':'2026-11-11T15:00:00+08:00'}),{'role':'assistant','content':'已创建'}])
        service.receive('one','owner','chat','下午三点提醒我')
        service.tick()
        task=self.tools.list_reminders()['reminders'][0]
        model.responses=[call('update_reminder',{'id':task['id'],'at':'2026-11-11T16:00:00+08:00'}),{'role':'assistant','content':'已修改'}]
        service.receive('two','owner','chat','改成四点')
        service.tick()
        self.assertIn('16:00:00',self.tools.list_reminders()['reminders'][0]['at'])
        self.assertIn('2026-11-11T10:00:00+08:00',model.calls[0][0]['content'])
        self.assertTrue(any(m.get('tool_calls') for m in model.calls[2]))

    def test_model_cannot_send_mail(self):
        draft_id=self.draft()
        service,model=self.assistant([call('send_email',{'id':draft_id}),{'role':'assistant','content':'没有发送工具'}])
        service.receive('x','owner','chat','发送')
        service.tick()
        self.assertEqual(FakeSMTP.sent,[])
        self.assertIn('未知工具',model.calls[1][-1]['content'])

    def test_unauthorized_user_never_calls_model(self):
        service,model=self.assistant([])
        service.receive('bad','stranger','chat','hello')
        service.tick()
        self.assertEqual(model.calls,[])

    def test_read_commands_do_not_call_model(self):
        service,model=self.assistant([])
        service.receive('help','owner','chat','/待办')
        service.tick()
        self.assertEqual(model.calls,[])
        self.assertIn('没有',self.sent[0][1])

    def test_reply_failure_retries_without_reexecuting_model(self):
        service,model=self.assistant([{'role':'assistant','content':'你好'}])
        service.receive('m','owner','chat','你好')
        service.send=lambda *a:(_ for _ in ()).throw(TimeoutError())
        service.tick()
        service.send=lambda *a:self.sent.append(a)
        service.tick()
        self.assertEqual(len(model.calls),1)
        self.assertEqual(len(self.sent),1)

    def test_completed_side_effect_reported_if_next_model_call_fails(self):
        service,model=self.assistant([call('add_todo',{'title':'交报告'}),RuntimeError('network')])
        service.receive('m','owner','chat','记个待办')
        service.tick()
        self.assertIn('已记录待办',self.sent[0][1])
        self.assertEqual(len(self.tools.list_todos()['todos']),1)
