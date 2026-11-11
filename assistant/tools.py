"""Explicit, validated capabilities exposed to the language model."""
import hashlib
import json
import time
from datetime import datetime

from .mail import address
from .state import parse_at


def string(description, limit=500, **extra):
    return {'type': 'string', 'description': description, 'maxLength': limit, **extra}


def spec(name, description, properties=None, required=None):
    return {'type': 'function', 'function': {'name': name, 'description': description,
            'parameters': {'type': 'object', 'properties': properties or {},
                           'required': required or [], 'additionalProperties': False}}}


REPEAT = string('once=单次，daily=每天，weekdays=周一至周五，weekly=每周', enum=['once','daily','weekdays','weekly'])
AT = string('未来的完整 ISO 日期时间，包含时区偏移，例如 2026-11-12T15:00:00+08:00', 80)
ID = string('从工具结果获得的真实编号，不能编造', 64)
SPECS = [
    spec('create_reminder', '保存提醒。先明确内容和未来执行时间，再创建；返回真实编号和时间。',
         {'text': string('提醒内容', 1000), 'at': AT, 'repeat': REPEAT}, ['text','at']),
    spec('update_reminder', '修改本人已有提醒或简报，先从上下文或列表获得编号。',
         {'id': ID, 'text': string('新内容',1000), 'at': AT, 'repeat': REPEAT}, ['id']),
    spec('list_reminders', '列出本人当前生效的提醒和简报。'),
    spec('cancel_reminder', '取消本人指定提醒的后续执行。', {'id': ID}, ['id']),
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
    spec('schedule_daily_brief', '按指定未来时间定期向本人飞书汇总未完成待办，内容不需要再次调用模型。',
         {'at':AT,'repeat':REPEAT}, ['at']),
    spec('remember_preference', '仅在用户要求记住偏好时保存，不保存密钥、密码或授权码。',
         {'key':string('偏好名称',enum=['language','email_signature','brief_style']), 'value':string('偏好值',500)}, ['key','value']),
    spec('start_code_repair', '触发原有 GitHub 演示代码修复，仅支持 demo_shop/pricing.py。',
         {'problem':string('完整问题描述',3000)}, ['problem']),
]


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
        if 'enum' in rule and value not in rule['enum']:
            raise ValueError('参数取值不支持：' + key)


class Tools:
    def __init__(self, state, owner, chat, message_id, zone, legacy=None, clock=time.time):
        self.state, self.owner, self.chat = state, owner, chat
        self.message_id, self.zone, self.legacy, self.clock = message_id, zone, legacy, clock

    def execute(self, name, args):
        validate(name, args)
        effect_id = hashlib.sha256((self.message_id + name + json.dumps(args,sort_keys=True,ensure_ascii=False)).encode()).hexdigest()[:16]
        cached = self.state.rows('SELECT result FROM effects WHERE id=?', (effect_id,))
        if cached:
            return json.loads(cached[0]['result'])
        handlers = {'create_reminder':self.create_reminder,'update_reminder':self.update_reminder,
                    'list_reminders':self.list_reminders,'cancel_reminder':self.cancel_reminder,
                    'add_todo':self.add_todo,'list_todos':self.list_todos,'complete_todo':self.complete_todo,
                    'save_contact':self.save_contact,'find_contact':self.find_contact,'draft_email':self.draft_email,
                    'list_email_drafts':self.list_email_drafts,'schedule_daily_brief':self.schedule_daily_brief,
                    'remember_preference':self.remember_preference,'start_code_repair':self.start_code_repair}
        result = handlers[name](effect_id=effect_id, **args)
        if not name.startswith(('list_', 'find_')):
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

    def list_reminders(self, effect_id=''):
        rows=self.state.rows("SELECT id,text,kind,due,repeat FROM schedules WHERE owner=? AND status='active' ORDER BY due LIMIT 50",(self.owner,))
        for row in rows:
            row['at']=self.format_due(row.pop('due'))
        return {'ok':True,'reminders':rows}

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
        return {'ok':True,'drafts':self.state.rows('SELECT id,recipient,subject,status,detail FROM drafts WHERE owner=? ORDER BY rowid DESC LIMIT 20',(self.owner,))}

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
