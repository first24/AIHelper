import hashlib
import json
import logging
import re
import threading
import time
from pathlib import Path

from .core import Config, Engine, GitHub, Store, request_json


class SafeConnectionLog(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        if record.levelno < logging.WARNING:
            if 'disconnected to ' in message:
                message = '飞书长连接已断开，等待重连'
            elif 'connected to ' in message:
                message = '飞书长连接已建立'
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
    sender = FeishuSender(config.app_id, config.app_secret)
    stop = threading.Event()
    if not discover:
        config.validate()
        github = GitHub(config)
        print("GitHub 检查：", github.check(), flush=True)
        engine = Engine(config, Store(config.db), github, sender)

        def worker():
            while not stop.is_set():
                engine.tick()
                stop.wait(config.poll)

        threading.Thread(target=worker, daemon=True).start()

    def on_message(data):
        event = data.event
        message = event.message
        if event.sender.sender_type != "user" or message.chat_type != "p2p" or message.message_type != "text":
            return
        open_id = event.sender.sender_id.open_id
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
            engine.receive(message.message_id, open_id, message.chat_id, text)
        except Exception as error:
            detail = str(error) if isinstance(error, RuntimeError) else type(error).__name__
            print("处理消息失败：" + detail, flush=True)

    handler = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(on_message).build()
    sdk_logger.addFilter(SafeConnectionLog())
    client = lark.ws.Client(config.app_id, config.app_secret, event_handler=handler, log_level=lark.LogLevel.INFO)
    print("飞书长连接启动中；" + ("仅处理 /whoami" if discover else "可发送 /修复 演示"), flush=True)
    try:
        client.start()
    finally:
        stop.set()
