"""以已验证补丁供给新旧原文；语法树辅助取证，不决定是否允许审查。"""
import posixpath
import re

from .common import ReviewError, Stale, encode
from .snapshot import parse_source
from .context import source, operations
from .context_parts import Headers

LIMIT = 48000
KEYWORDS = set('int void char unsigned signed long short const static struct enum return volatile sizeof if else for while switch case break default typedef extern define include'.split())


class View:
    def __init__(self, text, path, language=None):
        self.text, self.path = text, path
        self.raw, self.tree = parse_source(text, path, language)
        self.lines = text.split('\n')
        self.nodes, self.functions, self.errors = [], [], []
        pending = [self.tree.root_node]
        while pending:
            node = pending.pop(); self.nodes.append(node)
            if node.type == 'ERROR' or node.is_missing:
                self.errors.append({'code':'PARSER_LIMITATION', 'start_line':node.start_point.row+1,
                                    'end_line':node.end_point.row+1, 'reason':'语法节点缺失' if node.is_missing else '原始语法暂不支持',
                                    'node':node.type})
            if node.type == 'function_definition':
                declarator = node.child_by_field_name('declarator')
                self.functions.append((self.code(declarator) if declarator else '未命名函数', node))
            pending.extend(reversed(node.children))

    def code(self, node):
        return self.raw[node.start_byte:node.end_byte].decode()

    def span(self, node):
        # 结束点位于下一行第零列时，该行不属于节点。
        return node.start_point.row, node.end_point.row + bool(node.end_point.column)

    def function(self, lo, hi):
        rows = [(name,node) for name,node in self.functions if self.span(node)[0] <= lo and hi <= self.span(node)[1]]
        return rows[0] if len(rows)==1 else None

    def declared(self,node):
        if node.type in ('preproc_def','preproc_function_def','struct_specifier','enum_specifier'):
            name=node.child_by_field_name('name')
            return {self.code(name)} if name else set()
        result=set()
        for declaration in node.children_by_field_name('declarator'):
            while declaration is not None:
                if declaration.type in ('identifier','type_identifier'):
                    result.add(self.code(declaration));break
                declaration=declaration.child_by_field_name('declarator')
        return result

    def visible_scope(self,node,span):
        if span is None:return True
        parent=node.parent
        while parent is not None:
            if parent.type in ('compound_statement','for_statement','function_definition','translation_unit'):
                a,b=self.span(parent)
                return a<=span[0] and span[1]<=b
            parent=parent.parent
        return True

    def window(self, lo, hi, pad=3):
        if not self.text:return (0,0)
        # 小文件直接给出完整两版，避免文件级宏修改只看到半个使用者或半条条件分支。
        if len(self.text)<=7000:return (0,len(self.lines))
        lo=max(0,min(lo,len(self.lines)-1)); hi=max(lo,min(hi,len(self.lines)))
        own=self.function(lo,max(lo+1,hi))
        if own and len(self.code(own[1])) <= 7000:return self.span(own[1])
        start,end=max(0,lo-pad),min(len(self.lines),max(hi,lo+1)+pad)
        # 普通 declaration 不带下划线后缀；同样保留完整声明。
        for node in self.nodes:
            a,b=self.span(node)
            if node.type in ('declaration','type_definition','preproc_def','preproc_function_def') or node.type.endswith('_statement'):
                if node.type != 'compound_statement' and a <= lo and hi <= b and len(self.code(node)) <= 7000:
                    start=min(start,a);end=max(end,b)
        if len('\n'.join(self.lines[start:end]))>10000:
            start,end=lo,max(hi,lo+1)
        return start,end

    def excerpt(self, snapshot, version, role, span):
        a,b=span
        item=source(snapshot,self.path,version,role,'\n'.join(self.lines[a:b]),a+1)
        item['availability']='present' if self.text else 'absent'
        return item

    def dependencies(self, snapshot, version, used, span, limit=6500):
        names=set(re.findall(r'\b[A-Za-z_]\w*\b',used))-KEYWORDS
        candidates=[]
        # 声明按其绑定的名称匹配，而非因 initializer 或公共类型相同而把无关局部量全带进来。
        declarations=[]
        for node in self.nodes:
            if node.type in ('declaration','type_definition','preproc_def','preproc_function_def','struct_specifier','enum_specifier') and self.visible_scope(node,span):
                declarations.append(node)
        selected=set()
        for _ in range(4):
            added=False
            for node in declarations:
                if node.id in selected or not self.declared(node)&names:continue
                selected.add(node.id);added=True
                names.update(set(re.findall(r'\b[A-Za-z_]\w*\b',self.code(node)))-KEYWORDS)
            if not added:break
        for node in self.nodes:
            a,b=self.span(node)
            if span and span[0] <= a and b <= span[1]:continue
            if node.type not in ('declaration','type_definition','preproc_def','preproc_function_def','struct_specifier','enum_specifier',
                                 'function_definition','if_statement','for_statement','while_statement','switch_statement','preproc_if','preproc_ifdef'):continue
            text=self.code(node)
            if node.type in ('declaration','type_definition','preproc_def','preproc_function_def','struct_specifier','enum_specifier'):
                if node.id in selected:candidates.append((a,b,'相关声明',text))
            elif node.type == 'function_definition':
                declarator=node.child_by_field_name('declarator'); body=node.child_by_field_name('body')
                if span and a <= span[0] < b and body:
                    end=min(b,body.start_point.row+1)
                    candidates.append((a,end,'所属函数签名','\n'.join(self.lines[a:end])))
                elif declarator:
                    ids=re.findall(r'\b([A-Za-z_]\w*)\s*\(',self.code(declarator))
                    if ids and re.search(r'\b'+re.escape(ids[0])+r'\s*\(',used):candidates.append((a,b,'相关调用实现',text))
            elif span and a <= span[0] and span[1] <= b and node.type in ('if_statement','for_statement','while_statement','switch_statement','preproc_if','preproc_ifdef'):
                field=node.child_by_field_name('condition') or node.child_by_field_name('name')
                end=field.end_point.row+1 if field else min(a+1,b)
                candidates.append((a,end,'包围条件','\n'.join(self.lines[a:end])))
            elif span and b<=span[0] and node.type=='if_statement' and self.visible_scope(node,span) and len(text)<=1800:
                condition=node.child_by_field_name('condition')
                if condition and names & (set(re.findall(r'\b[A-Za-z_]\w*\b',self.code(condition)))-KEYWORDS):
                    candidates.append((a,b,'相关前置检查',text))
        result=[];seen=set();size=0
        for a,b,role,text in candidates:
            if (a,b) in seen or not text.strip():continue
            seen.add((a,b))
            evidence=source(snapshot,self.path,version,role,text,a+1);cost=len(encode(evidence))
            if size+cost>limit:continue
            result.append(evidence);size+=cost
        return result


def changes(file):
    """只用已验证补丁的行种类映射；不通过相似文本猜测新旧配对。"""
    if not file.get('patch'):raise ReviewError('当前快照缺少已验证补丁映射，请重新审查')
    for hunk,h in enumerate(file['patch']):
        left=h['old_start']-bool(h['old_count']);right=h['new_start']-bool(h['new_count'])
        pending=None
        for kind in [*h['kinds'],' ']:
            if kind==' ' and pending is not None:
                yield (hunk,pending[0],left,pending[1],right);pending=None
            if kind!=' ' and pending is None:pending=(left,right)
            if kind!='+':left+=1
            if kind!='-':right+=1


def function_parts(change, views):
    """仅用两侧可核对的函数边界细分同一编辑块，不在函数内部猜测对应关系。"""
    hunk,ol,oh,nl,nh=change
    def functions(view):
        found={}
        for _,node in view.functions:
            if node.has_error:continue
            name=node.child_by_field_name('declarator')
            while name is not None and name.type not in ('identifier','qualified_identifier','operator_name','destructor_name'):
                name=name.child_by_field_name('declarator')
            if name is not None:found.setdefault(view.code(name),[]).append(view.span(node))
        # 重载、同名条件定义等不能依靠名称证明对应关系。
        return {name:rows[0] for name,rows in found.items() if len(rows)==1}
    before,after=functions(views['old']),functions(views['new'])
    cuts={(ol,nl),(oh,nh)}
    if ol==oh:
        cuts.update((ol,row) for span in after.values() for row in span if nl<row<nh)
    elif nl==nh:
        cuts.update((row,nl) for span in before.values() for row in span if ol<row<oh)
    else:
        for name in before.keys() & after.keys():
            for left,right in zip(before[name],after[name]):
                if ol<=left<=oh and nl<=right<=nh:cuts.add((left,right))
    ordered=sorted(cuts)
    # 函数移动或重排导致边界交叉时，保留原始配对块。
    if any(a[1]>b[1] for a,b in zip(ordered,ordered[1:])):return [change]
    return [(hunk,a[0],b[0],a[1],b[1]) for a,b in zip(ordered,ordered[1:])]


def changed_symbols(snapshot,views,anchors):
    """列出变更声明及同文件的字面引用，只给源码事实，不推断宏值、绑定或缺陷。"""
    names={}
    for side,view in views.items():
        changed={a['line'] for a in anchors if a['side']==side}
        for node in view.nodes:
            if node.type not in ('declaration','type_definition','preproc_def','preproc_function_def','struct_specifier','enum_specifier'):continue
            lo,hi=view.span(node)
            if not any(lo<line<=hi for line in changed):continue
            for name in sorted(view.declared(node)):
                names.setdefault(name,node.type)
    result=[]
    for name,kind in list(names.items())[:8]:
        references=[]
        for side,version in (('old','before'),('new','after')):
            view=views[side];pattern=re.compile(r'\b'+re.escape(name)+r'\b')
            hits=[(i,line) for i,line in enumerate(view.lines,1) if pattern.search(line)]
            # 给出相邻完整源码行及版本，不把同名文本当作已经解析的符号绑定。
            for i,line in hits[:8]:
                if len(line)>1000:continue
                references.append(source(snapshot,view.path,version,'变更名称的字面引用',line,i))
        entry={'name':name,'kind':kind,'references':references,
               'note':'仅同文件的有限字面引用，可能存在条件编译或同名变量；跨文件使用者可继续检索'}
        if len(encode(result+[entry]))>6000:break
        result.append(entry)
    return result


def header_context(cache, client, snapshot, file, used):
    result=[];diagnostics=[]
    if not client:return result,diagnostics
    for side,version,sha in (('old','before',snapshot['merge_base']),('new','after',snapshot['head'])):
        path=file.get('old_path' if side=='old' else 'new_path') or file['path']
        for name in dict.fromkeys(re.findall(r'^\s*#\s*include\s*"([^"\n]+)"',file[side],re.M)):
            target=posixpath.normpath(posixpath.join(posixpath.dirname(path),name))
            try:
                if target.startswith(('../','/')):raise ReviewError('头文件路径超出仓库')
                actual,text=cache.read(target,name,sha)
                if not hasattr(cache,'views'):cache.views={}
                key=(sha,actual)
                if key not in cache.views:
                    if len(cache.views)>=16:cache.views.pop(next(iter(cache.views)))
                    cache.views[key]=View(text,actual)
                view=cache.views[key]
                pieces=view.dependencies(snapshot,version,used,None,5000)
                if not pieces and len(text)<=3000:pieces=[source(snapshot,actual,version,'头文件',text)]
                result.extend(pieces)
                diagnostics.extend(dict(e,path=actual,side=side,affects_review=False) for e in view.errors[:8])
            except Stale:raise
            except (ReviewError,UnicodeError) as exc:
                if getattr(exc,'fatal_job',False):raise
                diagnostics.append({'path':target,'side':side,'code':'HEADER_LOOKUP_AVAILABLE',
                                    'reason':str(exc),'affects_review':True})
    return result,diagnostics


def build_items(snapshot, client=None, on_file=None):
    items=[];coverage=snapshot['coverage'];diagnostics=coverage.setdefault('diagnostics',[])
    header_cache=Headers(client) if client else None
    required={aid for file in snapshot['files'] for aid in file['anchors'] if snapshot['anchors'][aid]['quote'].strip()};supplied=set()
    for file in snapshot['files']:
        language='c' if file['path'].lower().endswith(('.c','.h')) else 'cpp'
        views={side:View(file[side],file.get(side+'_path') or file['path'],language) for side in ('old','new')}
        if file['path'].lower().endswith('.h') and any(v.errors for v in views.values()):
            cpp={side:View(file[side],views[side].path,'cpp') for side in views}
            if not any(v.errors for v in cpp.values()):views=cpp;language='cpp'
        file['parser_language']=language
        for side,view in views.items():diagnostics.extend(dict(e,path=view.path,side=side,affects_review=False) for e in view.errors[:16])
        anchors=[snapshot['anchors'][aid] for aid in file['anchors'] if snapshot['anchors'][aid]['quote'].strip()]
        required.update(a['id'] for a in anchors)
        parsed={s:(v.raw,v.tree) for s,v in views.items()}
        pending=[part for change in changes(file) for part in function_parts(change,views)]
        while pending:
            hunk,ol,oh,nl,nh=pending.pop(0)
            own=[a for a in anchors if a['hunk']==hunk and ((a['side']=='old' and ol<a['line']<=oh) or (a['side']=='new' and nl<a['line']<=nh))]
            if not own:continue
            spans={'old':views['old'].window(ol,oh),'new':views['new'].window(nl,nh)}
            fn=views['new'].function(nl,max(nl+1,nh)) or views['old'].function(ol,max(ol+1,oh))
            item={'analysis_version':4,'snapshot_id':snapshot['id'],'path':file['path'],
                  'scope':{'name':fn[0] if fn else '文件级修改','start':nl+1,'end':max(nl+1,nh),'part':f'{hunk}:{ol}-{oh}:{nl}-{nh}'},
                  'before':views['old'].excerpt(snapshot,'before','修改及相邻原文',spans['old']),
                  'after':views['new'].excerpt(snapshot,'after','修改及相邻原文',spans['new']),
                  'anchors':own,'operations':operations(snapshot,file,own,parsed),
                  'context':[],'context_gaps':[], 'context_lookup':True,
                  'parse_warning':any(v.errors for v in views.values())}
            for side,version in (('old','before'),('new','after')):
                # 空文件仍然存在；是否新增或删除由经过校验的补丁路径决定。
                item[version]['availability']='absent' if side+'_path' in file and file[side+'_path'] is None else 'present'
            if len(encode(item))>30000 and max(oh-ol,nh-nl)>1:
                om=(ol+oh)//2;nm=(nl+nh)//2
                pending[:0]=[(hunk,ol,om,nl,nm),(hunk,om,oh,nm,nh)];continue
            if len(encode(item))>LIMIT-2000:
                coverage['skipped'].append({'path':file['path'],'code':'CHANGE_INPUT_LIMIT','reason':'单条修改原文超过输入预算','start_line':nl+1,'end_line':nh});continue
            used=item['before']['code']+'\n'+item['after']['code']
            symbols=changed_symbols(snapshot,views,own)
            if len(encode(dict(item,changed_symbols=symbols)))<=LIMIT-2000:item['changed_symbols']=symbols
            candidates=[]
            for side,version in (('old','before'),('new','after')):
                candidates.extend(views[side].dependencies(snapshot,version,used,spans[side]))
            headers,notes=header_context(header_cache,client,snapshot,file,used)
            diagnostics.extend(notes);candidates.extend(headers)
            item['header_needs']=[{'path':n['path'],'version':'before' if n['side']=='old' else 'after','reason':n['reason']}
                                  for n in notes if n.get('affects_review')]
            item['context_gaps']=[n['version']+' 头文件尚未读取：'+n['path']+'（'+n['reason']+'）' for n in item['header_needs']]
            for candidate in candidates:
                item['context'].append(candidate)
                if len(encode(item))>32000:item['context'].pop()
            # 只列出实际出现且尚无函数体的调用，避免让模型误以为声明即完整契约。
            needs=[]
            for side,version in (('old','before'),('new','after')):
                view=views[side];a,b=spans[side]
                calls=[]
                for node in view.nodes:
                    if node.type!='call_expression' or not a<=node.start_point.row<b:continue
                    callee=node.child_by_field_name('function')
                    if callee is not None and callee.type=='identifier':calls.append(view.code(callee))
                provided='\n'.join(s['code'] for s in [item[version],*item['context']] if s['version']==version)
                for name in dict.fromkeys(calls):
                    if re.search(r'\b'+re.escape(name)+r'\s*\([^;{}]*\)\s*\{',provided):continue
                    needs.append({'symbol':name,'version':version,'reason':'仅有调用或声明，尚未提供实现；需核对其返回范围、前置条件和副作用'})
            item['context_needs']=needs[:16]
            while item['context'] and len(encode(item))>LIMIT:item['context'].pop()
            while item['context_needs'] and len(encode(item))>LIMIT:item['context_needs'].pop()
            for a in own:
                src=item['before' if a['side']=='old' else 'after'];rows=src['code'].split('\n');index=a['line']-src['start']
                if not 0<=index<len(rows) or rows[index].removesuffix('\r')!=a['quote']:
                    raise ReviewError('变更原文供给核对失败，停止错误覆盖计数')
                view=views[a['side']];function=view.function(a['line']-1,a['line'])
                scope_rows=rows
                if function and not function[1].has_error:
                    lo,hi=view.span(function[1]);scope_rows=view.lines[lo:hi]
                a['ambiguous']=sum(line.removesuffix('\r')==a['quote'] for line in scope_rows)>1
                supplied.add(a['id'])
            items.append(item)
        if on_file:
            on_file(items)
            items=[]
    coverage['diagnostics']=list({encode(d):d for d in diagnostics}.values())[:200]
    coverage['change_evidence']={'expected':len(required),'supplied':len(supplied)}
    coverage['complete']=not coverage['skipped'] and required==supplied
    return items
