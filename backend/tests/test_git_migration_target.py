"""模拟 Git 服务端拒绝删除当前分支，验证临时传输分支不会污染默认分支。"""
from pathlib import Path
import tempfile,unittest
from types import SimpleNamespace
from unittest.mock import Mock
from git_migration.target import Receiver
from svn_migration.common import MigrationError


class TargetTests(unittest.TestCase):
    def test_temporary_head_is_switched_before_its_deletion(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder)/'repository.git').mkdir()
            receiver=object.__new__(Receiver);receiver.actor={'login':'admin'}
            receiver.repository=Mock(return_value={'id':1,'private':True,'has_actions':False,'empty':False})
            receiver.remote=Mock(return_value='https://target.test/admin/repo.git');receiver.environment=Mock(return_value={})
            remote={};state={'head':None,'database':'refs/heads/main'}
            def command(args,**kwargs):
                if args[1]=='ls-remote' and '--symref' in args:return ('ref: '+state['head']+'\tHEAD\n').encode()
                if args[1]=='ls-remote':return ''.join(v+'\t'+k+'\n' for k,v in sorted(remote.items())).encode()
                if args[1]=='push':
                    for item in args:
                        if item.startswith(':refs/'):
                            ref=item[1:]
                            if ref==state['head']:raise MigrationError('服务端禁止删除当前分支')
                            remote.pop(ref,None)
                        elif len(item)>41 and item[40:41]==':' and item[41:].startswith('refs/'):
                            sha,ref=item.split(':',1);remote[ref]=sha
                            if state['head'] is None and ref.startswith('refs/heads/'):state['head']=ref
                return b''
            receiver.process=SimpleNamespace(run=command,tick=lambda:None)
            def api(method,path,data):
                branch='refs/heads/'+data['default_branch']
                if state['database']!=branch:state.update(head=branch,database=branch)
            receiver.api=api
            receiver.import_objects=lambda key,rows,checkpoint,save:remote.update({row['ref']:row['commit'] for row in rows})
            checkpoint={'key':'a'*32};snapshot={'refs':{'HEAD':'a'*40,'refs/heads/main':'a'*40,'refs/pull/1/head':'b'*40},'default_ref':'refs/heads/main'}
            receiver.transfer_code(snapshot,folder,{'id':1,'name':'repo'},checkpoint,lambda _:None)
            self.assertEqual(state['head'],'refs/heads/main')
            self.assertEqual(remote,{'refs/heads/main':'a'*40,'refs/pull/1/head':'b'*40})
            self.assertTrue(checkpoint['code_transferred'])
