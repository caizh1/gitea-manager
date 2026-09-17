"""数据库定位必须对应实际原生进程，异常时不能改用不完整 API 结果。"""
import copy
import json
from types import SimpleNamespace
import time
import unittest
from unittest.mock import patch

from approval.native_metadata import NativeMetadata, Transport, decode_endpoint
from approval.rules import ApprovalError


class FakeTransport:
    def __init__(self):
        self.calls=[]
        self.infos={name:{'Id':name,'State':{'Running':True,'StartedAt':'初始启动'},'NetworkSettings':{
            'Networks':{'隔离网络':{'IPAddress':address,'GlobalIPv6Address':''}}}} for name,address in (
                ('gitea','10.0.0.1'),('postgres','10.0.0.2'))}
        self.ini='APP_NAME=隔离实例\n[database]\nDB_TYPE=postgres\nHOST=postgres:5432\nNAME=gitea\nUSER=gitea\nPASSWD=不得出现在异常中的样本口令\n'
        self.dns='10.0.0.2 postgres\n'
        self.runtime='进程 17 1234\n句柄 socket:[123]\ntcp 0100000A:C350 0200000A:1538 01 123\n'
        self.clients=[{'address':'10.0.0.1','port':50000}]
        self.ticks='1234'

    def inspect(self,name):return copy.deepcopy(self.infos[name])

    def execute(self,container,args,user=None):
        self.calls.append(args)
        if args[0]=='gitea':return 'ConfigFile: /data/gitea/conf/app.ini\n'
        if args[:2]==['cat','/data/gitea/conf/app.ini']:return self.ini
        if args[0]=='cat':return ' '.join(['17','(gitea)']+['0']*19+[self.ticks])
        if args[0]=='getent':return self.dns
        if args[0]=='sh':return self.runtime
        if args[0]=='psql':
            if 'pg_stat_activity' in args[-1]:return json.dumps(self.clients)
            return json.dumps({'pr_id':3,'repo_id':1,'number':2,'count':0,'max_id':0,'events':[]})
        raise AssertionError('未声明的只读操作')

    def close(self):pass


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.server=SimpleNamespace(is_local=True,gitea_container='gitea',pg_container='postgres',pg_dbname='gitea',pg_user='gitea')
        self.reader=NativeMetadata(self.server,time.monotonic()+8)
        self.fake=FakeTransport();self.reader.transport=self.fake

    def test_matches_running_web_socket_and_uses_readonly_transaction(self):
        result=self.reader.snapshot(1,2)
        self.assertEqual(result['pr_id'],3)
        sql=self.fake.calls[-2][-1]
        queries=[args[-1] for args in self.fake.calls if args[0]=='psql']
        self.assertEqual(len(queries),2)
        self.assertTrue(all(q.startswith('BEGIN READ ONLY;') and q.rstrip().endswith('ROLLBACK;') for q in queries))
        self.assertIn('LIMIT 5001',queries[-1])
        self.assertIn("statement_timeout='1000ms'",queries[-1])

    def test_wrong_database_address_or_user_stops_before_query(self):
        for mutation in ('数据库名称','数据库用户','数据库地址'):
            with self.subTest(定位错误=mutation):
                self.setUp()
                if mutation=='数据库名称':self.server.pg_dbname='other'
                elif mutation=='数据库用户':self.server.pg_user='other'
                else:self.fake.dns='10.8.8.8 other\n'
                with self.assertRaises(ApprovalError):self.reader.bind()
                self.assertFalse(any(a[0]=='psql' for a in self.fake.calls))

    def test_stale_disk_config_does_not_identify_actual_database(self):
        self.fake.clients=[{'address':'10.0.0.1','port':50001}]
        with self.assertRaises(ApprovalError):self.reader.snapshot(1,2)
        self.assertFalse(any('FROM comment' in a[-1] for a in self.fake.calls))

    def test_container_or_web_restart_invalidates_binding(self):
        for mutation in ('容器重启','进程重启','数据库停止'):
            with self.subTest(运行时变化=mutation):
                self.setUp();self.reader.bind()
                if mutation=='容器重启':self.fake.infos['gitea']['State']['StartedAt']='新启动'
                elif mutation=='进程重启':self.fake.ticks='1235'
                else:self.fake.infos['postgres']['State']['Running']=False
                with self.assertRaises(ApprovalError):self.reader.snapshot(1,2)

    def test_invalid_config_and_identity_cannot_inject_or_leak_secrets(self):
        for fragment in ('NAME=gitea;DROP TABLE comment','DB_TYPE=mysql','USER=host=other'):
            self.setUp();key=fragment.split('=')[0]
            lines=[fragment if line.startswith(key+'=') else line for line in self.fake.ini.splitlines()]
            self.fake.ini='\n'.join(lines)
            with self.subTest(错误字段=key),self.assertRaises(ApprovalError) as context:self.reader.bind()
            self.assertNotIn('样本口令',str(context.exception))
        self.setUp();self.reader.bind()
        for value in (True,0,'1;DROP TABLE comment'):
            with self.subTest(仓库身份=value),self.assertRaises(ApprovalError):self.reader.snapshot(value,2)

    def test_expired_budget_does_not_connect(self):
        transport=Transport(self.server,time.monotonic()-1)
        with self.assertRaises(ApprovalError):transport.remaining()
        self.assertIsNone(transport.docker);self.assertIsNone(transport.ssh)

    def test_kernel_address_encoding(self):
        self.assertEqual(decode_endpoint('0100000A:C350'),('10.0.0.1',50000))
        self.assertEqual(decode_endpoint('0000000000000000FFFF00000100000A:1538'),('10.0.0.1',5432))
        with self.assertRaises((ApprovalError,ValueError)):decode_endpoint('bad:port')

    def bind_with_snapshots(self, values):
        execute = self.fake.execute
        reads = []
        variants = iter(values)
        def capture(container, args, user=None):
            if args[0] == 'sh':
                reads.append(args)
                value = next(variants)
                if isinstance(value, Exception):
                    raise value
                return value
            return execute(container, args, user)
        self.runtime_reads = reads
        with patch.object(self.fake, 'execute', side_effect=capture):
            return self.reader.bind()

    def test_connection_rotates_between_kernel_and_database_snapshots(self):
        first = self.fake.runtime
        second = first.replace('C350', 'C351')
        self.fake.clients = [{'address': '10.0.0.1', 'port': 50001}]
        result = self.bind_with_snapshots([first, second])
        self.assertEqual(result['web_pid'], '17')
        self.assertEqual(len(self.runtime_reads), 2)
        self.assertEqual(sum('pg_stat_activity' in args[-1] for args in self.fake.calls), 1)

    def test_still_wrong_connection_never_becomes_verified(self):
        first = self.fake.runtime
        self.fake.clients = [{'address': '10.0.0.1', 'port': 50001}]
        with self.assertRaises(ApprovalError):
            self.bind_with_snapshots([first, first])
        self.assertLessEqual(len(self.runtime_reads), 2)
        self.assertIsNone(self.reader.binding)

    def test_second_rotation_is_not_retried_indefinitely(self):
        first = self.fake.runtime
        self.fake.clients = [{'address': '10.0.0.1', 'port': 50001}]
        with self.assertRaises(ApprovalError):
            self.bind_with_snapshots([first, first.replace('C350', 'C352')])
        self.assertLessEqual(len(self.runtime_reads), 2)
        self.assertIsNone(self.reader.binding)

    def test_restarted_or_duplicate_web_process_still_rejected(self):
        first = self.fake.runtime
        self.fake.clients = [{'address': '10.0.0.1', 'port': 50001}]
        for process in ('进程 17 1235', '进程 18 1234', '进程 17 1234\n进程 18 1234'):
            second = first.replace('C350', 'C351').replace('进程 17 1234', process)
            with self.subTest(进程=process), self.assertRaises(ApprovalError):
                self.bind_with_snapshots([first, second])
            self.assertIsNone(self.reader.binding)

    def test_empty_database_clients_does_not_scan_again(self):
        self.fake.clients = []
        with self.assertRaises(ApprovalError):
            self.bind_with_snapshots([self.fake.runtime])
        self.assertEqual(len(self.runtime_reads), 1)

    def test_second_snapshot_timeout_preserves_no_binding(self):
        self.fake.clients = [{'address': '10.0.0.1', 'port': 50001}]
        with self.assertRaises(ApprovalError):
            self.bind_with_snapshots([self.fake.runtime, ApprovalError('总预算已耗尽')])
        self.assertIsNone(self.reader.binding)


if __name__=='__main__':unittest.main(verbosity=2)
