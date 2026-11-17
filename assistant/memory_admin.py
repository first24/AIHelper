"""Explicit-path, offline memory maintenance; never loads .env or service config.

python -m assistant.memory_admin --database /path/to/COPY.sqlite3 stats
"""
import argparse
import json
from pathlib import Path
import sqlite3

from .memory_types import MemorySettings
from .state import State


def backup_database(source, target):
    source,target=Path(source).resolve(),Path(target).resolve()
    if not source.is_file() or target.exists() or source==target:
        raise ValueError('源库必须存在，备份目标必须是全新文件')
    target.parent.mkdir(parents=True,exist_ok=True)
    # URI read-only prevents typo-created empty production databases.
    one=sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)
    two=sqlite3.connect(str(target))
    try:
        one.backup(two)
    finally:
        one.close()
        two.close()


def main():
    parser=argparse.ArgumentParser(description='离线记忆维护；仅显式指定数据库，不加载账号配置')
    parser.add_argument('--database',required=True)
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('stats')
    sub.add_parser('migrate')
    sub.add_parser('reindex')
    sub.add_parser('maintain')
    backup=sub.add_parser('backup')
    backup.add_argument('--output',required=True)
    restore=sub.add_parser('apply-deletions')
    restore.add_argument('--ledger',required=True)
    args=parser.parse_args()
    if args.command=='backup':
        backup_database(args.database,args.output)
        print('数据库备份完成；删除清单需独立保留并在恢复前应用。')
        return
    if not Path(args.database).is_file():
        raise ValueError('数据库不存在；维护工具不会创建未指定的新业务库')
    # Maintenance must work without keys and without accidental network work.
    state=State(args.database,memory_settings=MemorySettings())
    try:
        if args.command=='apply-deletions':
            print(json.dumps({'applied':state.memory.apply_deletion_ledger(args.ledger)}))
        elif args.command=='reindex':
            with state.transaction() as db:
                for row in list(db.execute("SELECT t.id,t.last_seq,s.owner FROM memory_turns t JOIN memory_sessions s ON s.id=t.session_id WHERE t.visible=1 AND t.status!='processing' AND s.auto_recall_enabled=1")):
                    state.memory.enqueue_job(db,row['owner'],'index_turn',row['id'],row['last_seq'])
                for row in list(db.execute("SELECT * FROM memory_source_revisions WHERE status='active'")):
                    state.memory.enqueue_job(db,row['owner'],'index_source',row['kind']+':'+row['id'],row['version'])
                db.execute("UPDATE memory_jobs SET status='pending',retry_at=0 WHERE kind IN ('index_turn','index_source') AND status IN ('done','stale')")
            print('已排队重建，执行时仍检查来源版本、删除清单和可见性；此命令未调用模型。')
        elif args.command=='maintain':
            from .memory_jobs import MemoryJobs
            MemoryJobs(state).maintain()
            print('保留策略已应用；业务对象和备份未删除。')
        else:
            print(json.dumps(state.memory.stats(),ensure_ascii=False))
    finally:
        state.db.close()


if __name__=='__main__':
    main()
