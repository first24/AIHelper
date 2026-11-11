"""Explicit, validated capabilities exposed to the language model."""
import hashlib
import json
import time
from datetime import datetime, timedelta

from .mail import MAIL_STATUSES, address, format_email
from .state import parse_at
from .schemas import AT,ID,REPEAT,spec,string
from .connected_schemas import EXTRA_NAMES,EXTRA_SPECS
SPECS = [
    spec('create_reminder', '保存提醒。先明确内容和未来执行时间，再创建；返回真实编号和时间。',
         {'text': string('提醒内容', 1000), 'at': AT, 'repeat': REPEAT}, ['text','at']),
    spec('update_reminder', '修改本人已有提醒或简报，先从上下文或列表获得编号。',
         {'id': ID, 'text': string('新内容',1000), 'at': AT, 'repeat': REPEAT}, ['id']),
    spec('list_reminders', '列出本人提醒和简报。要找刚刚触发的提醒以便稍后再提醒时，设置 include_completed=true。',
         {'include_completed': {'type':'boolean'}}),
    spec('cancel_reminder', '取消本人指定提醒的后续执行。', {'id': ID}, ['id']),
    spec('snooze_reminder', '在当前聊天稍后再次提醒本人。单次提醒会改期或重新启用；重复提醒会额外创建单次提醒，保留原重复规则。先查到真实编号。',
         {'id': ID, 'minutes': {'type': 'integer', 'minimum': 1, 'maximum': 10080}}, ['id', 'minutes']),
    spec('get_daily_overview', '查看本人今天的提醒、今日到期待办、逾期待办及未设截止日期的待办。'),
    spec('add_todo', '保存本人待办。due 可填写用户给定的截止日期/时间，未知时留空。',
         {'title': string('待办内容',500), 'due': string('截止日期或日期时间，可留空',80)}, ['title']),
    spec('list_todos', '查看本人待办。', {'include_completed': {'type':'boolean'} }),
    spec('complete_todo', '将指定本人待办标记为已完成。', {'id':ID}, ['id']),
    spec('save_contact', '按用户明确提供的资料保存姓名和邮箱。',
         {'name':string('联系人姓名',80),'email':string('完整邮箱地址',254)}, ['name','email']),
    spec('find_contact', '查找用户自己的联系人邮箱，找不到时询问，不编造地址。', {'name':string('联系人姓名',80)}, ['name']),
    spec('draft_email', '生成邮件草稿并显示预览。这个工具不会发送邮件；发送需用户输入 /发送邮件 草稿编号。',
         {'to':string('一个完整邮箱地址，不能是姓名',254),'subject':string('邮件主题',200),'body':string('纯文本邮件正文',8000)}, ['to','subject','body']),
    spec('list_email_drafts', '查看本人最近的邮件草稿和发送状态。'),
    spec('get_email_draft', '查看本人保存的完整邮件内容与当前发送状态，只查询记录。', {'id': ID}, ['id']),
    spec('update_email_draft', '修改尚未发送的邮件草稿并展示完整新预览。已发送、正在发送或结果未知的邮件不能修改。',
         {'id': ID, 'to': string('新的完整收件邮箱',254), 'subject': string('新主题',200),
          'body': string('完整的新正文',8000)}, ['id']),
    spec('save_note', '按用户明确要求保存一则随手记，之后可按关键词检索。',
         {'title': string('简短标题',120), 'body': string('笔记正文',8000)}, ['title','body']),
    spec('list_notes', '查看本人最近的随手记，或按关键词匹配标题和正文。',
         {'query': string('关键词，留空列出最近记录',200)}),
    spec('get_note', '按真实编号查看本人随手记全文。', {'id': ID}, ['id']),
    spec('update_note', '按用户要求修改本人随手记的标题或正文。',
         {'id': ID, 'title': string('新标题',120), 'body': string('完整的新正文',8000)}, ['id']),
    spec('delete_note', '仅在用户明确要求删除指定随手记时使用。', {'id': ID}, ['id']),
    spec('schedule_daily_brief', '按指定未来时间定期在当前聊天向本人汇总未完成待办，内容不需要再次调用模型。',
         {'at':AT,'repeat':REPEAT}, ['at']),
    spec('remember_preference', '仅在用户要求记住偏好时保存，不保存密钥、密码或授权码。',
         {'key':string('偏好名称',enum=['language','email_signature','brief_style']), 'value':string('偏好值',500)}, ['key','value']),
    spec('start_code_repair', '触发原有 GitHub 演示代码修复，仅支持 demo_shop/pricing.py。',
         {'problem':string('完整问题描述',3000)}, ['problem']),
]
SPECS+=EXTRA_SPECS


def validate(name, args):
    known = next((s['function']['parameters'] for s in SPECS if s['function']['name'] == name), None)
    if known is None or not isinstance(args, dict):
        raise ValueError('未知工具或无效参数')
    if not set(args) <= set(known['properties']) or not set(known['required']) <= set(args):
        raise ValueError('工具参数缺失或包含不支持的字段')
    for key, value in args.items():
        rule = known['properties'][key]
        if rule['type'] == 'string':
            if not isinstance(value, str) or len(value) > rule['maxLength']:
                raise ValueError('参数长度或类型不正确：' + key)
            if key in known['required'] and not value.strip():
                raise ValueError('必填参数不能为空：' + key)
        elif rule['type'] == 'boolean' and type(value) is not bool:
            raise ValueError('参数必须为布尔值：' + key)
        elif rule['type'] == 'integer':
            if type(value) is not int or not rule['minimum'] <= value <= rule['maximum']:
                raise ValueError(f"参数 {key} 须为 {rule['minimum']}–{rule['maximum']} 之间的整数")
        if 'enum' in rule and value not in rule['enum']:
            raise ValueError('参数取值不支持：' + key)


class Tools:
    def __init__(self, state, owner, chat, message_id, zone, legacy=None, clock=time.time, services=None):
        self.state, self.owner, self.chat = state, owner, chat
        self.message_id, self.zone, self.legacy, self.clock = message_id, zone, legacy, clock
        self.services=services

    def execute(self, name, args):
        validate(name, args)
        effect_id = hashlib.sha256((self.message_id + name + json.dumps(args,sort_keys=True,ensure_ascii=False)).encode()).hexdigest()[:16]
        cached = self.state.rows('SELECT result FROM effects WHERE id=?', (effect_id,))
        if cached:
            return json.loads(cached[0]['result'])
        handlers = {'create_reminder':self.create_reminder,'update_reminder':self.update_reminder,
                    'list_reminders':self.list_reminders,'cancel_reminder':self.cancel_reminder,
                    'snooze_reminder':self.snooze_reminder,'get_daily_overview':self.get_daily_overview,
                    'add_todo':self.add_todo,'list_todos':self.list_todos,'complete_todo':self.complete_todo,
                    'save_contact':self.save_contact,'find_contact':self.find_contact,'draft_email':self.draft_email,
                    'list_email_drafts':self.list_email_drafts,'schedule_daily_brief':self.schedule_daily_brief,
                    'get_email_draft':self.get_email_draft,'update_email_draft':self.update_email_draft,
                    'save_note':self.save_note,'list_notes':self.list_notes,'get_note':self.get_note,
                    'update_note':self.update_note,'delete_note':self.delete_note,
                    'remember_preference':self.remember_preference,'start_code_repair':self.start_code_repair}
        if name in EXTRA_NAMES:
            if self.services is None:
                raise ValueError('联网扩展尚未启动，请检查助理运行配置')
            result=self.services.execute(name,args,self.owner,self.chat,self.message_id,effect_id)
        else:
            result = handlers[name](effect_id=effect_id, **args)
        if not name.startswith(('list_', 'find_', 'get_')) and name not in ('web_search','read_webpage'):
            self.state.execute('INSERT OR IGNORE INTO effects(id,result) VALUES(?,?)', (effect_id,json.dumps(result,ensure_ascii=False)))
        return result

    def format_due(self, stamp):
        return datetime.fromtimestamp(stamp,self.zone).isoformat(timespec='seconds')

    def create_reminder(self, text, at, repeat='once', effect_id='', kind='reminder'):
        due = parse_at(at,self.zone,self.clock())
        if repeat == 'weekdays' and datetime.fromtimestamp(due,self.zone).weekday() >= 5:
            raise ValueError('工作日提醒的首次时间应在周一至周五')
        schedule_id = 'r_' + effect_id
        self.state.execute('INSERT OR IGNORE INTO schedules(id,owner,chat,text,kind,due,repeat) VALUES(?,?,?,?,?,?,?)',
                           (schedule_id,self.owner,self.chat,text,kind,due,repeat))
        return {'ok':True,'id':schedule_id,'at':self.format_due(due),'repeat':repeat,
                'receipt':f"已创建{'待办简报' if kind=='brief' else '提醒'} {schedule_id}：{text}\n时间：{self.format_due(due)}；规则：{repeat}"}

    def schedule_daily_brief(self, at, repeat='daily', effect_id=''):
        return self.create_reminder('未完成待办汇总',at,repeat,effect_id,'brief')

    def list_reminders(self, include_completed=False, effect_id=''):
        order='DESC' if include_completed else 'ASC'
        rows=self.state.rows("SELECT id,text,kind,due,repeat,chat,status FROM schedules WHERE owner=? AND (status='active' OR (? AND status='completed')) ORDER BY due "+order+" LIMIT 50",
                             (self.owner,int(include_completed)))
        for row in rows:
            row['at']=self.format_due(row.pop('due'))
            row['channel']='微信' if row.pop('chat').startswith('weixin:') else '飞书'
        return {'ok':True,'reminders':rows}

    def snooze_reminder(self, id, minutes, effect_id=''):
        with self.state.transaction() as db:
            previous=db.execute('SELECT result FROM effects WHERE id=?',(effect_id,)).fetchone()
            if previous:
                return json.loads(previous['result'])
            row=db.execute("SELECT * FROM schedules WHERE id=? AND owner=? AND status IN ('active','completed')",
                           (id,self.owner)).fetchone()
            if not row:
                raise ValueError('未找到可稍后提醒的记录，请先用 /提醒 查询编号')
            due=self.clock()+minutes*60
            if row['repeat']=='once':
                schedule_id=id
                db.execute("UPDATE schedules SET due=?,status='active',chat=? WHERE id=? AND owner=?",
                           (due,self.chat,id,self.owner))
                detail=''
            else:
                schedule_id='r_'+effect_id
                db.execute('INSERT OR IGNORE INTO schedules(id,owner,chat,text,kind,due,repeat) VALUES(?,?,?,?,?,?,?)',
                           (schedule_id,self.owner,self.chat,row['text'],row['kind'],due,'once'))
                detail='\n原重复提醒继续按原规则执行。'
            db.execute("UPDATE deliveries SET status='canceled' WHERE schedule_id=? AND owner=? AND status='pending'",
                       (id,self.owner))
            result={'ok':True,'id':schedule_id,'at':self.format_due(due),
                    'receipt':f"已安排 {minutes} 分钟后再次提醒 {schedule_id}：{row['text']}\n时间：{self.format_due(due)}"+detail}
            db.execute('INSERT OR IGNORE INTO effects(id,result) VALUES(?,?)',
                       (effect_id,json.dumps(result,ensure_ascii=False)))
            return result

    def get_daily_overview(self, effect_id=''):
        now=datetime.fromtimestamp(self.clock(),self.zone)
        tomorrow=(now+timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0)
        groups={'overdue':[],'today':[],'undated':[]}
        todos=self.state.rows('SELECT id,title,due FROM todos WHERE owner=? AND done=0 ORDER BY due,id',(self.owner,))
        for todo in todos:
            if not todo['due']:
                groups['undated'].append(todo)
                continue
            try:
                deadline=datetime.fromisoformat(todo['due'].replace('Z','+00:00'))
                if deadline.tzinfo is None:
                    deadline=deadline.replace(tzinfo=self.zone)
                deadline=deadline.astimezone(self.zone)
            except (ValueError,AttributeError):
                groups['undated'].append(todo)
                continue
            date_only=len(todo['due'])==10
            overdue=deadline.date()<now.date() if date_only else deadline<now
            if overdue:
                groups['overdue'].append(todo)
            elif deadline.date()==now.date():
                groups['today'].append(todo)
        reminders=self.state.rows("SELECT id,text,due,chat FROM schedules WHERE owner=? AND status='active' AND due<? ORDER BY due",
                                  (self.owner,tomorrow.timestamp()))
        for reminder in reminders:
            reminder['at']=self.format_due(reminder.pop('due'))
            reminder['channel']='微信' if reminder.pop('chat').startswith('weixin:') else '飞书'
        lines=[f"今日概览 {now:%Y-%m-%d}",f"未完成待办共 {len(todos)} 项"]
        for key,label in (('overdue','已逾期待办'),('today','今日到期待办'),('undated','截止日期待补充的待办')):
            items=groups[key]
            lines.append(f"\n{label}（{len(items)} 项）")
            lines.extend(f"• {item['id']}｜{item['title']}"+(f"｜{item['due']}" if item['due'] else '') for item in items[:5])
            if len(items)>5:
                lines.append('更多项目请用 /待办 查看。')
        lines.append(f"\n今天待触发的提醒（{len(reminders)} 项，含待补发）")
        lines.extend(f"• {item['id']}｜{item['text']}｜{item['at']}｜{item['channel']}" for item in reminders[:10])
        if len(reminders)>10:
            lines.append('更多提醒请用 /提醒 查看。')
        calendar_events=[]
        calendar_status='未连接'
        if self.services:
            try:
                start=now.replace(hour=0,minute=0,second=0,microsecond=0)
                calendar_events=self.services.calendar.events(self.owner,start.isoformat(),tomorrow.isoformat())['events']
                calendar_status='已连接'
                lines.append(f'\n今日个人飞书日程（{len(calendar_events)} 项）')
                lines.extend(f"• {item['title']}｜{item['start']} 至 {item['end']}｜{item['id']}" for item in calendar_events[:20])
                if len(calendar_events)>20:
                    lines.append('更多个人日程请用 /日历 查看。')
            except (ValueError,RuntimeError,OSError):
                calendar_status='暂不可用，请用 /连接状态 检查授权'
                lines.append('\n个人飞书日历：'+calendar_status)
        return {'ok':True,'date':now.date().isoformat(),'pending_todos':len(todos),
                'counts':{**{key:len(items) for key,items in groups.items()},'reminders':len(reminders)},
                'todos':{key:items[:20] for key,items in groups.items()},
                'reminders':reminders[:20],'calendar_status':calendar_status,'calendar_events':calendar_events[:20],
                'receipt':'\n'.join(lines)}

    def update_reminder(self, id, text=None, at=None, repeat=None, effect_id=''):
        with self.state.transaction() as db:
            row=db.execute("SELECT * FROM schedules WHERE id=? AND owner=? AND status='active'",(id,self.owner)).fetchone()
            if not row:
                raise ValueError('未找到你的有效提醒')
            due=parse_at(at,self.zone,self.clock()) if at else row['due']
            rule=repeat if repeat is not None else row['repeat']
            if rule=='weekdays' and datetime.fromtimestamp(due,self.zone).weekday()>=5:
                raise ValueError('工作日提醒的时间应在周一至周五')
            db.execute('UPDATE schedules SET text=?,due=?,repeat=? WHERE id=? AND owner=?',
                       (text if text is not None else row['text'],due,rule,id,self.owner))
            db.execute("UPDATE deliveries SET status='canceled' WHERE schedule_id=? AND owner=? AND status='pending'",(id,self.owner))
        return {'ok':True,'id':id,'at':self.format_due(due),'repeat':rule,'receipt':f'已修改提醒 {id}，下次时间：{self.format_due(due)}；规则：{rule}'}

    def cancel_reminder(self,id,effect_id=''):
        with self.state.transaction() as db:
            changed=db.execute("UPDATE schedules SET status='canceled' WHERE id=? AND owner=?",(id,self.owner)).rowcount
            if not changed:
                raise ValueError('未找到你的提醒')
            db.execute("UPDATE deliveries SET status='canceled' WHERE schedule_id=? AND owner=? AND status='pending'",(id,self.owner))
        return {'ok':True,'receipt':'已取消提醒 '+id}

    def add_todo(self,title,due='',effect_id=''):
        if due:
            try:
                datetime.fromisoformat(due.replace('Z','+00:00'))
            except ValueError:
                raise ValueError('待办截止时间须为 ISO 日期或日期时间') from None
        todo_id='t_'+effect_id
        self.state.execute('INSERT OR IGNORE INTO todos(id,owner,title,due) VALUES(?,?,?,?)',(todo_id,self.owner,title,due))
        return {'ok':True,'id':todo_id,'receipt':f'已记录待办 {todo_id}：{title}'+(f'；截止：{due}' if due else '')}

    def list_todos(self,include_completed=False,effect_id=''):
        return {'ok':True,'todos':self.state.rows('SELECT id,title,due,done FROM todos WHERE owner=? AND (? OR done=0) ORDER BY rowid DESC LIMIT 50',(self.owner,int(include_completed)))}

    def complete_todo(self,id,effect_id=''):
        changed=self.state.execute('UPDATE todos SET done=1 WHERE id=? AND owner=?',(id,self.owner))
        if not changed:
            raise ValueError('未找到你的待办')
        return {'ok':True,'receipt':'已完成待办 '+id}

    def save_contact(self,name,email,effect_id=''):
        address(email)
        self.state.execute('INSERT INTO contacts(owner,name,email) VALUES(?,?,?) ON CONFLICT(owner,name) DO UPDATE SET email=excluded.email',(self.owner,name,email))
        return {'ok':True,'receipt':f'已保存联系人：{name} <{email}>'}

    def find_contact(self,name,effect_id=''):
        return {'ok':True,'contacts':self.state.rows('SELECT name,email FROM contacts WHERE owner=? AND name=?',(self.owner,name))}

    def draft_email(self,to,subject,body,effect_id=''):
        address(to)
        if '\r' in subject or '\n' in subject:
            raise ValueError('邮件主题不能包含换行')
        draft_id='e_'+effect_id
        self.state.execute('INSERT OR IGNORE INTO drafts(id,owner,chat,recipient,subject,body) VALUES(?,?,?,?,?,?)',
                           (draft_id,self.owner,self.chat,to,subject,body))
        return {'ok':True,'id':draft_id,'receipt':f'邮件草稿 {draft_id}（尚未发送）\n收件人：{to}\n主题：{subject}\n\n{body}\n\n发送请回复：/发送邮件 {draft_id}'}

    def list_email_drafts(self,effect_id=''):
        rows=self.state.rows('SELECT id,recipient,subject,status,detail FROM drafts WHERE owner=? ORDER BY rowid DESC LIMIT 20',(self.owner,))
        for row in rows:
            row['status_label']=MAIL_STATUSES.get(row['status'],row['status'])
        return {'ok':True,'drafts':rows}

    def get_email_draft(self,id,effect_id=''):
        draft=self.state.draft(id,self.owner)
        draft={key:draft[key] for key in ('id','recipient','subject','body','status','detail')}
        return {'ok':True,'id':id,'draft':draft,'receipt':format_email(draft)}

    def update_email_draft(self,id,to=None,subject=None,body=None,effect_id=''):
        if to is None and subject is None and body is None:
            raise ValueError('请说明要修改的收件人、主题或正文')
        if to is not None:
            address(to)
        if subject is not None and (not subject.strip() or '\r' in subject or '\n' in subject):
            raise ValueError('邮件主题不能为空或包含换行')
        if body is not None and not body.strip():
            raise ValueError('邮件正文不能为空')
        with self.state.transaction() as db:
            row=db.execute('SELECT * FROM drafts WHERE id=? AND owner=?',(id,self.owner)).fetchone()
            if not row:
                raise ValueError('未找到你的邮件草稿')
            if row['status'] not in ('draft','failed'):
                raise ValueError('仅可修改待发送或发送失败的草稿，请为这次修改新建邮件')
            db.execute("UPDATE drafts SET recipient=?,subject=?,body=?,status='draft',detail='' WHERE id=? AND owner=?",
                       (to if to is not None else row['recipient'],subject if subject is not None else row['subject'],
                        body if body is not None else row['body'],id,self.owner))
        result=self.get_email_draft(id)
        result['receipt']='已更新草稿，尚未发送。\n'+result['receipt']
        return result

    def save_note(self,title,body,effect_id=''):
        note_id='n_'+effect_id
        now=self.clock()
        self.state.execute('INSERT OR IGNORE INTO notes(id,owner,title,body,created,updated) VALUES(?,?,?,?,?,?)',
                           (note_id,self.owner,title,body,now,now))
        return {'ok':True,'id':note_id,'receipt':f'已保存随手记 {note_id}：{title}'}

    def list_notes(self,query='',effect_id=''):
        query=query.strip()
        escaped=query.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
        rows=self.state.rows("SELECT id,title,body,updated FROM notes WHERE owner=? AND (title LIKE ? ESCAPE ? OR body LIKE ? ESCAPE ?) ORDER BY updated DESC,id LIMIT 20",
                             (self.owner,'%'+escaped+'%','\\','%'+escaped+'%','\\'))
        notes=[{**row,'body':row['body'][:200]} for row in rows]
        lines=['随手记检索结果' if query else '最近的随手记']
        lines.extend(f"• {row['id']}｜{row['title']}\n{row['body']}" for row in notes)
        if not notes:
            lines.append('没有找到对应的随手记。')
        else:
            lines.append('查看全文：/查看笔记 笔记编号')
        return {'ok':True,'notes':notes,'receipt':'\n'.join(lines)}

    def get_note(self,id,effect_id=''):
        rows=self.state.rows('SELECT id,title,body,created,updated FROM notes WHERE id=? AND owner=?',(id,self.owner))
        if not rows:
            raise ValueError('未找到你的随手记')
        note=rows[0]
        return {'ok':True,'id':id,'note':note,'receipt':f"随手记 {id}｜{note['title']}\n\n{note['body']}"}

    def update_note(self,id,title=None,body=None,effect_id=''):
        if title is None and body is None:
            raise ValueError('请说明随手记的新标题或正文')
        if (title is not None and not title.strip()) or (body is not None and not body.strip()):
            raise ValueError('随手记标题和正文不能为空')
        with self.state.transaction() as db:
            row=db.execute('SELECT title,body FROM notes WHERE id=? AND owner=?',(id,self.owner)).fetchone()
            if not row:
                raise ValueError('未找到你的随手记')
            db.execute('UPDATE notes SET title=?,body=?,updated=? WHERE id=? AND owner=?',
                       (title if title is not None else row['title'],body if body is not None else row['body'],self.clock(),id,self.owner))
        result=self.get_note(id)
        result['receipt']='已更新'+result['receipt']
        return result

    def delete_note(self,id,effect_id=''):
        changed=self.state.execute('DELETE FROM notes WHERE id=? AND owner=?',(id,self.owner))
        if not changed:
            raise ValueError('未找到你的随手记')
        return {'ok':True,'receipt':'已删除随手记 '+id}

    def remember_preference(self,key,value,effect_id=''):
        self.state.execute('INSERT INTO preferences(owner,key,value) VALUES(?,?,?) ON CONFLICT(owner,key) DO UPDATE SET value=excluded.value',(self.owner,key,value))
        return {'ok':True,'receipt':f'已保存偏好：{key}={value}'}

    def start_code_repair(self,problem,effect_id=''):
        if self.legacy is None:
            raise ValueError('GitHub 修复尚未配置')
        message_id='nl-repair:'+self.message_id
        self.legacy.receive(message_id,self.owner,self.chat,'/修复 '+problem)
        job_id=hashlib.sha256(message_id.encode()).hexdigest()[:16]
        if not self.legacy.store.get(job_id):
            raise ValueError('代码任务未能进入队列，请查看单独的回执后重试')
        return {'ok':True,'id':job_id,'receipt':f'已提交 GitHub 演示修复任务 {job_id}，完成后会另发 PR 结果；当前尚未修复完成。'}
