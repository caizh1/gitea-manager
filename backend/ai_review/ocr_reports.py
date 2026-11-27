"""按同一 OCR 结果生成可离线打开的 HTML 与 Markdown。"""
import html
import json
import re
from urllib.parse import quote

LABELS = {'html': 'HTML', 'md': 'Markdown', 'json': 'JSON', 'txt': 'TXT',
          'sarif': 'SARIF', 'native': 'OCR 原生 HTML'}


SEVERITIES = {
    'critical': '严重', 'error': '高', 'high': '高', 'medium': '中',
    'warning': '中', 'low': '低', 'info': '提示', 'suggestion': '建议',
}
SUMMARY_TITLES = {
    'top issues': '重点问题', 'module hotspots': '模块热点',
    'cross-cutting concerns': '跨模块风险', 'quick wins': '优先改进建议',
}
EXCLUSION_REASONS = {
    'unsupported_ext': '文件类型未纳入', 'secret_exclude': '命中敏感文件排除规则',
    'too_large': '文件大小或上下文超限', 'gitignore': '命中 Git 忽略规则', 'excluded': '命中 OCR 排除规则',
}
INLINE = re.compile(r'(`[^`]+`|\*\*.+?\*\*)')
HEAD = re.compile(r'^#{1,6}\s+(.+)$')
LIST_ITEM = re.compile(r'^((?:[-*+]|\d+[.)])\s+)(.+)$')


def formatted(value):
    """把 OCR 常见文本结构拆成展示数据。所有内容由客户端按纯文本渲染。"""
    blocks = []
    paragraph = []
    fence = None

    def inline(text):
        tokens = []
        for index, part in enumerate(INLINE.split(text)):
            if not part:
                continue
            if index % 2 == 0:
                tokens.append(['text', part])
            elif part.startswith('`'):
                tokens.append(['code', part[1:-1]])
            else:
                tokens.append(['strong', part[2:-2]])
        return tokens

    def flush():
        if paragraph:
            blocks.append({'kind': 'paragraph', 'text': inline(' '.join(paragraph))})
            paragraph.clear()

    for raw in str(value or '').splitlines():
        line = raw.strip()
        if line.startswith('```'):
            flush()
            if fence is None:
                fence = []
            else:
                blocks.append({'kind': 'code', 'text': '\n'.join(fence)})
                fence = None
        elif fence is not None:
            fence.append(raw)
        elif not line:
            flush()
        elif heading := HEAD.fullmatch(line):
            flush()
            title = heading[1].strip()
            blocks.append({'kind': 'heading', 'text': inline(SUMMARY_TITLES.get(title.lower(), title))})
        elif entry := LIST_ITEM.fullmatch(line):
            flush()
            kind = 'ordered' if entry[1][0].isdigit() else 'unordered'
            if not blocks or blocks[-1]['kind'] != kind:
                blocks.append({'kind': kind, 'items': []})
            blocks[-1]['items'].append(inline(entry[2]))
        elif not paragraph and raw[:1].isspace() and blocks and blocks[-1]['kind'] in ('ordered', 'unordered'):
            blocks[-1]['items'][-1].extend([['text', ' '], *inline(line)])
        else:
            paragraph.append(line)
    flush()
    if fence is not None:
        blocks.append({'kind': 'code', 'text': '\n'.join(fence)})
    return blocks


def normalized(raw, task, repo):
    findings = []
    summary = raw.get('project_summary') or ''
    for row in raw.get('comments') or []:
        if not isinstance(row, dict):
            continue
        path = row.get('path') or ''
        line = row.get('start_line') or 0
        link = ''
        if path in task['progress'].get('files', {}):
            link = (repo['gitea_url'].rstrip('/') + '/' + quote(repo['repo_name'], safe='/') +
                    '/src/commit/' + task['sha'] + '/' + quote(path, safe='/'))
            if type(line) is int and line > 0:
                link += f'#L{line}'
        severity = row.get('severity') or '未分级'
        content = row.get('content') or ''
        findings.append({'path': path, 'line': line, 'severity': severity,
                         'severity_label': SEVERITIES.get(severity.lower(), severity), 'content': content,
                         'content_blocks': formatted(content), 'suggestion': row.get('suggestion_code') or '',
                         'source': row.get('existing_code') or '', 'link': link})
    files = task['progress'].get('files', {})
    return {'repository': repo['repo_name'], 'branch': task['branch'], 'sha': task['sha'],
            'model': task['config']['model'], 'tokens': task['tokens'],
            'seconds': max(0, round(task['updated'] - task['created'])), 'completed': len(files),
            'total': task['preview'].get('reviewable_count', 0), 'excluded': task['preview'].get('excluded_count', 0),
            'findings': findings, 'summary': summary, 'summary_blocks': formatted(summary),
            'scope': task['preview'].get('files', [])}


def markdown(value):
    if not value['findings']:
        body = '未发现问题。'
    else:
        body = '\n\n'.join(f"## {row['path']}:{row['line']} · {row['severity']}\n\n{row['content']}\n\n{row['suggestion']}"
                            for row in value['findings'])
    return (f"# 整仓扫描报告\n\n{value['repository']} · {value['branch']}\n\n"
            f"提交：{value['sha']}\n\n已完成 {value['completed']}/{value['total']}，"
            f"排除 {value['excluded']}，消耗 {value['tokens']} token。\n\n"
            f"{value['summary']}\n\n{body}\n")


def escape(item):
    return html.escape(str(item), quote=True)


def render_blocks(blocks):
    def inline(parts):
        elements = {'code': 'code', 'strong': 'strong'}
        return ''.join(f'<{elements[kind]}>{escape(text)}</{elements[kind]}>' if kind in elements else escape(text)
                       for kind, text in parts)

    output = []
    for block in blocks:
        kind = block['kind']
        if kind in ('ordered', 'unordered'):
            tag = 'ol' if kind == 'ordered' else 'ul'
            items = ''.join(f'<li>{inline(parts)}</li>' for parts in block['items'])
            output.append(f'<{tag}>{items}</{tag}>')
        elif kind == 'code':
            output.append(f'<pre><code>{escape(block["text"])}</code></pre>')
        else:
            tag = 'h3' if kind == 'heading' else 'p'
            output.append(f'<{tag}>{inline(block["text"])}</{tag}>')
    return ''.join(output)


def finding_card(row):
    location = escape(f"{row['path']}:{row['line']}")
    if row['link']:
        location = f'<a href="{escape(row["link"])}" rel="noreferrer">{location}</a>'
    snippets = []
    for key, title in (('source', '相关代码'), ('suggestion', '修改建议')):
        if row[key]:
            snippets.append(f'<div class="snippet"><span>{title}</span><pre><code>{escape(row[key])}</code></pre></div>')
    return (f'<article class="finding" data-level="{escape(row["severity"].lower())}"><header>'
            f'<span class="severity">{escape(row["severity_label"])}</span><code>{location}</code></header>'
            f'<div class="narrative">{render_blocks(row["content_blocks"])}</div>{"".join(snippets)}</article>')


def document(value):
    cards = ''.join(finding_card(row) for row in value['findings'])
    levels = {row['severity'].lower(): row['severity_label'] for row in value['findings']}
    choices = ''.join(f'<option value="{escape(level)}">{escape(label)}</option>' for level, label in sorted(levels.items()))
    scope = []
    for row in value['scope']:
        included = row.get('will_review')
        reason = row.get('exclude_reason') or 'OCR 排除'
        status = '已扫描' if included else '已排除'
        scope.append(f'<tr><td><code>{escape(row.get("path", ""))}</code></td><td>{status}</td>'
                     f'<td>{escape("—" if included else EXCLUSION_REASONS.get(reason, reason))}</td></tr>')
    elapsed = f"{value['seconds']:,} 秒"
    metrics = [('已排除', value['excluded']), ('发现', len(value['findings'])),
               ('Token 用量', f"{value['tokens']:,}"), ('执行耗时', elapsed)]
    count = len(value['findings'])
    tiles = ''.join(f'<div class="metric"><dt>{label}</dt><dd>{escape(amount)}</dd></div>' for label, amount in metrics)
    summary = render_blocks(value['summary_blocks'])
    return f"""<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; base-uri 'none'">
<title>整仓扫描报告</title>
<style>
:root{{font:15px/1.55 system-ui,sans-serif;color:#14233c;background:#edf2f9;--border:#dbe5f2;--muted:#4d607a}}
*{{box-sizing:border-box}}body{{margin:0;padding:24px 18px}}main{{max-width:1120px;margin:auto;display:grid;gap:16px}}
h1,h2,h3,p,dl,dd{{margin:0}} h1{{font-size:clamp(1.5rem,4vw,2rem);line-height:1.15}}
h2{{font-size:1.2rem}}h3{{font-size:1.05rem}}a{{color:#1755bb;text-underline-offset:3px}}
code{{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;overflow-wrap:anywhere}}
.panel,.finding{{background:#fff;border:1px solid var(--border);border-radius:14px}}
.panel{{padding:20px 22px}}.masthead{{border-top:4px solid #2862c6}}
.repo,.metadata,.finding header{{display:flex;align-items:center;gap:8px;flex-wrap:wrap;min-width:0}}
.repo{{margin-block:8px;font-size:1.05rem;overflow-wrap:anywhere}}
.branch,.severity{{padding:3px 9px;border-radius:999px;font-size:.8rem;font-weight:650;line-height:1.35}}
.branch,.severity{{background:#e9f1ff;color:#1e50a4}}
.metadata{{color:var(--muted);font-size:.83rem}}.metadata code{{padding:2px 6px;background:#eef3fa;border-radius:5px;color:#284362}}
.metrics{{
  display:grid;grid-template-columns:1.12fr repeat(4,minmax(0,1fr));margin-top:18px;
  padding:14px 0;background:#eef5fc;border-radius:10px
}}
.metric{{min-width:0;padding:0 18px;border-inline-start:1px solid #cddbeb}}
.metric:first-child{{border-inline-start:0}}
.metric dt,.snippet>span{{color:var(--muted);font-size:.82rem;font-weight:650}}
.metric dd{{margin-top:4px;font-size:clamp(1.25rem,2.4vw,1.75rem);line-height:1.25;font-weight:750;
  font-variant-numeric:tabular-nums;overflow-wrap:anywhere}}
.coverage-progress{{display:block;appearance:none;border:0;width:min(100%,144px);height:5px;
  border-radius:8px;margin-top:7px;background:#dbe6f5;color:#225cc2;overflow:hidden}}
.coverage-progress::-webkit-progress-bar{{background:#dbe6f5}}
.coverage-progress::-webkit-progress-value{{background:#225cc2;border-radius:8px}}
.coverage-progress::-moz-progress-bar{{background:#225cc2;border-radius:8px}}
.toolbar{{display:grid;gap:12px;margin-bottom:18px}}
.list-heading{{display:flex;align-items:baseline;gap:9px}}
.match-count{{color:var(--muted);font-size:.9rem;font-variant-numeric:tabular-nums}}
.filters{{display:grid;grid-template-columns:minmax(0,1fr) minmax(165px,22%);gap:10px;min-width:0}}
.filter-control{{height:44px;min-width:0;background:#fff;border:1px solid #adbed4;border-radius:8px}}
.filter-control:hover{{border-color:#7497c7}}
.filter-control:focus-within{{outline:2px solid #2460c8;outline-offset:2px}}
.filter-search{{display:flex;align-items:center;gap:9px;padding-inline:12px}}
.filter-control input,.filter-control select{{min-width:0;width:100%;height:100%;padding:0;border:0;outline:0;
  background:transparent;color:#14233c;font:inherit;font-size:14px;line-height:normal}}
.filter-control input::placeholder{{color:#536781}}
.search-icon{{display:inline-grid;grid-template:1fr / 1fr;place-items:center;flex:0 0 18px;height:18px;color:#526785}}
.search-icon::before,.search-icon::after{{content:"";grid-area:1/1}}
.search-icon::before{{width:10px;height:10px;border:1.6px solid currentColor;border-radius:50%;transform:translate(-1px,-1px)}}
.search-icon::after{{width:6px;height:1.6px;border-radius:2px;background:currentColor;transform:translate(4px,4px) rotate(45deg)}}
.filter-choice{{display:grid;grid-template:1fr / minmax(0,1fr);align-items:center}}
.filter-choice select,.filter-chevron{{grid-area:1/1}}
.filter-choice select{{appearance:none;padding-inline:12px 33px;min-height:0;cursor:pointer}}
.filter-chevron{{justify-self:end;margin-inline-end:14px;width:8px;height:8px;border:solid #465e7d;
  border-width:0 1.6px 1.6px 0;transform:translateY(-2px) rotate(45deg);pointer-events:none}}
summary:focus-visible{{outline:2px solid #2460c8;outline-offset:3px}}
.finding{{padding:13px 15px;margin-bottom:10px;overflow-wrap:anywhere}}.finding h3{{margin-top:6px}}
[data-level="error"] .severity,[data-level="high"] .severity,[data-level="critical"] .severity{{background:#fee9e9;color:#8f2020}}
[data-level="warning"] .severity,[data-level="medium"] .severity{{background:#fff0d1;color:#754608}}
.narrative{{margin-top:8px}}.narrative > *+*,.summary > *+*{{margin-top:6px}}.narrative ul,.narrative ol,.summary ul,.summary ol{{padding-inline-start:22px}}
.narrative p code,.summary p code,.summary li code,.narrative li code{{background:#eef3fa;padding:1px 4px;border-radius:4px}}
.snippet{{margin-top:8px}}pre{{color:#244061;background:#f3f6fa;border-radius:6px;border:1px solid var(--border);padding:8px 10px;margin:3px 0 0;white-space:pre-wrap;overflow-wrap:anywhere}}
summary{{font-weight:700;cursor:pointer}}details[open]>summary{{margin-bottom:10px}}.summary{{overflow-wrap:anywhere}}
table{{width:100%;border-collapse:collapse;text-align:left}}th,td{{padding:6px 8px;border-bottom:1px solid var(--border);overflow-wrap:anywhere}}
th{{background:#f0f5fd;font-size:.85rem}}tr th:nth-child(2){{min-width:68px}}#empty{{color:var(--muted)}}
@media (max-width:620px){{
  body{{padding:10px}}.panel{{padding:14px}}
  .metrics{{grid-template-columns:repeat(2,minmax(0,1fr));padding:0 12px;margin-top:15px}}
  .metric{{padding:9px 10px;border-inline-start:0;border-top:1px solid #cddbeb}}
  .metric:nth-child(odd):not(:first-child){{border-inline-start:1px solid #cddbeb}}
  .metric-primary{{grid-column:1/-1;border-top:0}}
}}
@media (max-width:410px){{.filters{{grid-template-columns:minmax(0,1fr)}}}}
</style></head>
<body><main>
<section class="panel masthead"><h1>整仓扫描报告</h1>
<div class="repo"><strong>{escape(value['repository'])}</strong><span class="branch">{escape(value['branch'])}</span></div>
<div class="metadata"><span>提交</span><code>{escape(value['sha'])}</code><span>模型 {escape(value['model'])}</span></div>
<dl class="metrics"><div class="metric metric-primary">
<dt>文件覆盖</dt><dd>{value['completed']}/{value['total']}</dd>
<progress class="coverage-progress" aria-label="文件覆盖率" value="{value['completed']}" max="{max(1, value['total'])}"></progress>
</div>{tiles}</dl></section>
<section class="panel" aria-labelledby="findings-heading">
<div class="toolbar">
<div class="list-heading"><h2 id="findings-heading">问题清单</h2><span id="match-count" class="match-count" aria-live="polite">{count} 项</span></div>
<div class="filters" role="group" aria-label="筛选问题">
<label class="filter-control filter-search" for="search"><span class="search-icon" aria-hidden="true"></span>
<input id="search" type="search" aria-label="筛选文件或问题" placeholder="搜索文件或问题"></label>
<div class="filter-control filter-choice"><select id="level" aria-label="严重程度">
<option value="">全部严重程度</option>{choices}</select><span class="filter-chevron" aria-hidden="true"></span></div>
</div></div>
<div id="findings">{cards}</div><p id="empty"{' hidden' if cards else ''} role="status">没有匹配的问题</p></section>
{'<details class="panel"><summary>OCR 汇总</summary><div class="summary">' + summary + '</div></details>' if summary else ''}
<details class="panel"><summary>文件覆盖明细</summary>
<table><thead><tr><th scope="col">文件路径</th><th scope="col">扫描状态</th><th scope="col">排除原因</th></tr></thead>
<tbody>{''.join(scope)}</tbody></table></details>
</main><script>
const search = document.querySelector('#search');
const level = document.querySelector('#level');
const empty = document.querySelector('#empty');
const matchCount = document.querySelector('#match-count');
const findings = [...document.querySelectorAll('.finding')];
function filter() {{
  const query = search.value.trim().toLowerCase();
  let visible = 0;
  for (const row of findings) {{
    row.hidden = Boolean((level.value && row.dataset.level !== level.value) ||
                         !row.textContent.toLowerCase().includes(query));
    visible += Number(!row.hidden);
  }}
  empty.hidden = visible > 0;
  matchCount.textContent = visible + ' 项';
}}
search.addEventListener('input', filter);
level.addEventListener('change', filter);
</script></body></html>"""

def save(folder, value):
    (folder / 'report.html').write_text(document(value), encoding='utf-8')
    (folder / 'report.md').write_text(markdown(value), encoding='utf-8')
    (folder / 'report-view.json').write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
