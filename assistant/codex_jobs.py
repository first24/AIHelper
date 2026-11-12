"""Durable local-Codex queue; runners access it only over authenticated SSH."""
import hashlib
import hmac
import json
import os
import re
import secrets
import time


FINAL={'done','failed','canceled','interrupted'}


class CodexJobs:
    def __init__(self,config,state,clock=time.time):
        self.config,self.state,self.clock=config,state,clock

    def projects(self):
        value=json.loads(os.getenv('CODEX_PROJECTS_JSON','{}'))
        if not isinstance(value,dict) or any(not re.fullmatch(r'[a-z0-9_-]{1,40}',key) or not isinstance(label,str) or len(label)>120 for key,label in value.items()):
            raise ValueError('Codex 项目配置无效')
        return value

    def submit(self,owner,chat,message_id,prompt,project='aihelper',parent_id=''):
        if owner not in self.config.users:
            raise ValueError('当前用户未获授权')
        if project not in self.projects():
            raise ValueError('本机项目未接入，请先查询 /codex项目')
        if not isinstance(prompt,str) or not 1<=len(prompt.strip())<=6000:
            raise ValueError('Codex 任务需要 1–6000 字的明确说明')
        job_id='cx_'+hashlib.sha256(('codex:'+message_id).encode()).hexdigest()[:16]
        with self.state.transaction() as db:
            previous=db.execute('SELECT id FROM codex_jobs WHERE id=? AND owner=?',(job_id,owner)).fetchone()
            if previous:
                return {'ok':True,'id':job_id,'receipt':'已接收 Codex 任务 '+job_id+'，未重复创建。'}
            if parent_id:
                parent=db.execute('SELECT * FROM codex_jobs WHERE id=? AND owner=?',(parent_id,owner)).fetchone()
                if not parent or parent['project']!=project or parent['status'] not in FINAL or not parent['session_id']:
                    raise ValueError('原任务尚未结束、没有可继续的会话，或不属于此项目')
                if db.execute("SELECT 1 FROM codex_jobs WHERE parent_id=? AND status IN ('queued','running','cancel_requested')",(parent_id,)).fetchone():
                    raise ValueError('这个任务已有后续请求在执行，请等它完成后继续')
            if db.execute("SELECT count(*) FROM codex_jobs WHERE status IN ('queued','running','cancel_requested')").fetchone()[0]>=5:
                raise ValueError('当前已有 5 个 Codex 任务，请等待或取消后再提交')
            now=self.clock()
            db.execute('INSERT INTO codex_jobs(id,owner,chat,project,prompt,parent_id,created,updated) VALUES(?,?,?,?,?,?,?,?)',
                       (job_id,owner,chat,project,prompt.strip(),parent_id,now,now))
        return {'ok':True,'id':job_id,'receipt':f'已提交本机 Codex 任务 {job_id}\n项目：{self.projects()[project]}\n电脑开机且执行端在线后开始，结果会回到当前聊天。\n查询：/codex状态 {job_id}'}

    def get(self,owner,id):
        rows=self.state.rows('SELECT id,project,parent_id,status,created,started,updated,result,workdir,progress FROM codex_jobs WHERE id=? AND owner=?',(id,owner))
        if not rows:
            raise ValueError('未找到你的 Codex 任务')
        labels={'queued':'排队中','running':'执行中','cancel_requested':'正在停止','done':'已完成','failed':'失败','canceled':'已取消','interrupted':'连接中断，待检查'}
        row=rows[0]
        row['receipt']=f"Codex 任务 {id}｜{labels[row['status']]}\n{row['result'] or row['progress'] or '等待本机执行端领取'}"
        return {'ok':True,**row}

    def list(self,owner):
        rows=self.state.rows('SELECT id,project,status FROM codex_jobs WHERE owner=? ORDER BY created DESC LIMIT 10',(owner,))
        return {'ok':True,'jobs':rows,'receipt':'\n'.join(f"{r['id']}｜{r['project']}｜{r['status']}" for r in rows) or '还没有 Codex 任务。'}

    def cancel(self,owner,id):
        with self.state.transaction() as db:
            row=db.execute('SELECT status FROM codex_jobs WHERE id=? AND owner=?',(id,owner)).fetchone()
            if not row:
                raise ValueError('未找到你的 Codex 任务')
            if row['status'] in FINAL:
                return {'ok':True,'id':id,'receipt':'这个任务已结束，未再次执行取消。'}
            status='canceled' if row['status']=='queued' else 'cancel_requested'
            db.execute('UPDATE codex_jobs SET status=?,updated=? WHERE id=?',(status,self.clock(),id))
        return {'ok':True,'id':id,'receipt':('已取消排队任务 ' if status=='canceled' else '已请求停止本机任务，待执行端确认：')+id+'\n已产生的本机文件会保留。'}

    def connection(self):
        rows=self.state.rows('SELECT last_seen,busy,version FROM codex_workers WHERE id=?',(os.getenv('CODEX_RUNNER_ID','hp-local'),))
        online=bool(rows and self.clock()-rows[0]['last_seen']<100)
        return {'ok':True,'online':online,'projects':self.projects(),
                'receipt':'本机 Codex：'+('在线' if online else '离线，已提交任务保留排队')+('，正在执行 '+rows[0]['busy'] if online and rows[0]['busy'] else '')}

    def worker(self,value):
        if value!=os.getenv('CODEX_RUNNER_ID','hp-local'):
            raise ValueError('执行端编号不匹配')

    def _lease(self,db,id,worker,lease):
        self.worker(worker)
        row=db.execute('SELECT * FROM codex_jobs WHERE id=? AND worker=?',(id,worker)).fetchone()
        if not row or not isinstance(lease,str) or not hmac.compare_digest(row['lease_hash'],hashlib.sha256(lease.encode()).hexdigest()):
            raise ValueError('任务租约不匹配')
        return row

    def claim(self,worker,projects,version=''):
        self.worker(worker)
        if not isinstance(projects,list) or any(project not in self.projects() for project in projects):
            raise ValueError('执行端项目列表不匹配')
        self.expire()
        now=self.clock()
        with self.state.transaction() as db:
            busy=db.execute("SELECT id FROM codex_jobs WHERE worker=? AND status IN ('running','cancel_requested')",(worker,)).fetchone()
            db.execute('INSERT INTO codex_workers(id,last_seen,busy,version) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen,busy=excluded.busy,version=excluded.version',
                       (worker,now,busy['id'] if busy else '',str(version)[:80]))
            if busy:
                return {'ok':True,'job':None}
            rows=db.execute("SELECT * FROM codex_jobs WHERE status='queued' ORDER BY created").fetchall()
            row=next((r for r in rows if r['project'] in projects and r['owner'] in self.config.users),None)
            if row is None:
                return {'ok':True,'job':None}
            token=secrets.token_urlsafe(32)
            db.execute("UPDATE codex_jobs SET status='running',started=?,updated=?,worker=?,lease_hash=?,lease_until=?,progress=?,progress_at=? WHERE id=?",
                       (now,now,worker,hashlib.sha256(token.encode()).hexdigest(),now+180,'Codex 已开始执行',now,row['id']))
            db.execute('UPDATE codex_workers SET busy=? WHERE id=?',(row['id'],worker))
            self._notice(db,row,'started','本机 Codex 已开始：'+row['id'])
            job={key:row[key] for key in ('id','project','prompt','parent_id')}
            job['lease']=token
            if row['parent_id']:
                parent=db.execute('SELECT session_id,workdir FROM codex_jobs WHERE id=?',(row['parent_id'],)).fetchone()
                job['session_id'],job['workdir']=parent['session_id'],parent['workdir']
        return {'ok':True,'job':job}

    def heartbeat(self,worker,id,lease,progress='',session_id='',workdir=''):
        now=self.clock()
        if session_id and not re.fullmatch(r'[0-9a-fA-F-]{36}',session_id):
            raise ValueError('Codex 会话编号无效')
        with self.state.transaction() as db:
            row=self._lease(db,id,worker,lease)
            active=row['status'] in ('running','cancel_requested')
            if active:
                db.execute('UPDATE codex_jobs SET updated=?,lease_until=?,progress=?,session_id=?,workdir=? WHERE id=?',
                           (now,now+180,str(progress)[:200],session_id or row['session_id'],str(workdir or row['workdir'])[:1000],id))
                db.execute('UPDATE codex_workers SET last_seen=?,busy=? WHERE id=?',(now,id,worker))
                if progress and now-row['progress_at']>=300:
                    self._notice(db,row,'progress:'+str(int(now//300)),f'Codex 任务 {id} 仍在执行：{str(progress)[:200]}')
                    db.execute('UPDATE codex_jobs SET progress_at=? WHERE id=?',(now,id))
        return {'ok':True,'stop':not active or row['status']=='cancel_requested'}

    def finish(self,worker,id,lease,status,result,session_id='',workdir=''):
        if status not in ('done','failed','canceled') or not isinstance(result,str) or not 1<=len(result)<=14000:
            raise ValueError('任务结果无效')
        with self.state.transaction() as db:
            row=self._lease(db,id,worker,lease)
            if row['status'] in ('done','failed','canceled'):
                return {'ok':True,'status':row['status']}
            if row['status']=='cancel_requested':
                status='canceled'
            db.execute('UPDATE codex_jobs SET status=?,result=?,updated=?,session_id=?,workdir=? WHERE id=?',
                       (status,result,self.clock(),session_id or row['session_id'],str(workdir or row['workdir'])[:1000],id))
            db.execute('UPDATE codex_workers SET busy=?,last_seen=? WHERE id=?',('',self.clock(),worker))
            label={'done':'完成','failed':'失败','canceled':'已停止'}[status]
            self._notice(db,row,'finish',f'Codex 任务 {id}｜{label}\n\n{result}')
        return {'ok':True,'status':status}

    @staticmethod
    def _notice(db,row,suffix,text):
        db.execute('INSERT OR IGNORE INTO deliveries(id,schedule_id,owner,chat,text) VALUES(?,?,?,?,?)',
                   ('codex:'+row['id']+':'+suffix,'codex:'+row['id'],row['owner'],row['chat'],text))

    def expire(self):
        now=self.clock()
        with self.state.transaction() as db:
            for row in db.execute("SELECT * FROM codex_jobs WHERE status IN ('running','cancel_requested') AND lease_until<?",(now,)).fetchall():
                message='本机执行端连接中断，任务不会自动重跑。请在电脑检查任务记录后，使用 /codex继续 原任务编号 后续要求，或重新提交明确任务。'
                db.execute("UPDATE codex_jobs SET status='interrupted',updated=?,result=? WHERE id=?",(now,message,row['id']))
                self._notice(db,row,'interrupted',f"Codex 任务 {row['id']}｜连接中断\n{message}")


def main():
    """Single JSON request on stdin; invoked using docker exec over SSH."""
    import sys
    from .core import Config
    from .state import State
    raw=sys.stdin.buffer.read(64001)
    state=None
    try:
        if len(raw)>64000:
            raise ValueError('执行端请求过大')
        request=json.loads(raw)
        method=request.pop('method')
        if method not in ('claim','heartbeat','finish'):
            raise ValueError('未知执行端操作')
        state=State(Config.from_env().db)
        result=getattr(CodexJobs(Config.from_env(),state),method)(**request)
    except (ValueError,TypeError,KeyError,json.JSONDecodeError):
        result={'ok':False,'error':'执行端请求或任务状态无效'}
    finally:
        if state:
            state.db.close()
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    main()
