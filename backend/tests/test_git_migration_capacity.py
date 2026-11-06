"""容量预算与持续监测必须拒绝不足和无法核实的目标。"""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
from git_migration.capacity import GIB,budget,verify,Monitor
from svn_migration.common import MigrationError

class CapacityTests(unittest.TestCase):
    def test_budget_includes_multiple_copies_and_reserve(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp)/'data').write_bytes(b'x'*100)
            value=budget({'projects':[{'source_manifest':{'files':[{'size':200},{}]}}]},tmp)
            self.assertEqual(value['manager_required'],4*GIB+700)
            self.assertEqual(value['target_required'],4*GIB+900)
            self.assertEqual(value['unknown_file_sizes'],1)
    def test_final_budget_uses_measured_files_without_ignoring_unknown_sizes(self):
        with tempfile.TemporaryDirectory() as tmp:
            inventory={'projects':[{'project':{'id':7},'source_manifest':{'files':[{'source_id':'log-1'},{'source_id':'missing'}]}}]}
            value=budget(inventory,tmp,{'7':{'downloads':{'log-1':{'size':1000}}}})
            self.assertEqual((value['asset_bytes'],value['unknown_file_sizes']),(1000,1))
            self.assertEqual(value['target_required'],4*GIB+3000)
    def test_low_target_storage_and_external_storage_fail_closed(self):
        receiver=Mock();wanted={'manager_required':5*GIB,'target_required':6*GIB,'inode_required':10000}
        with patch('git_migration.capacity.disk',return_value={'free_bytes':10*GIB,'free_inodes':20000}):
            receiver.api.return_value={'storage':{'repositories':{'free_bytes':GIB,'free_inodes':20000}},'database':'writable','unsupported':[]}
            with self.assertRaisesRegex(MigrationError,'容量或 inode 不足'):verify(receiver,'.',wanted)
            receiver.api.return_value={'storage':{},'database':'writable','unsupported':['packages']}
            with self.assertRaisesRegex(MigrationError,'外部存储'):verify(receiver,'.',wanted)
    def test_monitor_rechecks_capacity_and_stops_on_new_failure(self):
        with patch('git_migration.capacity.verify',side_effect=[{'result':'通过'},MigrationError('容量不足','capacity')]),patch('git_migration.capacity.time.monotonic',side_effect=[10,10,20]):
            save=Mock();tick=Mock();m=Monitor(Mock(),'.',{},tick,save);m.tick();save.assert_called_once()
            with self.assertRaises(MigrationError):m.tick()
    def test_live_monitor_reads_database_without_repeating_write_probe(self):
        receiver=Mock();receiver.api.return_value={'storage':{'data':{'free_bytes':10*GIB,'free_inodes':20000}},'database':'readable'}
        with patch('git_migration.capacity.disk',return_value={'free_bytes':10*GIB,'free_inodes':20000}):
            wanted={'manager_required':GIB,'target_required':GIB,'inode_required':10000}
            verify(receiver,'.',wanted,False)
            receiver.api.assert_called_once_with('GET','/governance/migration-capacity?live=true')
            with self.assertRaises(MigrationError):verify(receiver,'.',wanted,True)
