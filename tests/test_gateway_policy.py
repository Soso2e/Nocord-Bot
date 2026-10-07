import unittest
from core.gateway_config import ArchivePolicy


class PolicyTests(unittest.TestCase):
    def test_default_deny_and_explicit_parent_consent(self):
        self.assertFalse(ArchivePolicy({}).allows('10', '20'))
        p = ArchivePolicy({'enabled': True, 'allow_guilds': {'10': {'channels': ['20']}}})
        self.assertTrue(p.allows('10', '20', '40'))
        self.assertFalse(p.allows('11', '20'))
        self.assertFalse(p.allows('10', '21'))
        self.assertFalse(p.allows(None, '20'))
        clause, params = p.sql_scope()
        self.assertEqual(params, ['10', '20'])
        self.assertIn('guild_id', clause)

    def test_zone_paths_are_independent(self):
        personal = ArchivePolicy({'trust_zone': 'personal'})
        school = ArchivePolicy({'trust_zone': 'school'})
        self.assertNotEqual(personal.database_path(), school.database_path())
