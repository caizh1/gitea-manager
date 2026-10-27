"""大 PR 的非分页评论回读；不得循环收集同一批或放行损坏集合。"""
import unittest
from unittest.mock import Mock

from ai_review.common import ReviewError
from ai_review.gitea import Gitea


class ReviewCommentsTests(unittest.TestCase):
    path = '/repos/test/code/pulls/3/reviews/1/comments'

    def client(self, rows, total=None):
        client = Gitea.__new__(Gitea)
        client.page_limit = None
        client.response_total = total
        client.request = Mock(return_value=rows)
        return client

    def test_observed_121_comments_are_returned_once_without_paging(self):
        rows = [{'id': i, 'body': '固定审查意见'} for i in range(1, 122)]
        client = self.client(rows)
        self.assertEqual(client.pages(self.path), rows)
        client.request.assert_called_once_with('GET', self.path)

    def test_empty_complete_collection_is_valid(self):
        client = self.client([])
        self.assertEqual(client.pages(self.path), [])
        client.request.assert_called_once()

    def test_malformed_duplicates_and_wrong_count_are_rejected(self):
        for rows, total in [(None, None), ({'items': []}, None), ([{}], None),
                            ([{'id': True}], None), ([{'id': 0}], None),
                            ([{'id': 1}, {'id': 1}], None), ([{'id': 1}], 2)]:
            with self.subTest(rows=rows, total=total), self.assertRaises(ReviewError):
                self.client(rows, total).pages(self.path)

    def test_regular_review_listing_still_reads_every_page(self):
        client = self.client(None)
        client.page_limit = 2
        client.request.side_effect = [[{'id': 1}, {'id': 2}], [{'id': 3}]]
        path = '/repos/test/code/pulls/3/reviews'
        self.assertEqual(client.pages(path, limit=2), [{'id': 1}, {'id': 2}, {'id': 3}])
        self.assertEqual(client.request.call_count, 2)


if __name__ == '__main__':
    unittest.main()
