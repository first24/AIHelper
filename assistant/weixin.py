"""Personal Weixin text channel using Tencent's documented iLink protocol.

Protocol reference: https://github.com/Tencent/openclaw-weixin/blob/main/docs/protocol.md
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


API_BASE = 'https://ilinkai.weixin.qq.com'
PROTOCOL_VERSION = '3.0.0/aihelper-2.1'


class WeixinError(RuntimeError):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def api_origin(value):
    parsed = urlsplit(value)
    host = parsed.hostname or ''
    if (parsed.scheme != 'https' or parsed.port not in (None, 443)
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in ('', '/')
            or not host.endswith(('.weixin.qq.com', '.wechat.com'))):
        raise ValueError('微信返回了不支持的 API 地址')
    return 'https://' + host


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        raise WeixinError('微信接口意外返回 HTTP 重定向', code)


class WeixinAPI:
    def __init__(self, base=API_BASE, token=''):
        self.base = api_origin(base)
        self.token = token
        self.opener = build_opener(NoRedirect())

    def request(self, path, body=None, timeout=40):
        headers = {'iLink-App-Id': 'bot', 'iLink-App-ClientVersion': str(3 << 16)}
        payload = None
        if body is not None:
            body = dict(body)
            if self.token:
                body['base_info'] = {'channel_version': PROTOCOL_VERSION,
                                     'bot_agent': 'AIHelper/2.1'}
                headers['Authorization'] = 'Bearer ' + self.token
            headers.update({'Content-Type': 'application/json',
                            'AuthorizationType': 'ilink_bot_token',
                            'X-WECHAT-UIN': base64.b64encode(str(secrets.randbits(32)).encode()).decode()})
            payload = json.dumps(body, ensure_ascii=False).encode()
        request = Request(self.base + '/' + path, data=payload, headers=headers,
                          method='POST' if body is not None else 'GET')
        try:
            with self.opener.open(request, timeout=timeout) as response:
                data = json.load(response)
        except HTTPError as error:
            code = error.code
            error.close()
            raise WeixinError(f'微信 HTTP {code}', code) from None
        except (URLError, TimeoutError, OSError):
            raise WeixinError('微信网络连接暂时失败') from None
        except (ValueError, UnicodeDecodeError):
            raise WeixinError('微信接口返回格式无效') from None
        if not isinstance(data, dict):
            raise WeixinError('微信接口返回格式无效')
        for field in ('ret', 'errcode'):
            code = data.get(field)
            if code not in (None, 0):
                safe_code = code if isinstance(code, int) else 'unknown'
                raise WeixinError(f'微信接口返回错误 {safe_code}', safe_code)
        return data


def storage_dir(db_path):
    return Path(db_path).parent / 'weixin'


def write_private_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    temporary = path.with_suffix('.tmp')
    fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o600)
    os.replace(temporary, path)


def load_account(db_path):
    path = storage_dir(db_path) / 'account.json'
    if not path.exists():
        return None
    try:
        account = json.loads(path.read_text(encoding='utf-8'))
        for key in ('bot_token', 'ilink_bot_id', 'ilink_user_id', 'baseurl'):
            if not isinstance(account.get(key), str) or not account[key].strip():
                raise ValueError()
        account['baseurl'] = api_origin(account['baseurl'])
        return account
    except (OSError, ValueError, AttributeError):
        raise WeixinError('微信绑定文件无效，请重新运行 weixin-login') from None


def login(db_path):
    """Bind the operator's own ClawBot. The QR image is stored in the data volume."""
    import qrcode
    from qrcode.image.pure import PyPNGImage
    from .state import acquire_instance_lock

    folder = storage_dir(db_path)
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o700)
    # Login has a separate lock from the running assistant.
    lock = acquire_instance_lock(folder / 'login.sqlite3')
    try:
        previous = load_account(db_path)
        tokens = [previous['bot_token']] if previous else []
        for _ in range(3):
            api = WeixinAPI()
            qr = api.request('ilink/bot/get_bot_qrcode?bot_type=3', {'local_token_list': tokens})
            if not isinstance(qr.get('qrcode'), str) or not isinstance(qr.get('qrcode_img_content'), str):
                raise WeixinError('微信未返回绑定二维码')
            image_path = folder / 'login.png'
            qrcode.make(qr['qrcode_img_content'], image_factory=PyPNGImage).save(str(image_path))
            image_path.chmod(0o600)
            print('QR_READY ' + str(image_path), flush=True)
            deadline = time.monotonic() + 240
            verify_code = ''
            while time.monotonic() < deadline:
                query = {'qrcode': qr['qrcode']}
                if verify_code:
                    query['verify_code'] = verify_code
                try:
                    result = api.request('ilink/bot/get_qrcode_status?' + urlencode(query))
                except WeixinError:
                    time.sleep(2)
                    continue
                status = result.get('status', 'wait')
                if status == 'confirmed':
                    account = {key: result.get(key) for key in
                               ('bot_token', 'ilink_bot_id', 'ilink_user_id', 'baseurl')}
                    if any(not isinstance(value, str) or not value for value in account.values()):
                        raise WeixinError('微信绑定返回缺少必要字段')
                    account['baseurl'] = api_origin(account['baseurl'])
                    write_private_json(folder / 'account.json', account)
                    image_path.unlink(missing_ok=True)
                    print('WEIXIN_BOUND 微信绑定成功，凭证已在数据卷安全保存。', flush=True)
                    return
                if status in ('expired', 'verify_code_blocked'):
                    print('QR_EXPIRED 正在刷新二维码。', flush=True)
                    break
                if status == 'scaned_but_redirect' and result.get('redirect_host'):
                    api = WeixinAPI('https://' + result['redirect_host'])
                if status == 'scaned':
                    verify_code = ''
                if 'need_verifycode' in status:
                    print('PIN_REQUIRED 请将手机显示的数字填写到数据卷 weixin/verify-code 文件。', flush=True)
                    pin_path = folder / 'verify-code'
                    while time.monotonic() < deadline and not pin_path.exists():
                        time.sleep(1)
                    if pin_path.exists():
                        verify_code = pin_path.read_text(encoding='utf-8-sig').strip()
                        pin_path.unlink()
                        if not verify_code.isascii() or not verify_code.isdigit():
                            raise WeixinError('微信验证码应为数字')
                if status == 'binded_redirect':
                    raise WeixinError('微信已绑定其他实例，请在微信 ClawBot 中解除旧绑定后重试')
                time.sleep(1)
        raise WeixinError('微信扫码绑定已超时，请重新运行 weixin-login')
    finally:
        lock.close()


class WeixinChannel:
    def __init__(self, config, state):
        self.config = config
        self.state = state
        owner = os.getenv('WEIXIN_OWNER_OPEN_ID', '').strip()
        if not owner and len(config.users) == 1:
            owner = next(iter(config.users))
        if owner not in config.users:
            raise ValueError('请将 WEIXIN_OWNER_OPEN_ID 配置为已授权的本人飞书 open_id')
        self.owner = owner
        self.status = 'login_required'
        self.stale_token = ''
        self.send_lock = threading.Lock()
        with state.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS weixin_sync (account TEXT PRIMARY KEY, cursor TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS weixin_context (account TEXT, peer TEXT, token TEXT, PRIMARY KEY(account,peer))')
            db.execute('CREATE TABLE IF NOT EXISTS weixin_inbox (id TEXT PRIMARY KEY, account TEXT, chat TEXT, body TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS weixin_sent (id TEXT PRIMARY KEY, sent_at REAL)')

    @staticmethod
    def chat_id(account):
        digest = hashlib.sha256(account['ilink_bot_id'].encode()).hexdigest()[:16]
        return 'weixin:' + digest + ':' + account['ilink_user_id']

    def ingest(self, account, result):
        """Persist the cursor and pending messages atomically before enqueueing work."""
        messages = result.get('msgs', [])
        if not isinstance(messages, list) or any(not isinstance(msg, dict) for msg in messages):
            raise WeixinError('微信消息批次格式无效，游标未推进')
        bot = account['ilink_bot_id']
        peer = account['ilink_user_id']
        with self.state.transaction() as db:
            for msg in messages:
                if msg.get('message_type') != 1 or msg.get('from_user_id') != peer or msg.get('group_id'):
                    continue
                if msg.get('to_user_id') and msg['to_user_id'] != bot:
                    continue
                if msg.get('message_state') not in (None, 2):
                    continue
                context = msg.get('context_token')
                if isinstance(context, str) and context:
                    db.execute('INSERT INTO weixin_context VALUES(?,?,?) ON CONFLICT(account,peer) DO UPDATE SET token=excluded.token',
                               (bot, peer, context))
                items = msg.get('item_list') or []
                parts = []
                if isinstance(items, list):
                    for item in items:
                        if not isinstance(item, dict) or item.get('type') != 1:
                            continue
                        content = item.get('text_item')
                        if isinstance(content, dict) and isinstance(content.get('text'), str):
                            parts.append(content['text'])
                text = '\n'.join(parts)
                if not text.strip():
                    text = '/微信附件'
                if len(text) > 6000:
                    text = '/微信长文本'
                source_id = msg.get('message_id') or msg.get('client_id')
                if not source_id:
                    source_id = json.dumps(msg, sort_keys=True, ensure_ascii=False)
                digest = hashlib.sha256(json.dumps([bot, peer, str(source_id)]).encode()).hexdigest()
                db.execute('INSERT OR IGNORE INTO weixin_inbox VALUES(?,?,?,?)',
                           ('weixin:' + digest, bot, self.chat_id(account), text.strip()))
            cursor = result.get('get_updates_buf')
            if isinstance(cursor, str) and cursor:
                db.execute('INSERT INTO weixin_sync VALUES(?,?) ON CONFLICT(account) DO UPDATE SET cursor=excluded.cursor',
                           (bot, cursor))

    def drain(self, account):
        for row in self.state.rows('SELECT * FROM weixin_inbox WHERE account=? ORDER BY rowid LIMIT 30',
                                   (account['ilink_bot_id'],)):
            try:
                self.state.enqueue(row['id'], self.owner, row['chat'], row['body'])
            except ValueError:
                return False
            self.state.execute('DELETE FROM weixin_inbox WHERE id=?', (row['id'],))
        return True

    def run(self, stop):
        while not stop.is_set():
            try:
                account = load_account(self.config.db)
                if not account or account['bot_token'] == self.stale_token:
                    self.status = 'login_required'
                    stop.wait(5)
                    continue
                if not self.drain(account):
                    stop.wait(2)
                    continue
                rows = self.state.rows('SELECT cursor FROM weixin_sync WHERE account=?', (account['ilink_bot_id'],))
                cursor = rows[0]['cursor'] if rows else ''
                result = WeixinAPI(account['baseurl'], account['bot_token']).request(
                    'ilink/bot/getupdates', {'get_updates_buf': cursor})
                self.ingest(account, result)
                self.drain(account)
                if self.status != 'connected':
                    print('微信消息通道已连接。', flush=True)
                self.status = 'connected'
                stop.wait(0.2)
            except WeixinError as error:
                if error.code == -14:
                    self.stale_token = account['bot_token']
                    self.status = 'login_required'
                    print('微信登录已失效，请重新扫码绑定。', flush=True)
                else:
                    self.status = 'retrying'
                    print(str(error), flush=True)
                stop.wait(5)
            except Exception as error:
                self.status = 'retrying'
                print('微信通道待重试：' + type(error).__name__, flush=True)
                stop.wait(5)

    def send(self, chat, text, key):
        with self.send_lock:
            account = load_account(self.config.db)
            if not account or chat != self.chat_id(account):
                raise WeixinError('微信接收账号尚未绑定或已变更')
            context = self.state.rows('SELECT token FROM weixin_context WHERE account=? AND peer=?',
                                      (account['ilink_bot_id'], account['ilink_user_id']))
            api = WeixinAPI(account['baseurl'], account['bot_token'])
            for index, start in enumerate(range(0, len(text), 3500)):
                client_id = 'aihelper-' + hashlib.sha256(json.dumps([chat, key, index]).encode()).hexdigest()[:32]
                if self.state.rows('SELECT id FROM weixin_sent WHERE id=?', (client_id,)):
                    continue
                message = {'from_user_id': '', 'to_user_id': account['ilink_user_id'],
                           'client_id': client_id, 'message_type': 2, 'message_state': 2,
                           'item_list': [{'type': 1, 'text_item': {'text': text[start:start+3500]}}]}
                if context:
                    message['context_token'] = context[0]['token']
                api.request('ilink/bot/sendmessage', {'msg': message}, timeout=25)
                self.state.execute('INSERT OR IGNORE INTO weixin_sent VALUES(?,?)', (client_id, time.time()))
