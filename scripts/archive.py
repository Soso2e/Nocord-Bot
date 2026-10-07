"""Local status, derived index rebuild, and trust-zone backup/restore."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.discord_archive import DiscordArchive
from core.archive_operations import archive_status, backup_archive, restore_archive
from core.archive_search import create_search
from core.gateway_config import load_policy


def main():
    from dotenv import load_dotenv
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['status', 'reindex', 'backup', 'restore'])
    parser.add_argument('--path', help='Backup file or restore source; keep inside the same trust zone')
    args = parser.parse_args()
    policy = load_policy()
    if not policy.enabled:
        parser.error('Archive disabled in selected NOCORD_CONFIG')
    if args.command == 'restore':
        if not args.path:
            parser.error('--path required')
        restore_archive(args.path, policy, policy.database_path())
        print('Archive restored in the configured trust zone.')
        return
    archive = DiscordArchive(policy.database_path())
    if args.command == 'status':
        print(json.dumps(archive_status(archive, policy), ensure_ascii=True, indent=2))
    elif args.command == 'backup':
        if not args.path:
            parser.error('--path required')
        backup_archive(archive, policy, args.path)
        print('Local archive backup completed.')
    else:
        search = create_search(archive, policy)
        search.rebuild_fts()
        if search.embed:
            search.index_pending(rebuild=True)
            while search.index_pending():
                pass
        print('Derived indexes rebuilt from Archive.')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'Archive operation failed ({type(exc).__name__}); inspect local configuration.', file=sys.stderr)
        sys.exit(1)
