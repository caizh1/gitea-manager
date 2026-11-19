"""研发上库容易误判的提交和缺陷边界。"""
import unittest
from datetime import datetime
from unittest.mock import patch

from engineering_activity.collector import feeds, reconstruct
from engineering_activity.git_data import line_counts
from engineering_activity.rules import bug_events, day, person, utc


def push(identifier, ref, new, old=None, created='2026-11-19T16:00:00Z'):
    import json
    return {'id': identifier, 'op_type': 'commit_repo', 'ref_name': f'refs/heads/{ref}',
            'act_user_id': 8, 'created': created,
            'content': json.dumps({'HeadCommit': {'Sha1': new}, 'CompareURL': f'/compare/{old}...{new}'}) if new else ''}


class EngineeringRulesTests(unittest.TestCase):
    def test_china_midnight_uses_push_time(self):
        self.assertEqual(day(utc('2026-11-19T15:59:59Z')), '2026-11-19')
        self.assertEqual(day(utc('2026-11-19T16:00:00Z')), '2026-11-20')

    def test_feed_marker_can_arrive_between_newer_ids_in_same_second(self):
        initial = push(10, 'main', 'a' * 40)
        newer = push(13, 'main', 'b' * 40, 'a' * 40)
        seen = push(7, 'main', None)
        with patch('engineering_activity.collector.page_rows', return_value=iter([seen, initial, newer])):
            self.assertEqual(feeds(None, '', initial['id'], datetime(2026, 11, 18)), ([newer], 13, True))

    def test_multiple_branch_projection_replays_force_and_copies(self):
        a, b, c = ('a' * 40, 'b' * 40, 'c' * 40)
        old = {'refs/heads/main': a}
        head = {'refs/heads/main': c, 'refs/heads/feature': b}
        replay, verified = reconstruct([push(12, 'feature', None), push(13, 'feature', b),
                                       push(14, 'main', c, a)], head, old, True)
        self.assertTrue(verified)
        self.assertEqual((replay[0][4], set(replay[1][4])), ((a,), {a, b}))
        self.assertEqual(reconstruct([push(14, 'main', c, a)], head, old, True), ([], False))

    def test_bug_reopens_then_restores_closing_time_assignee(self):
        members = [{'id': 1, 'login': 'first'}, {'id': 2, 'login': 'second'}]
        prior, final = '2026-11-19T08:00:00Z', '2026-11-20T08:00:00Z'
        issue = {'number': 2, 'state': 'closed', 'assignees': [members[1]]}
        timeline = [{'type': 'close', 'id': 1, 'created_at': prior}, {'type': 'reopen', 'id': 2, 'created_at': prior},
                    {'type': 'close', 'id': 3, 'created_at': final}, {'type': 'assignees', 'id': 4, 'created_at': final,
                     'assignee': members[0], 'removed_assignee': True},
                    {'type': 'assignees', 'id': 5, 'created_at': final, 'assignee': members[1]}]
        fixed, reopens = bug_events(issue, timeline, lambda *_: None)
        self.assertEqual((fixed[0]['id'], fixed[1]['id'], reopens[0]['id']), (3, 1, 2))
        self.assertIsNone(bug_events({**issue, 'state': 'open'}, timeline, lambda *_: None)[0])

    def test_line_classification_respects_firmware_and_repository_paths(self):
        lines = line_counts([['a.ld', 2, 3], ['board/start.S', 1, 1], ['sdk/source.c', 9, 0],
                             ['test/generated.c', 4, 0], ['build/a.c', 6, 0], ['README.md', 8, 0],
                             ['logo.png', None, None]], {'third_party': ['sdk']})
        self.assertEqual(lines['code'], [7, 4])
        self.assertEqual(lines['doc'], [8, 0])
        self.assertEqual(lines['generated'], [6, 0])
        self.assertEqual(lines['third_party'], [9, 0])
        self.assertEqual(lines['binary'], [1, 0])

    def test_display_name_is_not_identity(self):
        self.assertNotEqual(person({'id': 1, 'full_name': '同名'})[0], person({'id': 2, 'full_name': '同名'})[0])


if __name__ == '__main__':
    unittest.main()
