"""Authoritative memory storage and the common visibility/deletion boundary.

All methods accepting db participate in the caller's transaction. Network work
belongs in memory_jobs, never in this module or a SQLite transaction.
"""
import json
import os
from pathlib import Path
import time

from .memory_types import MemoryConflict, encoded, scrub, uid


class MemoryStore:
    def __init__(self, state, clock=time.time):
        self.state, self.clock = state, clock

    def owner(self, db, owner):
        db.execute('INSERT OR IGNORE INTO memory_owner_state(owner) VALUES(?)', (owner,))
        return dict(db.execute('SELECT * FROM memory_owner_state WHERE owner=?', (owner,)).fetchone())

    def session(self, db, owner, chat):
        self.owner(db, owner)
        row = db.execute('SELECT * FROM memory_sessions WHERE owner=? AND chat=? ORDER BY epoch DESC LIMIT 1', (owner, chat)).fetchone()
        if row and row['closed_at'] is None:
            return dict(row)
        epoch = row['epoch'] + 1 if row else 0
        identity = uid('ms_', owner, chat, epoch)
        db.execute('INSERT INTO memory_sessions(id,owner,chat,epoch,created_at) VALUES(?,?,?,?,?)', (identity, owner, chat, epoch, self.clock()))
        return dict(db.execute('SELECT * FROM memory_sessions WHERE id=?', (identity,)).fetchone())

    def current_session(self, owner, chat):
        with self.state.transaction() as db:
            return self.session(db, owner, chat)

    def begin(self, db, message_id, owner, chat, text, received_at, completeness='full', source='message'):
        session = self.session(db, owner, chat)
        turn = uid('mt_', session['id'], source, message_id)
        exists = db.execute('SELECT id FROM memory_turns WHERE id=?', (turn,)).fetchone()
        if exists:
            return turn
        db.execute('INSERT INTO memory_turns(id,session_id,input_message_id,received_at,trace_completeness) VALUES(?,?,?,?,?)',
                   (turn, session['id'], message_id, received_at, completeness))
        self.append(db, turn, 'input', 'user', 'control' if text == '/清空对话' else 'user',
                    {'role': 'user', 'content': text}, event_at=received_at, source=source)
        return turn

    def turn(self, owner, chat, message_id):
        rows = self.state.rows('''SELECT t.*,s.owner,s.chat FROM memory_turns t JOIN memory_sessions s ON s.id=t.session_id
            WHERE s.owner=? AND s.chat=? AND t.input_message_id=? ORDER BY s.epoch DESC LIMIT 1''', (owner, chat, message_id))
        return rows[0] if rows else None

    def rebind_queued(self, db, message_id, owner, chat):
        """Messages queued after a reset command follow the new processing epoch.

        They have only an input event at this point; never move executed tools.
        """
        session=self.session(db,owner,chat)
        turn=db.execute('SELECT t.* FROM memory_turns t JOIN memory_sessions s ON s.id=t.session_id WHERE t.input_message_id=? AND s.owner=? AND s.chat=? ORDER BY s.epoch DESC LIMIT 1',(message_id,owner,chat)).fetchone()
        if not turn or turn['session_id']==session['id']:
            return
        events=list(db.execute('SELECT * FROM memory_events WHERE turn_id=?',(turn['id'],)))
        if len(events)!=1 or events[0]['kind'] not in ('user','control'):
            return
        seq=session['last_seq']+1
        db.execute('UPDATE memory_turns SET session_id=?,first_seq=?,last_seq=? WHERE id=?',(session['id'],seq,seq,turn['id']))
        db.execute('UPDATE memory_events SET session_id=?,seq=? WHERE turn_id=?',(session['id'],seq,turn['id']))
        db.execute('UPDATE memory_sessions SET last_seq=?,revision=revision+1 WHERE id=?',(seq,session['id']))

    def append(self, db, turn, key, role, kind, payload, call_id='', event_at=None, source='event'):
        event_key = turn + ':' + key
        identity = uid('ev_', event_key)
        if db.execute('SELECT 1 FROM memory_events WHERE event_key=?', (event_key,)).fetchone():
            return identity
        row = db.execute('''SELECT t.session_id,t.visible,s.owner,s.last_seq FROM memory_turns t
            JOIN memory_sessions s ON s.id=t.session_id WHERE t.id=?''', (turn,)).fetchone()
        if not row:
            raise ValueError('归档轮次不存在')
        generation = self.owner(db, row['owner'])['generation']
        seq = row['last_seq'] + 1
        now = self.clock()
        clean = scrub(payload) if row['visible'] else {'redacted': True}
        raw = encoded(clean)
        parts = []
        if len(raw.encode()) > 256 * 1024:
            parts = [raw[i:i + 16000] for i in range(0, len(raw), 16000)]
            raw = encoded({'full_payload_available': True, 'payload_parts': len(parts), 'full_read_handle': identity})
        db.execute('''INSERT INTO memory_events(id,owner,session_id,turn_id,seq,event_key,role,kind,payload_json,
            tool_call_id,recorded_at,event_at,source_type,source_id,trust_level,visibility,generation)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                   (identity, row['owner'], row['session_id'], turn, seq, event_key, role, kind, raw,
                    call_id, now, event_at, source, identity, 'data', 'visible' if row['visible'] else 'excluded', generation))
        for index, part in enumerate(parts):
            db.execute('INSERT INTO memory_payloads VALUES(?,?,?,?)', (identity, index, part, now))
        db.execute('UPDATE memory_sessions SET last_seq=?,revision=revision+1 WHERE id=?', (seq, row['session_id']))
        db.execute('UPDATE memory_turns SET first_seq=coalesce(first_seq,?),last_seq=? WHERE id=?', (seq, seq, turn))
        return identity

    def record(self, owner, chat, message_id, key, role, kind, payload, call_id=''):
        turn = self.turn(owner, chat, message_id)
        if not turn or not self.state.memory_settings.archive_write:
            return None
        with self.state.transaction() as db:
            return self.append(db, turn['id'], key, role, kind, payload, call_id,event_at=self.clock())

    def finish(self, db, turn, reply, status='completed'):
        trace=db.execute('SELECT trace_completeness FROM memory_turns WHERE id=?',(turn,)).fetchone()
        self.append(db, turn, 'final', 'assistant', 'final', {'role': 'assistant', 'content': reply},event_at=self.clock() if trace['trace_completeness']=='full' else None)
        db.execute('UPDATE memory_turns SET status=?,completed_at=? WHERE id=?', (status, self.clock(), turn))
        row = db.execute('SELECT t.*,s.owner,s.revision FROM memory_turns t JOIN memory_sessions s ON s.id=t.session_id WHERE t.id=?', (turn,)).fetchone()
        self.enqueue_job(db, row['owner'], 'index_turn', turn, row['last_seq'])
        if self.state.memory_settings.summary:
            self.enqueue_job(db, row['owner'], 'summary', row['session_id'], row['revision'])

    def enqueue_job(self, db, owner, kind, source_id, version):
        generation = self.owner(db, owner)['generation']
        identity = uid('mj_', owner, kind, source_id, version, generation)
        db.execute('INSERT OR IGNORE INTO memory_jobs(id,owner,kind,source_id,source_version,generation) VALUES(?,?,?,?,?,?)',
                   (identity, owner, kind, source_id, version, generation))

    def events(self, owner, turn_id, db=None):
        query = '''SELECT e.* FROM memory_events e JOIN memory_turns t ON t.id=e.turn_id
            WHERE e.owner=? AND e.turn_id=? AND e.visibility='visible' AND t.visible=1 ORDER BY e.seq'''
        if db is not None:
            rows = [dict(r) for r in db.execute(query, (owner, turn_id))]
        else:
            rows = self.state.rows(query, (owner, turn_id))
        for row in rows:
            row['payload'] = json.loads(row.pop('payload_json'))
            # Large payloads are deliberately read through the paged evidence API.
        return rows

    def reset(self, owner, chat):
        with self.state.transaction() as db:
            current = self.session(db, owner, chat)
            db.execute('UPDATE memory_sessions SET auto_recall_enabled=0,closed_at=? WHERE owner=? AND chat=?', (self.clock(), owner, chat))
            next_epoch = current['epoch'] + 1
            identity = uid('ms_', owner, chat, next_epoch)
            db.execute('INSERT INTO memory_sessions(id,owner,chat,epoch,created_at) VALUES(?,?,?,?,?)', (identity, owner, chat, next_epoch, self.clock()))
            db.execute('DELETE FROM turns WHERE owner=? AND chat=?', (owner, chat))
        return identity

    def source(self, db, owner, kind, identity, content, recorded_at=None):
        """Version a domain source without creating a second editable authority."""
        previous = db.execute('SELECT * FROM memory_source_revisions WHERE owner=? AND kind=? AND id=? ORDER BY version DESC LIMIT 1', (owner, kind, identity)).fetchone()
        raw = encoded(scrub(content))
        if previous and previous['content_json'] == raw and previous['status'] == 'active':
            return previous['version']
        version = previous['version'] + 1 if previous else 1
        db.execute("UPDATE memory_source_revisions SET status='superseded' WHERE owner=? AND kind=? AND id=? AND status='active'", (owner, kind, identity))
        db.execute('INSERT INTO memory_source_revisions VALUES(?,?,?,?,?,?,?)', (owner, kind, identity, version, raw, 'active', recorded_at))
        # Superseded note/preference copies cannot be automatically presented as current.
        if previous:
            self.invalidate_source(db, owner, kind, identity, previous['version'], erase=False)
        self.enqueue_job(db, owner, 'index_source', kind + ':' + identity, version)
        return version

    def link(self, db, owner, derived_kind, derived_id, source_kind, source_id, source_version=1, session=None, seq=None):
        db.execute('INSERT OR IGNORE INTO memory_sources VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (derived_kind, derived_id, source_kind, source_id, source_version, owner, session, seq, 'read', self.clock()))

    def tool_event(self, owner, chat, message_id, key, name, args, result=None, call_id='', error=False):
        turn = self.turn(owner, chat, message_id)
        if not turn or not self.state.memory_settings.archive_write:
            return None
        with self.state.transaction() as db:
            started = result is None
            outcome = 'unknown' if isinstance(result, dict) and result.get('status') in ('unknown', 'sending') else ('failed' if error or isinstance(result, dict) and result.get('status') == 'failed' else 'succeeded')
            payload = {'name': name, 'arguments': args, 'status': 'started' if started else outcome}
            if not started:
                payload['result'] = result
            identity = self.append(db, turn['id'], key, 'metadata' if started or not call_id else 'tool',
                                   'tool_start' if started else ('tool_result' if call_id else 'command_result'), payload, call_id,event_at=self.clock())
            # Reference domain objects used by a tool, including query results and writes.
            refs = set()
            def collect(value):
                if isinstance(value, dict):
                    for k, v in value.items():
                        if k == 'id' and isinstance(v, str): refs.add(v)
                        collect(v)
                elif isinstance(value, list):
                    for item in value: collect(item)
            collect(args)
            collect(result)
            if name in ('remember_preference','get_preference_history'):
                refs.add('preference:' + args['key'])
            for ref in refs:
                if ref.startswith('ev_'):
                    source_event = db.execute("SELECT * FROM memory_events WHERE id=? AND owner=? AND visibility='visible'", (ref,owner)).fetchone()
                    if source_event:
                        self.link(db,owner,'event',identity,'event',ref,1,source_event['session_id'],source_event['seq'])
                    continue
                kind = 'preference' if ref.startswith(('preference:', 'pref:')) else ('note' if ref.startswith('n_') else 'item')
                source_id = ref.split(':', 1)[1] if kind == 'preference' else ref
                source = db.execute('SELECT version FROM memory_source_revisions WHERE owner=? AND kind=? AND id=? AND status=?', (owner, kind, source_id, 'active')).fetchone()
                if source:
                    self.link(db, owner, 'event', identity, kind, source_id, source['version'], turn['session_id'])
                    if db.execute('SELECT 1 FROM memory_tombstones WHERE owner=? AND target_type=? AND target_id=?',(owner,'exclude_'+kind,source_id)).fetchone():
                        # Explicit domain reads still work, but must not turn a
                        # previously excluded note back into automatic memory.
                        self._invalidate_turns(db,owner,[turn['id']],False)
            return identity

    def import_legacy(self):
        with self.state.transaction() as db:
            if db.execute("SELECT 1 FROM memory_imports WHERE import_key='legacy-v1-complete'").fetchone():
                return
            for row in list(db.execute('SELECT * FROM messages_v2 ORDER BY created,id')):
                turn = self.begin(db, row['id'], row['owner'], row['chat'], row['text'], row['created'],
                                  'legacy_final_only' if row['status'] == 'ready' else 'partial')
                if row['status'] == 'ready':
                    self.finish(db, turn, row['reply'])
                db.execute('INSERT OR IGNORE INTO memory_imports VALUES(?,?)', ('message:' + row['id'], turn))
            for row in list(db.execute('SELECT * FROM turns ORDER BY id')):
                key = 'legacy-turn:' + str(row['id'])
                turn = self.begin(db, key, row['owner'], row['chat'], '[旧轮次；原始发生时间未知，未与消息强行关联]', None, 'partial', 'legacy_turn')
                try:
                    body = json.loads(row['body'])
                except (ValueError, TypeError):
                    body = {'unreadable_legacy': True}
                self.append(db, turn, 'legacy_body', 'metadata', 'legacy_turn', {'messages': body}, source='legacy_turn')
                self.finish(db, turn, '[旧轮次存档]')
                db.execute('INSERT OR IGNORE INTO memory_imports VALUES(?,?)', (key, turn))
            for row in list(db.execute('SELECT * FROM notes')):
                self.source(db, row['owner'], 'note', row['id'], dict(row), row['updated'])
            for row in list(db.execute('SELECT * FROM preferences')):
                self.source(db, row['owner'], 'preference', row['key'], {'key': row['key'], 'value': row['value'], 'origin': 'existing_preference', 'original_time': 'unknown'})
            db.execute("INSERT INTO memory_imports VALUES('legacy-v1-complete','complete')")

    def _invalidate_turns(self, db, owner, turn_ids, erase):
        pending = list(set(turn_ids))
        visited = set()
        while pending:
            turn = pending.pop()
            if turn in visited:
                continue
            visited.add(turn)
            row = db.execute('SELECT t.*,s.owner FROM memory_turns t JOIN memory_sessions s ON s.id=t.session_id WHERE t.id=? AND s.owner=?', (turn, owner)).fetchone()
            if not row:
                continue
            db.execute('UPDATE memory_turns SET visible=0 WHERE id=?', (turn,))
            db.execute("UPDATE memory_events SET visibility='excluded' WHERE turn_id=?", (turn,))
            db.execute("UPDATE memory_segments SET status='invalid',text='' WHERE owner=? AND source_id=? AND source_type='turn'", (owner, turn))
            db.execute("UPDATE memory_segments SET status='invalid',text='' WHERE owner=? AND source_type='event' AND source_id IN (SELECT id FROM memory_events WHERE turn_id=?)", (owner,turn))
            dependencies = db.execute("""SELECT DISTINCT e.turn_id FROM memory_sources s JOIN memory_events e ON e.id=s.derived_id
                WHERE s.derived_kind='event' AND s.source_kind='event' AND s.source_owner=?
                AND s.source_id IN (SELECT id FROM memory_events WHERE turn_id=?)""", (owner,turn))
            pending.extend(r['turn_id'] for r in dependencies)
            # Any summary that READ the range is invalid, regardless of model citations.
            db.execute("UPDATE memory_summaries SET status='invalid',content_json='{}' WHERE session_id=? AND from_seq<=? AND upto_seq>=?", (row['session_id'], row['last_seq'], row['first_seq']))
            db.execute('UPDATE memory_sessions SET revision=revision+1 WHERE id=?', (row['session_id'],))
            if erase:
                db.execute("UPDATE memory_events SET payload_json='{}',visibility='deleted' WHERE turn_id=?", (turn,))
                db.execute('DELETE FROM memory_payloads WHERE event_id IN (SELECT id FROM memory_events WHERE turn_id=?)', (turn,))
                db.execute("UPDATE messages_v2 SET text='',reply=CASE WHEN delivered=1 THEN '' ELSE '该记录已按要求停止使用，原回复不再投递。' END WHERE id=? AND owner=?", (row['input_message_id'], owner))
                db.execute("UPDATE effects SET result=? WHERE id IN (SELECT effect_id FROM memory_effects WHERE owner=? AND input_message_id=?)",
                           (encoded({'ok': True, 'receipt': '此操作已处理；正文已清理，不重复执行。', 'redacted': True}), owner, row['input_message_id']))
            # Legacy cache has no reliable per-message mapping. Clear conservatively.
            db.execute('DELETE FROM turns WHERE owner=? AND chat IN (SELECT chat FROM memory_sessions WHERE id=?)', (owner, row['session_id']))
        db.execute("DELETE FROM memory_embeddings WHERE segment_id IN (SELECT id FROM memory_segments WHERE owner=? AND status!='active')", (owner,))

    def invalidate_source(self, db, owner, kind, identity, upto_version=None, erase=True):
        version_sql = ' AND source_version<=?' if upto_version is not None else ''
        args = (owner, kind, identity) + ((upto_version,) if upto_version is not None else ())
        events = list(db.execute('SELECT derived_id FROM memory_sources WHERE source_owner=? AND source_kind=? AND source_id=? AND derived_kind=\'event\'' + version_sql, args))
        turns = [r['turn_id'] for event in events for r in db.execute('SELECT turn_id FROM memory_events WHERE id=? AND owner=?', (event['derived_id'], owner))]
        self._invalidate_turns(db, owner, turns, erase)
        segment_identity='pref:'+identity if kind=='preference' else identity
        db.execute("UPDATE memory_segments SET status='invalid',text='' WHERE owner=? AND source_type=? AND source_id=?", (owner, kind, segment_identity))
        db.execute("DELETE FROM memory_embeddings WHERE segment_id IN (SELECT id FROM memory_segments WHERE owner=? AND status!='active')", (owner,))
        if erase:
            db.execute('UPDATE effects SET result=? WHERE id IN (SELECT derived_id FROM memory_sources WHERE source_owner=? AND source_kind=? AND source_id=? AND derived_kind=?)',
                       (encoded({'ok':True,'redacted':True,'receipt':'该操作已处理，内容已清理，不会重做。'}),owner,kind,identity,'effect'))
            db.execute("UPDATE memory_source_revisions SET status='deleted',content_json='{}' WHERE owner=? AND kind=? AND id=?", (owner, kind, identity))
        # Old traces cannot be safely joined to their input/tool sources. Exclude
        # uncertain legacy copies rather than inventing links or deleting other business data.
        legacy = [r['id'] for r in db.execute("SELECT t.id FROM memory_turns t JOIN memory_sessions s ON s.id=t.session_id WHERE s.owner=? AND t.trace_completeness!='full' AND t.visible=1", (owner,))]
        self._invalidate_turns(db,owner,legacy,False)

    def tombstone(self, db, owner, kind, identity, reason='user_request'):
        self.owner(db, owner)
        db.execute('UPDATE memory_owner_state SET generation=generation+1,memory_version=memory_version+1 WHERE owner=?', (owner,))
        generation = self.owner(db, owner)['generation']
        db.execute('INSERT OR REPLACE INTO memory_tombstones VALUES(?,?,?,?,?,?)', (owner, kind, identity, generation, self.clock(), reason))
        # Write-ahead exclusion log is deliberately OUTSIDE the database backup.
        # If the DB transaction later fails, conservative exclusion on restore is
        # safer than reviving deleted text. Never contains the deleted body.
        if self.state.path != ':memory:':
            path=Path(self.state.path).parent/'memory-deletions.jsonl'
            entry={'owner':owner,'target_type':kind,'target_id':identity,'generation':generation,'deleted_at':self.clock(),'reason_code':reason}
            with open(path,'a',encoding='utf-8') as stream:
                stream.write(encoded(entry)+'\n')
                stream.flush()
                os.fsync(stream.fileno())
        return generation

    def apply_deletion_ledger(self, path=None):
        if path is None and self.state.path == ':memory:':
            return 0
        path=Path(path) if path else Path(self.state.path).parent/'memory-deletions.jsonl'
        if not path.exists():
            return 0
        # A corrupt ledger fails closed, before the State object is returned.
        entries=[]
        with open(path,encoding='utf-8') as stream:
            for line in stream:
                entry=json.loads(line)
                if set(entry)!={'owner','target_type','target_id','generation','deleted_at','reason_code'} or type(entry['generation']) is not int:
                    raise ValueError('删除清单损坏；修复前不能启用记忆读取')
                entries.append(entry)
        applied=0
        with self.state.transaction() as db:
            for entry in entries:
                owner,kind,identity=entry['owner'],entry['target_type'],entry['target_id']
                old=db.execute('SELECT generation FROM memory_tombstones WHERE owner=? AND target_type=? AND target_id=?',(owner,kind,identity)).fetchone()
                if old and old['generation']>=entry['generation']:
                    continue
                self.owner(db,owner)
                db.execute('UPDATE memory_owner_state SET generation=max(generation,?),memory_version=memory_version+1 WHERE owner=?',(entry['generation'],owner))
                db.execute('INSERT OR REPLACE INTO memory_tombstones VALUES(?,?,?,?,?,?)',(owner,kind,identity,entry['generation'],entry['deleted_at'],entry['reason_code']))
                if kind in ('note','preference','item','exclude_note'):
                    source_kind='note' if kind=='exclude_note' else kind
                    if kind=='note': db.execute('DELETE FROM notes WHERE owner=? AND id=?',(owner,identity))
                    if kind=='preference': db.execute('DELETE FROM preferences WHERE owner=? AND key=?',(owner,identity))
                    if kind=='item': db.execute("UPDATE memory_items SET status='deleted',content_json='{}' WHERE owner=? AND id=?",(owner,identity))
                    self.invalidate_source(db,owner,source_kind,identity,erase=kind!='exclude_note')
                elif kind=='session':
                    turns=[r['id'] for r in db.execute('SELECT t.id FROM memory_turns t JOIN memory_sessions s ON s.id=t.session_id WHERE s.id=? AND s.owner=?',(identity,owner))]
                    self._invalidate_turns(db,owner,turns,True)
                    db.execute('UPDATE memory_sessions SET auto_recall_enabled=0,closed_at=coalesce(closed_at,?) WHERE id=? AND owner=?',(self.clock(),identity,owner))
                elif kind=='turn':
                    self._invalidate_turns(db,owner,[identity],True)
                else:
                    raise ValueError('删除清单包含未知目标，停止读取')
                applied+=1
        return applied

    def save_item(self, owner, chat, message_id, logical_key, text, kind='fact', scope='owner', expected_version=None, identity=None, valid_until=None):
        if kind not in ('fact', 'decision') or scope not in ('owner', 'session') or not text.strip() or len(text) > 4000 or len(logical_key) > 120:
            raise ValueError('记忆类型、范围或长度不正确')
        with self.state.transaction() as db:
            session = self.session(db, owner, chat)
            scope_id = owner if scope == 'owner' else session['id']
            active = db.execute("SELECT * FROM memory_items WHERE owner=? AND scope=? AND scope_id=? AND logical_key=? AND status='active'", (owner, scope, scope_id, logical_key)).fetchone()
            if identity and (not active or active['id'] != identity):
                raise MemoryConflict('记忆已变化，请重新查询当前版本')
            if active and (expected_version is None or expected_version != active['version']):
                raise MemoryConflict('记忆版本冲突，请重新查询；当前版本 ' + str(active['version']))
            if not active and expected_version not in (None, 0):
                raise MemoryConflict('记忆已删除或不存在')
            if valid_until is not None and valid_until <= self.clock():
                raise ValueError('记忆有效期必须在未来')
            if active:
                db.execute("UPDATE memory_items SET status='superseded' WHERE id=?", (active['id'],))
                self.invalidate_source(db, owner, 'item', active['id'], erase=False)
            maximum = db.execute('SELECT coalesce(max(version),0) FROM memory_items WHERE owner=? AND scope=? AND scope_id=? AND logical_key=?', (owner, scope, scope_id, logical_key)).fetchone()[0]
            version = maximum + 1
            identity = uid('mi_', owner, scope_id, logical_key, version)
            generation = self.owner(db, owner)['generation']
            content = encoded(scrub({'text': text}))
            db.execute('INSERT INTO memory_items VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (identity, logical_key, version, owner, scope, scope_id, kind, 'user_explicit', content, 'active', self.clock(), valid_until, self.clock(), active['id'] if active else None, generation))
            self.source(db, owner, 'item', identity, {'text': text, 'kind': kind}, self.clock())
            source = db.execute('''SELECT e.id,e.session_id,e.seq FROM memory_events e JOIN memory_turns t ON t.id=e.turn_id
                WHERE e.owner=? AND t.input_message_id=? AND e.kind='user' ORDER BY e.recorded_at DESC LIMIT 1''', (owner, message_id)).fetchone()
            if source:
                self.link(db, owner, 'item', identity, 'event', source['id'], 1, source['session_id'], source['seq'])
                self.link(db, owner, 'event', source['id'], 'item', identity, 1, source['session_id'], source['seq'])
            db.execute('UPDATE memory_owner_state SET memory_version=memory_version+1 WHERE owner=?', (owner,))
        return {'ok': True, 'id': identity, 'version': version, 'receipt': f'已保存记忆 {identity}｜版本 {version}'}

    def get_item(self, owner, chat, identity):
        session = self.current_session(owner, chat)
        rows = self.state.rows('''SELECT * FROM memory_items WHERE id=? AND owner=? AND status NOT IN ('deleted','candidate')
            AND (scope='owner' OR scope_id=?)''', (identity, owner, session['id']))
        if not rows:
            raise ValueError('未找到可访问的记忆')
        row = rows[0]
        row['content'] = json.loads(row.pop('content_json'))
        row['sources'] = self.state.rows("SELECT source_kind,source_id,source_version,source_session_id FROM memory_sources WHERE derived_kind='item' AND derived_id=? AND source_owner=?", (identity, owner))
        if row['valid_until'] is not None and row['valid_until'] <= self.clock() and row['status'] == 'active':
            row['status'] = 'expired'
        return row

    def list_items(self, owner, chat, query='', include_history=False):
        session = self.current_session(owner, chat)
        pattern = '%' + query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        rows = self.state.rows('''SELECT id FROM memory_items WHERE owner=? AND (scope='owner' OR scope_id=?)
            AND status IN (''' + ("'active','superseded','expired'" if include_history else "'active'") + ''')
            AND content_json LIKE ? ESCAPE '\\' ''' + ('' if include_history else ' AND (valid_until IS NULL OR valid_until>?)') +
            ' ORDER BY recorded_at DESC,id LIMIT 20', (owner, session['id'], pattern) + (() if include_history else (self.clock(),)))
        return [self.get_item(owner, chat, row['id']) for row in rows]

    def forget_item(self, owner, chat, identity):
        item = self.get_item(owner, chat, identity)
        with self.state.transaction() as db:
            ids = [r['id'] for r in db.execute('SELECT id FROM memory_items WHERE owner=? AND scope=? AND scope_id=? AND logical_key=?', (owner, item['scope'], item['scope_id'], item['logical_key']))]
            for target in ids:
                self.tombstone(db, owner, 'item', target)
                self.invalidate_source(db, owner, 'item', target)
                db.execute("UPDATE memory_items SET status='deleted',content_json='{}' WHERE id=? AND owner=?", (target, owner))
        return {'ok': True, 'memory_reset': True, 'receipt': '该记忆及历史版本已停止使用，应用内相关记忆正文已清理。业务对象、本机 Codex 产物和备份不在此次删除范围。'}

    def delete_session(self, owner, chat, session_id, expected_revision, request_message_id=None):
        with self.state.transaction() as db:
            row = db.execute('SELECT * FROM memory_sessions WHERE id=? AND owner=? AND chat=?', (session_id, owner, chat)).fetchone()
            offset = 0
            if row and request_message_id:
                last = db.execute('SELECT t.input_message_id,e.kind FROM memory_events e JOIN memory_turns t ON t.id=e.turn_id WHERE e.session_id=? ORDER BY e.seq DESC LIMIT 1',(session_id,)).fetchone()
                offset = int(bool(last and last['input_message_id']==request_message_id and last['kind']=='user'))
            if not row or row['revision'] - offset != expected_revision:
                raise MemoryConflict('会话不存在或范围已变化，请重新预览')
            self.tombstone(db, owner, 'session', session_id)
            db.execute('UPDATE memory_sessions SET auto_recall_enabled=0,closed_at=? WHERE id=?', (self.clock(),session_id))
            turns = [r['id'] for r in db.execute('SELECT id FROM memory_turns WHERE session_id=?', (session_id,))]
            self._invalidate_turns(db, owner, turns, True)
        return {'ok': True, 'memory_reset': True, 'receipt': '指定周期的应用内会话正文已清理；业务数据、长期记忆、本机产物和备份保留。'}

    def stats(self):
        result = {}
        for name in ('events', 'items', 'summaries', 'segments', 'embeddings', 'tombstones'):
            result[name] = self.state.rows('SELECT count(*) AS count FROM memory_' + name)[0]['count']
        result['jobs'] = self.state.rows('SELECT status,count(*) AS count FROM memory_jobs GROUP BY status')
        result['semantic_available'] = bool(self.state.memory_settings.semantic_search and getattr(self.state, 'embedding_provider', None))
        result['count_quality'] = 'estimated'
        result['stored_text_bytes']={}
        for table,column in (('events','payload_json'),('payloads','text'),('items','content_json'),('source_revisions','content_json'),('summaries','content_json'),('segments','text'),('embeddings','vector_blob')):
            result['stored_text_bytes'][table]=self.state.rows('SELECT coalesce(sum(length(CAST('+column+' AS BLOB))),0) AS n FROM memory_'+table)[0]['n']
        return result
