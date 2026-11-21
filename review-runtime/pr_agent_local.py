"""固定 Git 工作区中的原生 LocalGitProvider 和旧编码兼容。"""
from collections import Counter

from ai_review.common import ReviewError, digest
from ai_review.diff import parse_diff, quote_text, validate_patch
from ai_review.pr_agent_encoding import PROTOCOL, blocks, decode_blob, render_block, validate_config
from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.git_providers import local_git_provider
from pr_agent.git_providers.diff_parsing import to_hunk_only_patch


def change_digest(patch):
    rows = []
    for hunk in patch.hunks:
        old = hunk.old_start - bool(hunk.old_count)
        new = hunk.new_start - bool(hunk.new_count)
        for kind, raw in hunk.rows:
            old += kind != '+'
            new += kind != '-'
            if kind in ('+', '-'):
                rows.append((kind, new if kind == '+' else old, quote_text(raw)))
    return digest(sorted(rows))


def local_provider(task):
    """保留原生补丁计算。仅对原生 UTF-8 读取失败的文件补齐无损解码。"""
    encodings = validate_config(task['source_encodings'])
    partial_protocol = task.get('pr_agent_protocol') == PROTOCOL
    selected = set(task['review_paths']) if partial_protocol else None

    class FixedLocalGitProvider(local_git_provider.LocalGitProvider):
        def _prepare_repo(self):
            super()._prepare_repo()
            if (self.repo.head.commit.hexsha != task['head']
                    or self.repo.commit(self.target_branch_name).hexsha != task['merge_base']):
                raise ValueError('pr_agent_workspace_version_changed')

        def get_diff_files(self):
            if getattr(self, 'diff_files', None) is not None:
                return self.diff_files
            # 以固定提交的原始 Git 差异核对原生渲染，覆盖检查独立于过滤结果。
            raw = self.repo.git.diff('--no-ext-diff', '--no-textconv', '-M',
                                     task['merge_base'], task['head'], '--', stdout_as_string=False,
                                     strip_newline_in_stdout=False)
            records = {}
            counts = Counter()
            for block in blocks(raw):
                patch, = parse_diff(block)
                path = patch.new_path or patch.old_path
                counts.update(kind for hunk in patch.hunks for kind, _ in hunk.rows)
                if selected is not None and path not in selected:
                    continue
                if patch.unsupported or not patch.hunks:
                    raise ValueError('pr_agent_local_unsupported')
                if path in records:
                    raise ValueError('pr_agent_changes_missing')
                records[path] = patch, block
            if counts['+'] + counts['-'] > task['max_changed_lines']:
                raise ValueError('pr_agent_local_line_limit')

            if selected is not None:
                if set(records) != selected:
                    raise ValueError('pr_agent_changes_missing')
                self.diff_files, self.expected_changes = self._selected_files(records)
                self.filtered_diff_file_names = []
                self.change_counts = {'added': counts['+'], 'deleted': counts['-']}
                return self.diff_files
            files = super().get_diff_files()
            filtered = self.get_filtered_diff_file_names()
            if set(records) != {f.filename for f in files} or filtered:
                raise ValueError('pr_agent_changes_missing')
            expected = {}
            for file in files:
                patch, block = records[file.filename]
                if file.content_fetch_failed:
                    block = self._restore_legacy_text(file, patch, block)
                    patch, = parse_diff(block)
                expected[file.filename] = change_digest(patch)
            self.expected_changes = expected
            self.change_counts = {'added': counts['+'], 'deleted': counts['-']}
            return files

        def _selected_files(self, records):
            # 跳过的大二进制可能超出内存预算，只读取已纳入的文件，并保留原生动态上下文。
            files, expected = [], {}
            trees = {commit: self.repo.commit(commit).tree for commit in (task['merge_base'], task['head'])}
            for path, (patch, block) in records.items():
                if not patch.old_path:
                    edit_type = EDIT_TYPE.ADDED
                elif not patch.new_path:
                    edit_type = EDIT_TYPE.DELETED
                elif patch.old_path != patch.new_path:
                    edit_type = EDIT_TYPE.RENAMED
                else:
                    edit_type = EDIT_TYPE.MODIFIED
                file = FilePatchInfo('', '', '', path, edit_type=edit_type,
                                     old_filename=patch.old_path if patch.old_path != patch.new_path else None)
                normalized = self._restore_legacy_text(file, patch, block, trees)
                verified, = parse_diff(normalized)
                expected[path] = change_digest(verified)
                files.append(file)
            return files, expected

        def _restore_legacy_text(self, file, patch, block, trees=None):
            def read(commit, path):
                if path is None:
                    return b''
                tree = trees[commit] if trees else self.repo.commit(commit).tree
                blob = tree / path
                if blob.type != 'blob' or blob.mode not in (0o100644, 0o100755):
                    raise ValueError('pr_agent_local_unsupported')
                return blob.data_stream.read()

            old = read(task['merge_base'], patch.old_path)
            new = read(task['head'], patch.new_path)
            try:
                file.base_file, old_codec = decode_blob(old, patch.old_path or file.filename, '旧侧', encodings,
                                                        reject_mixed=partial_protocol)
                file.head_file, new_codec = decode_blob(new, patch.new_path or file.filename, '新侧', encodings,
                                                        reject_mixed=partial_protocol)
                codecs = {'old': old_codec, 'new': new_codec}
                validate_patch(patch, old, new, 'pr_agent_local', codecs)
                normalized = render_block(block, patch, codecs)
            except (ReviewError, UnicodeError):
                raise ValueError('pr_agent_local_encoding') from None
            file.patch = to_hunk_only_patch(normalized)
            file.num_plus_lines = sum(kind == '+' for hunk in patch.hunks for kind, _ in hunk.rows)
            file.num_minus_lines = sum(kind == '-' for hunk in patch.hunks for kind, _ in hunk.rows)
            file.content_fetch_failed = False
            return normalized.encode()

    return FixedLocalGitProvider
