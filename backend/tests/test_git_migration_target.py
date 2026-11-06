"""模拟 Git 服务端拒绝删除当前分支，验证临时传输分支不会污染默认分支。"""
from pathlib import Path
import tempfile,unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch
from git_migration.target import Receiver,MonitoredUpload
from svn_migration.common import MigrationError


class TargetTests(unittest.TestCase):
    def test_fixed_length_upload_and_empty_log_http_framing(self):
        import io,requests
        tick=Mock();stream=MonitoredUpload(io.BytesIO(b'abc'),3,tick)
        request=requests.Request('POST','https://target.test/upload',data=stream).prepare()
        self.assertEqual(request.headers['Content-Length'],'3')
        self.assertNotIn('Transfer-Encoding',request.headers)
        self.assertEqual(request.body.read(2),b'ab');tick.assert_called_once()
        empty=requests.Request('POST','https://target.test/upload',data=b'',headers={'Content-Length':'0'}).prepare()
        self.assertEqual(empty.headers['Content-Length'],'0')
        self.assertNotIn('Transfer-Encoding',empty.headers)
    def test_final_subgroup_rechecks_use_canonical_path(self):
        receiver=object.__new__(Receiver);receiver.base='https://target.test';receiver.verify=True
        receiver.actor={'login':'admin'};receiver.token='固定验收令牌'
        receiver.process=SimpleNamespace(tick=lambda:None)
        snapshot={'refs':{},'default_ref':'','lfs_objects':[]}
        repo={'id':7,'name':'repo','full_name':'internal-group/repo','full_path':'root/team/repo'}
        with patch('git_migration.target.Transport'),patch('git_migration.target.Provider'),patch('git_migration.target.GitSnapshot') as capture:
            capture.return_value.capture.return_value=snapshot
            receiver.verify_code(snapshot,repo,Path('/unused'),{},lambda _:None)
            self.assertEqual(capture.call_args.args[1]['path'],'root/team/repo')
            receiver.verify_wiki({'wiki_snapshot':snapshot},repo,Path('/unused-wiki'))
            self.assertEqual(capture.call_args.args[1]['path'],'root/team/repo')

    def test_sealed_asset_recheck_detects_content_change(self):
        import hashlib
        receiver=object.__new__(Receiver);receiver.base='https://target.test';receiver.verify=True
        receiver.process=SimpleNamespace(tick=lambda:None)
        reply=Mock(status_code=200);reply.__enter__=Mock(return_value=reply);reply.__exit__=Mock(return_value=False)
        reply.iter_content.return_value=[b'changed']
        receiver.session=SimpleNamespace(get=Mock(return_value=reply))
        checkpoint={'key':'a'*32,'assets':{'asset':{'id':1,'size':7,'sha256':hashlib.sha256(b'correct').hexdigest()}}}
        with self.assertRaisesRegex(MigrationError,'内容变化'):receiver.verify_assets(checkpoint)
        reply.iter_content.return_value=[b'correct']
        receiver.verify_assets(checkpoint)

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
