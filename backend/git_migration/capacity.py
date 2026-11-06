"""预迁移容量预算、实际写入探测和持续低水位监测。"""
import os
from pathlib import Path
import time
from svn_migration.common import require

GIB=1024**3

def disk(path):
    values=os.statvfs(path)
    return {'free_bytes':values.f_bavail*values.f_frsize,'free_inodes':values.f_favail}

def budget(inventory, directory, checkpoints=None):
    code=sum(p.stat().st_size for p in Path(directory).rglob('*') if p.is_file() and not p.is_symlink())
    files=[f for project in inventory['projects'] for f in project.get('source_manifest',{}).get('files',[])]
    measured={}
    for key,progress in (checkpoints or {}).items():
        if not key.isdigit():continue
        measured[key]=progress.get('downloads',{})
    assets=unknown=0
    for project in inventory['projects']:
        downloads=measured.get(str(project['project']['id']),{}) if measured else {}
        for f in project.get('source_manifest',{}).get('files',[]):
            size=f.get('size',downloads.get(f.get('source_id'),{}).get('size'))
            if size is None:unknown+=1
            else:assets+=size
    # 已有盘点快照之外，还需要隔离导入、回读及失败恢复副本。
    return {'code_bytes':code,'asset_bytes':assets,'unknown_file_sizes':unknown,
            'manager_required':4*GIB+3*code+2*assets,'target_required':4*GIB+3*code+3*assets,'inode_required':max(10000,len(files)*4)}

def verify(receiver, path, wanted, reserve=True):
    local=disk(path)
    required=wanted['manager_required'] if reserve else 4*GIB
    require(local['free_bytes']>=required and local['free_inodes']>=wanted['inode_required'],
            'Manager 工作盘容量或 inode 不足：需要至少 '+str(required)+' 字节和 '+str(wanted['inode_required'])+' 个 inode','capacity')
    remote=receiver.api('GET','/governance/migration-capacity'+('' if reserve else '?live=true'))
    require(not remote.get('unsupported'),'目标使用尚未完成容量核验的外部存储，不能标记可迁移','capacity')
    require(remote.get('storage') and remote.get('database') in (('writable',) if reserve else ('readable','writable')),'目标容量或数据库探测缺失','capacity')
    required=wanted['target_required'] if reserve else 4*GIB
    for name,value in remote['storage'].items():
        require(value['free_bytes']>=required and value['free_inodes']>=wanted['inode_required'],
                '目标 '+name+' 容量或 inode 不足：需要至少 '+str(required)+' 字节和 '+str(wanted['inode_required'])+' 个 inode','capacity')
    return {'budget':wanted,'manager':local,'target':remote,'checked_at':time.time()}

class Monitor:
    def __init__(self,receiver,path,wanted,base_tick,save):
        self.receiver,self.path,self.wanted,self.base_tick,self.save=receiver,path,wanted,base_tick,save
        self.last=0;self.checking=False
    def tick(self):
        self.base_tick()
        if self.checking or time.monotonic()-self.last<5:return
        self.checking=True
        try:self.save(verify(self.receiver,self.path,self.wanted,False));self.last=time.monotonic()
        finally:self.checking=False
