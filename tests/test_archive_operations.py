from pathlib import Path
import sqlite3
from tests_support import ArchiveTestCase
from test_discord_archive import message
from core.discord_archive import DiscordArchive
from core.gateway_config import ArchivePolicy
from core.archive_operations import archive_status, backup_archive, restore_archive
from core.archive_search import ArchiveSearch


class OperationsTests(ArchiveTestCase):
    async def test_backup_restore_zone_and_integrity(self):
        self.archive.upsert_message(message())
        backup = Path(self.tmp.name) / 'backup.sqlite'
        backup_archive(self.archive, self.policy, backup)
        restored = Path(self.tmp.name) / 'restored.sqlite'
        restore_archive(backup, self.policy, restored)
        archive = DiscordArchive(restored)
        self.assertEqual(archive.get_message('1')['content'], 'WEVORA')
        with self.assertRaises(ValueError):
            restore_archive(backup, ArchivePolicy({'trust_zone': 'school'}), Path(self.tmp.name) / 'school.sqlite')
        with self.assertRaises(ValueError):
            backup_archive(self.archive, self.policy, backup)

    async def test_health_has_no_content_or_path(self):
        self.archive.upsert_message(message())
        status = archive_status(self.archive, self.policy)
        self.assertTrue(status['healthy'])
        self.assertEqual(status['message_count'], 1)
        self.assertEqual(status['pending_index_count'], 1)
        self.assertNotIn('WEVORA', str(status))
        self.assertNotIn(str(self.archive.path), str(status))

    async def test_schema_reopen_migration_and_future_version_rejection(self):
        self.archive.upsert_message(message())
        # Recreate the released v1 shape before the private-thread migration.
        self.archive.upsert_message(message('2', thread_id='40'))
        with self.archive.connect() as db:
            db.execute('ALTER TABLE discord_messages DROP COLUMN is_private_thread')
            db.execute('PRAGMA user_version=1')
        self.archive = DiscordArchive(self.archive.path)
        self.assertEqual(self.archive.get_message('1')['is_private_thread'], 0)
        self.assertEqual(self.archive.get_message('2')['is_private_thread'], 1)
        with self.archive.connect() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 2)
        search = ArchiveSearch(self.archive, self.policy)
        reopened = DiscordArchive(self.archive.path)
        self.assertEqual(ArchiveSearch(reopened, self.policy).search('WEVORA')[0]['message_id'], '1')
        with self.assertRaises(ValueError):
            ArchiveSearch(reopened, ArchivePolicy({'trust_zone': 'school'}))
        with self.archive.connect() as db:
            db.execute('PRAGMA user_version=999')
        with self.assertRaises(ValueError):
            DiscordArchive(self.archive.path)
