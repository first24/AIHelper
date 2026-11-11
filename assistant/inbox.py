"""Read-only QQ IMAP access. Reads use BODY.PEEK and never set Seen flags."""
from contextlib import contextmanager
import email
from email import policy
from email.header import decode_header, make_header
from email.utils import getaddresses
import imaplib
import os
import re
import ssl

from .mail import address
from .web import text_from_html


def decode_field(value):
    try:
        return str(make_header(decode_header(value or '')))
    except (LookupError,UnicodeError,ValueError):
        return str(value or '')


class Inbox:
    def __init__(self,config,factory=imaplib.IMAP4_SSL):
        self.config=config
        self.factory=factory
        self.user=os.getenv('SMTP_USER','')
        self.password=os.getenv('SMTP_AUTH_CODE','')
        self.host=os.getenv('IMAP_HOST','imap.qq.com')

    def authorize(self,owner):
        configured=os.getenv('MAILBOX_OWNER_OPEN_ID','').strip()
        if not configured and len(self.config.users)==1:
            configured=next(iter(self.config.users))
        if owner!=configured or owner not in self.config.users:
            raise ValueError('当前用户未关联这个 QQ 邮箱')

    @contextmanager
    def connection(self,owner):
        self.authorize(owner)
        if not self.user or not self.password:
            raise ValueError('请先配置 QQ 邮箱地址和客户端授权码')
        client=None
        try:
            client=self.factory(self.host,993,timeout=25,ssl_context=ssl.create_default_context())
            client.login(self.user,self.password)
            status,_=client.select('INBOX',readonly=True)
            if status!='OK':
                raise RuntimeError('QQ 收件箱无法以只读方式打开，请检查 IMAP 服务')
            yield client
        except imaplib.IMAP4.error:
            raise RuntimeError('QQ IMAP 操作失败，请确认已开启 IMAP 并使用有效的客户端授权码') from None
        except (OSError,TimeoutError):
            raise RuntimeError('QQ IMAP 网络连接失败，请稍后重试') from None
        finally:
            if client:
                try:
                    client.logout()
                except (imaplib.IMAP4.error,OSError):
                    pass

    @staticmethod
    def validity(client):
        response=client.response('UIDVALIDITY')[1]
        value=response[0].decode('ascii') if response and response[0] else ''
        if not value.isdigit():
            raise RuntimeError('QQ 未返回有效的邮箱 UIDVALIDITY')
        return value

    @staticmethod
    def uids(client,unread_only=False):
        status,data=client.uid('search',None,'UNSEEN' if unread_only else 'ALL')
        if status!='OK':
            raise RuntimeError('QQ 邮件查询失败')
        return sorted(int(value) for value in (data[0] or b'').split())

    @staticmethod
    def fetch(client,uid,validity,full=False):
        status,data=client.uid('fetch',str(uid),'(UID RFC822.SIZE BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID REPLY-TO REFERENCES)])')
        records=[item for item in data or [] if isinstance(item,tuple) and isinstance(item[1],bytes)]
        if status!='OK' or not records:
            raise ValueError('这封邮件已不存在或无法读取')
        metadata,raw=records[0]
        size=re.search(rb'RFC822.SIZE (\d+)',metadata)
        if full:
            if not size:
                raise ValueError('邮箱未返回邮件大小，暂不读取正文')
            if int(size[1])>2000000:
                raise ValueError('这封邮件超过 2 MB，请先在 QQ 邮箱查看，当前支持小型纯文本或 HTML 邮件')
            status,data=client.uid('fetch',str(uid),'(UID BODY.PEEK[])')
            records=[item for item in data or [] if isinstance(item,tuple) and isinstance(item[1],bytes)]
            if status!='OK' or not records:
                raise RuntimeError('QQ 邮件正文读取失败')
            raw=records[0][1]
            if len(raw)>2000000:
                raise ValueError('邮件内容超过当前读取大小限制')
        message=email.message_from_bytes(raw,policy=policy.default)
        result={'id':f'm_{validity}_{uid}','subject':decode_field(message.get('Subject')),
                'sender':decode_field(message.get('From')),'date':str(message.get('Date','')),
                'message_id':str(message.get('Message-ID','')),
                'references':str(message.get('References','')),
                'reply_to':str(message.get('Reply-To') or message.get('From') or '')}
        if full:
            body=message.get_body(preferencelist=('plain','html'))
            text=''
            if body:
                try:
                    text=body.get_content()
                except (LookupError,UnicodeError):
                    text=(body.get_payload(decode=True) or b'').decode('utf-8',errors='replace')
                if body.get_content_subtype()=='html':
                    text=text_from_html(text)[1]
            result['body']=text[:16000]
            result['truncated']=len(text)>16000
        return result

    def list(self,owner,unread_only=True,query='',limit=10):
        with self.connection(owner) as client:
            validity=self.validity(client)
            uids=self.uids(client,unread_only)
            results=[]
            for uid in reversed(uids[-50:]):
                message=self.fetch(client,uid,validity)
                if query and query.casefold() not in (message['subject']+' '+message['sender']).casefold():
                    continue
                results.append({key:message[key] for key in ('id','subject','sender','date')})
                if len(results)>=limit:
                    break
        return {'ok':True,'messages':results,'unread_only':unread_only,
                'notice':'只读查询，未标记已读。关键词匹配最近 50 封候选邮件的主题和发件人。'}

    def read(self,owner,id):
        match=re.fullmatch(r'm_(\d+)_(\d+)',id)
        if not match:
            raise ValueError('请先查询收件箱，使用返回的 m_ 邮件编号')
        with self.connection(owner) as client:
            validity=self.validity(client)
            if validity!=match[1]:
                raise ValueError('邮箱编号已更新，请重新查询收件箱')
            return self.fetch(client,int(match[2]),validity,full=True)

    def reply_draft(self,state,owner,chat,id,body,effect_id):
        original=self.read(owner,id)
        recipients=getaddresses([original['reply_to']])
        if len(recipients)!=1:
            raise ValueError('原邮件的回复地址不明确，请直接指定收件人新建草稿')
        recipient=address(recipients[0][1])
        subject=original['subject']
        if not subject.lower().startswith('re:'):
            subject='Re: '+subject
        subject=subject.replace('\r',' ').replace('\n',' ')[:200]
        message_id=original['message_id']
        references=(original['references']+' '+message_id).strip()
        for value in (message_id,references):
            if '\r' in value or '\n' in value or len(value)>4000:
                raise ValueError('原邮件的线程标识格式无效，请新建独立草稿')
        draft_id='e_'+effect_id
        state.execute('INSERT OR IGNORE INTO drafts(id,owner,chat,recipient,subject,body,in_reply_to,references_header) VALUES(?,?,?,?,?,?,?,?)',
                      (draft_id,owner,chat,recipient,subject,body,message_id,references))
        return draft_id
