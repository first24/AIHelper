"""Local-only Codex worker. No account credentials are sent to the cloud relay."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import signal
import subprocess
import threading
import time

from .state import acquire_instance_lock


def redact(value):
    value=re.sub(r'\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)\b','[凭证已隐藏]',str(value))
    return re.sub(r'(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]{16,}',r'\1[已隐藏]',value)


def save(path,value):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix('.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    os.chmod(temp,0o600)
    os.replace(temp,path)


def clean_env():
    allowed={'path','userprofile','appdata','localappdata','systemroot','windir','temp','tmp','comspec','pathext',
             'homedrive','homepath','username','os','programfiles','programfiles(x86)','programdata',
             'processor_architecture','http_proxy','https_proxy','all_proxy','no_proxy','lang','lc_all',
             'ssl_cert_file','ssl_cert_dir','requests_ca_bundle','curl_ca_bundle','node_extra_ca_certs'}
    result={key:value for key,value in os.environ.items() if key.lower() in allowed}
    result['PYTHONUTF8']='1'
    result['GIT_TERMINAL_PROMPT']='0'
    result['GCM_INTERACTIVE']='never'
    return result


class ProcessTree:
    """Windows job objects terminate children if the worker unexpectedly exits."""
    def __init__(self,process):
        self.process=process
        self.handle=None
        if os.name=='nt':
            import ctypes
            from ctypes import wintypes as w
            class Basic(ctypes.Structure):
                _fields_=[('process_time',ctypes.c_longlong),('job_time',ctypes.c_longlong),('flags',w.DWORD),
                          ('min_ws',ctypes.c_size_t),('max_ws',ctypes.c_size_t),('active',w.DWORD),
                          ('affinity',ctypes.c_size_t),('priority',w.DWORD),('scheduling',w.DWORD)]
            class IO(ctypes.Structure):
                _fields_=[(name,ctypes.c_ulonglong) for name in ('read','write','other','read_bytes','write_bytes','other_bytes')]
            class Extended(ctypes.Structure):
                _fields_=[('basic',Basic),('io',IO),('process_memory',ctypes.c_size_t),('job_memory',ctypes.c_size_t),
                          ('peak_process',ctypes.c_size_t),('peak_job',ctypes.c_size_t)]
            api=ctypes.WinDLL('kernel32',use_last_error=True)
            api.CreateJobObjectW.restype=w.HANDLE
            api.CreateJobObjectW.argtypes=[w.LPVOID,w.LPCWSTR]
            api.SetInformationJobObject.argtypes=[w.HANDLE,ctypes.c_int,w.LPVOID,w.DWORD]
            api.AssignProcessToJobObject.argtypes=[w.HANDLE,w.HANDLE]
            api.TerminateJobObject.argtypes=[w.HANDLE,w.UINT]
            api.CloseHandle.argtypes=[w.HANDLE]
            handle=api.CreateJobObjectW(None,None)
            info=Extended()
            info.basic.flags=0x2000
            if not handle or not api.SetInformationJobObject(handle,9,ctypes.byref(info),ctypes.sizeof(info)) or not api.AssignProcessToJobObject(handle,w.HANDLE(process._handle)):
                if handle:
                    api.CloseHandle(handle)
                process.kill()
                raise RuntimeError('无法建立 Codex 任务进程隔离')
            self.handle,self.api=handle,api

    def terminate(self):
        if self.handle:
            self.api.TerminateJobObject(self.handle,1)
        elif self.process.poll() is None:
            os.killpg(self.process.pid,signal.SIGTERM)

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle=None


class Worker:
    def __init__(self,settings):
        self.settings=settings
        self.root=Path(settings['data_dir']).resolve()
        self.root.mkdir(parents=True,exist_ok=True)
        self.jobs=self.root/'jobs'
        self.jobs.mkdir(exist_ok=True)
        self.tasks=Path(settings['tasks_dir']).resolve()
        self.tasks.mkdir(parents=True,exist_ok=True)
        self.worker=settings.get('worker','hp-local')
        if not re.fullmatch(r'[a-zA-Z0-9_.-]{1,50}',self.worker):
            raise ValueError('执行端编号无效')
        for project,options in settings['projects'].items():
            if not re.fullmatch(r'[a-z0-9_-]{1,40}',project):
                raise ValueError('本机项目代号无效')
            if not (Path(options['source']).resolve()/'.git').exists():
                raise ValueError('本机项目必须是 Git 工作目录')
        self.stop=threading.Event()

    def rpc(self,method,**args):
        ssh=self.settings['ssh']
        if not re.fullmatch(r'[A-Za-z0-9_.-]+@[A-Za-z0-9.-]+',ssh['host']):
            raise ValueError('SSH 主机格式无效')
        container=ssh.get('container','aihelper-v2-assistant-1')
        if not re.fullmatch(r'[A-Za-z0-9_.-]+',container):
            raise ValueError('容器名称无效')
        command=['ssh','-i',ssh['identity'],'-o','BatchMode=yes','-o','IdentitiesOnly=yes','-o','StrictHostKeyChecking=yes',
                 '-o','UserKnownHostsFile='+ssh['known_hosts'],'-o','ConnectTimeout=10','-o','ServerAliveInterval=10',
                 '-o','ServerAliveCountMax=2',ssh['host'],
                 'sudo -n docker exec -i '+container+' python -m assistant.codex_jobs']
        payload=json.dumps({'method':method,'worker':self.worker,**args},ensure_ascii=False)
        result=subprocess.run(command,input=payload,encoding='utf-8',capture_output=True,timeout=45,
                              creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        if result.returncode:
            raise RuntimeError('云端转发连接暂不可用')
        try:
            response=json.loads(result.stdout)
        except ValueError:
            raise RuntimeError('云端转发响应无效') from None
        if not response.get('ok'):
            raise RuntimeError(response.get('error','任务状态无效'))
        return response

    def workdir(self,job,record):
        if job.get('parent_id'):
            source=self.jobs/job['parent_id']/'record.json'
            if not source.exists():
                raise ValueError('本机缺少原任务记录，不能继续该会话')
            parent=json.loads(source.read_text(encoding='utf-8'))
            path=Path(parent['workdir']).resolve()
            if not path.is_relative_to(self.tasks) or parent.get('project')!=job['project'] or not parent.get('session_id'):
                raise ValueError('原任务工作目录或项目不匹配')
            if parent['session_id']!=job.get('session_id'):
                raise ValueError('原任务会话与云端不匹配')
            record['session_id']=parent['session_id']
            return path
        path=self.tasks/job['id']
        project=self.settings['projects'][job['project']]
        if path.exists():
            raise ValueError('任务目录已存在，停止自动重跑，请先检查本机记录')
        result=subprocess.run(['git','-c','safe.directory='+str(Path(project['source']).resolve()),'-C',project['source'],'worktree','add','--detach',str(path),project.get('ref','HEAD')],
                              capture_output=True,timeout=60)
        if result.returncode:
            raise RuntimeError('无法为本机项目创建独立工作目录')
        return path

    def command(self,record):
        command=[self.settings['codex_executable'],'exec','-s','workspace-write',
                 '-C',record['workdir'],'-c','approval_policy="never"','-c','notify=[]']
        for name in self.settings.get('disabled_mcp_servers',[]):
            if not re.fullmatch(r'[A-Za-z0-9_-]+',name):
                raise ValueError('MCP 配置名称无效')
            command+=['-c','mcp_servers.'+name+'.enabled=false']
        if record.get('session_id'):
            command+=['resume',record['session_id']]
        return command+['--json','-']

    def execute(self,job):
        if not re.fullmatch(r'cx_[0-9a-f]{16}',job.get('id','')) or job.get('project') not in self.settings['projects']:
            raise ValueError('云端返回了无效任务')
        folder=self.jobs/job['id']
        folder.mkdir(exist_ok=True)
        journal=folder/'record.json'
        if journal.exists():
            # A queue claim must never cause a completed/local-started task to execute twice.
            raise ValueError('本机已有执行记录，拒绝重复执行')
        record={**job,'state':'preparing','created':time.time(),'session_id':job.get('session_id','')}
        save(journal,record)
        process=tree=None
        try:
            directory=self.workdir(job,record)
            record.update({'workdir':str(directory),'state':'running'})
            save(journal,record)
            prompt=('你是通过用户本人微信或飞书指令启动的本机 Codex。只在当前已接入项目的工作目录完成任务。'
                    '不得读取密码、API Key、邮箱授权码、Codex 登录凭证或 SSH 私钥；不要修改 .git、.github、.env 或启动其他代理。'
                    '不要推送、合并、部署、发邮件或向其他人发消息。遇到沙箱阻止或缺少依赖，在结果中说明，不要绕过限制。'
                    '用户要求修改时实际编辑并运行相关验证；只要求分析时不改文件。最终用中文说明结果、文件和验证情况。\n\n用户任务：\n'+job['prompt'])
            environment=clean_env()
            if self.settings.get('tls_cert_file'):
                environment['SSL_CERT_FILE']=self.settings['tls_cert_file']
            executable=Path(self.settings['codex_executable'])
            environment['PATH']=str(executable.parent)+os.pathsep+environment.get('PATH',environment.get('Path',''))
            process=subprocess.Popen(self.command(record),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                                     cwd=directory,env=environment,
                                     creationflags=subprocess.CREATE_NO_WINDOW|subprocess.CREATE_NEW_PROCESS_GROUP if os.name=='nt' else 0,
                                     start_new_session=os.name!='nt')
            tree=ProcessTree(process)
            events=queue.Queue(maxsize=2000)
            def read_stdout():
                while True:
                    line=process.stdout.readline(1000001)
                    if not line:
                        break
                    try:
                        event=json.loads(line) if len(line)<=1000000 else {}
                    except (ValueError,UnicodeError):
                        continue
                    events.put(event)
                events.put(None)
            errors=[]
            def read_errors():
                for line in iter(process.stderr.readline,b''):
                    if len(errors)<10:
                        errors.append(redact(line.decode('utf-8',errors='replace'))[:300])
            threading.Thread(target=read_stdout,daemon=True).start()
            threading.Thread(target=read_errors,daemon=True).start()
            process.stdin.write(prompt.encode('utf-8'))
            process.stdin.close()
            last_ping=0
            failures=0
            completed=False
            canceled=False
            failure=''
            last_error=''
            answer=''
            progress='正在分析项目'
            began=time.monotonic()
            while True:
                if self.stop.is_set() or time.monotonic()-began>self.settings.get('task_timeout_seconds',3600):
                    failure='任务超过执行时限或执行端正在停止，已停止任务进程。'
                    tree.terminate()
                    break
                try:
                    event=events.get(timeout=0.5)
                    if event is None:
                        break
                    kind=event.get('type','')
                    if kind=='thread.started':
                        record['session_id']=event['thread_id']
                        save(journal,record)
                    elif kind=='turn.completed':
                        completed=True
                    elif kind=='turn.failed':
                        failure=redact(event.get('error',{}).get('message','') if isinstance(event.get('error'),dict) else event.get('message','Codex 执行失败'))[:500]
                    elif kind=='error':
                        last_error=redact(event.get('message','Codex 连接暂时失败'))[:500]
                    elif kind.startswith('item.'):
                        item=event.get('item',{})
                        category=item.get('type')
                        if category=='agent_message' and item.get('text'):
                            answer=redact(item['text'])[:11000]
                        progress={'command_execution':'正在运行项目命令或验证','file_change':'正在修改项目文件',
                                  'agent_message':'正在整理结果','reasoning':'正在分析项目'}.get(category,progress)
                except queue.Empty:
                    pass
                if time.monotonic()-last_ping>=20:
                    try:
                        response=self.rpc('heartbeat',id=job['id'],lease=job['lease'],progress=progress,
                                          session_id=record.get('session_id',''),workdir=record['workdir'])
                        failures=0
                        if response['stop']:
                            canceled=True
                            tree.terminate()
                            break
                    except (RuntimeError,OSError,subprocess.TimeoutExpired):
                        failures+=1
                        if failures>=3:
                            failure='执行端与云端的连接持续失败，已停止执行，工作文件保留。'
                            tree.terminate()
                            break
                    last_ping=time.monotonic()
            try:
                code=process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                tree.terminate()
                code=process.wait(timeout=10)
            record['status']='canceled' if canceled else ('done' if code==0 and completed and answer and not failure else 'failed')
            if record['status']!='done':
                answer=(failure or ('用户已要求停止任务。' if canceled else last_error or 'Codex 未正常完成，请检查本机执行记录。'))+'\n'+answer[:6000]
            change=subprocess.run(['git','-c','safe.directory='+str(directory),'status','--short'],cwd=directory,capture_output=True,encoding='utf-8',errors='replace',timeout=15)
            files=redact(change.stdout.strip())[:1800]
            record['result']=(answer+'\n\n本机工作目录：'+record['workdir']+('\n文件变更：\n'+files if files else '\n未产生文件变更。'))[:14000]
        except Exception as error:
            if tree:
                tree.terminate()
            record.update({'status':'failed','result':'本机执行未完成：'+(str(error) if isinstance(error,(ValueError,RuntimeError)) else type(error).__name__)})
        finally:
            if tree:
                tree.close()
        record['state']='report_pending'
        record['finished']=time.time()
        save(journal,record)
        self.report(journal,record)

    def report(self,journal,record):
        self.rpc('finish',id=record['id'],lease=record['lease'],status=record['status'],result=record['result'],
                 session_id=record.get('session_id',''),workdir=record.get('workdir',''))
        record['state']='reported'
        save(journal,record)

    def recover(self):
        for journal in sorted(self.jobs.glob('*/record.json')):
            record=json.loads(journal.read_text(encoding='utf-8'))
            if record['state']=='reported':
                continue
            if record['state']!='report_pending':
                record.update({'status':'failed','result':'本机执行端曾意外退出。任务没有自动重跑，原工作目录和修改保留，请检查后继续。','state':'report_pending'})
                save(journal,record)
            self.report(journal,record)

    def run(self,once=False):
        lock=acquire_instance_lock(self.root/'worker.sqlite3')
        try:
            while not self.stop.is_set():
                try:
                    self.recover()
                    response=self.rpc('claim',projects=list(self.settings['projects']),version='local-codex-1.0')
                    save(self.root/'health.json',{'updated':time.time(),'connected':True,'pid':os.getpid(),'busy':response['job']['id'] if response.get('job') else ''})
                    if response.get('job'):
                        self.execute(response['job'])
                        if once:
                            return
                        continue
                except Exception as error:
                    save(self.root/'health.json',{'updated':time.time(),'connected':False,'pid':os.getpid(),'error':type(error).__name__})
                    print('本机执行端待重试：'+type(error).__name__,flush=True)
                if once:
                    return
                self.stop.wait(15)
        finally:
            lock.close()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',required=True)
    parser.add_argument('--once',action='store_true')
    args=parser.parse_args()
    settings=json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
    worker=Worker(settings)
    def stop(*args):
        worker.stop.set()
    signal.signal(signal.SIGINT,stop)
    signal.signal(signal.SIGTERM,stop)
    worker.run(args.once)


if __name__=='__main__':
    main()
