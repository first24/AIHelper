from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener, urlopen

HELP = "指令：\n/修复 演示\n/修复 <问题描述>\n/状态 <任务编号>\n/帮助\n本示例只修复 demo_shop/pricing.py，成功后创建 PR。"
DEMO_TASK = "修复订单折扣金额：19.99 元打 9 折应为 17.99，当前返回 18.99。金额使用 Decimal，保留两位小数，四舍五入。运行 shop_tests 全部测试。只修改 demo_shop/pricing.py。"


def existing_git_token():
    """Ask Git's configured credential helper; never print or persist its response."""
    try:
        result = subprocess.run(['git', 'credential', 'fill'],
                                input='protocol=https\nhost=github.com\n\n', text=True,
                                capture_output=True, timeout=30,
                                env={**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GCM_INTERACTIVE': 'never'})
    except (OSError, subprocess.TimeoutExpired):
        return ''
    if result.returncode:
        return ''
    fields = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    return fields.get('password', '')


@dataclass(frozen=True)
class Config:
    repo: str
    ref: str = "main"
    workflow: str = "assistant-fix.yml"
    token: str = ""
    app_id: str = ""
    app_secret: str = ""
    users: frozenset[str] = frozenset()
    db: str = "data/jobs.sqlite3"
    poll: int = 15
    timeout: int = 2400

    @classmethod
    def from_env(cls):
        return cls(
            repo=os.getenv("GITHUB_REPOSITORY", "first24/AIHelper"),
            ref=os.getenv("GITHUB_REF_NAME", "main"),
            token=os.getenv("GH_TOKEN", ""),
            app_id=os.getenv("FEISHU_APP_ID", ""),
            app_secret=os.getenv("FEISHU_APP_SECRET", ""),
            users=frozenset(x.strip() for x in os.getenv("ALLOWED_OPEN_IDS", "").split(",") if x.strip()),
            db=os.getenv("ASSISTANT_DB", "data/jobs.sqlite3"),
        )

    def validate(self):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repo):
            raise ValueError("GITHUB_REPOSITORY 格式应为 owner/repo")
        missing = [k for k, v in {"GH_TOKEN": self.token, "FEISHU_APP_ID": self.app_id,
                   "FEISHU_APP_SECRET": self.app_secret, "ALLOWED_OPEN_IDS": self.users}.items() if not v]
        if missing:
            raise ValueError("尚未配置：" + ", ".join(missing))


def request_json(url, method="GET", body=None, headers=None, opener=None):
    data = None if body is None else json.dumps(body).encode()
    req = Request(url, data=data, method=method,
                  headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with (opener.open(req, timeout=25) if opener else urlopen(req, timeout=25)) as response:
            raw = response.read()
            return json.loads(raw) if raw else None
    except HTTPError as error:
        # Do not put request headers, tokens or arbitrary upstream bodies into chat/logs.
        raise RuntimeError(f"远端 API HTTP {error.code}") from None


class GitHub:
    def __init__(self, config):
        self.config = config
        self.root = f"https://api.github.com/repos/{config.repo}"
        self.headers = {"Authorization": f"Bearer {config.token}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "AIHelper"}
        proxy = os.getenv("GITHUB_PROXY", "")
        if not proxy:
            try:
                proxy = subprocess.run(['git', 'config', '--get-urlmatch', 'http.proxy', self.root],
                                       capture_output=True, text=True, timeout=5).stdout.strip()
            except (OSError, subprocess.TimeoutExpired):
                pass
        self.opener = build_opener(ProxyHandler({'https': proxy})) if proxy else None

    def call(self, path, method="GET", body=None):
        return request_json(self.root + path, method, body, self.headers, self.opener)

    def check(self):
        repo = self.call("")
        workflow = self.call(f"/actions/workflows/{self.config.workflow}")
        return {"repository": repo["full_name"], "default_branch": repo["default_branch"],
                "workflow_state": workflow["state"]}

    def dispatch(self, job):
        self.call(f"/actions/workflows/{self.config.workflow}/dispatches", "POST",
                  {"ref": self.config.ref, "inputs": {"job_id": job["id"], "request_text": job["text"]}})

    def find_run(self, job):
        query = urlencode({"event": "workflow_dispatch", "branch": self.config.ref, "per_page": 100})
        runs = self.call(f"/actions/workflows/{self.config.workflow}/runs?{query}")["workflow_runs"]
        matches = [r for r in runs if r.get("display_title") == "assistant-" + job["id"]]
        return max(matches, key=lambda r: (r["id"], r.get("run_attempt", 1))) if matches else None

    def run(self, run_id):
        return self.call(f"/actions/runs/{run_id}")

    def find_pr(self, job):
        owner = self.config.repo.split("/")[0]
        query = urlencode({"head": f"{owner}:assistant/{job['id']}", "base": self.config.ref, "state": "all"})
        pulls = self.call("/pulls?" + query)
        return pulls[0]["html_url"] if pulls else None

    def create_pr(self, job, run):
        result = self.call('/pulls', 'POST', {
            'title': 'fix: 修复订单折扣金额计算',
            'head': 'assistant/' + job['id'], 'base': self.config.ref, 'draft': True,
            'body': f"修复订单折扣金额计算，使用原始回归测试验证。\n\n任务编号：{job['id']}\n"
                    f"验证：业务回归及助理服务测试通过。\n执行记录：{run['html_url']}\n\n请审查后手动合并。"})
        return result['html_url']


class Store:
    def __init__(self, path):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.conn.execute("""CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, message_id TEXT UNIQUE, sender TEXT, chat TEXT, text TEXT,
            status TEXT, created REAL, run_id INTEGER, run_url TEXT DEFAULT '',
            result TEXT DEFAULT '', notified INTEGER DEFAULT 0)""")
        self.conn.commit()

    def get(self, job_id):
        with self.lock:
            row = self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return dict(row) if row else None

    def add(self, message_id, sender, chat, text):
        job_id = hashlib.sha256(message_id.encode()).hexdigest()[:16]
        with self.lock:
            old = self.get(job_id)
            if old:
                return old, False
            count = self.conn.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','waiting','running')").fetchone()[0]
            if count >= 5:
                raise ValueError("待处理任务已达 5 个，请稍后重试")
            self.conn.execute("INSERT INTO jobs(id,message_id,sender,chat,text,status,created) VALUES(?,?,?,?,?,'queued',?)",
                              (job_id, message_id, sender, chat, text, time.time()))
            self.conn.commit()
            return self.get(job_id), True

    def update(self, job_id, **values):
        allowed = {"status", "run_id", "run_url", "result", "notified"}
        if not values or not values.keys() <= allowed:
            raise ValueError("invalid fields")
        with self.lock:
            columns = ",".join(k + "=?" for k in values)
            self.conn.execute(f"UPDATE jobs SET {columns} WHERE id=?", (*values.values(), job_id))
            self.conn.commit()

    def pending(self):
        with self.lock:
            return [dict(r) for r in self.conn.execute(
                "SELECT * FROM jobs WHERE status NOT IN ('done','failed') OR notified=0 ORDER BY created")]


class Engine:
    def __init__(self, config, store, github, send):
        self.config, self.store, self.github, self.send = config, store, github, send

    def receive(self, message_id, sender, chat, text):
        if sender not in self.config.users:
            return  # Ignore messages outside the configured user allowlist.
        text = text.strip()
        if text in ("/帮助", "/help"):
            self.send(chat, HELP, message_id + "-help")
        elif text.startswith("/状态 "):
            job = self.store.get(text.split(maxsplit=1)[1])
            if not job or job["sender"] != sender:
                self.send(chat, "未找到你的任务。", message_id + "-status")
            else:
                self.send(chat, f"任务 {job['id']}：{job['status']}\n{job['result'] or job['run_url']}", message_id + "-status")
        elif text.startswith("/修复 "):
            task = text.split(maxsplit=1)[1].strip()
            if not task or len(task) > 3000:
                self.send(chat, "问题描述应为 1–3000 字。", message_id + "-invalid")
                return
            try:
                job, fresh = self.store.add(message_id, sender, chat, DEMO_TASK if task == "演示" else task)
            except ValueError as error:
                self.send(chat, str(error), message_id + "-busy")
                return
            if fresh:
                self.send(chat, f"已接收任务 {job['id']}，即将启动 GitHub 修复。\n用 /状态 {job['id']} 查询。", job['id'] + "-ack")
        else:
            self.send(chat, HELP, message_id + "-help")

    def tick(self):
        for job in self.store.pending():
            try:
                self._advance(job)
            except Exception as error:
                # Network and notification errors remain retryable; never redispatch a waiting job.
                print(f"任务 {job['id']} 暂遇 {type(error).__name__}，下轮继续", flush=True)

    def _advance(self, job):
        if job["status"] in ("done", "failed"):
            self.send(job["chat"], job["result"], job["id"] + "-complete")
            self.store.update(job["id"], notified=1)
            return
        if job["status"] == "queued":
            if any(other["status"] in ("waiting", "running") for other in self.store.pending()):
                return
            # Persist BEFORE the POST: timeout/restart cannot accidentally create duplicate runs.
            self.store.update(job["id"], status="waiting")
            self.github.dispatch(job)
            return
        if time.time() - job["created"] > self.config.timeout:
            result = f"任务 {job['id']}：等待超时，请检查 GitHub Actions；没有自动重新触发。\n{job['run_url'] or 'https://github.com/' + self.config.repo + '/actions'}"
            self.store.update(job["id"], status="failed", result=result)
            return
        run = self.github.run(job["run_id"]) if job["run_id"] else self.github.find_run(job)
        if run:
            self.store.update(job["id"], run_id=run["id"], run_url=run["html_url"], status="running")
            if run["status"] == "completed":
                pr = None
                if run["conclusion"] == "success":
                    pr = self.github.find_pr(job) or self.github.create_pr(job, run)
                result = (f"任务 {job['id']}：修复完成，测试通过。\nPR：{pr}\n等待你审查合并。" if pr else
                          f"任务 {job['id']}：本次未产出已验证的 PR（{run['conclusion']}）。\n日志：{run['html_url']}")
                self.store.update(job["id"], status="done" if pr else "failed", result=result)
                return
