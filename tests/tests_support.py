import tempfile
import unittest
from pathlib import Path
from core.discord_archive import DiscordArchive
from core.gateway_config import ArchivePolicy


class ArchiveTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.archive = DiscordArchive(Path(self.tmp.name) / 'archive.sqlite')
        self.policy = ArchivePolicy({'enabled': True, 'allow_guilds': {'10': {'channels': ['20']}}})
