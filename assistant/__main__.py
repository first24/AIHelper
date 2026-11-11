import argparse
import os
import json
import time
from dataclasses import replace
from pathlib import Path

from .core import Config, GitHub, existing_git_token


def main():
    parser = argparse.ArgumentParser(description="飞书 AI 个人助理")
    parser.add_argument("command", choices=["run", "discover", "doctor", "demo", "demo-assistant", "health", "weixin-login"])
    args = parser.parse_args()
    if args.command == "demo":
        from .demo import demo
        demo()
        return
    if args.command == 'demo-assistant':
        from .general_demo import demo
        demo()
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        raise SystemExit("请先运行 python -m pip install -r requirements.txt") from None
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False, interpolate=False)
    config = Config.from_env()
    if args.command == 'weixin-login':
        from .weixin import login
        login(config.db)
        return
    if args.command == 'health':
        try:
            health=json.loads((Path(config.db).parent/'health.json').read_text(encoding='utf-8'))
            if time.time()-health['updated_at']>120 or not health['conversation_worker_alive'] or not health.get('connected'):
                raise ValueError('助理健康检查未通过')
            if health.get('weixin_worker_alive') is False:
                raise ValueError('微信工作线程已停止')
        except (OSError,KeyError,ValueError):
            raise SystemExit(1) from None
        print('healthy')
        return
    if args.command != 'discover' and not config.token and os.getenv('USE_GIT_LOGIN', '').lower() == 'true':
        config = replace(config, token=existing_git_token())
    if args.command == "doctor":
        for key in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "GH_TOKEN", "ALLOWED_OPEN_IDS"):
            state = '使用本机 Git 登录' if key == 'GH_TOKEN' and config.token and not os.getenv(key) else ('已配置' if os.getenv(key) else '未配置')
            print(f"{key}: {state}")
        general=os.getenv('ASSISTANT_MODE','general')=='general'
        config.validate(require_github=not general)
        if general:
            from .agent import DeepSeek
            from .state import user_zone
            DeepSeek()
            user_zone(os.getenv('ASSISTANT_TIMEZONE','Asia/Shanghai'))
            print('DeepSeek 配置：已配置（未调用模型）')
            print('QQ 发信配置：'+('已配置' if os.getenv('SMTP_USER') and os.getenv('SMTP_AUTH_CODE') else '未配置，草稿功能仍可使用'))
        if config.token:
            try:
                print("GitHub：", GitHub(config).check())
            except Exception as error:
                if not general:
                    raise
                print('GitHub 暂不可用：'+type(error).__name__+'；日常助理功能仍可启动')
        else:
            print('GitHub：未配置，代码修复暂不可用')
        print("本机配置及 GitHub 读取检查通过；真实发送仍需在飞书验收。")
    else:
        from .feishu import run
        run(config, discover=args.command == "discover")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError) as error:
        raise SystemExit(str(error)) from None
