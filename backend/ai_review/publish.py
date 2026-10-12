"""本地校验后直接发布正式 Review；回读核对，未知结果禁止盲目重放。"""
import contextlib
import json
import re
from urllib.parse import urlsplit

from .common import ReviewError, Stale, PublishUnknown, digest
from .diff import file_lines, quote_text
from .gitea import assert_version


def marker(job):
    return '<!-- ai-review:v1:job:' + job['id'] + ' -->'


def prose(value):
    value = value.replace('@', '＠').replace('<', '＜').replace('>', '＞')
    return re.sub(r'([\\`*_\[\]!])', r'\\\1', value)


def finding_body(finding, job):
    from .dataset import finding_id
    extra = ''
    if finding.get('quality_version', 0) >= 3:
        extra = '\n\n意见编号：`' + finding_id(job, finding) + '`'
        if finding.get('assessment') == 'uncertain':
            extra += '\n\n**待开发者确认：** ' + prose(finding.get('verification', '反证未完成'))
        extra += '\n\n反馈：在 PR 对话页填写 `/ai-review 反馈 ' + finding_id(job, finding) + ' 误报 具体原因`；也可使用 排除、部分错误、定位错误、有效、撤销。'
    return (marker(job) + '\n<!-- ai-review:finding:' + digest(finding['fingerprint'])[:24] + ' -->\n'
            + f"**[{finding['severity']}] {prose(finding['title'])}**\n\n"
            + '触发条件：' + prose(finding['trigger']) + '\n\n'
            + '原因：' + prose(finding['explanation']) + '\n\n'
            + '影响：' + prose(finding['impact']) + '\n\n'
            + '建议：' + prose(finding['recommendation']) + extra)


def summary(job, snapshot, result):
    coverage = result['coverage']
    text = (marker(job) + '\n### AI Code Review（辅助意见）\n\n'
            + f"审查版本：`{snapshot['head']}`；比较基点：`{snapshot['merge_base']}`。\n\n"
            + f"已读取 {coverage['files_reviewed']}/{coverage['files_total']} 个变更文件。")
    if not coverage['complete']:
        text += '\n\n**本次部分完成，存在未完成或未覆盖内容，不能据此判断整个 PR 没有问题。**\n'
        text += '\n'.join('- ' + prose(s['path']) + '：' + prose(s['reason']) for s in coverage['skipped'][:40])
    if result.get('retained_findings'):
        text += f"\n\n其余 {result['retained_findings']} 项意见保存在 Manager 任务详情中；全部计划单元仍已处理。"
    findings = result['findings']
    if not findings:
        text += '\n\n在已覆盖的修改中未发现证据充分、值得提出的明显问题。'
    else:
        text += f'\n\n发现 {len(findings)} 项值得开发者检查的问题。\n'
        for row in findings:
            text += '\n\n' + finding_body(row, job)
            text += '\n\n文件：' + prose(row['path']) + '；范围：' + prose(row['scope']) + '。'
            if row.get('existing_review_id'):
                text += f"\n\n该问题已在历史 Review #{row['existing_review_id']} 中提出；本次重新核对仍成立，不重复添加行内意见。"
            elif not row.get('anchor'):
                text += '\n\n定位：' + prose(row.get('fallback_reason') or '无法可靠映射，作为普通意见保留') + '。'
            else:
                text += f"\n\n定位：{'新' if row['anchor']['side']=='new' else '旧'}侧第 {row['anchor']['line']} 行。"
    return text


def expected_comments(job, result):
    return [{'path': row['anchor']['path'], 'body': finding_body(row, job),
             'old_position': row['anchor']['line'] if row['anchor']['side'] == 'old' else 0,
             'new_position': row['anchor']['line'] if row['anchor']['side'] == 'new' else 0}
            for row in result['findings'] if row.get('anchor') and not row.get('existing_review_id')]


def verify_hunk(comment, anchor, snapshot):
    if (comment.get('path') != anchor['path']
            or comment.get('position', 0) != (anchor['line'] if anchor['side'] == 'new' else 0)
            or comment.get('original_position', 0) != (anchor['line'] if anchor['side'] == 'old' else 0)):
        raise ReviewError('Gitea 保存的位置与已验证锚点不一致')
    raw = comment.get('diff_hunk', '')
    if not isinstance(raw, str) or not raw.startswith('@@ '):
        raise ReviewError('Gitea 未返回可校验的评论补丁')
    # Gitea 展示片段可能在问题行截断，hunk 头仍保留原区域计数；不能把它
    # 当成完整可应用补丁。完整补丁已在 capture 中逐字节重建校验。
    rows = file_lines(raw.encode())
    header = re.fullmatch(rb'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@[^\n]*\n?', rows[0])
    if not header:
        raise ReviewError('Gitea 评论补丁区域格式错误')
    old_start, old_count, new_start, new_count = [int(v) if v is not None else 1 for v in header.groups()]
    file = next(f for f in snapshot['files'] if f['path'] == anchor['path'])
    left, right = file_lines(file['old'].encode()), file_lines(file['new'].encode())
    found = False
    old = old_start - (1 if old_count else 0)
    new = new_start - (1 if new_count else 0)
    consumed_old = consumed_new = 0
    for row in rows[1:]:
        if row.startswith(b'\\ No newline at end of file'):
            continue
        kind, text = chr(row[0]), row[1:]
        if kind not in ('+', '-', ' '):
            raise ReviewError('Gitea 评论包含无法校验的补丁行')
        # Gitea 将 CRLF 展示为 LF，并省略片段末尾 LF。仅归一化行结束符；
        # 行号、修改侧、所有其他空白和源码字符仍必须逐字一致。
        if kind != '+':
            if not 0 <= old < len(left) or quote_text(left[old]) != quote_text(text):
                raise ReviewError('Gitea 评论旧侧上下文不属于该固定版本')
            old += 1
            consumed_old += 1
        if kind != '-':
            if not 0 <= new < len(right) or quote_text(right[new]) != quote_text(text):
                raise ReviewError('Gitea 评论新侧上下文不属于该固定版本')
            new += 1
            consumed_new += 1
        if ((kind == '+' and anchor['side'] == 'new' and new == anchor['line'])
                or (kind == '-' and anchor['side'] == 'old' and old == anchor['line'])):
            if quote_text(text) != anchor['quote']:
                raise ReviewError('Gitea 评论问题行引用不一致')
            found = True
    if consumed_old > old_count or consumed_new > new_count:
        raise ReviewError('Gitea 评论片段超出声明的修改区域')
    if not found:
        raise ReviewError('Gitea 保存的补丁不包含问题变更行')


class Publisher:
    def __init__(self, store, client, job, repo, snapshot, result):
        self.store, self.client, self.job, self.repo = store, client, job, repo
        self.snapshot = snapshot
        self.full_result = result
        limit = result.get('publication_limit', len(result.get('findings', [])))
        self.result = result.get('publication') or result
        self.page_size = max(1,limit)
        self.path = client.prefix + f"/pulls/{job['number']}/reviews"
        self.mark = marker(job)
        self.cancelled = False

    def comments(self, review):
        return self.client.pages(self.path + '/' + str(review['id']) + '/comments')

    def owned(self, review, comments=None):
        if review.get('user', {}).get('id') != self.repo['bot_id']:
            return False
        if self.mark in review.get('body', ''):
            return True
        if review.get('state') != 'PENDING':
            return False
        rows = self.comments(review) if comments is None else comments
        return bool(rows) and all(self.mark in c.get('body', '') and c.get('user', {}).get('id') == self.repo['bot_id'] for c in rows)

    def verify(self, review):
        if review.get('state') != 'COMMENT':
            raise PublishUnknown('Review 尚未正式发布，未自动续发')
        rows = self.comments(review)
        if not self.owned(review, rows) or review.get('commit_id') != self.snapshot['head']:
            raise ReviewError('Review 的机器人身份或固定版本不匹配')
        if review.get('body') != summary(self.job, self.snapshot, self.result):
            raise ReviewError('已发布总结与本地冻结结果不一致')
        expected = expected_comments(self.job, self.result)
        if len(rows) != len(expected):
            raise ReviewError('已发布评论集合不完整或混入其他意见')
        by_body = {r['body']: r for r in rows}
        if len(by_body) != len(rows):
            raise ReviewError('已发布 Review 包含重复评论')
        for item in self.result['findings']:
            if not item.get('anchor') or item.get('existing_review_id'):
                continue
            body = finding_body(item, self.job)
            actual = by_body.get(body)
            if not actual or actual.get('user', {}).get('id') != self.repo['bot_id']:
                raise ReviewError('已发布评论内容或归属不匹配')
            verify_hunk(actual, item['anchor'], self.snapshot)
        return rows

    def validate_local(self):
        """发布前重新确认锚点来自本任务快照，不根据模型路径或行号补救。"""
        for item in self.result['findings']:
            anchor = item.get('anchor')
            if not anchor:
                continue
            canonical = self.snapshot['anchors'].get(anchor.get('id'))
            file = next((f for f in self.snapshot['files'] if f['path'] == anchor.get('path')), None)
            valid = (canonical == anchor and anchor.get('snapshot_id') == self.snapshot['id']
                     and file is not None and anchor.get('side') in ('new', 'old')
                     and type(anchor.get('line')) is int and anchor['line'] > 0)
            if valid:
                lines = file_lines(file[anchor['side']].encode())
                valid = (anchor['line'] <= len(lines)
                         and quote_text(lines[anchor['line']-1]) == anchor['quote']
                         and bool(anchor['quote'].strip()) and not anchor.get('ambiguous'))
            if valid and item.get('quality_version', 0) >= 2:
                from .evidence import revalidate_location
                valid = revalidate_location(item, self.snapshot)
            if not valid:
                item['anchor'] = None
                item.pop('existing_review_id', None)
                item['fallback_reason'] = '本地锚点与固定版本源码不一致，已降级为普通意见'

    def reference(self, review):
        """仅提取当前 PR 的结果链接，不信任服务端返回的内网主机名。"""
        url = urlsplit(review.get('html_url', ''))
        expected_path = '/' + self.repo['repo_name'] + f"/pulls/{self.job['number']}"
        fragment = url.fragment if url.path.endswith(expected_path) and re.fullmatch(r'issuecomment-[1-9][0-9]*', url.fragment) else ''
        return {'review_id': review['id'], 'review_fragment': fragment}

    def prior_findings(self):
        with contextlib.closing(self.store.connect()) as c:
            rows = c.execute("SELECT review_id,result FROM jobs WHERE repository=? AND number=? AND status='succeeded' AND result IS NOT NULL ORDER BY created DESC LIMIT 10",
                             (self.job['repository'], self.job['number'])).fetchall()
        def history_key(item):
            from .engine import re_title
            return (item['path'], item['scope'], re_title(item['title']), item['anchor']['quote'])
        known = {}
        for record in rows:
            result = json.loads(record['result'])
            for item in result.get('publication', result).get('findings', []):
                if item.get('anchor'):
                    known.setdefault(history_key(item), item.get('existing_review_id') or record['review_id'])
        for item in self.result['findings']:
            anchor = item.get('anchor')
            if anchor and history_key(item) in known:
                item['existing_review_id'] = known[history_key(item)]

    def publish(self, recovery=False):
        if self.full_result.get('publication_pages') or len(self.result['findings'])>self.page_size or len(summary(self.job,self.snapshot,self.result))>12000 or any(len(c['body'])>12000 for c in expected_comments(self.job,self.result)):
            return self.publish_pages(recovery)
        with self.store.lock(f"发布-{self.job['repository']}-{self.job['number']}"):
            self.store.active(self.job['id'], self.job['owner'])
            reviews = self.client.pages(self.path)
            mine = [r for r in reviews if self.owned(r)]
            if len(mine) > 1:
                raise PublishUnknown('发现重复任务标记，停止自动发布')
            if mine:
                if mine[0]['state'] != 'COMMENT':
                    raise PublishUnknown('本任务存在未完成草稿；请核对并清理后人工重审，未自动续发')
                self.save_reference(mine[0])
                return mine[0]['id']
            if any(r['state'] == 'PENDING' and r.get('user', {}).get('id') == self.repo['bot_id'] for r in reviews):
                raise PublishUnknown('机器人存在其他未完成草稿，未合并或发布；请先核对处理')
            if recovery:
                raise PublishUnknown('尚未查到原请求结果，未重发写入')
            sent = False
            try:
                self.client.version()
                self.store.update(self.job['id'], self.job['owner'], stage='校验本地行内位置', progress_stage='location')
                self.validate_local()
                self.prior_findings()
                # 冻结实际提交内容，恢复只能核对这份结果，不重新计算历史去重。
                self.store.update(self.job['id'], self.job['owner'], result=dict(self.full_result, publication=self.result) if 'publication_limit' in self.full_result else self.result)
                assert_version(self.client, self.job['number'], self.snapshot)
                self.store.update(self.job['id'], self.job['owner'], status='publishing', stage='发布正式 Review 和行内评论')
                self.store.active(self.job['id'], self.job['owner'])
                sent = True
                published = self.client.request('POST', self.path, {
                    'event': 'COMMENT', 'commit_id': self.snapshot['head'],
                    'body': summary(self.job, self.snapshot, self.result),
                    'comments': expected_comments(self.job, self.result)})
                self.store.update(self.job['id'], self.job['owner'], stage='核对已发布评论', progress_stage='publication_check')
                self.save_reference(published)
                return published['id']
            except Stale:
                self.cancelled = not sent
                raise

    def publish_pages(self, recovery=False):
        """分页内容先冻结；已发送的页面只核对，不重放不确定写入。"""
        with self.store.lock(f"发布-{self.job['repository']}-{self.job['number']}"):
            self.store.active(self.job['id'],self.job['owner'])
            pages=self.full_result.get('publication_pages')
            if not pages:
                self.validate_local();self.prior_findings()
                pages=[]
                findings=self.result['findings']
                groups=[findings[i:i+self.page_size] for i in range(0,len(findings),self.page_size)] or [[]]
                for group in groups:
                    text=summary(self.job,self.snapshot,dict(self.result,findings=group))
                    bodies=[text[i:i+12000] for i in range(0,len(text),12000)]
                    comments=[]
                    for item in group:
                        if not item.get('anchor') or item.get('existing_review_id'):continue
                        body=finding_body(item,self.job)
                        for offset in range(0,len(body),12000):
                            comments.append({'path':item['anchor']['path'],'body':body[offset:offset+12000],
                                'old_position':item['anchor']['line'] if item['anchor']['side']=='old' else 0,
                                'new_position':item['anchor']['line'] if item['anchor']['side']=='new' else 0,
                                'anchor':item['anchor']})
                    count=max(len(bodies),(len(comments)+self.page_size-1)//self.page_size,1)
                    for index in range(count):
                        mark='<!-- ai-review:v1:job:'+self.job['id']+':page:'+str(len(pages)+1)+' -->'
                        body=mark+'\n审查结果分页 '+str(len(pages)+1)+'\n\n'+(bodies[index] if index<len(bodies) else '本页包含后续行内意见。')
                        rows=[]
                        for part,comment in enumerate(comments[index*self.page_size:(index+1)*self.page_size]):
                            rows.append(dict(comment,body=mark+'\n意见片段 '+str(part+1)+'\n'+comment['body']))
                        pages.append({'marker':mark,'body':body,'comments':rows,'state':'pending'})
                self.full_result=dict(self.full_result,publication=self.result,publication_pages=pages)
                self.store.update(self.job['id'],self.job['owner'],result=self.full_result)
            reviews=self.client.pages(self.path)
            if any(r['state']=='PENDING' and r.get('user',{}).get('id')==self.repo['bot_id'] for r in reviews):
                raise PublishUnknown('机器人存在未完成草稿，未开始分页发布')
            first=None
            for page in pages:
                self.store.active(self.job['id'],self.job['owner'])
                assert_version(self.client,self.job['number'],self.snapshot)
                matching=[r for r in reviews if page['marker'] in r.get('body','') and r.get('user',{}).get('id')==self.repo['bot_id']]
                if len(matching)>1:raise PublishUnknown('分页标记重复，停止核对')
                if matching:
                    published=self.client.request('GET',self.path+'/'+str(matching[0]['id']))
                else:
                    if page['state']!='pending':
                        raise PublishUnknown('分页请求结果不确定，未重放；已保留先前发布页')
                    self.client.version()
                    page['state']='sending'
                    self.store.update(self.job['id'],self.job['owner'],status='publishing',result=self.full_result,
                                      stage='正在发布审查结果分页',progress_stage='publishing',batch_started=__import__('time').time())
                    published=self.client.request('POST',self.path,{'event':'COMMENT','commit_id':self.snapshot['head'],
                              'body':page['body'],'comments':[{k:v for k,v in c.items() if k!='anchor'} for c in page['comments']]})
                    published=self.client.request('GET',self.path+'/'+str(published['id']))
                if published.get('state')!='COMMENT' or published.get('commit_id')!=self.snapshot['head'] or published.get('body')!=page['body'] or published.get('user',{}).get('id')!=self.repo['bot_id']:
                    raise PublishUnknown('分页内容或归属未通过回读核对')
                actual=self.comments(published)
                if len(actual)!=len(page['comments']):raise PublishUnknown('分页行内评论数量不一致')
                by_body={c['body']:c for c in actual}
                if len(by_body)!=len(actual):raise PublishUnknown('分页评论存在重复内容')
                for expected in page['comments']:
                    comment=by_body.get(expected['body'])
                    if not comment or comment.get('user',{}).get('id')!=self.repo['bot_id']:raise PublishUnknown('分页评论内容或归属不一致')
                    verify_hunk(comment,expected['anchor'],self.snapshot)
                page.update(state='confirmed',review_id=published['id'])
                first=first or published
                self.store.update(self.job['id'],self.job['owner'],result=self.full_result)
            self.store.update(self.job['id'],self.job['owner'],**self.reference(first))
            return first['id']

    def save_reference(self, review):
        """核对公开结果；不删除正式 Review，也不把不一致的结果标记成功。"""
        actual = self.client.request('GET', self.path + '/' + str(review['id']))
        self.verify(actual)
        self.store.update(self.job['id'], self.job['owner'], **self.reference(actual))
