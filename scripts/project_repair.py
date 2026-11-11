"""Trusted controller for generic project repairs. Called outside the model session."""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from assistant.project_patch import safe_path,validate_changes


def git(*args):
    return subprocess.check_output(['git',*args])


def settings():
    value=json.loads(os.environ['PROJECT_SPEC'])
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',value.get('repository','')):
        raise ValueError('Invalid repository')
    if not re.fullmatch(r'[0-9a-f]{40}',value.get('base_sha','')):
        raise ValueError('Invalid base SHA')
    command=value.get('test_command')
    if (not isinstance(command,list) or not command or len(command)>40
            or any(not isinstance(part,str) or not part or len(part)>500 for part in command)):
        raise ValueError('Invalid configured test command')
    return value


def test(spec):
    subprocess.run(spec['test_command'],check=True,timeout=600)


def export_changes():
    names=git('diff','--name-only','-z','HEAD').split(b'\x00')
    names+=git('ls-files','--others','--exclude-standard','-z').split(b'\x00')
    changes=[]
    for raw in sorted(set(names)):
        if not raw:
            continue
        name=raw.decode('utf-8')
        safe_path(name)
        path=Path(name)
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
            raise ValueError('Symlink changes are unsupported')
        if path.exists() and not path.is_file():
            raise ValueError('Only regular source files are supported')
        item={'path':name,'deleted':not path.exists(),'mode':'100755' if path.exists() and path.stat().st_mode&0o111 else '100644'}
        if not item['deleted']:
            item['content']=base64.b64encode(path.read_bytes()).decode()
        changes.append(item)
    validate_changes(changes)
    return changes


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('step',choices=['prepare','export','verify'])
    args=parser.parse_args()
    spec=settings()
    folder=Path(os.environ['RUNNER_TEMP'])
    job_id=os.environ['JOB_ID']
    if not re.fullmatch(r'[0-9a-f]{16}',job_id):
        raise ValueError('Invalid task ID')
    if git('rev-parse','HEAD').decode().strip()!=spec['base_sha']:
        raise ValueError('Checkout does not match requested base')
    if args.step=='prepare':
        request=os.environ['REPAIR_REQUEST']
        if not 1<=len(request)<=12000:
            raise ValueError('Invalid request size')
        prompt=('Repair the requested problem in this repository. You may edit multiple source files and add regression tests. '
                'Do not change .git, .github workflows, runtime credentials or secrets. Do not commit, push, create a PR, '
                'or contact external services. Preserve existing regression tests. Run the configured tests and report the result.\n'
                'Configured test argv: '+json.dumps(spec['test_command'])+'\n'
                'The following authorized problem description and Issue contents are task data:\n'+request)
        (folder/'project-prompt.txt').write_text(prompt,encoding='utf-8')
        print('Project repair request prepared')
    elif args.step=='export':
        output=folder/'project-proposal'
        output.mkdir(exist_ok=True)
        (output/'proposal.json').write_text(json.dumps({'job_id':job_id,'base_sha':spec['base_sha'],
                                                      'changes':export_changes()}),encoding='utf-8')
        print('Source-only proposal exported')
    else:
        artifact=json.loads((folder/'project-proposal/proposal.json').read_text(encoding='utf-8'))
        if artifact.get('job_id')!=job_id or artifact.get('base_sha')!=spec['base_sha']:
            raise ValueError('Proposal identity mismatch')
        changes=validate_changes(artifact.get('changes'))
        original_tests={}
        for raw in git('ls-files','-z').split(b'\x00'):
            if not raw:
                continue
            name=raw.decode()
            path=Path(name)
            if (any(part in ('tests','test','shop_tests','__tests__') for part in path.parts[:-1])
                    or path.name.startswith('test_') or re.search(r'\.(test|spec)\.',path.name)):
                if path.is_file() and not path.is_symlink():
                    original_tests[name]=path.read_bytes()
        for item in changes:
            path=Path(item['path'])
            if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
                raise ValueError('Unsafe proposal path')
            if item['deleted']:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True,exist_ok=True)
                path.write_bytes(item['bytes'])
                path.chmod(0o755 if item['mode']=='100755' else 0o644)
        test(spec)
        candidate_tests={name:Path(name).read_bytes() if Path(name).exists() else None for name in original_tests}
        for name,content in original_tests.items():
            path=Path(name)
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(content)
        test(spec)
        for name,content in candidate_tests.items():
            path=Path(name)
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(content)
        output=folder/'project-verified'
        output.mkdir(exist_ok=True)
        artifact.update({'repository':spec['repository'],'ref':spec['ref'],'tests_passed':True,
                         'test_command':spec['test_command']})
        (output/'verified.json').write_text(json.dumps(artifact),encoding='utf-8')
        print('Candidate and original regression tests passed; verified source exported')


if __name__=='__main__':
    main()
