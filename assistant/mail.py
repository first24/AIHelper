"""QQ SMTP adapter. Credentials never enter model prompts."""
import os
import re
import smtplib
import ssl
from email.message import EmailMessage


def address(value):
    if not isinstance(value, str) or len(value) > 254 or not re.fullmatch(r'[A-Za-z0-9.!#$%&\x27*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', value):
        raise ValueError('请提供完整邮箱地址，不要只填写收件人姓名')
    return value


class Mailer:
    def __init__(self, state, host=None, user=None, password=None, port=None, smtp_factory=None):
        self.state = state
        self.host = host if host is not None else os.getenv('SMTP_HOST', 'smtp.qq.com')
        self.user = user if user is not None else os.getenv('SMTP_USER', '')
        self.password = password if password is not None else os.getenv('SMTP_AUTH_CODE', '')
        self.port = int(port or os.getenv('SMTP_PORT', '465'))
        self.factory = smtp_factory or smtplib.SMTP_SSL

    def send_draft(self, draft_id, owner):
        draft = self.state.draft(draft_id, owner)
        if draft['status'] == 'sent':
            return '此邮件已被 SMTP 服务端接受，未重复发送。草稿编号：' + draft_id
        if draft['status'] in ('sending', 'unknown'):
            return '此邮件正在发送或发送结果未知，请先核查邮箱，未重复发送。草稿编号：' + draft_id
        if not self.user or not self.password:
            raise ValueError('QQ 邮箱尚未配置：请在服务端填写 SMTP_USER 和 SMTP_AUTH_CODE 客户端授权码')
        address(self.user)
        address(draft['recipient'])
        changed = self.state.execute("UPDATE drafts SET status='sending',detail='' WHERE id=? AND owner=? AND status IN ('draft','failed')", (draft_id, owner))
        if not changed:
            return '邮件状态已变化，请使用 /草稿 查询，未重复发送。'
        message = EmailMessage()
        message['From'] = self.user
        message['To'] = draft['recipient']
        message['Subject'] = draft['subject']
        message['Message-ID'] = f'<{draft_id}@aihelper.local>'
        message.set_content(draft['body'])
        client = None
        status, detail = 'unknown', '发送结果未知，请核查邮箱，未自动重发'
        try:
            client = self.factory(self.host, self.port, timeout=25, context=ssl.create_default_context())
            client.login(self.user, self.password)
            refused = client.send_message(message, from_addr=self.user, to_addrs=[draft['recipient']])
            if refused:
                status, detail = 'failed', 'SMTP 服务拒绝收件人'
            else:
                status, detail = 'sent', 'SMTP 服务端已接受；不代表对方已阅读'
        except smtplib.SMTPAuthenticationError:
            status, detail = 'failed', '邮箱鉴权失败，请检查 QQ 客户端授权码和 SMTP 服务是否开启'
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError):
            status, detail = 'failed', 'SMTP 服务明确拒绝本次邮件，请核查账户和收件人'
        except (OSError, smtplib.SMTPException):
            pass
        finally:
            if client:
                try:
                    client.quit()
                except Exception:
                    try:
                        client.close()
                    except Exception:
                        pass
        self.state.execute('UPDATE drafts SET status=?,detail=? WHERE id=? AND owner=?', (status, detail, draft_id, owner))
        return f"邮件 {draft_id}：{detail}。"
