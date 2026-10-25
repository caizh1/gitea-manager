"""完整验收目录；未执行的必测项始终影响总结果，不用空占位冒充通过。"""

CASES = {
    'topology': '独立引擎、三网段、路由断开与恢复',
    'git': '完整 QEMU 与 OpenSSD 引用、对象、历史和 Git 协议',
    'packages': '四类软件包、多版本、正文和实际客户端使用',
    'lfs': 'LFS 正文下载与上传',
    'collaboration': 'Issue、PR、Wiki、附件、Release 和后续写入',
    'identities': '用户、禁用用户、2FA、密钥、Token 与权限拒绝',
    'secrets': '四种密钥来源、恢复解密和 Compose 重建',
    'governance': '父子群组、继承、审批、保护规则和审计',
    'settings': '源业务配置迁移与目标部署参数保留',
    'actions': '真实流水线、历史日志、制品、Secrets、取消与重跑',
    'integrations': '隔离 LDAP、OAuth、邮件、Webhook 和镜像调用',
    'normal': '双向、重复、历史、已有目标与重建后的完整恢复',
    'offline': '源离线及新 Manager 导入恢复',
    'schedule': '双 Worker 手工和定时调度互斥',
    'concurrent': '同目标、删除编辑、备份恢复争用与持续写入',
    'network': '稳定延迟、限速、丢包及各时长双向中断',
    'address': '地址、端口、DNS、HTTPS 和 API 单独失联',
    'ssh': '主机信任、私钥、权限失败',
    'storage': '各数据盘满、inode、只读及权限错误',
    'process': '备份和恢复各阶段终止及独立引擎重启',
    'database': '连接、额外客户端、导入和配置异常',
    'archive': '截断、正文损坏、路径穿越、软链接和解压上限',
    'rollback': '发布前失败回退和回退再次失败',
    'post_publish': '发布后新增数据及重复接管保护',
    'unsupported': '镜像、PG 主版本、存储布局和历史密钥准入',
    'manager': '登录、进度、告警、页面刷新及原记录完整性',
    'oracle': '验证器对七类数据和权限损坏的检出',
    'soak': '二十四小时稳定性与每两小时恢复',
    'package': '最终离线交付包重新部署与复验',
    'effort': '人工计时对照与百分之八十目标',
}


def verdict(records):
    """必测类别全部被真实通过覆盖才算通过，失败具有最高优先级。"""
    statuses = {key: '未执行' for key in CASES}
    for record in records:
        key = record.get('类别')
        if key in statuses:
            old, new = statuses[key], record['状态']
            if old == '失败' or new == '失败':
                statuses[key] = '失败'
            else:
                statuses[key] = new
    preparation_categories = {'seed', 'baseline', 'integrations-seed', 'checkpoint', 'checkpoint-full'}
    preparation_failures = [record for record in records if record.get('状态') == '失败'
                            and record.get('类别') in preparation_categories]
    functional_failures = [record for record in records if record.get('状态') == '失败'
                           and record.get('类别') not in preparation_categories]
    if '失败' in statuses.values() or functional_failures:
        outcome = 'FAIL'
    elif all(value == '通过' for value in statuses.values()):
        outcome = 'PASS'
    else:
        outcome = 'INCOMPLETE'
    return {'功能验收': outcome, '类别': statuses, '准备阶段首败数': len(preparation_failures),
            '功能检查失败数': len(functional_failures)}
