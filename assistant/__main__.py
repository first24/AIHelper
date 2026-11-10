import argparse
import os
from dataclasses import replace
from pathlib import Path

from .core import Config, GitHub, existing_git_token


def main():
    parser = argparse.ArgumentParser(description="飞书代码修复助理")
    parser.add_argument("command", choices=["run", "discover", "doctor", "demo"])
    args = parser.parse_args()
    if args.command == "demo":
        from .demo import demo
        demo()
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        raise SystemExit("请先运行 python -m pip install -r requirements.txt") from None
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False, interpolate=False)
    config = Config.from_env()
    if args.command != 'discover' and not config.token and os.getenv('USE_GIT_LOGIN', '').lower() == 'true':
        config = replace(config, token=existing_git_token())
    if args.command == "doctor":
        for key in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "GH_TOKEN", "ALLOWED_OPEN_IDS"):
            state = '使用本机 Git 登录' if key == 'GH_TOKEN' and config.token and not os.getenv(key) else ('已配置' if os.getenv(key) else '未配置')
            print(f"{key}: {state}")
        config.validate()
        print("GitHub：", GitHub(config).check())
        print("本机配置及 GitHub 读取检查通过；真实发送仍需在飞书验收。")
    else:
        from .feishu import run
        run(config, discover=args.command == "discover")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError) as error:
        raise SystemExit(str(error)) from None
