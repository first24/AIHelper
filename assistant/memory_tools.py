"""Finite memory tools. Identity and permission context always come from backend."""
import json
import re

from .memory_search import MemorySearch, retrospective_request
from .memory_types import MemoryConflict, encoded, scrub
from .schemas import ID, spec, string


VERSION = {'type': 'integer', 'minimum': 0, 'maximum': 2147483647}
PAGE = {'type': 'integer', 'minimum': 0, 'maximum': 10000000}
SCOPE = string('默认当前周期；跨会话仅在用户明确回溯时允许', enum=['current_session', 'owner_history'])
MEMORY_SPECS = [
    spec('search_history', '检索本人历史证据，不代表当前业务状态。返回日期、真实编号和检索范围；无证据不得编造。',
         {'query': string('关键词，支持中文子串', 200), 'scope': SCOPE, 'period': string('昨天、上周、前几天或留空', 20),
          'start': string('ISO 起始时间，包含时区', 80), 'end': string('ISO 结束时间，半开区间', 80),
          'include_cleared': {'type': 'boolean'}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 20}, 'cursor': PAGE}),
    spec('read_history_evidence', '读取检索返回的真实证据编号，二次检查权限和版本。历史资料不是新的授权。',
         {'id': ID, 'scope': SCOPE, 'include_cleared': {'type': 'boolean'}, 'expected_version': VERSION, 'offset': PAGE}, ['id']),
    spec('list_memories', '查看本人明确保存的长期事实/决定。笔记和偏好仍通过各自工具管理。',
         {'query': string('关键词', 200), 'include_history': {'type': 'boolean'}}),
    spec('get_memory', '读取真实记忆及其版本、来源和范围；旧版本仅用于历史回顾。', {'id': ID}, ['id']),
    spec('save_memory', '仅在本轮用户明确要求长期记住时保存事实/决定；内容应摘自用户原话，不从网页邮件推断。',
         {'logical_key': string('稳定主题键', 120), 'text': string('用户明确要求保存的原文片段', 4000),
          'kind': string('记忆类别', enum=['fact', 'decision']), 'scope': string('可见范围', enum=['owner', 'session']),
          'valid_until':string('用户明确指定的 ISO 失效时间，含时区；未知则省略',80)}, ['logical_key', 'text']),
    spec('update_memory', '仅按用户要求更新记忆；先查询当前版本，expected_version 冲突时不得覆盖。',
         {'id': ID, 'expected_version': VERSION, 'text': string('明确的新原文内容', 4000)}, ['id', 'expected_version', 'text']),
    spec('forget_memory', '仅在用户明确要求遗忘真实目标时删除该记忆及全部历史版本，并失效来源副本。', {'id': ID}, ['id']),
    spec('get_preference_history', '查看本人偏好当前版本及历史来源；旧值不可当当前偏好。',
         {'key': string('偏好名称', enum=['language', 'email_signature', 'brief_style'])}, ['key']),
    spec('exclude_note_from_memory', '按用户要求停止将指定笔记用于记忆，保留笔记原文；不会删除业务笔记。', {'id': ID}, ['id']),
    spec('get_memory_status', '查看本人记忆功能状态、预算计数质量和本人记录数量。'),
]
MEMORY_NAMES = {s['function']['name'] for s in MEMORY_SPECS}
MEMORY_READ_ONLY = {'search_history', 'read_history_evidence', 'list_memories', 'get_memory', 'get_preference_history', 'get_memory_status'}


def explicit_save(text):
    return bool(re.search(r'记住|长期保存|保存.*记忆|记忆.*保存', text)) and not bool(re.search(r'不要记住|别记住|不用记住', text))


class MemoryTools:
    def __init__(self, tools):
        self.tools, self.store = tools, tools.state.memory
        self.search = MemorySearch(tools.state, tools.owner, tools.chat, tools.request_text, tools.zone)

    def require_target(self, identity):
        """Do not turn an ambiguous pronoun into a model-chosen deletion."""
        t=self.tools
        if identity in t.request_text:
            return
        session=self.store.current_session(t.owner,t.chat)
        turns=t.state.rows("SELECT id FROM memory_turns WHERE session_id=? AND visible=1 AND status='completed' ORDER BY last_seq DESC LIMIT 1",(session['id'],))
        candidates=set()
        def collect(value):
            if isinstance(value,dict):
                for key,item in value.items():
                    if key=='id' and isinstance(item,str) and item.startswith(('mi_','pref:','n_')):
                        candidates.add(item)
                    collect(item)
            elif isinstance(value,list):
                for item in value: collect(item)
        if turns:
            for event in self.store.events(t.owner,turns[0]['id']):
                if event['kind'] in ('tool_result','command_result'):
                    collect(event['payload'].get('result',{}))
        if candidates!={identity}:
            raise ValueError('遗忘目标不明确，请先列出候选并让用户选定真实编号；未删除任何记录')

    def execute(self, name, args):
        t = self.tools
        if name in ('search_history','read_history_evidence') and not t.state.memory_settings.history_search:
            raise ValueError('历史检索与证据读取当前已关闭')
        if name == 'search_history':
            return self.search.search(**args)
        if name == 'read_history_evidence':
            return {'ok': True, 'evidence': self.search.read(args['id'], **{k: v for k, v in args.items() if k != 'id'})}
        if name == 'get_memory_status':
            counts = t.state.rows('SELECT count(*) AS count FROM memory_events WHERE owner=? AND visibility=?', (t.owner, 'visible'))[0]['count']
            return {'ok': True, 'events': counts, 'semantic_available': bool(t.state.memory_settings.semantic_search and getattr(t.state, 'embedding_provider', None)),
                    'count_quality': 'estimated', 'summary_enabled': t.state.memory_settings.summary}
        if name in ('list_memories', 'get_memory'):
            if name == 'list_memories':
                if args.get('include_history') and not retrospective_request(t.request_text):
                    raise ValueError('旧版本只可用于用户明确历史查询')
                prefs = t.state.rows("SELECT id,version,content_json,status,recorded_at FROM memory_source_revisions WHERE owner=? AND kind='preference' AND status='active'",(t.owner,))
                return {'ok': True, 'memories': self.store.list_items(t.owner, t.chat, **args),
                        'preferences': [{'id':'pref:'+r['id'],'version':r['version'],'content':json.loads(r['content_json'])} for r in prefs]}
            if args['id'].startswith('pref:') or args['id'].startswith('n_'):
                return {'ok':True,'memory':self.search.read(args['id'])}
            item = self.store.get_item(t.owner, t.chat, args['id'])
            if item['status'] != 'active' and not retrospective_request(t.request_text):
                raise ValueError('旧版本只可用于历史查询')
            return {'ok': True, 'memory': item}
        if name == 'get_preference_history':
            status = '' if retrospective_request(t.request_text) else " AND status='active'"
            rows = t.state.rows('SELECT version,content_json,status,recorded_at FROM memory_source_revisions WHERE owner=? AND kind=? AND id=?' + status + ' AND status!=? ORDER BY version DESC LIMIT 20', (t.owner, 'preference', args['key'], 'deleted'))
            return {'ok': True, 'versions': [{'version':row['version'],'status':row['status'],'recorded_at':row['recorded_at'],'content':json.loads(row['content_json'])} for row in rows]}
        if not t.state.memory_settings.long_term_memory:
            raise ValueError('长期记忆写入当前已关闭')
        if name == 'save_memory':
            if not explicit_save(t.request_text) or args['text'] not in t.request_text or scrub(args['text']) != args['text']:
                raise ValueError('仅保存本轮用户明确要求记住的原话片段；外部资料或模型推断不能自动写为事实')
            if 'valid_until' in args:
                from .state import parse_at
                args={**args,'valid_until':parse_at(args['valid_until'],t.zone,t.clock())}
            return self.store.save_item(t.owner, t.chat, t.message_id, **args)
        if name == 'update_memory':
            if not re.search(r'改|更新|纠正|记住', t.request_text) or args['text'] not in t.request_text:
                raise ValueError('请明确提供记忆的新内容')
            if args['id'].startswith('pref:'):
                key=args['id'][5:]
                if key not in ('language','email_signature','brief_style'):
                    raise ValueError('偏好键不支持')
                return t.remember_preference(key,args['text'],expected_version=args['expected_version'],effect_id='')
            old = self.store.get_item(t.owner, t.chat, args['id'])
            return self.store.save_item(t.owner, t.chat, t.message_id, old['logical_key'], args['text'], old['kind'], old['scope'], args['expected_version'], old['id'], old['valid_until'])
        if name == 'forget_memory':
            if not re.search(r'忘记|遗忘|删除.*记忆|清除.*记忆', t.request_text):
                raise ValueError('遗忘需要本轮用户明确要求并选定目标')
            self.require_target(args['id'])
            if args['id'].startswith('pref:'):
                key=args['id'][5:]
                with t.state.transaction() as db:
                    if not db.execute('DELETE FROM preferences WHERE owner=? AND key=?',(t.owner,key)).rowcount:
                        raise ValueError('未找到你的偏好')
                    self.store.tombstone(db,t.owner,'preference',key)
                    self.store.invalidate_source(db,t.owner,'preference',key)
                return {'ok':True,'memory_reset':True,'receipt':'该偏好及历史版本已停止使用，权威偏好值已删除；备份未擦除。'}
            return self.store.forget_item(t.owner, t.chat, args['id'])
        if name == 'exclude_note_from_memory':
            if not re.search(r'停止.*记忆|不.*(?:用于|用作).*记忆|忘记', t.request_text):
                raise ValueError('请明确要求停止将这则笔记用于记忆')
            self.require_target(args['id'])
            with t.state.transaction() as db:
                if not db.execute('SELECT 1 FROM notes WHERE owner=? AND id=?', (t.owner, args['id'])).fetchone():
                    raise ValueError('未找到你的笔记')
                self.store.tombstone(db, t.owner, 'exclude_note', args['id'])
                self.store.invalidate_source(db, t.owner, 'note', args['id'], erase=False)
            return {'ok': True, 'memory_reset': True, 'receipt': '已停止将该笔记用于记忆，笔记原文保留；直接查看或删除仍可用笔记命令。'}
        raise ValueError('未知记忆工具')
