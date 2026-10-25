#!/usr/bin/env python3
"""本机跨网段 Gitea 灾备验收入口，详见 docs/remote-backup-acceptance.md。"""
from remote_backup.host import main

if __name__ == '__main__':
    main()
