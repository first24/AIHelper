"""Persistent personal-assistant data and notification outboxes."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def acquire_instance_lock(path):
    lock_path=Path(path).parent/'assistant.lock'
    lock_path.parent.mkdir(parents=True,exist_ok=True)
    stream=open(lock_path,'a+b')
    try:
        if os.name=='nt':
            import msvcrt
            if lock_path.stat().st_size==0:
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:
        stream.close()
        raise RuntimeError('同一数据目录已有助理实例在运行，请先停止旧实例') from None
    return stream


def user_zone(name='Asia/Shanghai'):
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name == 'Asia/Shanghai':
            return timezone(timedelta(hours=8), name)
        raise ValueError('时区数据不可用，请安装 tzdata') from None


def parse_at(value, zone, now):
    try:
        date = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (ValueError, AttributeError):
        raise ValueError('请提供完整日期和时间，例如 2026-11-12T15:00:00+08:00') from None
    if date.tzinfo is None:
        date = date.replace(tzinfo=zone)
    stamp = date.timestamp()
    if stamp <= now:
        raise ValueError('这个时间已经过去，请提供未来的时间')
    return stamp


def next_due(previous, now, repeat, zone):
    if repeat == 'once':
        return None
    original = datetime.fromtimestamp(previous, zone)
    current = datetime.fromtimestamp(now, zone)
    candidate = original.replace(year=current.year, month=current.month, day=current.day)
    for _ in range(9):
        valid = repeat == 'daily' or (repeat == 'weekdays' and candidate.weekday() < 5) or (
            repeat == 'weekly' and candidate.weekday() == original.weekday())
        if candidate.timestamp() > now and valid:
            return candidate.timestamp()
        candidate += timedelta(days=1)
    raise ValueError('无法计算重复任务的下次时间')


class State:
    def __init__(self, path):
        if path != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=20, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS messages_v2 (
            id TEXT PRIMARY KEY, owner TEXT, chat TEXT, text TEXT, status TEXT,
            created REAL, reply TEXT DEFAULT '', delivered INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS turns (
            id INTEGER PRIMARY KEY, owner TEXT, chat TEXT, body TEXT);
        CREATE TABLE IF NOT EXISTS schedules (
            id TEXT PRIMARY KEY, owner TEXT, chat TEXT, text TEXT, kind TEXT,
            due REAL, repeat TEXT, status TEXT DEFAULT 'active');
        CREATE TABLE IF NOT EXISTS deliveries (
            id TEXT PRIMARY KEY, schedule_id TEXT, owner TEXT, chat TEXT, text TEXT,
            status TEXT DEFAULT 'pending', retry_at REAL DEFAULT 0, attempts INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS todos (
            id TEXT PRIMARY KEY, owner TEXT, title TEXT, due TEXT, done INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS contacts (
            owner TEXT, name TEXT, email TEXT, PRIMARY KEY(owner,name));
        CREATE TABLE IF NOT EXISTS drafts (
            id TEXT PRIMARY KEY, owner TEXT, chat TEXT, recipient TEXT, subject TEXT,
            body TEXT, status TEXT DEFAULT 'draft', detail TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS preferences (
            owner TEXT, key TEXT, value TEXT, PRIMARY KEY(owner,key));
        CREATE TABLE IF NOT EXISTS effects (
            id TEXT PRIMARY KEY, result TEXT);
        ''')
        self.db.commit()

    @contextmanager
    def transaction(self):
        with self.lock:
            try:
                self.db.execute('BEGIN IMMEDIATE')
                yield self.db
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def rows(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args)]

    def execute(self, sql, args=()):
        with self.transaction() as db:
            return db.execute(sql, args).rowcount

    def recover(self):
        with self.transaction() as db:
            db.execute("UPDATE messages_v2 SET status='ready', reply='上次处理被服务重启中断。请重新发送请求；已有任务可通过 /提醒、/待办、/草稿 查询。' WHERE status='processing'")
            db.execute("UPDATE drafts SET status='unknown',detail='发送期间服务重启，结果需核查，未自动重发' WHERE status='sending'")
            db.execute("UPDATE deliveries SET status='pending' WHERE status='sending'")

    def enqueue(self, message_id, owner, chat, text):
        with self.transaction() as db:
            if db.execute('SELECT 1 FROM messages_v2 WHERE id=?', (message_id,)).fetchone():
                return False
            if db.execute("SELECT count(*) FROM messages_v2 WHERE status IN ('queued','processing')").fetchone()[0] >= 30:
                raise ValueError('当前队列已满，请稍后重试')
            db.execute("INSERT INTO messages_v2(id,owner,chat,text,status,created) VALUES(?,?,?,?,'queued',?)",
                       (message_id, owner, chat, text, time.time()))
            return True

    def claim_message(self):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM messages_v2 WHERE status='queued' ORDER BY created,id LIMIT 1").fetchone()
            if not row:
                return None
            db.execute("UPDATE messages_v2 SET status='processing' WHERE id=?", (row['id'],))
            return dict(row)

    def history(self, owner, chat):
        rows = self.rows('SELECT body FROM turns WHERE owner=? AND chat=? ORDER BY id DESC LIMIT 6', (owner, chat))
        return [message for row in reversed(rows) for message in json.loads(row['body'])]

    def save_turn(self, owner, chat, messages):
        with self.transaction() as db:
            db.execute('INSERT INTO turns(owner,chat,body) VALUES(?,?,?)', (owner, chat, json.dumps(messages, ensure_ascii=False)))
            db.execute('DELETE FROM turns WHERE owner=? AND chat=? AND id NOT IN (SELECT id FROM turns WHERE owner=? AND chat=? ORDER BY id DESC LIMIT 6)', (owner, chat, owner, chat))

    def draft(self, draft_id, owner):
        rows = self.rows('SELECT * FROM drafts WHERE id=? AND owner=?', (draft_id, owner))
        if not rows:
            raise ValueError('未找到你的邮件草稿')
        return rows[0]

    def due_deliveries(self, now, zone):
        with self.transaction() as db:
            for row in db.execute("SELECT * FROM schedules WHERE status='active' AND due<=?", (now,)).fetchall():
                due = row['due']
                delivery_id = f"{row['id']}:{int(due)}"
                if row['kind'] == 'brief':
                    todos = db.execute('SELECT id,title,due FROM todos WHERE owner=? AND done=0 ORDER BY rowid LIMIT 30', (row['owner'],)).fetchall()
                    body = '\n'.join(f"• {t['title']}" + (f"（截止 {t['due']}）" if t['due'] else '') for t in todos) or '目前没有未完成待办。'
                    text = '今日待办简报\n' + body
                else:
                    late = '（补发提醒）' if now - due > 120 else ''
                    text = f"提醒{late}：{row['text']}\n任务编号：{row['id']}"
                db.execute('INSERT OR IGNORE INTO deliveries(id,schedule_id,owner,chat,text) VALUES(?,?,?,?,?)',
                           (delivery_id, row['id'], row['owner'], row['chat'], text))
                following = next_due(due, now, row['repeat'], zone)
                if following is None:
                    db.execute("UPDATE schedules SET status='completed' WHERE id=?", (row['id'],))
                else:
                    db.execute('UPDATE schedules SET due=? WHERE id=?', (following, row['id']))
        return self.rows("SELECT * FROM deliveries WHERE status='pending' AND retry_at<=? ORDER BY rowid LIMIT 20", (now,))

    def deliver(self, send, now, zone):
        for row in self.due_deliveries(now, zone):
            changed = self.execute("UPDATE deliveries SET status='sending' WHERE id=? AND status='pending'", (row['id'],))
            if not changed:
                continue
            try:
                send(row['chat'], row['text'], 'schedule-' + row['id'])
            except Exception:
                self.execute("UPDATE deliveries SET status='pending',attempts=attempts+1,retry_at=? WHERE id=?",
                             (now + min(300, 5 * 2 ** min(row['attempts'], 6)), row['id']))
            else:
                self.execute("UPDATE deliveries SET status='sent' WHERE id=?", (row['id'],))
