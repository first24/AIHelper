"""Runnable offline scenario. No real model calls, Feishu messages or email."""
import json
from datetime import datetime,timedelta
from pathlib import Path

from .agent import Assistant
from .core import Config
from .mail import Mailer
from .state import State,user_zone


def demo():
    zone=user_zone('Asia/Shanghai')
    start=datetime.now(zone).replace(microsecond=0)
    clock=[start.timestamp()]
    state=State(':memory:')
    sent=[]
    mail=[]

    class Model:
        def __init__(self):
            self.pending=[]
        def complete(self,messages):
            if messages[-1]['role']=='tool':
                return {'role':'assistant','content':'已按工具回执处理。'}
            text=messages[-1]['content']
            if '提醒' in text:
                name,args='create_reminder',{'text':'提交报告','at':(start+timedelta(seconds=30)).isoformat()}
            elif '待办' in text:
                name,args='add_todo',{'title':'准备项目周报'}
            else:
                name,args='draft_email',{'to':'self@example.com','subject':'项目进展','body':'这是离线演示邮件，不会实际发送。'}
            return {'role':'assistant','content':None,'tool_calls':[{'id':'demo-call','type':'function','function':{'name':name,'arguments':json.dumps(args)}}]}

    class SMTP:
        def __init__(self,*args,**kwargs):
            pass
        def login(self,*args):
            pass
        def send_message(self,message,**kwargs):
            mail.append({'to':message['To'],'subject':message['Subject']})
            return {}
        def quit(self):
            pass

    config=Config(repo='first24/AIHelper',users=frozenset({'demo-owner'}))
    service=Assistant(config,state,Model(),Mailer(state,user='demo@qq.com',password='fake',smtp_factory=SMTP),
                      lambda chat,text,key:sent.append({'text':text,'key':key}),zone,clock=lambda:clock[0])
    for index,text in enumerate(['30秒后提醒我提交报告','记一条待办','帮我写封邮件']):
        service.receive('demo-'+str(index),'demo-owner','demo-chat',text)
        service.tick()
    draft=state.rows('SELECT id FROM drafts')[0]['id']
    service.receive('demo-send','demo-owner','demo-chat','/发送邮件 '+draft)
    service.tick()
    clock[0]+=31
    state.deliver(lambda chat,text,key:sent.append({'text':text,'key':key}),clock[0],zone)
    assert len(mail)==1
    assert any('提醒：提交报告' in item['text'] for item in sent)
    assert state.rows('SELECT count(*) AS count FROM todos')[0]['count']==1
    report={'mode':'offline simulation','real_model':False,'real_feishu':False,'real_email':False,
            'messages':sent,'simulated_mail':mail,'status':'passed'}
    Path('assistant-demo-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    for item in sent:
        print('[飞书模拟] '+item['text'])
    print('第二版离线演示通过：提醒、待办、邮件草稿和显式发送。记录：assistant-demo-report.json')
    state.db.close()
