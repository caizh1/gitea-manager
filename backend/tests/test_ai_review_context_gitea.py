"""固定 Git 对象的分页、类型及身份校验；不以协议替身替代真实模型评分。"""
import base64
import hashlib
import unittest
from unittest.mock import Mock, patch

from ai_review.common import ReviewError
from ai_review.gitea import Gitea


class ContextGiteaTests(unittest.TestCase):
    def client(self):
        client=object.__new__(Gitea);client.prefix='/repos/device/firmware';client.max_file_bytes=2*1024*1024
        return client

    def entry(self,index,mode='100644'):
        return {'type':'blob','mode':mode,'path':f'src/module_{index}.c','sha':'c'*40,'size':100}

    def page(self,rows,total=205,sha='d'*40):
        return {'tree':rows,'sha':sha,'total_count':total,'truncated':len(rows)<total}

    def test_directory_pagination_excludes_links_and_preserves_all_regular_files(self):
        client=self.client();rows=[self.entry(i) for i in range(205)];rows[2]['mode']='120000'
        client.request=Mock(side_effect=[self.page(rows[:100]),self.page(rows[100:200]),self.page(rows[200:])])
        result=client.context_tree('a'*40)
        self.assertTrue(result['complete']);self.assertEqual(len(result['files']),204)
        self.assertNotIn('src/module_2.c',result['files'])
        self.assertEqual([c.kwargs['params']['page'] for c in client.request.call_args_list],[1,2,3])
        self.assertTrue(all(c.args[1].endswith('a'*40) for c in client.request.call_args_list))

    def test_changed_tree_duplicate_page_and_changed_count_fail_closed(self):
        first=self.page([self.entry(0)],total=2)
        for second in (self.page([self.entry(1)],total=2,sha='e'*40),
                       self.page([self.entry(0)],total=2),self.page([self.entry(1)],total=3)):
            client=self.client();client.request=Mock(side_effect=[first,second])
            with self.subTest(响应=second),self.assertRaises(ReviewError):client.context_tree('a'*40)

    def test_directory_deadline_retains_partial_index_as_incomplete(self):
        client=self.client();client.request=Mock(side_effect=[self.page([self.entry(0)],total=2),self.page([self.entry(1)],total=2)])
        with patch('time.monotonic',side_effect=[0,13,26]):result=client.context_tree('a'*40)
        self.assertTrue(result['complete']);self.assertEqual(len(result['files']),2)

    def test_file_requires_valid_blob_size_encoding_and_regular_type(self):
        raw='// 固定版本\r\nint value;\r\n'.encode()
        value={'type':'file','encoding':'base64','size':len(raw),'content':base64.b64encode(raw).decode(),
               'sha':hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()}
        client=self.client();client.request=Mock(return_value=value)
        self.assertEqual(client.file('src/device.c','a'*40),raw)
        for changes in ({'sha':'e'*40},{'size':len(raw)+1},{'type':'symlink'},
                        {'content':value['content']+'!'}, {'size':2*1024*1024+1}):
            client.request=Mock(return_value=dict(value,**changes))
            with self.subTest(改变=changes),self.assertRaises(ReviewError):client.file('src/device.c','a'*40)


if __name__=='__main__':unittest.main()
