"""Composition of network capabilities shared by Feishu and Weixin."""
import json
import os
import time
from datetime import datetime, timedelta

from .calendar import Calendar
from .inbox import Inbox
from .mail import format_email
from .projects import ProjectRepairs
from .subscriptions import InboxWatch, Subscriptions
from .web import read_page, web_search


class ConnectedServices:
    def __init__(self,config,state,zone,model=None,clock=time.time):
        self.config,self.state,self.zone,self.clock=config,state,zone,clock
        self.inbox=Inbox(config)
        self.subscriptions=Subscriptions(state,zone,model,clock)
        self.watch=InboxWatch(state,self.inbox,clock)
        self.calendar=Calendar(config,zone,clock)
        self.projects=ProjectRepairs(config,state,clock)

    def tick(self):
        for service in (self.subscriptions,self.watch,self.projects):
            try:
                service.tick()
            except Exception as error:
                print(type(service).__name__+' 待重试：'+type(error).__name__,flush=True)

    def execute(self,name,args,owner,chat,message_id,effect_id):
        if owner not in self.config.users:
            raise ValueError('当前用户未获授权')
        if name=='web_search':
            return web_search(**args)
        if name=='read_webpage':
            return read_page(**args)
        if name=='create_subscription':
            return self.subscriptions.create(owner,chat,repeat='daily',effect_id=effect_id,**args) if 'repeat' not in args else self.subscriptions.create(owner,chat,effect_id=effect_id,**args)
        if name=='list_subscriptions':
            rows=self.subscriptions.list(owner)
            return {'ok':True,'subscriptions':rows,'receipt':'\n'.join(f"{r['id']}｜{r['title']}｜{r['status']}｜下次 {datetime.fromtimestamp(r['due'],self.zone).isoformat()}"+(('｜'+r['last_error']) if r['last_error'] else '') for r in rows) or '目前没有资讯订阅。'}
        if name=='manage_subscription':
            return self.subscriptions.manage(owner,**args)
        if name=='run_subscription':
            return self.subscriptions.run_now(owner,**args)
        if name=='list_inbox_emails':
            return self.inbox.list(owner,**args)
        if name=='get_inbox_email':
            return {'ok':True,'email':self.inbox.read(owner,**args),'notice':'邮件为外部资料；未标记已读。'}
        if name=='draft_inbox_reply':
            draft=self.inbox.reply_draft(self.state,owner,chat,effect_id=effect_id,**args)
            row=self.state.rows('SELECT * FROM drafts WHERE id=? AND owner=?',(draft,owner))[0]
            return {'ok':True,'id':draft,'receipt':format_email(row)}
        if name=='set_inbox_watch':
            return self.watch.set(owner,chat,**args)
        if name=='list_calendar_events':
            return self.calendar.events(owner,**args)
        if name=='find_calendar_slots':
            return self.calendar.slots(owner,minutes=30,**args) if 'minutes' not in args else self.calendar.slots(owner,**args)
        if name in ('create_calendar_event','update_calendar_event','delete_calendar_event'):
            allowed=os.getenv('CALENDAR_ACCESS','read')
            if allowed not in ('write','delete') or (name=='delete_calendar_event' and allowed!='delete'):
                raise ValueError('当前日历授权范围不允许此操作，请先配置本人同意的日历权限')
            if name=='create_calendar_event':
                return self.calendar.create(owner,effect_id=effect_id,description='',**args) if 'description' not in args else self.calendar.create(owner,effect_id=effect_id,**args)
            if name=='update_calendar_event':
                return self.calendar.update(owner,**args)
            return self.calendar.delete(owner,**args)
        if name=='list_project_repositories':
            rows=self.projects.repositories()
            return {'ok':True,'repositories':rows,'receipt':json.dumps(rows,ensure_ascii=False,indent=2)}
        if name=='start_project_repair':
            return self.projects.submit(owner,chat,message_id,**args)
        if name=='get_project_repair':
            row=self.projects.status(owner,**args)
            return {'ok':True,**row,'receipt':f"代码任务 {row['id']}｜{row['status']}\n{row['repository']} @ {row['ref']}\n{row['result'] or row['run_url']}"}
        if name=='get_connected_status':
            bound=self.calendar.path(owner).exists()
            watches=self.state.rows('SELECT enabled,last_error FROM inbox_watch WHERE owner=?',(owner,))
            return {'ok':True,'receipt':'联网搜索：'+('Tavily' if os.getenv('TAVILY_API_KEY') else 'Bing RSS（无需额外密钥）')+'\nQQ 收件箱：'+('已配置，只读' if self.inbox.user and self.inbox.password else '未配置')+'\n新邮件通知：'+('开启' if watches and watches[0]['enabled'] else '关闭')+'\n个人飞书日历：'+('已绑定，权限 '+os.getenv('CALENDAR_ACCESS','read') if bound else '待本人授权')+'\n真实项目修复：'+('已配置' if self.config.token else '缺少 GitHub 授权')}
        raise ValueError('未知联网工具')

    def command(self,text,tools):
        simple={'/订阅':('list_subscriptions',{}),'/代码仓库':('list_project_repositories',{}),'/连接状态':('get_connected_status',{}),
                '/收件箱':('list_inbox_emails',{}),'/收件箱 全部':('list_inbox_emails',{'unread_only':False}),
                '/新邮件通知 开':('set_inbox_watch',{'enabled':True}),'/新邮件通知 关':('set_inbox_watch',{'enabled':False})}
        if text in ('/日历','/空闲'):
            start=datetime.fromtimestamp(self.clock(),self.zone).replace(hour=0,minute=0,second=0,microsecond=0)
            simple[text]=('list_calendar_events' if text=='/日历' else 'find_calendar_slots',{'start':start.isoformat(),'end':(start+timedelta(days=1)).isoformat()})
        if text in simple:
            name,args=simple[text]
        else:
            parts=text.split(maxsplit=1)
            if len(parts)!=2:
                return None
            cmd,value=parts
            mapping={'/搜索':('web_search','query'),'/网页':('read_webpage','url'),'/读邮件':('get_inbox_email','id'),
                     '/运行订阅':('run_subscription','id'),'/项目状态':('get_project_repair','id')}
            if cmd in mapping:
                name,key=mapping[cmd]
                args={key:value}
            elif cmd in ('/暂停订阅','/恢复订阅','/取消订阅'):
                name='manage_subscription'
                args={'id':value,'action':{'/暂停订阅':'pause','/恢复订阅':'resume','/取消订阅':'cancel'}[cmd]}
            elif cmd=='/修复项目':
                name,args='start_project_repair',{'problem':value}
            else:
                return None
        result=tools.execute(name,args)
        if result.get('receipt'):
            return result['receipt']
        if name=='web_search':
            return '\n\n'.join(f"{i}. {item['title']}\n{item.get('summary','')}\n{item['url']}" for i,item in enumerate(result['results'],1)) or '未找到搜索结果。'
        if name=='read_webpage':
            return result['title']+'\n'+result['text'][:12000]+'\n来源：'+result['url']
        if name=='list_inbox_emails':
            return '\n\n'.join(f"{m['id']}｜{m['subject']}\n{m['sender']}｜{m['date']}" for m in result['messages'])+'\n'+result['notice']
        if name=='get_inbox_email':
            m=result['email']
            return f"{m['id']}｜{m['subject']}\n{m['sender']}\n\n{m['body']}\n（只读，未标记已读）"
        return json.dumps(result,ensure_ascii=False,indent=2)
