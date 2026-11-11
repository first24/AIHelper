"""Bounded, validated source changes exchanged with the isolated repair workflow."""
import base64
from pathlib import PurePosixPath


MAX_PATCH_BYTES=4000000


def safe_path(value):
    if not isinstance(value,str) or not value or '\\' in value or '\x00' in value:
        raise ValueError('修复产物的文件路径无效')
    path=PurePosixPath(value)
    if path.is_absolute() or any(part in ('','..','.') for part in value.split('/')):
        raise ValueError('修复产物包含越界路径')
    if path.parts[0] in ('.git','.github') or any(part=='.env' or part.startswith('.env.') for part in path.parts):
        raise ValueError('代码修复不能直接修改 Git 元数据、工作流或运行凭证')
    return path


def validate_changes(changes):
    if not isinstance(changes,list) or not 1<=len(changes)<=50:
        raise ValueError('修复产物须包含 1–50 个文件变更')
    seen=set()
    total=0
    checked=[]
    for item in changes:
        if not isinstance(item,dict):
            raise ValueError('修复产物结构无效')
        path=str(safe_path(item.get('path')))
        if path in seen:
            raise ValueError('修复产物包含重复路径')
        seen.add(path)
        deleted=item.get('deleted',False)
        if type(deleted) is not bool:
            raise ValueError('修复删除标记无效')
        mode=item.get('mode','100644')
        if mode not in ('100644','100755'):
            raise ValueError('修复产物仅支持普通代码文件')
        content=b''
        if not deleted:
            try:
                content=base64.b64decode(item.get('content',''),validate=True)
                content.decode('utf-8')
            except (ValueError,UnicodeError,TypeError):
                raise ValueError('修复产物须为有效 UTF-8 源文件') from None
            if len(content)>512000:
                raise ValueError('修复文件超过 512 KB')
        total+=len(content)
        if total>MAX_PATCH_BYTES:
            raise ValueError('修复产物总大小超过 4 MB')
        checked.append({'path':path,'deleted':deleted,'mode':mode,'bytes':content})
    return checked
