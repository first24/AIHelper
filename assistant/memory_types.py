"""Small, provider-independent contracts for memory. No credentials or model imports."""
from dataclasses import dataclass
import hashlib
import json
import os
import re


def uid(prefix, *parts):
    return prefix + hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()[:24]


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def scrub(value):
    """Best effort only; not a promise to detect every secret supplied by a user."""
    if isinstance(value, dict):
        return {k: ('[redacted]' if re.search(r'(?i)(password|api_key|authorization|bot_token|access_token|refresh_token|private_key)', k)
                    else scrub(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, str):
        value = re.sub(r'(?s)-----BEGIN[^\n]*PRIVATE KEY-----.*?-----END[^\n]*PRIVATE KEY-----', '[redacted private key]', value)
        return re.sub(r'(?i)\b(?:sk-[\w-]{16,}|gh[pousr]_[\w]{12,}|bearer\s+[\w.\-]{12,})', '[redacted]', value)
    return value


@dataclass(frozen=True)
class MemorySettings:
    archive_write: bool = True
    context_v2: bool = True
    summary: bool = False
    history_search: bool = True
    semantic_search: bool = False
    long_term_memory: bool = True
    app_token_limit: int = 64000
    context_limit: int = 0  # Unconfirmed provider capacity. Zero never means unlimited.
    max_output: int = 2500
    safety_margin: int = 1600
    retention_days: int = 90
    payload_days: int = 7
    scan_limit: int = 1000
    tokenizer_id: str = 'utf8-upper-estimate'
    counter_version: str = '1'
    model_id: str = ''

    @classmethod
    def from_env(cls):
        values = {}
        for name, field in cls.__dataclass_fields__.items():
            raw = os.getenv('MEMORY_' + name.upper())
            if raw is None:
                continue
            if type(field.default) is bool:
                if raw.lower() not in ('true', 'false'):
                    raise ValueError('MEMORY_' + name.upper() + ' 必须为 true/false')
                values[name] = raw.lower() == 'true'
            elif type(field.default) is int:
                values[name] = int(raw)
            else:
                values[name] = raw
        result = cls(**values)
        if min(result.app_token_limit, result.max_output, result.safety_margin, result.retention_days, result.payload_days, result.scan_limit) <= 0 or result.context_limit < 0:
            raise ValueError('记忆容量、保留天数和扫描上限必须为正数')
        return result


class MemoryConflict(ValueError):
    pass


class BudgetExceeded(RuntimeError):
    pass


class ProviderContextLimit(RuntimeError):
    pass


def valid_protocol(messages):
    """Validate complete parallel call groups, not merely alternating roles."""
    pending = set()
    for message in messages:
        if not isinstance(message, dict) or message.get('role') not in ('user', 'assistant', 'tool'):
            return False
        if message['role'] == 'tool':
            call_id = message.get('tool_call_id')
            if call_id not in pending:
                return False
            pending.remove(call_id)
        else:
            if pending:
                return False
            calls = message.get('tool_calls', [])
            if calls:
                if message['role'] != 'assistant' or not isinstance(calls, list):
                    return False
                ids = [c.get('id') for c in calls if isinstance(c, dict)]
                if len(ids) != len(calls) or any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
                    return False
                if any(not isinstance(c.get('function'),dict) or not isinstance(c['function'].get('name'),str) or not c['function']['name']
                       or not isinstance(c['function'].get('arguments'),str) for c in calls):
                    return False
                pending.update(ids)
    return not pending


SUMMARY_FIELDS = ('goals', 'constraints', 'confirmed_decisions', 'entities', 'observed_actions', 'pending', 'uncertainties')


def validate_summary(value, events):
    allowed = {e['id'] for e in events}
    if not isinstance(value, dict) or set(value) != {'schema_version', 'source_refs', *SUMMARY_FIELDS} or value['schema_version'] != 1:
        raise ValueError('摘要结构不正确')
    if not isinstance(value['source_refs'], list) or not set(value['source_refs']) <= allowed:
        raise ValueError('摘要引用不正确')
    by_id={e['id']:e for e in events}
    for key in SUMMARY_FIELDS:
        if not isinstance(value[key], list) or len(value[key]) > 30:
            raise ValueError('摘要条目过多或类型错误')
        for item in value[key]:
            if not isinstance(item, dict) or not isinstance(item.get('text'), str) or len(item['text']) > 1200:
                raise ValueError('摘要条目无效')
            refs = item.get('source_refs')
            if not isinstance(refs, list) or not refs or not set(refs) <= allowed:
                raise ValueError('摘要条目缺少真实来源')
            if key == 'observed_actions' and item.get('status') not in ('succeeded', 'failed', 'unknown'):
                raise ValueError('摘要动作缺少结果状态')
            if key=='observed_actions':
                observed={by_id[ref]['payload'].get('status') for ref in refs if by_id[ref]['kind'] in ('tool_result','command_result','tool_start','interrupted')}
                if item['status']=='unknown':
                    if not observed:
                        raise ValueError('摘要动作缺少执行来源')
                elif item['status'] not in observed:
                    raise ValueError('摘要动作状态与执行证据不符')
            referenced_text=encoded([by_id[ref]['payload'] for ref in refs])
            for identifier in re.findall(r'\b(?:r_|t_|e_|m_|n_|cx_|mi_)[A-Za-z0-9_-]+', item['text']):
                if identifier not in referenced_text:
                    raise ValueError('摘要包含来源中不存在的编号')
            for number in re.findall(r'\d+(?:\.\d+)?',item['text']):
                if number not in referenced_text:
                    raise ValueError('摘要数字或时间不在引用来源中')
    if len(encoded(value).encode()) > 24000:
        raise ValueError('摘要过大')
    return value
