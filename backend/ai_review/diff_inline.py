"""从固定 PR diff 生成行号；文字报告中的可选定位不影响审查成功。"""
import re

from .common import ReviewError, digest
from .diff import parse_diff, quote_text, file_lines


def index(snapshot):
    anchors, hunks = {}, {}
    for patch in parse_diff(snapshot['diff'].encode()):
        # Gitea 1.26 的评论按单一路径重算 diff：重命名会被视作整文件增删，
        # 旧侧甚至返回新文件的上下文。仅保留报告，避免发布后才发现错位。
        if patch.unsupported or (patch.old_path and patch.new_path and patch.old_path != patch.new_path):
            continue
        path = patch.new_path or patch.old_path
        old_end = new_end = delta = 0
        for number, hunk in enumerate(patch.hunks):
            old = hunk.old_start - bool(hunk.old_count)
            new = hunk.new_start - bool(hunk.new_count)
            if old < old_end or new < new_end or new - old != delta:
                raise ReviewError('补丁行号重叠或累计偏移不一致，不生成行内定位')
            rows = []
            # Gitea 的截取逻辑会把无换行标记计作源码行，重算错误的 hunk 头。
            # 该 hunk 仅参与文字报告；其他正常 hunk 仍可定位。
            inline = all(raw.endswith(b'\n') for _, raw in hunk.rows)
            for kind, raw in hunk.rows:
                old += kind != '+'
                new += kind != '-'
                quote = quote_text(raw)
                rows.append((old, new, kind, quote))
                if inline and kind in ('+', '-') and quote.strip() and '\0' not in quote:
                    ident = 'R' + str(len(anchors) + 1)
                    anchors[ident] = {'id': ident, 'snapshot_id': snapshot['id'], 'path': path,
                        'old_path': patch.old_path, 'new_path': patch.new_path, 'hunk': number,
                        'side': 'new' if kind == '+' else 'old',
                        'line': new if kind == '+' else old, 'quote': quote}
            hunks[(path, number)] = rows
            old_end, new_end = old, new
            delta += hunk.new_count - hunk.old_count
    return anchors, hunks


def locations(snapshot):
    """索引只重复 diff 内的变更行，原始补丁保持完整，不读取工程。"""
    import json
    return '\n'.join(json.dumps({'定位': key, '文件': a['path'],
        '侧': a['side'], '行': a['line'], '原文': a['quote']}, ensure_ascii=False)
        for key, a in snapshot.get('anchors', {}).items())


def _label(text):
    # 只归一化标题和定位字段的展示标记，意见正文及源码不改写。
    text = re.sub(r'^\s*(?:[-+*]|\d+[.)、])\s+', '', text)
    return re.sub(r'\*\*|__|`', '', text).strip()


def _heading(line):
    text = re.sub(r'^ {0,3}#{1,6}\s+', '', line)
    match = re.fullmatch(r'(?:\[(P[0-3])\]|(P[0-3]))(?:\s+|[：:、.-]\s*)(\S.*)', _label(text), re.I)
    if not match:
        return None
    title = match[3]
    location = re.search(r'(?:\s+|[，,；;（(]\s*)定位\s*[:：]?\s*(.*)$', title)
    return ((match[1] or match[2]).upper(), title[:location.start()].rstrip() if location else title,
            location[1].rstrip('）) ').strip() if location else None)


def extract(text, snapshot, complete, include_unanchored=False):
    if not complete:
        return []  # 被截断的结论只保留在报告，不发布为独立缺陷。
    lines = text.splitlines()
    headers, fence, parents, visible = [], None, [], set()
    for i, line in enumerate(lines):
        match = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', line)
        if match:
            token, tail = match.groups()
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence) and not tail.strip():
                fence = None
            continue
        if fence is not None or re.match(r'^(?: {4}|\t|\s*>)', line):
            continue
        visible.add(i)
        markdown_heading = re.match(r'^ {0,3}(#{1,6})\s+(.*)', line)
        heading = _heading(line)
        if markdown_heading:
            depth = len(markdown_heading[1])
            parents = [(d, pending) for d, pending in parents if d < depth]
            if not heading:
                parents.append((depth, bool(re.search(r'待确认|未确认|待验证|未验证|unverified', markdown_heading[2], re.I))))
        if heading or markdown_heading:
            headers.append((i, heading if not any(pending for _, pending in parents) else None))
    findings, seen = [], set()
    for (start, heading), (end, _) in zip(headers, headers[1:] + [(len(lines), None)]):
        if not heading:
            continue
        severity, title, inline_location = heading
        locations = [inline_location] if inline_location is not None else []
        body_lines = []
        for i in range(start + 1, end):
            location = re.fullmatch(r'定位\s*[:：]?\s*(.*)', _label(lines[i])) if i in visible else None
            if location:
                locations.append(location[1])
            else:
                body_lines.append(lines[i])
        # 一项意见只能指定一个确切编号；不把多个位置、范围或无效编号猜成一行。
        if not locations or any(not re.fullmatch(r'R[1-9][0-9]*|无', value, re.I) for value in locations):
            continue
        identities = {value.upper() for value in locations}
        if len(identities) != 1:
            continue
        ident = identities.pop()
        anchor = snapshot.get('anchors', {}).get(ident)
        body = '\n'.join(body_lines).strip()
        if (not anchor and not (include_unanchored and ident == '无')) or not body or not title:
            continue
        fingerprint = digest({'anchor': anchor, 'severity': severity, 'title': title, 'body': body})
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        findings.append({'kind': 'diff_inline', 'severity': severity, 'title': title,
            'comment_markdown': body, 'path': anchor['path'] if anchor else '', 'scope': 'PR diff',
            'anchor': dict(anchor) if anchor else None, 'quote': anchor['quote'] if anchor else '', 'fingerprint': fingerprint})
    return findings


def publication_status(snapshot, result):
    """只统计本地可证实的处理结果，不把没有提取到定位说成没有缺陷。"""
    findings = result.get('findings', [])
    located = sum(bool(f.get('anchor')) for f in findings)
    repeated = sum(bool(f.get('anchor') and f.get('existing_review_id')) for f in findings)
    rejected = sum(bool(f.get('fallback_reason')) for f in findings)
    text = f'本次新增行内评论 {located - repeated} 条；识别到有效定位 {located} 项，历史重复 {repeated} 项，本地定位校验未通过 {rejected} 项。\n\n'
    if not findings:
        if not result['coverage']['complete']:
            text += '报告未完整结束，本次仅保留文字报告。\n\n'
        elif not snapshot.get('anchors'):
            text += snapshot.get('inline_unavailable') or '本次 diff 没有可用的行内位置，意见保留在报告中'
            text += '。\n\n'
        else:
            text += '未从报告提取到可发布的定位；可能未提供可识别的定位或没有确定问题，不能仅据此判断报告没有问题。\n\n'
    return text


def verify_comment(raw, anchor, snapshot):
    """Gitea 可能只返回到问题行；逐行核对固定 diff 的坐标和原文。"""
    anchors, hunks = index(snapshot)
    if anchors.get(anchor['id']) != anchor:
        raise ReviewError('评论锚点不属于固定 PR diff')
    rows = file_lines(raw.encode())
    header = re.fullmatch(rb'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@[^\n]*\n?', rows[0])
    if not header:
        raise ReviewError('Gitea 评论补丁区域格式错误')
    old_start, old_count, new_start, new_count = [int(v) if v is not None else 1 for v in header.groups()]
    old, new = old_start - bool(old_count), new_start - bool(new_count)
    expected = hunks[(anchor['path'], anchor['hunk'])]
    positions = {(a, b, kind): (i, quote) for i, (a, b, kind, quote) in enumerate(expected)}
    # Gitea 1.26.4 截取连续替换块时，空侧沿用“下一行”游标，
    # 零长度区间因此偏一行。只在固定 diff 证实首行匹配该差异时校正空侧；
    # 有源码的一侧、后续每行的坐标和正文仍逐字核对。
    if len(rows) > 1:
        kind = chr(rows[1][0])
        first = (old + (kind != '+'), new + (kind != '-'), kind)
        if first not in positions:
            if (not old_count and kind == '+' and any(k != '+' for _, _, k, _ in expected)
                    and (old - 1, new + 1, kind) in positions):
                old -= 1
            elif (not new_count and kind == '-' and any(k != '-' for _, _, k, _ in expected)
                    and (old + 1, new - 1, kind) in positions):
                new -= 1
    last, found, consumed_old, consumed_new = None, False, 0, 0
    for row in rows[1:]:
        if row.startswith(b'\\ No newline at end of file'):
            continue
        kind, text = chr(row[0]), quote_text(row[1:])
        if kind not in ('+', '-', ' '):
            raise ReviewError('Gitea 评论包含无法校验的补丁行')
        old += kind != '+'
        new += kind != '-'
        consumed_old += kind != '+'
        consumed_new += kind != '-'
        match = positions.get((old, new, kind))
        if not match or match[1] != text or (last is not None and match[0] != last + 1):
            raise ReviewError('Gitea 评论片段与固定 PR diff 不一致')
        last = match[0]
        if ((kind == '+' and anchor['side'] == 'new' and new == anchor['line']) or
                (kind == '-' and anchor['side'] == 'old' and old == anchor['line'])):
            found = text == anchor['quote']
    if not found or consumed_old > old_count or consumed_new > new_count:
        raise ReviewError('Gitea 评论片段没有包含正确的问题变更行')
