"""Hidden Windows launch with bounded local logs and Task Scheduler failure codes."""
import asyncio
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class LogStream:
    def __init__(self, logger, secrets):
        self.logger, self.secrets, self.pending = logger, secrets, ''

    def write(self, text):
        self.pending += text
        while '\n' in self.pending:
            line, self.pending = self.pending.split('\n', 1)
            for secret in self.secrets:
                line = line.replace(secret, '[REDACTED]')
            if line.strip():
                self.logger.info(line.rstrip())
        return len(text)

    def flush(self):
        if self.pending:
            self.write('\n')

    def isatty(self):
        return False


def run():
    from dotenv import dotenv_values, load_dotenv
    from core.process_lock import AlreadyRunning, ProcessLock
    os.chdir(ROOT)
    load_dotenv(ROOT / '.env', encoding='utf-8-sig')
    values = dotenv_values(ROOT / '.env', encoding='utf-8-sig')
    secrets = [v for k,v in values.items() if v and len(v) >= 8 and
               any(word in k.upper() for word in ('TOKEN', 'KEY', 'SECRET', 'PASSWORD'))]
    secrets += [v for k,v in os.environ.items() if len(v) >= 8 and
                any(word in k.upper() for word in ('TOKEN', 'KEY', 'SECRET', 'PASSWORD'))]
    log_dir = ROOT / 'data' / 'runtime'
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger('nocord.launcher')
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = RotatingFileHandler(log_dir / 'nocord.log', maxBytes=1_000_000, backupCount=3, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
    logger.addHandler(handler)
    stream = LogStream(logger, secrets)
    sys.stdout = sys.stderr = stream
    try:
        with ProcessLock(log_dir / 'bot.lock'):
            token = os.environ.get('DISCORD_TOKEN', '')
            if not token or token.lower().startswith(('your_', '取得した', 'コピーした')):
                logger.error('DISCORD_TOKEN is not configured; waiting for local .env update.')
                return 1
            import main
            logger.info('Nocord starting')
            asyncio.run(main.main())
    except AlreadyRunning:
        logger.info('Nocord already running; duplicate launch skipped.')
        return 0
    except KeyboardInterrupt:
        logger.info('Nocord stopped by user')
        return 0
    except Exception as exc:
        logger.error('Nocord stopped (%s); Task Scheduler may restart it.', type(exc).__name__)
        return 1
    finally:
        stream.flush()
    return 0


if __name__ == '__main__':
    sys.exit(run())
