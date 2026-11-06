"""完整清单跨语言编码、封存恢复与错误身份阻断。"""
import copy
import unittest
from unittest.mock import Mock
from git_migration.manifest import summary, metadata_digest, recover
from svn_migration.common import MigrationError

class ManifestTests(unittest.TestCase):
    def test_portable_metadata(self):
        value=dict(source_id='附件-😀',name='a\nb.zip',parent_kind='comment',parent_id='9007199254740993',sha256='摘要',size=123,created=1600000000)
        self.assertEqual(metadata_digest(value),'1a224ce7d11d06f22b21f11b8f61d4dc707072dee76e84ad7c4320436e4f1fda')

    def fixture(self, state='sealed'):
        receiver=Mock()
        p={'key':'a'*32,'session_id':1,'repo_id':2,'source_digest':'b'*64,'batch_size':1,
           'objects':{'issue:1':{'id':3,'kind':'issue','source_id':'1','target_id':4,'digest':'c'*64}},'assets':{}}
        p['seal_intent']=summary(p)
        status={'id':1,'key':p['key'],'repo_id':2,'actor_id':7,'batch_key':'d'*32,'source_digest':p['source_digest'],
                'publish_barrier':True,'state':state,'seal_manifest_version':1,'seal_manifest_digest':p['seal_intent']['digest']}
        batch={'key':'d'*32,'actor_id':7,'expected':1,'state':'preparing'}
        manifest={**p['seal_intent'],'revision':4}
        def api(method,path):
            self.assertEqual(method,'GET')
            if path.endswith('/status'):return status
            if path.endswith('/manifest'):return manifest
            return batch
        receiver.api.side_effect=api
        return receiver,p,status,batch,manifest

    def test_recovery_matrix_and_repeat(self):
        for state in ('loading','sealed','handed_off'):
            for local in (False,True):
                with self.subTest(state=state,local=local):
                    r,p,_,_,_=self.fixture(state)
                    if local:p['sealed']=True
                    save=Mock()
                    if state=='loading' and local:
                        with self.assertRaises(MigrationError):recover(r,p,'d'*32,{'id':7},save)
                        continue
                    recover(r,p,'d'*32,{'id':7},save)
                    if state!='loading':
                        self.assertTrue(p['sealed'])
                        r.verify_objects.assert_called_once()
                        r.verify_assets.assert_called_once()
                        recover(r,p,'d'*32,{'id':7},save)
                    r.import_objects.assert_not_called()
                    r.transfer_code.assert_not_called()

    def test_missing_intent_and_mutations_fail_closed(self):
        for field in ('id','key','repo_id','actor_id','batch_key','source_digest','publish_barrier','state','seal_manifest_digest'):
            r,p,status,_,_=self.fixture();status[field]='错误身份或状态'
            with self.subTest(field=field),self.assertRaises(MigrationError):recover(r,p,'d'*32,{'id':7},Mock())
        for part in ('local','remote','intent','published','abandoned'):
            r,p,_,batch,m=self.fixture()
            if part=='local':p['objects']={}
            elif part=='remote':m['digest']='e'*64
            elif part=='intent':p.pop('seal_intent')
            else:batch['state']=part
            with self.subTest(part=part),self.assertRaises(MigrationError):recover(r,p,'d'*32,{'id':7},Mock())

    def test_manifest_covers_equal_count_substitutions(self):
        _,p,_,_,_=self.fixture();baseline=summary(p)
        for field in ('id','kind','source_id','target_id','digest'):
            changed=copy.deepcopy(p);changed['objects']['issue:1'][field]=99 if field in ('id','target_id') else '其他'
            self.assertNotEqual(summary(changed),baseline)
        self.assertNotEqual(summary({'objects':{}}),baseline)

    def test_receipt_pagination_rejects_changed_revision(self):
        from git_migration.target import Receiver
        for changed_at in ('page','end'):
            with self.subTest(changed_at=changed_at):
                receiver=object.__new__(Receiver)
                status={'id':1,'key':'a'*32,'repo_id':2,'source_digest':'b'*64,'revision':3,'state':'sealed'}
                first={'id':7,'kind':'comment','source_id':'1','target_id':9,'digest':'c'*64}
                second={**first,'id':8,'source_id':'2'}
                checkpoint={'session_id':1,'key':status['key'],'repo_id':2,'source_digest':status['source_digest'],'objects':{'comment:1':first,'comment:2':second}}
                def api(method,path):
                    if path.endswith('/status'):return {**status,'revision':4 if changed_at=='end' else 3}
                    if path.endswith('after_id=0'):return {'session':status,'objects':[{'receipt':first,'matches':True}],'next_id':7}
                    return {'session':{**status,'revision':4 if changed_at=='page' else 3},'objects':[{'receipt':second,'matches':True}],'next_id':0}
                receiver.api=api
                with self.assertRaisesRegex(MigrationError,'变化'):
                    receiver.verify_objects(status['key'],checkpoint,Mock())


class NativeManifestTests(unittest.TestCase):
    def test_native_manifest_must_match_frozen_checkpoint(self):
        from git_migration.manifest import verify
        receiver,p,_,_,remote=ManifestTests().fixture()
        p['native_manifest']={'native_version':1,'native_digest':'a'*64,'native_count':2}
        remote.update(p['native_manifest'])
        self.assertEqual(verify(receiver,p)['native_count'],2)
        for field in ('native_version','native_digest','native_count'):
            broken=dict(remote);broken[field]='已变化'
            receiver.api.return_value=broken;receiver.api.side_effect=None
            with self.subTest(field=field), self.assertRaises(MigrationError):verify(receiver,p)
