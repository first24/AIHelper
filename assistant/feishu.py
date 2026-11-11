import hashlib
import json
import logging
import os
import re
import signal
import threading
import time
from pathlib import Path

from .core import Config, Engine, GitHub, Store, request_json


class SafeConnectionLog(logging.Filter):
    def __init__(self, connection_status=None):
        super().__init__()
        self.connection_status = connection_status

    def filter(self, record):
        message = record.getMessage()
        if record.levelno < logging.WARNING:
            if 'disconnected to ' in message:
                message = '飞书长连接已断开，等待重连'
                if self.connection_status is not None:
                    self.connection_status['connected'] = False
            elif 'connected to ' in message:
                message = '飞书长连接已建立'
                if self.connection_status is not None:
                    self.connection_status['connected'] = True
            else:
                return False
        record.msg = re.sub(r'wss?://\S+', '[连接地址已隐藏]', message)
        record.args = ()
        return True


class FeishuSender:
    def __init__(self, app_id, secret):
        self.app_id, self.secret = app_id, secret
        self.token, self.expires = "", 0
        self.lock = threading.Lock()

    def __call__(self, chat, text, key):
        with self.lock:
            if time.time() >= self.expires:
                data = request_json("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                                    "POST", {"app_id": self.app_id, "app_secret": self.secret})
                if data.get("code", 0) != 0:
                    if data.get('code') == 21001:
                        raise RuntimeError('飞书平台策略拒绝访问 code=21001，请通过飞书客服核查账号和应用的访问资格')
                    raise RuntimeError(f"飞书鉴权失败 code={data.get('code')}")
                self.token = data["tenant_access_token"]
                self.expires = time.time() + data["expire"] - 120
            data = request_json("https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id", "POST",
                                {"receive_id": chat, "msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False),
                                 "uuid": hashlib.sha256(key.encode()).hexdigest()[:32]},
                                {"Authorization": "Bearer " + self.token})
            if data.get("code", 0) != 0:
                self.expires = 0
                raise RuntimeError(f"飞书发送失败 code={data.get('code')}")


def run(config: Config, discover=False):
    import lark_oapi as lark
    from lark_oapi.core.log import logger as sdk_logger

    if not config.app_id or not config.app_secret:
        raise ValueError("请先在 .env 配置 FEISHU_APP_ID 和 FEISHU_APP_SECRET")
    from .state import acquire_instance_lock
    instance_lock = acquire_instance_lock(config.db)
    feishu_sender = FeishuSender(config.app_id, config.app_secret)
    weixin_channel = None

    def sender(chat, text, key):
        if chat.startswith('weixin:'):
            if weixin_channel is None:
                raise RuntimeError('微信通道未启用')
            return weixin_channel.send(chat, text, key)
        return feishu_sender(chat, text, key)
    stop = threading.Event()
    general = os.getenv('ASSISTANT_MODE', 'general') == 'general'
    connection_status = {'connected': False}
    assistant = None
    if not discover:
        config.validate(require_github=not general)
        engine = None
        if config.token:
            try:
                github = GitHub(config)
                print("GitHub 检查：", github.check(), flush=True)
                engine = Engine(config, Store(config.db), github, sender)
            except Exception as error:
                if not general:
                    raise
                print('GitHub 暂不可用，日常助理继续启动：'+type(error).__name__, flush=True)

        def worker():
            while not stop.is_set():
                try:
                    engine.tick()
                except Exception as error:
                    print('代码任务待重试：'+type(error).__name__, flush=True)
                stop.wait(config.poll)

        if engine:
            threading.Thread(target=worker, daemon=True, name='github-worker').start()
        if general:
            from .agent import Assistant, DeepSeek
            from .mail import Mailer
            from .state import State, user_zone
            state = State(config.db)
            state.recover()
            zone = user_zone(os.getenv('ASSISTANT_TIMEZONE', 'Asia/Shanghai'))
            assistant = Assistant(config,state,DeepSeek(),Mailer(state),sender,zone,engine)
            weixin_thread = None
            if os.getenv('WEIXIN_ENABLED', '').lower() == 'true':
                from .weixin import WeixinChannel
                weixin_channel = WeixinChannel(config, state)
                weixin_thread = threading.Thread(target=weixin_channel.run, args=(stop,),
                                                 daemon=True, name='weixin-worker')
                weixin_thread.start()
                print('微信通道已启用；首次使用请运行 weixin-login 扫码绑定。', flush=True)

            def conversations():
                while not stop.is_set():
                    try:
                        assistant.tick()
                    except Exception as error:
                        print('会话处理待重试：'+type(error).__name__, flush=True)
                    stop.wait(1)

            conversation_thread = threading.Thread(target=conversations,daemon=True,name='conversation-worker')
            conversation_thread.start()

            def schedules():
                heartbeat = Path(config.db).parent / 'health.json'
                while not stop.is_set():
                    try:
                        state.deliver(sender,time.time(),zone)
                        health={'updated_at':time.time(), 'conversation_worker_alive':conversation_thread.is_alive(), **connection_status}
                        if weixin_channel:
                            health['weixin_status'] = weixin_channel.status
                            health['weixin_worker_alive'] = weixin_thread.is_alive()
                        temp = heartbeat.with_suffix('.tmp')
                        temp.write_text(json.dumps(health),encoding='utf-8')
                        os.replace(temp,heartbeat)
                    except Exception as error:
                        print('调度器待重试：'+type(error).__name__,flush=True)
                    stop.wait(2)

            threading.Thread(target=schedules,daemon=True,name='scheduler-worker').start()

    def on_message(data):
        event = data.event
        message = event.message
        if event.sender.sender_type != "user" or message.chat_type != "p2p" or message.message_type != "text":
            return
        open_id = event.sender.sender_id.open_id
        if not discover and open_id not in config.users:
            return
        try:
            text = json.loads(message.content).get("text", "").strip()
            if text.startswith('/'):
                record = {'sender_open_id': open_id, 'chat_id': message.chat_id,
                          'message_id': message.message_id, 'allowed': open_id in config.users,
                          'kind': 'whoami' if text == '/whoami' else ('repair' if text.startswith('/修复') else 'other'),
                          'received_at': time.time()}
                receipt_path = Path(config.db).parent / 'last-command.json'
                receipt_path.parent.mkdir(parents=True, exist_ok=True)
                receipt_path.write_text(json.dumps(record), encoding='utf-8')
                print(f"飞书指令已收到：{record['kind']}，授权匹配：{record['allowed']}", flush=True)
            if discover:
                if text == "/whoami":
                    sender(message.chat_id, "你的 open_id：" + open_id + "\n把它填入本机 .env 的 ALLOWED_OPEN_IDS 后退出发现模式。", message.message_id)
                return
            if assistant:
                assistant.receive(message.message_id, open_id, message.chat_id, text)
            else:
                engine.receive(message.message_id, open_id, message.chat_id, text)
        except Exception as error:
            detail = str(error) if isinstance(error, RuntimeError) else type(error).__name__
            print("处理消息失败：" + detail, flush=True)

    handler = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(on_message).build()
    sdk_logger.addFilter(SafeConnectionLog(connection_status))
    client = lark.ws.Client(config.app_id, config.app_secret, event_handler=handler, log_level=lark.LogLevel.INFO)
    print("飞书长连接启动中；" + ("仅处理 /whoami" if discover else ("通用个人助理模式" if general else "代码修复模式")), flush=True)
    if threading.current_thread() is threading.main_thread():
        def terminate(signum, frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, terminate)
    try:
        client.start()
    except KeyboardInterrupt:
        print('助理正在停止。任务保留在数据库中。',flush=True)
    finally:
        stop.set()
        instance_lock.close()
