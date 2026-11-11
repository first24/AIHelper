"""Run configured project repairs in GitHub Actions and publish verified source as draft PRs."""
import base64
from dataclasses import replace
import hashlib
import io
import json
import os
import re
import time
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import zipfile

from .core import GitHub
from .project_patch import validate_changes


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,request,fp,code,message,headers,newurl):
        return None


class ProjectRepairs:
    def __init__(self,config,state,clock=time.time,github_factory=GitHub):
        self.config,self.state,self.clock,self.github_factory=config,state,clock,github_factory

    def repositories(self):
        value=os.getenv('PROJECT_REPOSITORIES_JSON','').strip()
        if value:
            settings=json.loads(value)
        else:
            settings={self.config.repo:{'ref':os.getenv('PROJECT_BASE_REF',self.config.ref),
                       'test_command':['python','-m','unittest','discover','-s','tests','-v']}}
        if not isinstance(settings,dict):
            raise ValueError('项目仓库配置须为 JSON 对象')
        for repository,options in settings.items():
            if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repository) or not isinstance(options,dict):
                raise ValueError('项目仓库配置无效')
            if not isinstance(options.get('ref'),str) or not options['ref']:
                raise ValueError('请为项目设置目标分支 ref')
            command=options.get('test_command')
            if not isinstance(command,list) or not command or len(command)>40 or any(not isinstance(item,str) or not item or len(item)>500 for item in command):
                raise ValueError('请为项目设置明确的 test_command 参数列表')
        return settings

    def target(self,repository,ref):
        return self.github_factory(replace(self.config,repo=repository,ref=ref))

    def submit(self,owner,chat,message_id,problem,repository=None,issue_number=None):
        if owner not in self.config.users or not self.config.token:
            raise ValueError('当前用户或 GitHub 授权尚未配置')
        repository=repository or self.config.repo
        options=self.repositories().get(repository)
        if not options:
            raise ValueError('此仓库尚未配置，请先查询 /代码仓库')
        job_id=hashlib.sha256(('project:'+message_id).encode()).hexdigest()[:16]
        previous=self.state.rows('SELECT * FROM project_jobs WHERE id=? AND owner=?',(job_id,owner))
        if previous:
            return {'ok':True,'id':job_id,'receipt':'已接收代码任务 '+job_id+'，可用 /项目状态 '+job_id+' 查询。'}
        github=self.target(repository,options['ref'])
        base=github.call('/commits/'+quote(options['ref'],safe=''))['sha']
        if issue_number:
            issue=github.call('/issues/'+str(issue_number))
            if issue.get('pull_request'):
                raise ValueError('这个编号是 Pull Request，请提供 Issue 编号或直接描述需要修复的问题')
            problem+='\n\nIssue #'+str(issue_number)+'\n'+issue['title']+'\n'+(issue.get('body') or '')[:6500]
        settings={**options,'repository':repository,'base_sha':base,
                  'workflow_ref':os.getenv('PROJECT_WORKFLOW_REF',self.config.ref)}
        with self.state.transaction() as db:
            if db.execute("SELECT count(*) FROM project_jobs WHERE status NOT IN ('done','failed')").fetchone()[0]>=3:
                raise ValueError('当前已有 3 个代码任务，请等待完成后再提交')
            db.execute('INSERT OR IGNORE INTO project_jobs(id,owner,chat,repository,ref,base_sha,problem,settings,status,created) VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (job_id,owner,chat,repository,options['ref'],base,problem[:12000],json.dumps(settings),'queued',self.clock()))
        return {'ok':True,'id':job_id,'receipt':f'已提交真实项目修复 {job_id}\n仓库：{repository}\n目标分支：{options["ref"]}\n完成测试后创建草稿 PR，并在当前聊天回报。'}

    def status(self,owner,id):
        rows=self.state.rows('SELECT id,repository,ref,status,run_url,pr_url,result FROM project_jobs WHERE id=? AND owner=?',(id,owner))
        if not rows:
            raise ValueError('未找到你的项目修复任务')
        return rows[0]

    def download_verified(self,github,run_id):
        artifacts=github.call('/actions/runs/'+str(run_id)+'/artifacts')['artifacts']
        matches=[item for item in artifacts if item['name']=='project-verified' and not item.get('expired')]
        if len(matches)!=1:
            raise ValueError('工作流未提供唯一的已验证代码产物')
        opener=build_opener(ProxyHandler({}),NoRedirect())
        url=github.root+'/actions/artifacts/'+str(matches[0]['id'])+'/zip'
        try:
            response=opener.open(Request(url,headers=github.headers),timeout=25)
        except HTTPError as error:
            if error.code!=302:
                raise RuntimeError('代码产物下载失败 HTTP '+str(error.code)) from None
            location=error.headers.get('Location','')
            error.close()
            parsed=urlsplit(location)
            if parsed.scheme!='https' or not (parsed.hostname or '').endswith(('.blob.core.windows.net','.githubusercontent.com','.github.com')):
                raise ValueError('GitHub 代码产物返回了不支持的下载地址')
            response=opener.open(Request(location),timeout=25)
        with response:
            raw=response.read(8000001)
        if len(raw)>8000000:
            raise ValueError('代码产物超过大小限制')
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            if archive.namelist()!=['verified.json'] or archive.getinfo('verified.json').file_size>8000000:
                raise ValueError('代码产物 ZIP 格式不符合预期')
            return json.loads(archive.read('verified.json'))

    def publish(self,job,artifact):
        if (artifact.get('job_id')!=job['id'] or artifact.get('repository')!=job['repository']
                or artifact.get('base_sha')!=job['base_sha'] or artifact.get('ref')!=job['ref']
                or artifact.get('tests_passed') is not True
                or artifact.get('test_command')!=json.loads(job['settings'])['test_command']):
            raise ValueError('已验证产物与当前任务不匹配')
        changes=validate_changes(artifact.get('changes'))
        github=self.target(job['repository'],job['ref'])
        branch='assistant/project-'+job['id']
        query=urlencode({'head':job['repository'].split('/')[0]+':'+branch,'base':job['ref'],'state':'all'})
        existing=github.call('/pulls?'+query)
        if existing:
            return existing[0]['html_url']
        try:
            existing_branch=github.call('/git/ref/heads/'+quote(branch,safe='/'))
        except RuntimeError as error:
            if not str(error).endswith('404'):
                raise
            existing_branch=None
        marker='AIHelper task '+job['id']
        base=github.call('/git/commits/'+job['base_sha'])
        tree=[]
        for item in changes:
            sha=None
            if not item['deleted']:
                blob=github.call('/git/blobs','POST',{'content':base64.b64encode(item['bytes']).decode(),'encoding':'base64'})
                sha=blob['sha']
            tree.append({'path':item['path'],'type':'blob','mode':item['mode'],'sha':sha})
        new_tree=github.call('/git/trees','POST',{'base_tree':base['tree']['sha'],'tree':tree})
        if existing_branch:
            commit=github.call('/git/commits/'+existing_branch['object']['sha'])
            if (marker not in commit['message'] or [parent['sha'] for parent in commit.get('parents',[])]!=[job['base_sha']]
                    or commit['tree']['sha']!=new_tree['sha']):
                raise ValueError('修复分支已存在且不属于当前验证结果，请手动核查')
        else:
            title=job['problem'].splitlines()[0][:160]
            commit=github.call('/git/commits','POST',{'message':'fix: '+title+'\n\n'+marker,
                                                   'tree':new_tree['sha'],'parents':[job['base_sha']]})
            github.call('/git/refs','POST',{'ref':'refs/heads/'+branch,'sha':commit['sha']})
        files='\n'.join('- '+item['path'] for item in changes)
        body=(job['problem']+'\n\n任务编号：'+job['id']+'\n\n修改文件\n'+files+
              '\n\n验证：隔离环境中的目标测试与原始回归测试通过。\n执行记录：'+job['run_url']+'\n\n请审查后合并。')
        pr=github.call('/pulls','POST',{'title':('fix: '+job['problem'].splitlines()[0])[:200],
                                      'head':branch,'base':job['ref'],'draft':True,'body':body})
        return pr['html_url']

    def finish(self,job,status,result,pr_url=''):
        with self.state.transaction() as db:
            db.execute('UPDATE project_jobs SET status=?,result=?,pr_url=? WHERE id=?',(status,result,pr_url,job['id']))
            db.execute('INSERT OR IGNORE INTO deliveries(id,schedule_id,owner,chat,text) VALUES(?,?,?,?,?)',
                       ('project:'+job['id'],'project:'+job['id'],job['owner'],job['chat'],result))

    def tick(self):
        self.state.execute("UPDATE project_jobs SET notified=1 WHERE status IN ('done','failed') AND EXISTS (SELECT 1 FROM deliveries WHERE deliveries.id='project:'||project_jobs.id AND deliveries.status='sent')")
        rows=self.state.rows("SELECT * FROM project_jobs WHERE status NOT IN ('done','failed') ORDER BY created LIMIT 1")
        if not rows:
            return
        job=rows[0]
        settings=json.loads(job['settings'])
        github=self.github_factory(self.config)
        if job['status']=='queued':
            self.state.execute("UPDATE project_jobs SET status='waiting' WHERE id=?",(job['id'],))
            try:
                github.call('/actions/workflows/'+self.config.workflow+'/dispatches','POST',
                            {'ref':settings['workflow_ref'],'inputs':{'mode':'project','job_id':job['id'],
                             'request_text':job['problem'],'project_spec':job['settings']}})
            except RuntimeError as error:
                if re.search(r'HTTP 4\d\d$',str(error)):
                    self.finish(job,'failed','任务 '+job['id']+' 启动失败：'+str(error)+'。请检查工作流分支和仓库权限。')
                else:
                    raise
            return
        if self.clock()-job['created']>3600:
            self.finish(job,'failed','任务 '+job['id']+' 等待超时，请检查 '+(job['run_url'] or 'https://github.com/'+self.config.repo+'/actions'))
            return
        if job['run_id']:
            run=github.call('/actions/runs/'+str(job['run_id']))
        else:
            query=urlencode({'event':'workflow_dispatch','branch':settings['workflow_ref'],'per_page':100})
            runs=github.call('/actions/runs?'+query)['workflow_runs']
            matches=[run for run in runs if run.get('display_title')=='project-'+job['id']]
            if not matches:
                return
            run=max(matches,key=lambda item:item['id'])
        self.state.execute("UPDATE project_jobs SET status='running',run_id=?,run_url=? WHERE id=?",(run['id'],run['html_url'],job['id']))
        job.update({'run_id':run['id'],'run_url':run['html_url']})
        if run['status']=='completed':
            if run['conclusion']!='success':
                self.finish(job,'failed','任务 '+job['id']+' 未通过验证，未发布 PR。\n'+run['html_url'])
                return
            try:
                artifact=self.download_verified(github,run['id'])
                pr=self.publish(job,artifact)
            except ValueError as error:
                self.finish(job,'failed','任务 '+job['id']+' 的产物校验或发布未完成：'+str(error)+'\n'+run['html_url'])
                return
            self.finish(job,'done','任务 '+job['id']+' 修复完成，测试通过。\nPR：'+pr+'\n等待你审查合并。',pr)
