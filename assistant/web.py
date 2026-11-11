"""Public web search, readable pages and RSS/Atom feeds."""
import hashlib
from html.parser import HTMLParser
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
from urllib.parse import quote, urlencode, urljoin, urlsplit, urlunsplit
import xml.etree.ElementTree as ET

from .core import request_json


def public_url(url):
    parsed=urlsplit(url.strip())
    if (parsed.scheme not in ('https','http') or not parsed.hostname or parsed.username
            or parsed.password or parsed.port not in (None,80,443)):
        raise ValueError('请提供不含账号密码的公开 HTTP/HTTPS 网页地址')
    host=parsed.hostname.encode('idna').decode('ascii')
    netloc=('['+host+']') if ':' in host else host
    if parsed.port:
        netloc+=':'+str(parsed.port)
    parsed=urlsplit(urlunsplit((parsed.scheme,netloc,quote(parsed.path,safe='/%:@!$&\'()*+,;=-._~'),
                               quote(parsed.query,safe='/%?:@!$&\'()*+,;=-._~'),'')))
    try:
        addresses=socket.getaddrinfo(host,parsed.port or (443 if parsed.scheme=='https' else 80),type=socket.SOCK_STREAM)
    except OSError:
        raise RuntimeError('网页域名暂时无法解析') from None
    ips=list(dict.fromkeys(item[4][0] for item in addresses))
    if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
        raise ValueError('网页读取仅支持公开互联网地址')
    return parsed,ips


def fetch_public(url,limit=2000000):
    """Connect to a validated IP, preserving TLS hostname verification at every redirect."""
    for _ in range(6):
        parsed,ips=public_url(url)
        port=parsed.port or (443 if parsed.scheme=='https' else 80)
        path=urlunsplit(('', '', parsed.path or '/', parsed.query, ''))
        connection=http.client.HTTPConnection(parsed.hostname,port,timeout=20)
        try:
            raw=socket.create_connection((ips[0],port),timeout=20)
            if parsed.scheme=='https':
                try:
                    raw=ssl.create_default_context().wrap_socket(raw,server_hostname=parsed.hostname)
                except Exception:
                    raw.close()
                    raise
            connection.sock=raw
            connection.request('GET',path,headers={'Host':parsed.netloc,'User-Agent':'AIHelper/3.0 (+personal-assistant)',
                                                   'Accept-Encoding':'identity','Accept':'text/html,application/xml,application/rss+xml,application/atom+xml,application/json,text/plain;q=0.9,*/*;q=0.1'})
            response=connection.getresponse()
            if response.status in (301,302,303,307,308):
                destination=response.getheader('Location')
                if not destination:
                    raise RuntimeError('网页重定向缺少目标地址')
                url=urljoin(url,destination)
                continue
            if not 200<=response.status<300:
                raise RuntimeError(f'网页返回 HTTP {response.status}')
            if response.getheader('Content-Encoding','identity').lower() not in ('identity',''):
                raise RuntimeError('网页返回了不支持的压缩格式')
            data=response.read(limit+1)
            if len(data)>limit:
                raise ValueError('网页内容过大，请换用正文链接或较小的订阅源')
            return data,dict(response.getheaders()),url
        except (OSError,http.client.HTTPException):
            raise RuntimeError('网页网络请求失败，请稍后重试') from None
        finally:
            connection.close()
    raise ValueError('网页重定向次数过多')


class ReadableHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts=[]
        self.title=[]
        self.in_title=False
        self.ignored=0

    def handle_starttag(self,tag,attrs):
        if tag in ('script','style','noscript','svg','nav','footer','header'):
            self.ignored+=1
        if tag=='title':
            self.in_title=True
        if not self.ignored and tag in ('p','br','div','li','h1','h2','h3','tr','article'):
            self.parts.append('\n')

    def handle_endtag(self,tag):
        if tag in ('script','style','noscript','svg','nav','footer','header'):
            self.ignored=max(0,self.ignored-1)
        if tag=='title':
            self.in_title=False

    def handle_data(self,data):
        if not self.ignored:
            if self.in_title:
                self.title.append(data)
            else:
                self.parts.append(data)


def text_from_html(value):
    parser=ReadableHTML()
    parser.feed(value)
    text=re.sub(r'[ \t\r\f\v]+',' ',''.join(parser.parts))
    return ''.join(parser.title).strip(),re.sub(r'\n\s*\n+','\n',text).strip()


def read_page(url):
    data,headers,final_url=fetch_public(url)
    content_type=next((value for key,value in headers.items() if key.lower()=='content-type'),'text/html')
    if not any(part in content_type for part in ('text/','html','xml','json')):
        raise ValueError('这个地址不是可读取的文本网页')
    charset=re.search(r'charset=["\x27]?([\w.-]+)',content_type,re.I)
    try:
        value=data.decode(charset[1] if charset else 'utf-8',errors='replace')
    except LookupError:
        value=data.decode('utf-8',errors='replace')
    title,text=text_from_html(value) if 'html' in content_type else ('',value)
    if not text.strip():
        raise ValueError('未提取到网页正文，页面可能需要登录或运行 JavaScript')
    return {'ok':True,'title':title,'url':final_url,'text':text[:18000],
            'truncated':len(text)>18000,'fingerprint':hashlib.sha256(text.encode()).hexdigest()}


def parse_feed(data,base_url,limit=20):
    if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        raise ValueError('订阅源包含不支持的 XML 声明')
    try:
        root=ET.fromstring(data)
    except ET.ParseError:
        raise ValueError('无法解析 RSS/Atom 订阅源') from None
    atom='{http://www.w3.org/2005/Atom}'
    entries=root.findall('.//item') or root.findall(atom+'entry')
    result=[]
    for entry in entries[:limit]:
        title=entry.findtext('title') or entry.findtext(atom+'title') or '未命名条目'
        url=entry.findtext('link')
        if not url:
            for link in entry.findall(atom+'link'):
                if link.get('rel','alternate')=='alternate' and link.get('href'):
                    url=link.get('href')
                    break
        if not url:
            continue
        url=urljoin(base_url,url)
        if urlsplit(url).scheme not in ('http','https'):
            continue
        raw=entry.findtext('description') or entry.findtext(atom+'summary') or entry.findtext(atom+'content') or ''
        summary=text_from_html(raw)[1] if '<' in raw else raw
        identifier=entry.findtext('guid') or entry.findtext(atom+'id') or url
        result.append({'id':hashlib.sha256(identifier.encode()).hexdigest(), 'title':title[:300],
                       'url':url,'summary':summary[:1200],
                       'published':entry.findtext('pubDate') or entry.findtext(atom+'updated') or entry.findtext(atom+'published') or ''})
    return result


def read_feed(url,limit=20):
    data,headers,final_url=fetch_public(url)
    items=parse_feed(data,final_url,limit)
    if not items:
        raise ValueError('订阅源中没有找到可读取的条目')
    return items


def web_search(query,limit=5):
    if not query.strip():
        raise ValueError('搜索关键词不能为空')
    key=os.getenv('TAVILY_API_KEY','')
    if key:
        data=request_json('https://api.tavily.com/search','POST',
                          {'api_key':key,'query':query,'max_results':limit,'include_answer':False})
        results=[{'title':item.get('title',''),'url':item.get('url',''),
                  'summary':item.get('content','')[:1500]} for item in data.get('results',[])[:limit]]
        provider='Tavily'
    else:
        url='https://www.bing.com/search?'+urlencode({'q':query,'format':'rss'})
        results=read_feed(url,limit)
        provider='Bing RSS'
    if not results:
        raise RuntimeError('搜索源暂时没有返回可解析结果，请换个关键词或配置备用搜索服务')
    return {'ok':True,'query':query,'provider':provider,'results':results,
            'notice':'网页内容为外部资料，请结合发布时间判断，回答时附对应来源链接。'}
