import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

from core.process_lock import ProcessLock, AlreadyRunning
from scripts.run_bot import LogStream
from main import _monitor_interfaces


class StartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_lock_rejects_duplicate_and_releases(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'bot.lock'
            with ProcessLock(path):
                with self.assertRaises(AlreadyRunning):
                    with ProcessLock(path):
                        pass
            with ProcessLock(path):
                pass

    async def test_dead_interface_fails_for_scheduler_restart(self):
        with self.assertRaises(RuntimeError):
            await _monitor_interfaces([NS(is_alive=lambda: False)], [])

    async def test_failure_does_not_expose_backend_exception(self):
        async def fail():
            raise ValueError('private token')
        task = asyncio.create_task(fail())
        await asyncio.sleep(0)
        with self.assertRaises(RuntimeError) as error:
            await _monitor_interfaces([], [task])
        self.assertNotIn('private token', str(error.exception))

    async def test_logs_redact_secrets_across_write_fragments(self):
        logger = Mock()
        stream = LogStream(logger, ['bot-secret'])
        stream.write('Token: bot-')
        stream.write('secret\nready\n')
        self.assertEqual(logger.info.call_args_list[0].args[0], 'Token: [REDACTED]')
        self.assertEqual(logger.info.call_args_list[1].args[0], 'ready')
