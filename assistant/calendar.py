"""Feishu personal-calendar OAuth and calendar tools."""
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import secrets
import threading
import time
from datetime import datetime, timedelta
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from .core import request_json
from .state import acquire_instance_lock
from .weixin import write_private_json


def scopes():
    import os
    access=os.getenv('CALENDAR_ACCESS','read')
    value='offline_access calendar:calendar:readonly'
    if access in ('write','delete'):
        value+=' calendar:calendar.event:create calendar:calendar.event:update'
    if access=='delete':
        value+=' calendar:calendar.event:delete'
    return value
REDIRECT_URI='http://127.0.0.1:8765/callback'


def token_request(config,body):
    data=request_json('https://open.feishu.cn/open-apis/authen/v2/oauth/token','POST',
                      {'client_id':config.app_id,'client_secret':config.app_secret,**body})
    result=data.get('data',data)
    if data.get('code',0)!=0 or not result.get('access_token'):
        code=data.get('code',data.get('error','unknown'))
        raise RuntimeError('飞书日历授权失败，返回码 '+str(code))
    result=dict(result)
    result['expires_at']=time.time()+int(result.get('expires_in',7200))
    result['client_id']=config.app_id
    return result


class Calendar:
    def __init__(self,config,zone,clock=time.time):
        self.config,self.zone,self.clock=config,zone,clock
        self.lock=threading.RLock()

    def path(self,owner):
        if owner not in self.config.users:
            raise ValueError('当前用户未获授权')
        return Path(self.config.db).parent/'calendar'/(hashlib.sha256(owner.encode()).hexdigest()[:24]+'.json')

    def token(self,owner):
        path=self.path(owner)
        with self.lock:
            if not path.exists():
                raise ValueError('个人飞书日历尚未授权，请先完成 calendar-login 本人授权')
            lock=acquire_instance_lock(path.parent/'credentials.sqlite3')
            try:
                data=json.loads(path.read_text(encoding='utf-8'))
                if data.get('client_id')!=self.config.app_id or data.get('open_id')!=owner:
                    raise ValueError('飞书日历绑定与当前账号不匹配，请重新授权')
                if data.get('expires_at',0)<=self.clock()+120:
                    if not data.get('refresh_token'):
                        raise ValueError('飞书日历授权已过期，请重新授权并允许离线访问')
                    refreshed=token_request(self.config,{'grant_type':'refresh_token','refresh_token':data['refresh_token']})
                    refreshed['refresh_token']=refreshed.get('refresh_token') or data['refresh_token']
                    refreshed['open_id']=owner
                    data=refreshed
                    write_private_json(path,data)
                return data['access_token']
            finally:
                lock.close()

    def call(self,owner,path,method='GET',body=None):
        data=request_json('https://open.feishu.cn/open-apis/calendar/v4/'+path,method,body,
                          {'Authorization':'Bearer '+self.token(owner)}) or {}
        if data.get('code',0)!=0:
            raise RuntimeError('飞书日历接口返回错误 '+str(data.get('code'))+'，请检查用户授权和应用日历权限')
        return data.get('data') or {}

    def primary(self,owner):
        data=self.call(owner,'calendars/primary?user_id_type=open_id','POST',{})
        for item in data.get('calendars',[]):
            calendar=item.get('calendar',item)
            if calendar.get('calendar_id'):
                return calendar['calendar_id']
        raise RuntimeError('飞书未返回个人主日历')

    def window(self,start,end):
        try:
            first=datetime.fromisoformat(start.replace('Z','+00:00'))
            last=datetime.fromisoformat(end.replace('Z','+00:00'))
        except (ValueError,AttributeError):
            raise ValueError('请提供有效的开始和结束日期时间') from None
        if first.tzinfo is None:
            first=first.replace(tzinfo=self.zone)
        if last.tzinfo is None:
            last=last.replace(tzinfo=self.zone)
        if last<=first or last-first>timedelta(days=31):
            raise ValueError('结束时间须晚于开始时间，单次查询范围不超过 31 天')
        return first,last

    def event_time(self,value):
        if value.get('timestamp'):
            return datetime.fromtimestamp(int(value['timestamp']),self.zone)
        return datetime.fromisoformat(value['date']).replace(tzinfo=self.zone)

    def events(self,owner,start,end):
        first,last=self.window(start,end)
        calendar=self.primary(owner)
        query=urlencode({'start_time':str(int(first.timestamp())),'end_time':str(int(last.timestamp()))})
        data=self.call(owner,'calendars/'+quote(calendar,safe='')+'/events/instance_view?'+query)
        if data.get('has_more'):
            raise RuntimeError('日程结果超过接口返回范围，请缩小查询时间，避免遗漏冲突')
        results=[]
        for event in data.get('items',[]):
            if event.get('status') in ('cancelled','canceled'):
                continue
            event_start=self.event_time(event['start_time'])
            event_end=self.event_time(event['end_time'])
            if event_start>=last or event_end<=first:
                continue
            results.append({'id':event['event_id'],'title':event.get('summary','未命名日程'),
                            'start':event_start.isoformat(),'end':event_end.isoformat(),
                            'busy':event.get('free_busy_status','busy')!='free' and event.get('self_rsvp_status') not in ('decline','declined'),
                            'url':event.get('app_link','')})
        return {'ok':True,'calendar_id':calendar,'events':sorted(results,key=lambda item:item['start'])}

    def slots(self,owner,start,end,minutes):
        first,last=self.window(start,end)
        first=max(first,datetime.fromtimestamp(self.clock(),self.zone))
        events=self.events(owner,start,end)['events']
        intervals=sorted((datetime.fromisoformat(event['start']),datetime.fromisoformat(event['end'])) for event in events if event['busy'])
        free=[]
        cursor=first
        for busy_start,busy_end in intervals+[(last,last)]:
            if busy_start>cursor and min(busy_start,last)-cursor>=timedelta(minutes=minutes):
                free.append({'start':cursor.isoformat(),'end':min(busy_start,last).isoformat()})
            cursor=max(cursor,busy_end)
            if cursor>=last:
                break
        return {'ok':True,'minutes':minutes,'slots':free[:20]}

    def create(self,owner,title,start,end,description,effect_id,allow_conflict=False):
        first,last=self.window(start,end)
        if first.timestamp()<=self.clock():
            raise ValueError('请提供未来的日程开始时间')
        data=self.events(owner,start,end)
        conflicts=[event for event in data['events'] if event['busy']]
        if conflicts and not allow_conflict:
            return {'ok':False,'conflicts':conflicts,'receipt':'这个时间与已有日程冲突，尚未创建。请换个时间，或明确说明仍要安排。'}
        calendar=data['calendar_id']
        body={'summary':title,'description':description,'start_time':{'timestamp':str(int(first.timestamp())),'timezone':str(self.zone)},
              'end_time':{'timestamp':str(int(last.timestamp())),'timezone':str(self.zone)},'visibility':'default',
              'free_busy_status':'busy','need_notification':False}
        key=hashlib.sha256(effect_id.encode()).hexdigest()[:32]
        event=self.call(owner,'calendars/'+quote(calendar,safe='')+'/events?'+urlencode({'idempotency_key':key}),'POST',body)['event']
        return {'ok':True,'id':event['event_id'],'url':event.get('app_link',''),
                'receipt':f"已创建个人日程 {event['event_id']}：{title}\n{first.isoformat()} 至 {last.isoformat()}\n"+event.get('app_link','')}

    def update(self,owner,id,title=None,start=None,end=None,description=None,allow_conflict=False):
        calendar=self.primary(owner)
        path='calendars/'+quote(calendar,safe='')+'/events/'+quote(id,safe='')
        event=self.call(owner,path)['event']
        if event.get('recurrence') or event.get('recurring_event_id'):
            raise ValueError('这是一条重复日程，请先在飞书明确选择修改本次或整个系列')
        start=start or self.event_time(event['start_time']).isoformat()
        end=end or self.event_time(event['end_time']).isoformat()
        first,last=self.window(start,end)
        if first.timestamp()<=self.clock():
            raise ValueError('修改后的日程开始时间须在未来')
        conflicts=[item for item in self.events(owner,start,end)['events'] if item['busy'] and item['id']!=id]
        if conflicts and not allow_conflict:
            return {'ok':False,'conflicts':conflicts,'receipt':'新时间与已有日程冲突，尚未修改。'}
        body={'start_time':{'timestamp':str(int(first.timestamp())),'timezone':str(self.zone)},
              'end_time':{'timestamp':str(int(last.timestamp())),'timezone':str(self.zone)}}
        if title is not None:
            body['summary']=title
        if description is not None:
            body['description']=description
        self.call(owner,path,'PATCH',body)
        return {'ok':True,'id':id,'receipt':f'已修改日程 {id}\n{first.isoformat()} 至 {last.isoformat()}'}

    def delete(self,owner,id):
        calendar=self.primary(owner)
        path='calendars/'+quote(calendar,safe='')+'/events/'+quote(id,safe='')
        event=self.call(owner,path)['event']
        if event.get('recurrence') or event.get('recurring_event_id'):
            raise ValueError('这是一条重复日程，请在飞书选择取消本次或整个系列')
        self.call(owner,path,'DELETE')
        return {'ok':True,'id':id,'receipt':'已取消个人日程 '+id}


def login(config,zone):
    service=Calendar(config,zone)
    state=secrets.token_urlsafe(32)
    verifier=secrets.token_urlsafe(48)
    import base64
    challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    outcome={}
    class Callback(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass
        def do_GET(self):
            parsed=urlsplit(self.path)
            params=parse_qs(parsed.query)
            if parsed.path!='/callback' or not hmac.compare_digest(params.get('state',[''])[0],state):
                self.send_response(400)
                self.end_headers()
                self.wfile.write('授权请求校验失败。'.encode())
                return
            try:
                code=params.get('code',[''])[0]
                if not code:
                    raise ValueError('飞书授权未完成')
                token=token_request(config,{'grant_type':'authorization_code','code':code,'redirect_uri':REDIRECT_URI,'code_verifier':verifier})
                info=request_json('https://open.feishu.cn/open-apis/authen/v1/user_info',headers={'Authorization':'Bearer '+token['access_token']})
                owner=info.get('data',{}).get('open_id')
                if owner not in config.users:
                    raise ValueError('授权账号与当前助理允许使用的本人账号不一致')
                if not token.get('refresh_token'):
                    raise ValueError('未取得离线授权，请在飞书允许 offline_access 后重新授权')
                token['open_id']=owner
                path=service.path(owner)
                path.parent.mkdir(parents=True,exist_ok=True)
                lock=acquire_instance_lock(path.parent/'credentials.sqlite3')
                try:
                    write_private_json(path,token)
                finally:
                    lock.close()
                outcome['owner']=owner
                message='个人日历授权成功，可以回到微信或飞书使用助理。'
                print('CALENDAR_AUTHORIZED 本人日历授权已保存。',flush=True)
            except (ValueError,RuntimeError) as error:
                message=str(error)
                outcome['error']=message
                print('CALENDAR_AUTH_FAILED '+message,flush=True)
            self.send_response(200)
            self.send_header('Content-Type','text/html; charset=utf-8')
            self.send_header('Cache-Control','no-store')
            self.end_headers()
            import html
            self.wfile.write(('<!doctype html><meta charset="utf-8"><h2>'+html.escape(message)+'</h2>').encode())
    server=HTTPServer(('0.0.0.0',8765),Callback)
    server.timeout=1
    query=urlencode({'client_id':config.app_id,'redirect_uri':REDIRECT_URI,'response_type':'code','scope':scopes(),
                     'state':state,'code_challenge':challenge,'code_challenge_method':'S256'})
    print('CALENDAR_AUTH_URL https://accounts.feishu.cn/open-apis/authen/v1/authorize?'+query,flush=True)
    try:
        deadline=time.monotonic()+600
        while not outcome and time.monotonic()<deadline:
            server.handle_request()
        if not outcome.get('owner'):
            raise ValueError(outcome.get('error','个人日历授权等待超时，请重新运行 calendar-login'))
    finally:
        server.server_close()
