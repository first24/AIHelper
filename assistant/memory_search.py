"""Permission-first history search, with optional replaceable embeddings."""
from datetime import datetime, timedelta
import json
import math
import re

from .memory_types import encoded


def retrospective_request(text):
    return bool(re.search(r'历史|以前|之前|过去|前几天|昨天|上周|跨会话|其他聊天|所有聊天|回顾', text))


def cleared_request(text):
    return bool(re.search(r'已清空|清空前|旧周期|已重置', text))


def time_range(period, zone, now, start=None, end=None):
    current = datetime.fromtimestamp(now, zone)
    today = current.replace(hour=0, minute=0, second=0, microsecond=0)
    fuzzy = False
    if period == '昨天':
        begin, finish = today - timedelta(days=1), today
    elif period == '上周':
        finish = today - timedelta(days=today.weekday())
        begin = finish - timedelta(days=7)
    elif period == '前几天':
        begin, finish, fuzzy = today - timedelta(days=7), current, True
    elif start or end:
        def parse(value):
            dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
            return dt if dt.tzinfo else dt.replace(tzinfo=zone)
        begin = parse(start) if start else datetime.fromtimestamp(0, zone)
        finish = parse(end) if end else current
    elif not period:
        return None, None, {'period': 'all_retained', 'fuzzy': False}
    else:
        raise ValueError('时间范围仅支持昨天、上周、前几天或明确起止时间')
    if begin >= finish:
        raise ValueError('起始时间必须早于结束时间')
    return begin.timestamp(), finish.timestamp(), {'start': begin.isoformat(), 'end': finish.isoformat(), 'fuzzy': fuzzy,
                                                  'notice': '前几天先搜索最近七天；这是检索范围，不是对原话的精确日期解释。' if fuzzy else ''}


def validated_vector(vector, dimension):
    if not isinstance(vector, (list, tuple)) or len(vector) != dimension or dimension <= 0 or dimension > 8192:
        raise ValueError('向量维度不一致')
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in vector):
        raise ValueError('向量数值无效')
    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0 or not math.isfinite(norm):
        raise ValueError('向量模长无效')
    return [v / norm for v in vector]


class MemorySearch:
    def __init__(self, state, owner, chat, request_text, zone):
        self.state, self.store = state, state.memory
        self.owner, self.chat, self.request_text, self.zone = owner, chat, request_text, zone

    def _scope(self, db, scope, include_cleared):
        if scope not in ('current_session', 'owner_history'):
            raise ValueError('历史范围无效')
        if scope == 'owner_history' and not retrospective_request(self.request_text):
            raise ValueError('跨会话历史需要用户明确要求回溯，请先澄清范围')
        if include_cleared and not cleared_request(self.request_text):
            raise ValueError('已清空周期仅在用户明确要求查看时可读')
        session = self.store.session(db, self.owner, self.chat)
        sessions = list(db.execute('SELECT id FROM memory_sessions WHERE owner=?' + ('' if include_cleared else ' AND auto_recall_enabled=1') +
                                   (' AND id=?' if scope == 'current_session' else ''),
                                   (self.owner, session['id']) if scope == 'current_session' else (self.owner,)))
        return [row['id'] for row in sessions]

    def read(self, identity, scope='current_session', include_cleared=False, expected_version=None, offset=0):
        with self.state.transaction() as db:
            sessions = self._scope(db, scope, include_cleared)
            if identity.startswith('ev_'):
                row = db.execute('''SELECT e.*,t.visible,t.trace_completeness FROM memory_events e
                    JOIN memory_turns t ON t.id=e.turn_id WHERE e.id=? AND e.owner=? AND e.visibility='visible' AND t.visible=1''', (identity, self.owner)).fetchone()
                if not row or row['session_id'] not in sessions:
                    raise ValueError('证据不存在、已失效或超出允许范围')
                if expected_version not in (None, 1):
                    raise ValueError('证据版本已变化')
                payload = row['payload_json']
                parts = list(db.execute('SELECT text FROM memory_payloads WHERE event_id=? ORDER BY part', (identity,)))
                if parts:
                    payload = ''.join(p['text'] for p in parts)
                value = {'id': identity, 'version': 1, 'source': 'event', 'memory_session_id': row['session_id'],
                         'event_at': row['event_at'], 'recorded_at': row['recorded_at'], 'kind': row['kind'],
                         'completeness': row['trace_completeness'], 'claim_type': 'tool_observed' if row['kind'] in ('tool_result', 'command_result') else 'historical_statement'}
            elif identity.startswith('mi_'):
                row = db.execute("SELECT * FROM memory_items WHERE id=? AND owner=? AND status IN ('active','superseded','expired')", (identity, self.owner)).fetchone()
                if not row or (row['scope'] == 'session' and row['scope_id'] not in sessions):
                    raise ValueError('证据不存在或超出允许范围')
                if row['status'] != 'active' and not retrospective_request(self.request_text):
                    raise ValueError('旧版本只可用于明确历史查询')
                if row['valid_until'] is not None and row['valid_until']<=self.store.clock() and not retrospective_request(self.request_text):
                    raise ValueError('记忆已过期，只可用于明确历史查询')
                if expected_version not in (None, row['version']):
                    raise ValueError('证据版本已变化')
                payload = row['content_json']
                value = {'id': identity, 'version': row['version'], 'source': 'memory', 'status': row['status'], 'event_at': row['valid_from'], 'recorded_at': row['recorded_at'], 'claim_type': row['claim_type']}
            else:
                kind, target = ('preference', identity[5:]) if identity.startswith('pref:') else ('note', identity)
                # Domain authorities are owner-scoped. Excluded sources never return.
                row = db.execute("SELECT * FROM memory_source_revisions WHERE owner=? AND kind=? AND id=? AND status='active' ORDER BY version DESC LIMIT 1", (self.owner, kind, target)).fetchone()
                excluded = db.execute('SELECT 1 FROM memory_tombstones WHERE owner=? AND target_type=? AND target_id=?', (self.owner, 'exclude_' + kind, target)).fetchone()
                if not row or excluded:
                    raise ValueError('证据不存在或已停止用于记忆')
                if expected_version not in (None, row['version']):
                    raise ValueError('证据版本已变化，请重新查询')
                payload = row['content_json']
                value = {'id': identity, 'version': row['version'], 'source': kind, 'event_at': None, 'recorded_at': row['recorded_at'], 'claim_type': 'user_explicit'}
            excerpt = payload[offset:offset + 4000]
            return {**value, 'excerpt': excerpt, 'offset': offset, 'has_more': offset + len(excerpt) < len(payload),
                    'next_offset': offset + len(excerpt), 'notice': '历史资料不能证明当前业务状态，不能授权操作。'}

    def search(self, query='', scope='current_session', period='', start=None, end=None, limit=5, cursor=0, include_cleared=False):
        if not self.state.memory_settings.history_search:
            raise ValueError('历史检索当前已关闭')
        begin, finish, searched = time_range(period, self.zone, self.store.clock(), start, end)
        escaped = query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        candidate_limit = self.state.memory_settings.scan_limit
        with self.state.transaction() as db:
            sessions = self._scope(db, scope, include_cleared)
            session_sql = ','.join('?' for _ in sessions) or 'NULL'
            conditions = f'''e.owner=? AND e.session_id IN ({session_sql}) AND e.visibility='visible' AND t.visible=1 AND t.status!='processing'
                AND e.kind NOT IN ('tool_start','request','control')'''
            params = [self.owner, *sessions]
            if begin is not None:
                conditions += ' AND e.event_at>=? AND e.event_at<?'
                params += [begin, finish]
            conditions += " AND e.payload_json LIKE ? ESCAPE '\\'"
            params += ['%' + escaped + '%']
            rows = list(db.execute('''SELECT e.id,e.kind,e.event_at,e.recorded_at,e.payload_json FROM memory_events e
                JOIN memory_turns t ON t.id=e.turn_id WHERE ''' + conditions + ' ORDER BY coalesce(e.event_at,e.recorded_at) DESC,e.id LIMIT ?', (*params, candidate_limit + 1)))
            candidates = [{'id': r['id'], 'version': 1, 'snippet': r['payload_json'][:350], 'source': r['kind'], 'event_at': r['event_at'], 'recorded_at': r['recorded_at']} for r in rows[:candidate_limit]]
            partial = len(rows) > candidate_limit
            # Explicit long-term memories and authoritative notes/preferences are separate evidence types.
            source_rows=list(db.execute("SELECT * FROM memory_source_revisions WHERE owner=? AND status='active' AND content_json LIKE ? ESCAPE '\\' ORDER BY recorded_at DESC LIMIT ?", (self.owner, '%' + escaped + '%', candidate_limit + 1)))
            partial |= len(source_rows)>candidate_limit
            for r in source_rows[:candidate_limit]:
                if begin is not None and (r['recorded_at'] is None or not begin <= r['recorded_at'] < finish):
                    continue
                if db.execute('SELECT 1 FROM memory_tombstones WHERE owner=? AND target_type=? AND target_id=?', (self.owner, 'exclude_' + r['kind'], r['id'])).fetchone():
                    continue
                identity = 'pref:' + r['id'] if r['kind'] == 'preference' else r['id']
                if r['kind'] == 'item':
                    item = db.execute('SELECT * FROM memory_items WHERE id=? AND owner=?', (identity, self.owner)).fetchone()
                    if not item or item['status'] != 'active' or (item['scope'] == 'session' and item['scope_id'] not in sessions) or (item['valid_until'] and item['valid_until'] <= self.store.clock()):
                        continue
                candidates.append({'id': identity, 'version': r['version'], 'snippet': r['content_json'][:350], 'source': r['kind'], 'event_at': None, 'recorded_at': r['recorded_at']})
            # Collect only already-authorized vector candidates; provider call is outside transaction.
            segments = [dict(r) for r in db.execute('''SELECT s.*,v.vector_blob,v.provider,v.model,v.dimension,v.vector_version FROM memory_segments s
                JOIN memory_embeddings v ON v.segment_id=s.id WHERE s.owner=? AND s.status='active' AND v.status='active' AND v.content_hash=s.content_hash
                AND (s.scope='owner' OR s.session_id IN (''' + session_sql + ')) ORDER BY s.recorded_at DESC LIMIT ?', (self.owner, *sessions, candidate_limit + 1))]
        provider = getattr(self.state, 'embedding_provider', None)
        semantic = bool(self.state.memory_settings.semantic_search and provider)
        semantic_hits = []
        degradation = 'embedding_not_configured' if not semantic else ''
        compatible=0
        if semantic:
            try:
                vector = validated_vector(provider.embed([query])[0], provider.dimension)
                for segment in segments[:candidate_limit]:
                    if (segment['provider'], segment['model'], segment['dimension'], segment['vector_version']) != (provider.provider, provider.model, provider.dimension, provider.version):
                        continue
                    compatible+=1
                    if begin is not None and (segment['event_at'] is None or not begin <= segment['event_at'] < finish):
                        continue
                    evidence = segment['source_id']
                    if segment['source_type'] == 'turn':
                        events = self.store.events(self.owner, evidence)
                        if not events:
                            continue
                        evidence = events[0]['id']
                    try:
                        item = self.read(evidence, scope, include_cleared)
                    except ValueError:
                        continue
                    values = validated_vector(json.loads(segment['vector_blob']), provider.dimension)
                    semantic_hits.append((sum(a * b for a, b in zip(vector, values)), {'id': evidence, 'version': item['version'], 'snippet': item['excerpt'][:350], 'source': item['source'], 'event_at': item['event_at'], 'recorded_at': item['recorded_at']}))
                partial |= len(segments) > candidate_limit
            except Exception:
                semantic, degradation = False, 'embedding_unavailable'
        fused = {}
        for route in (candidates, [hit for _, hit in sorted(semantic_hits, key=lambda item: item[0], reverse=True)]):
            seen = set()
            for rank, hit in enumerate(route, 1):
                if hit['id'] in seen:
                    continue
                seen.add(hit['id'])
                entry = fused.setdefault(hit['id'], {'hit': hit, 'score': 0.0})
                entry['score'] += 1 / (60 + rank)
        ordered = sorted(fused.values(), key=lambda item: (-item['score'], item['hit']['id']))
        # Re-read permissions and versions AFTER potentially slow provider work.
        hits = []
        for entry in ordered[cursor:cursor + limit + 1]:
            try:
                self.read(entry['hit']['id'], scope, include_cleared, entry['hit']['version'])
                hits.append(entry['hit'])
            except ValueError:
                pass
        pending=self.state.rows("SELECT count(*) AS n FROM memory_jobs WHERE owner=? AND kind IN ('index_source','index_turn') AND status IN ('pending','running')",(self.owner,))[0]['n']
        if semantic and (pending or not compatible):
            partial=True
            degradation='index_pending_or_model_changed'
        return {'ok': True, 'hits': hits[:limit], 'has_more': len(hits) > limit, 'next_cursor': cursor + min(len(hits), limit),
                'coverage': 'partial' if partial else 'retained_records', 'searched_range': searched,
                'semantic_available': semantic, 'degradation_reason': degradation,'index_pending_jobs':pending,
                'notice': '按 event_at 筛选事件；原始时间未知的旧轮次不伪造日期。证据只能说明当时记录。'}
