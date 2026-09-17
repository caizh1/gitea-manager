#!/usr/bin/env python3
"""仅绑定回环地址的 UI 固定样本入口；提供真实构建页面，不连接或修改 Gitea。

运行：python3 scripts/ui-fixture-server.py
默认每类 120 条；?ui_mode=empty 查看空态，?ui_mode=error 查看接口错误。
所有业务写请求均被拒绝。样本只用于显示、滚动、筛选和弹窗测试。
"""
import argparse
import json
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from services.policy_rules import BOOL_FIELDS, LIST_FIELDS, TEXT_FIELDS, normalize_rule

TIME = '2026-09-12T04:00:00'
COUNT = 120
LONG = '研发平台超长名称用于验证自动换行和表格边界'
servers = [dict(id=i, name=f'显示样本-{i:03d}-{LONG}', host='192.0.2.10',
                gitea_port=3000, ssh_port=22, role='primary' if i % 2 else 'backup',
                status='online' if i % 3 else 'offline', version='1.26.4', is_local=False,
                repo_count=120, user_count=60, last_checked_at=TIME)
           for i in range(1, COUNT + 1)]
backups = [dict(id=i, filename=f'显示测试-{i:03d}-'+LONG+'.zip', source_server_id=1,
                source_server_name=servers[0]['name'], file_size=1024**3,
                status=['success', 'failed', 'running'][i % 3], commit_snapshot_status='success',
                started_at=TIME, completed_at=TIME,
                error_msg=('用于验证错误换行的固定样本。' * 40 if i % 3 == 1 else ''))
           for i in range(1, COUNT + 1)]
restores = [dict(id=i, backup_id=i, backup_filename=b['filename'], target_server_id=2,
                 target_server_name=servers[1]['name'], status=b['status'],
                 verification_status='success', started_at=TIME, completed_at=TIME,
                 error_msg=b['error_msg']) for i, b in enumerate(backups, 1)]
schedules = [dict(id=i, name=f'每日备份-{i:03d}', source_server_id=1,
                  source_server_name=servers[0]['name'], target_ids=[2, 4],
                  schedule_hour=1, schedule_minute=30, enabled=False,
                  last_status='running' if i % 3 == 0 else 'success', last_run_at=TIME,
                  progress_percent=45, progress_label='正在恢复目标服务器',
                  progress_detail='已完成数据库校验，正在复制仓库数据。', last_log='固定显示样本')
             for i in range(1, COUNT + 1)]
repos = [dict(id=i, name=f'研发团队/显示样本-{i:03d}', full_name=f'研发团队/显示样本-{i:03d}-'+LONG,
              repo_name=f'研发团队/显示样本-{i:03d}-'+LONG, default_branch='main', manageable=i % 5 != 0,
              archived=i % 5 == 0, mirror=False, enabled=True, server_id=1,
              server_name=servers[0]['name'], gitea_version='1.26.4', bot_name='审查测试账号',
              hook_status='通知已安装', last_event=TIME, revision=1,
              install_status='success', rule_name='提交规范', applied_at=TIME, install_log='固定显示样本')
         for i in range(1, COUNT + 1)]
templates = [dict(id=i, name=f'研发保护模板-{i:03d}', description=LONG * 3,
                  rules=[normalize_rule({'rule_name': 'main'})], content_hash='固定样本',
                  updated_at=TIME) for i in range(1, COUNT + 1)]
batches = [dict(id=i, template_name=f'研发保护模板-{i:03d}', actor='共享管理员', total=120,
                status='completed', created_at=TIME) for i in range(1, COUNT + 1)]
authors = [dict(name=f'测试作者-{i:03d}', email=f'author-{i:03d}@example.invalid',
                commits=121-i, additions=2000, deletions=200, repo_count=COUNT,
                first_date=TIME, last_date=TIME) for i in range(1, COUNT + 1)]
tasks = [dict(id=str(i), repository=i, number=i, status=['succeeded', 'running', 'failed'][i % 3],
              stage='正在分析变更单元' if i % 3 == 1 else '审查已结束', updated=1789185600,
              progress=dict(label=['审查完成', '正在审查', '执行失败'][i % 3],
                            head='a'*40, units_total=12, units_completed=6))
         for i in range(1, COUNT + 1)]
mirrors = [dict(id=i, source_server_id=1, target_server_id=2, source_server_name=servers[0]['name'],
                target_server_name=servers[1]['name'], status='success', sync_interval=60,
                sync_on_commit=True, synced_repos=120, total_repos=120, last_sync_at=TIME)
           for i in range(1, COUNT + 1)]
alerts = [dict(id=i, alert_type='backup_failed', server_name=servers[0]['name'],
               message=LONG*8, status='active' if i % 2 else 'resolved', created_at=TIME)
          for i in range(1, COUNT + 1)]


def payload(path, query):
    def page(items, size=20):
        filtered = [r for r in items if query.get('q', [''])[0] in r.get('full_name', '')]
        number = int(query.get('page', ['1'])[0])
        return dict(items=filtered[(number-1)*size:number*size], total=len(filtered))
    mapping = {
        '/session': {'authenticated': True}, '/settings': {'host_ip': '192.0.2.1'},
        '/servers': servers, '/backups': backups, '/restore-tasks': restores, '/schedules': schedules,
        '/dashboard/recent': dict(recent_backups=backups[:5], recent_restores=restores[:5]),
        '/mirrors': mirrors, '/alerts': alerts,
        '/alerts/summary': dict(active_count=60, latest_alert=alerts[0]),
        '/commit-rules': [dict(id=i, name=f'提交规范-{i:03d}', pattern='.*', reject_message='请补充提交说明', enabled=True, assignment_count=12) for i in range(1, COUNT+1)],
        '/commit-gates/repos': repos,
        '/repository-policies/servers': servers[:2], '/repository-policies/csrf': {'token': '仅用于显示测试'},
        '/repository-policies/schema': dict(bool_fields=BOOL_FIELDS, list_fields=LIST_FIELDS, text_fields=TEXT_FIELDS),
        '/repository-policies/templates': templates,
        '/repository-policies/repositories': page(repos, 50), '/repository-policies/batches': page(batches),
        '/ai-review/repositories': repos, '/ai-review/servers': servers[:2],
        '/ai-review/tasks': page([t for t in tasks if not query.get('repository') or str(t['repository']) == query['repository'][0]]),
        '/ai-review/config': dict(config=dict(enabled=True, engine='new_api_http', model='固定显示样本', api_base_url='',
                                             api_key_configured=False, max_files=30, max_calls=8, task_timeout_seconds=600, max_findings=10), health=[]),
    }
    if path in mapping:
        if path == '/commit-rules':
            return dict(rules=mapping[path], default_rule=dict(name='提交规范', pattern='.*', reject_message='请补充说明', enabled=True))
        return mapping[path]
    if path.startswith('/servers/') and path.endswith('/detail'):
        return dict(servers[0], detail=dict(backup_count=COUNT, restore_count=COUNT, backups=backups,
                                           restores=restores, logs=('固定显示日志：校验通过。\n'*100)))
    if path.startswith('/schedules/') and path.endswith('/logs'):
        return [dict(id=i, started_at=TIME, status='success', backup_id=i, backup_filename=backups[0]['filename'],
                     steps=[dict(stage='备份', status='success', detail=LONG*3, started_at=TIME)]) for i in range(1, 121)]
    if path.startswith('/mirrors/'):
        if path.endswith('/status'):
            return [dict(r, status='success', last_synced_at=TIME) for r in repos]
        if path.endswith('/audit-logs'):
            return [dict(id=i, action='显示校验', status='success', repo_name=r['repo_name'], created_at=TIME, message=LONG) for i, r in enumerate(repos)]
    if path.startswith('/statistics/'):
        if path.endswith('/overview'):
            return dict(total_commits=7260, total_code_lines=12000, total_doc_lines=3000, total_other_lines=500, total_repos=120,
                        top_languages=[dict(name='Python', lines=9000), dict(name='JavaScript', lines=3000)])
        if path.endswith(('/commits', '/trend')):
            return [dict(period_key=f'2026-{i:02d}', commit_count=i*5, commits=i*5) for i in range(1, 13)]
        if path.endswith('/authors'): return authors[:int(query.get('limit', ['20'])[0])]
        if path.endswith('/repos'):
            return [dict(repo_name=r['repo_name'], commits=100, commit_count=100, additions=3000,
                         deletions=100, contribution_pct=42, last_date=TIME) for r in repos[:10 if '/authors/' not in path else COUNT]]
        if '/authors/' in path:
            return dict(authors[0], total_commits=120, total_additions=3000, total_deletions=200)
    if path.startswith('/ai-review/tasks/'):
        return dict(tasks[int(path.rsplit('/', 1)[1])-1], error=LONG*12, snapshot={'merge_base':'b'*40})
    return {}


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *_): pass

    def do_GET(self):
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query)
        if parsed.path.startswith('/api/'):
            # 模式由入口查询参数传给同源请求的 Referer，且不写 Cookie 或用户数据。
            mode = parse_qs(urlsplit(self.headers.get('Referer', '')).query).get('ui_mode', [''])[0]
            api_path = unquote(parsed.path[4:])
            data = payload(api_path, query)
            status = 200
            if mode == 'login' and api_path == '/session': data = {'authenticated': False}
            if mode == 'empty':
                if isinstance(data, list): data = []
                if isinstance(data, dict) and 'items' in data: data = {'items': [], 'total': 0}
            if mode == 'error' and api_path not in ('/session', '/settings', '/alerts', '/alerts/summary'):
                status, data = 503, {'error': '显示测试：服务暂时不可用，请稍后刷新。'}
            raw = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status); self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Cache-Control', 'no-store'); self.end_headers(); self.wfile.write(raw)
            return
        if not (ROOT / 'frontend/dist' / parsed.path.lstrip('/')).is_file(): self.path = '/index.html'
        super().do_GET()

    def do_POST(self):
        self.send_response(405); self.send_header('Content-Type', 'application/json; charset=utf-8'); self.end_headers()
        self.wfile.write(json.dumps({'error': '固定显示样本禁止业务写入。'}, ensure_ascii=False).encode())

    do_PUT = do_POST
    do_DELETE = do_POST


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='运行 UI 固定样本入口')
    parser.add_argument('--port', type=int, default=5519)
    args = parser.parse_args()
    from functools import partial
    server = ThreadingHTTPServer(('127.0.0.1', args.port), partial(Handler, directory=str(ROOT/'frontend/dist')))
    print(f'UI 固定样本入口：http://127.0.0.1:{args.port}；每类 {COUNT} 条，仅验证显示。', flush=True)
    server.serve_forever()
