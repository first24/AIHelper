import json
from contextlib import closing
from datetime import datetime
from pathlib import Path
import sqlite3
import tempfile
import unittest

from assistant.agent import Assistant
from assistant.core import Config
from assistant.mail import Mailer
from assistant.state import State, user_zone
from assistant.tools import Tools


class DailyAssistantTests(unittest.TestCase):
    def setUp(self):
        self.state=State(':memory:')
        self.zone=user_zone()
        self.now=datetime.fromisoformat('2026-11-11T10:00:00+08:00').timestamp()
        self.clock=lambda:self.now
        self.tools=Tools(self.state,'owner','weixin:account:peer','message',self.zone,clock=self.clock)
        self.config=Config(repo='first24/AIHelper',users=frozenset({'owner'}))

    def tearDown(self):
        self.state.db.close()

    def test_old_database_migrates_notes_without_losing_existing_records(self):
        with tempfile.TemporaryDirectory() as folder:
            path=str(Path(folder)/'old.sqlite3')
            with closing(sqlite3.connect(path)) as db:
                db.execute('CREATE TABLE todos(id TEXT PRIMARY KEY,owner TEXT,title TEXT,due TEXT,done INTEGER DEFAULT 0)')
                db.execute('INSERT INTO todos VALUES(?,?,?,?,?)',('t1','owner','旧待办','2026-11-11',0))
                db.commit()
            state=State(path)
            try:
                tools=Tools(state,'owner','chat','note1',self.zone,clock=self.clock)
                note=tools.execute('save_note',{'title':'旧库升级','body':'保留旧记录'})
                self.assertEqual(tools.list_todos()['todos'][0]['title'],'旧待办')
                self.assertEqual(tools.get_note(note['id'])['note']['body'],'保留旧记录')
                self.assertEqual(state.rows('PRAGMA quick_check')[0]['quick_check'],'ok')
            finally:
                state.db.close()

    def test_today_uses_local_dates_and_excludes_other_owners_and_completed_todos(self):
        records=[('yesterday','owner','昨天','2026-11-10',0),
                 ('today','owner','今天','2026-11-11',0),
                 ('morning','owner','上午已逾期','2026-11-11T09:00:00+08:00',0),
                 ('utc','owner','跨时区今天','2026-11-11T08:00:00Z',0),
                 ('tomorrow','owner','明天','2026-11-12',0),
                 ('undated','owner','没有日期','',0),
                 ('legacy','owner','旧格式','周五',0),
                 ('done','owner','已完成','2026-11-10',1),
                 ('private','other','其他用户','2026-11-10',0)]
        for record in records:
            self.state.execute('INSERT INTO todos VALUES(?,?,?,?,?)',record)
        self.tools.execute('create_reminder',{'text':'今日提醒','at':'2026-11-11T23:59:59+08:00'})
        other=Tools(self.state,'other','chat','other',self.zone,clock=self.clock)
        other.execute('create_reminder',{'text':'私密提醒','at':'2026-11-11T11:00:00+08:00'})
        overview=self.tools.execute('get_daily_overview',{})
        self.assertEqual(overview['counts'],{'overdue':2,'today':2,'undated':2,'reminders':1})
        self.assertEqual(overview['pending_todos'],7)
        self.assertNotIn('其他用户',overview['receipt'])
        self.assertNotIn('私密提醒',overview['receipt'])
        self.assertEqual(overview['reminders'][0]['channel'],'微信')

    def test_snoozing_completed_once_reminder_reactivates_it_and_is_idempotent(self):
        first=self.tools.execute('create_reminder',{'text':'喝水','at':'2026-11-11T10:01:00+08:00'})
        sent=[]
        self.state.deliver(lambda *args:sent.append(args),self.now+61,self.zone)
        self.now+=120
        snooze=Tools(self.state,'owner','feishu-chat','snooze1',self.zone,clock=self.clock)
        result=snooze.execute('snooze_reminder',{'id':first['id'],'minutes':10})
        original_due=self.state.rows('SELECT due FROM schedules')[0]['due']
        self.now+=60
        repeated=snooze.execute('snooze_reminder',{'id':first['id'],'minutes':10})
        self.assertEqual(result,repeated)
        self.assertEqual(self.state.rows('SELECT due FROM schedules')[0]['due'],original_due)
        self.state.deliver(lambda *args:sent.append(args),original_due+1,self.zone)
        self.assertEqual(len(sent),2)
        self.assertEqual(sent[-1][0],'feishu-chat')

    def test_snooze_cancels_old_retry_and_preserves_recurring_schedule(self):
        first=self.tools.execute('create_reminder',{'text':'日报','at':'2026-11-11T10:01:00+08:00','repeat':'daily'})
        self.state.deliver(lambda *args:(_ for _ in ()).throw(TimeoutError()),self.now+61,self.zone)
        original=self.state.rows('SELECT * FROM schedules WHERE id=?',(first['id'],))[0]
        snooze=Tools(self.state,'owner','weixin:account:peer','snooze2',self.zone,clock=self.clock)
        result=snooze.execute('snooze_reminder',{'id':first['id'],'minutes':10})
        preserved=self.state.rows('SELECT * FROM schedules WHERE id=?',(first['id'],))[0]
        self.assertEqual(original,preserved)
        self.assertEqual(self.state.rows('SELECT status FROM deliveries WHERE schedule_id=?',(first['id'],))[0]['status'],'canceled')
        self.assertNotEqual(result['id'],first['id'])
        self.assertEqual(self.state.rows('SELECT repeat FROM schedules WHERE id=?',(result['id'],))[0]['repeat'],'once')
        # A one-time snooze cancels pending delivery of the old occurrence.
        once=Tools(self.state,'owner','chat','once',self.zone,clock=self.clock).execute(
            'create_reminder',{'text':'单次','at':'2026-11-11T10:01:00+08:00'})
        self.state.deliver(lambda *args:(_ for _ in ()).throw(TimeoutError()),self.now+62,self.zone)
        snooze.execute('snooze_reminder',{'id':once['id'],'minutes':10})
        statuses=self.state.rows('SELECT status FROM deliveries WHERE schedule_id=?',(once['id'],))
        self.assertEqual(statuses[0]['status'],'canceled')

    def test_snooze_rejects_invalid_delay_and_other_users(self):
        first=self.tools.execute('create_reminder',{'text':'提醒','at':'2026-11-11T10:01:00+08:00'})
        for delay in (0,-1,10081,True,1.5,'10'):
            with self.assertRaises(ValueError):
                self.tools.execute('snooze_reminder',{'id':first['id'],'minutes':delay})
        other=Tools(self.state,'other','chat','other',self.zone,clock=self.clock)
        with self.assertRaises(ValueError):
            other.execute('snooze_reminder',{'id':first['id'],'minutes':10})

    def test_notes_are_shared_across_chats_private_across_owners_and_search_is_literal(self):
        note=self.tools.execute('save_note',{'title':'项目配置','body':'Python 3.12，完成度 50%_test'})
        same=Tools(self.state,'owner','feishu-chat','lookup',self.zone,clock=self.clock)
        other=Tools(self.state,'other','feishu-chat','other',self.zone,clock=self.clock)
        self.assertEqual(len(same.list_notes('python')['notes']),1)
        self.assertEqual(len(same.list_notes('50%_test')['notes']),1)
        self.assertEqual(same.list_notes('%')['notes'][0]['id'],note['id'])
        self.assertEqual(same.list_notes('100%')['notes'],[])
        self.assertEqual(other.list_notes()['notes'],[])
        for name,args in (('get_note',{'id':note['id']}),('update_note',{'id':note['id'],'body':'改写'}),
                          ('delete_note',{'id':note['id']})):
            with self.assertRaises(ValueError):
                other.execute(name,args)
        same.execute('update_note',{'id':note['id'],'body':'Python 3.13'})
        self.assertEqual(same.get_note(note['id'])['note']['body'],'Python 3.13')
        same.execute('delete_note',{'id':note['id']})
        self.assertEqual(same.list_notes()['notes'],[])

    def test_mail_preview_editing_keeps_body_and_never_sends_or_changes_sent_mail(self):
        draft=self.tools.execute('draft_email',{'to':'self@example.com','subject':'原主题','body':'原正文'})
        updated=self.tools.execute('update_email_draft',{'id':draft['id'],'subject':'新主题','body':'新正文'})
        self.assertIn('尚未发送',updated['receipt'])
        self.assertIn('新正文',updated['receipt'])
        self.assertEqual(updated['draft']['recipient'],'self@example.com')
        self.assertEqual(self.state.draft(draft['id'],'owner')['status'],'draft')
        with self.assertRaises(ValueError):
            self.tools.execute('update_email_draft',{'id':draft['id'],'subject':'主题\nBcc: bad@example.com'})
        for status in ('sent','sending','unknown'):
            self.state.execute('UPDATE drafts SET status=? WHERE id=?',(status,draft['id']))
            with self.assertRaises(ValueError):
                self.tools.execute('update_email_draft',{'id':draft['id'],'body':'再次修改'+status})
            preview=self.tools.get_email_draft(draft['id'])['receipt']
            self.assertNotIn('/发送邮件',preview)
        other=Tools(self.state,'other','chat','other',self.zone,clock=self.clock)
        with self.assertRaises(ValueError):
            other.execute('get_email_draft',{'id':draft['id']})

    def test_new_commands_use_backend_without_model_and_email_list_is_explicit(self):
        note=self.tools.execute('save_note',{'title':'测试资料','body':'正文'})
        draft=self.tools.execute('draft_email',{'to':'self@example.com','subject':'历史邮件','body':'正文'})
        self.state.execute("UPDATE drafts SET status='sent' WHERE id=?",(draft['id'],))
        sent=[]
        service=Assistant(self.config,self.state,None,Mailer(self.state),lambda *args:sent.append(args),
                          self.zone,clock=self.clock)
        commands=['/今日','/笔记','/查看笔记 '+note['id'],'/邮件','/查看邮件 '+draft['id'],'/不支持的命令']
        for index,text in enumerate(commands):
            service.receive('command-'+str(index),'owner','weixin:account:peer',text)
            service.tick()
        self.assertIn('今日概览',sent[0][1])
        self.assertIn('测试资料',sent[1][1])
        self.assertIn('本次查询不会发送邮件',sent[3][1])
        self.assertIn('已发送',sent[3][1])
        self.assertIn('开头无需加斜杠',sent[5][1])

    def test_multistep_mail_edit_reports_only_latest_preview(self):
        draft=self.tools.execute('draft_email',{'to':'self@example.com','subject':'测试','body':'旧正文'})
        class Model:
            def __init__(self):
                self.count=0
            def complete(self,messages):
                self.count+=1
                if self.count>2:
                    return {'role':'assistant','content':'已完成'}
                name='get_email_draft' if self.count==1 else 'update_email_draft'
                args={'id':draft['id']}
                if self.count==2:
                    args['body']='最终新正文'
                return {'role':'assistant','content':None,'tool_calls':[{
                    'id':'call'+str(self.count),'type':'function',
                    'function':{'name':name,'arguments':json.dumps(args)}}]}
        sent=[]
        service=Assistant(self.config,self.state,Model(),Mailer(self.state),lambda *args:sent.append(args),
                          self.zone,clock=self.clock)
        service.receive('change','owner','chat','修改这封邮件')
        service.tick()
        self.assertIn('最终新正文',sent[0][1])
        self.assertNotIn('旧正文',sent[0][1])


if __name__=='__main__':
    unittest.main()
