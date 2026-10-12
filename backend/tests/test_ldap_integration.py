"""显式启用的真实目录回归；未提供隔离环境时跳过，不以 Mock 冒充通过。"""
import json
import os
from pathlib import Path
import unittest

from access.ldap_directory import authenticate, search_users, revalidate
from access.errors import AccessError

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / 'work' / 'access-acceptance'


@unittest.skipUnless(os.environ.get('ACCESS_LDAP_INTEGRATION') == '1', '仅在明确启用真实 LDAP 验收时运行')
class RealDirectoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture = json.loads(os.environ['ACCESS_LDAP_TEST_FIXTURE']) if os.environ.get('ACCESS_LDAP_TEST_FIXTURE') else None
        cls.fixture = fixture
        cls.env = fixture['environment'] if fixture else dict(line.split('=',1) for line in (STATE/'.env').read_text().splitlines() if '=' in line and not line.startswith('#'))
        cls.host = 'openldap' if fixture else '127.0.0.1'
        cls.ad_host = 'samba' if fixture else '127.0.0.1'
        cls.password = cls.env['DIRECTORY_PASSWORD']
        cls.config = {'name':'真实目录验收','enabled':True,'type':'binddn','host':cls.host,
            'port':int(cls.env['LDAPS_PORT']),'security_protocol':'ldaps','ca_cert':fixture['ca_cert'] if fixture else (STATE/'fixtures/ca.pem').read_text(),
            'bind_dn':'cn=reader,dc=access,dc=test','bind_password':cls.password,'user_base':'ou=people,dc=access,dc=test',
            'filter':'(uid=%s)','attribute_username':'uid','attribute_name':'givenName','attribute_surname':'sn','attribute_mail':'mail'}

    def test_protocol_and_mode_matrix(self):
        for protocol, key in [('unencrypted','LDAP_PORT'),('ldaps','LDAPS_PORT'),('starttls','LDAP_PORT')]:
            cfg = self.config | {'port':int(self.env[key]),'security_protocol':protocol}
            for mode in ['binddn','anonymous','simple']:
                with self.subTest(protocol=protocol,mode=mode):
                    changes = {'bind_dn':'','bind_password':''} if mode=='anonymous' else {}
                    if mode=='simple':
                        changes = {'type':'simple','user_dn':'uid=%s,ou=people,dc=access,dc=test'}
                    identity = authenticate(cfg | changes,'user0000',self.password)
                    self.assertTrue(identity['subject'].startswith('entryUUID:'))
                    self.assertEqual(identity['username'],'user0000')

    def test_active_directory_and_disable(self):
        cfg = self.config | {'host':self.ad_host,'port':int(self.env['AD_LDAPS_PORT']),'bind_dn':'Administrator@ACCESS.TEST',
            'user_base':'CN=Users,DC=access,DC=test','filter':'(sAMAccountName=%s)','attribute_username':'sAMAccountName'}
        identity = authenticate(cfg,'user0000',self.password)
        self.assertTrue(identity['subject'].startswith('objectGUID:'))
        with self.assertRaises(AccessError):
            authenticate(cfg,'user0002',self.password)
        cfg |= {'paged_search_enabled':True,'paged_search_size':400,'filter':'(&(objectClass=user)(sAMAccountName=user*)(sAMAccountName=%s))'}
        users = search_users(cfg)
        self.assertEqual(len(users),1500,'1501 个测试账号中已禁用 1 个，不能出现在可授权列表')
        self.assertEqual(len({user['subject'] for user in users}),1500)

    def test_filters_groups_and_paging(self):
        cfg = self.config | {'filter':'(|(uid=%[1]s)(mail=%[1]s))','groups_enabled':True,
            'group_dn':'ou=groups,dc=access,dc=test','group_filter':'cn=department-a','group_member_uid':'member','user_uid':'dn'}
        self.assertEqual(authenticate(cfg,'user0000@access.test',self.password)['username'],'user0000')
        with self.assertRaises(AccessError):
            authenticate(cfg,'user0003',self.password)
        users = search_users(self.config | {'paged_search_enabled':True,'paged_search_size':400})
        self.assertEqual(len(users),1501)

    def test_revalidation_compares_stable_identity(self):
        identity = authenticate(self.config,'user0000',self.password)
        self.assertEqual(revalidate(self.config,'user0000',identity['subject'])['subject'],identity['subject'])
        with self.assertRaises(AccessError):
            revalidate(self.config,'user0000','entryUUID:00000000-0000-0000-0000-000000000000')

    def test_authentication_failures(self):
        for username,password in [('user0000',''),('user0000','错误密码'),('missing',self.password),('*)(uid=*)',self.password)]:
            with self.subTest(username=username):
                with self.assertRaises(AccessError):
                    authenticate(self.config,username,password)

    def test_real_matrix_evidence_has_no_failed_cases(self):
        records = self.fixture['records'] if self.fixture else json.loads((STATE/'ldap-results.json').read_text())
        self.assertGreaterEqual(len(records),50,'完整目录与 Gitea 差分矩阵尚未完成')
        self.assertEqual([r['用例'] for r in records if r['结果']!='通过'],[])


if __name__ == '__main__':
    unittest.main()
