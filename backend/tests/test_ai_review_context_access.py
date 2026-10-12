"""截图场景的原文供给验收：预期来自 Git 补丁，不从实现复制结果。"""
import difflib
import json
import unittest

from ai_review.diff import parse_diff, validate_patch
from ai_review.snapshot import work_items


def snapshot(old, new, path='src/device.c'):
    patch = parse_diff(('diff --git a/'+path+' b/'+path+'\n'+''.join(difflib.unified_diff(
        old.splitlines(True), new.splitlines(True), fromfile='a/'+path, tofile='b/'+path))).encode())[0]
    anchors = validate_patch(patch, old.encode(), new.encode(), '截图供给基准')
    value = {'id':'截图供给基准', 'analysis_version':4, 'merge_base':'a'*40, 'head':'b'*40,
             'files':[{'path':path, 'old_path':path if old else None, 'new_path':path if new else None, 'old':old, 'new':new,
                       'anchors':[a['id'] for a in anchors]}],
             'anchors':{a['id']:a for a in anchors}, 'coverage':{'skipped':[], 'complete':True}}
    # 只序列化已经由 validate_patch 逐字节核对的补丁，用于跨侧映射。
    value['files'][0]['patch'] = [{'old_start':h.old_start,'new_start':h.new_start,
                                  'old_count':h.old_count,'new_count':h.new_count,
                                  'kinds':[kind for kind,_ in h.rows]} for h in patch.hunks]
    return value


class ScreenshotContextTests(unittest.TestCase):
    def supplied(self, value):
        items = work_items(value)
        wanted = {a['id'] for a in value['anchors'].values() if a['quote'].strip()}
        actual = set()
        for item in items:
            self.assertLessEqual(len(json.dumps(item, ensure_ascii=False)), 48000)
            for anchor in item['anchors']:
                source = item['before' if anchor['side']=='old' else 'after']
                rows = source['code'].split('\n'); index = anchor['line']-source['start']
                self.assertGreaterEqual(index,0)
                self.assertLess(index,len(rows))
                self.assertEqual(rows[index].removesuffix('\r'),anchor['quote'])
                actual.add(anchor['id'])
        self.assertEqual(actual,wanted)
        return items

    def huge(self):
        return 'int dispatch(int x) {\n  int limit = 1;\n'+''.join(
            f'  x += {i};\n' for i in range(7000))+'  return x;\n}\n'

    def test_unrelated_macro_does_not_discard_global_change(self):
        old='typedef unsigned int u32;\n#define ATTR(x)\nATTR(tag) int unrelated(void) { return 0; }\n'
        added='#ifndef CONFIG_APP\nstatic u32 sequence = 0;\n#endif\n'
        value=snapshot(old,added+old)
        self.assertTrue(self.supplied(value))
        self.assertTrue(value['coverage']['complete'])

    def test_large_local_declaration_and_adjacent_expression_are_really_supplied(self):
        old=self.huge()
        for adjacent in (False,True):
            with self.subTest(相邻表达式=adjacent):
                new=old.replace('limit = 1','limit = 2')
                if adjacent:new=new.replace('x += 0;','x -= 0;')
                self.assertTrue(self.supplied(snapshot(old,new)))

    def test_insert_delete_keep_existing_other_version(self):
        old=self.huge()
        for new in (old.replace('  x += 50;','  x ^= limit;\n  x += 50;'),old.replace('  x += 50;\n','')):
            for item in self.supplied(snapshot(old,new)):
                self.assertTrue(item['before']['code'])
                self.assertTrue(item['after']['code'])

    def test_replacement_pairs_survive_old_twenty_four_anchor_boundary(self):
        old=self.huge()
        for count in (11,12,13,20):
            new=old
            for i in range(100,100+count):new=new.replace(f'x += {i};',f'x -= {i};')
            for item in self.supplied(snapshot(old,new)):
                self.assertTrue(item['before']['code']);self.assertTrue(item['after']['code'])

    def test_missing_brace_keeps_raw_diff_and_reports_parser_limitation(self):
        old='int f(void) {\nreturn 0;\n'
        value=snapshot(old,old.replace('return 0','return 1'))
        self.assertTrue(self.supplied(value))
        self.assertTrue(value['coverage'].get('diagnostics'))

    def test_new_and_deleted_files_have_explicit_absence(self):
        for old,new in (('', 'int f(void) { return 0; }\n'),('int f(void) { return 0; }\n','')):
            for item in self.supplied(snapshot(old,new)):
                side='before' if not old else 'after'
                self.assertEqual(item[side]['code'],'')
                self.assertEqual(item[side]['availability'],'absent')

    def test_existing_empty_file_is_not_reported_as_absent(self):
        value=snapshot('', 'int value;\n')
        value['files'][0]['old_path']='src/device.c'
        for item in self.supplied(value):
            self.assertEqual(item['before']['code'],'');self.assertEqual(item['before']['availability'],'present')

    def test_adjacent_functions_are_separate_without_splitting_replacements(self):
        old=''.join(f'int f_{i}(int x) {{ return x; }}\n' for i in range(9))
        new=old.replace('return x;', 'return x + 1;')
        for left,right in ((old,new),('',new),(old,'')):
            with self.subTest(新增=not left,删除=not right):
                items=self.supplied(snapshot(left,right))
                self.assertEqual(len(items),9)
                for item in items:
                    self.assertEqual(len({a['side'] for a in item['anchors']}),2 if left and right else 1)

    def test_identical_lines_in_different_functions_do_not_become_ambiguous(self):
        new='int a(void) {\n return 1;\n}\nint b(void) {\n return 1;\n}\n'
        items=self.supplied(snapshot('',new))
        returns=[a for item in items for a in item['anchors'] if a['quote']==' return 1;']
        self.assertEqual(len(returns),2)
        self.assertTrue(all(not a['ambiguous'] for a in returns))
        repeated='void a(void) {\n work();\n work();\n}\n'
        items=self.supplied(snapshot('',repeated))
        self.assertTrue(all(a['ambiguous'] for item in items for a in item['anchors'] if a['quote']==' work();'))

    def test_large_guarded_header_keeps_independent_required_declaration(self):
        class Client:
            def file(self,path,sha):
                return ('#ifndef DEVICE_H\n#define DEVICE_H\ntypedef unsigned int u32;\n'+''.join(
                    f'#define FILLER_{i} {i}\n' for i in range(1300))+
                    '#define ATTR(x)\nATTR(tag) int unrelated(void);\n#endif\n').encode()
        old='#include "device.h"\nu32 f(u32 x) { return x; }\n'
        items=work_items(snapshot(old,old.replace('return x','return x + 1')),Client())
        self.assertTrue(any('typedef unsigned int u32;' in s['code'] for i in items for s in i['context']))
        self.assertFalse(any('大头文件无法可靠解析' in str(i['context_gaps']) for i in items))


if __name__=='__main__':unittest.main()
