"""Warm the explicitly configured local routing model before the bot starts."""
import json
import os
from pathlib import Path
import shutil
import subprocess
from urllib.parse import urlparse

import httpx

from core.llm_client import _load_config


def prepare(logger):
    cfg = _load_config().get('routing_llm', {})
    managed = cfg.get('managed_lms')
    if not managed:
        return
    address = urlparse(cfg.get('base_url', ''))
    if address.hostname != '127.0.0.1' or address.scheme != 'http' or not address.port:
        raise ValueError('Managed router must use an explicit loopback HTTP port')
    cli = shutil.which('lms') or str(Path.home() / '.lmstudio' / 'bin' / 'lms.exe')
    temporary = Path(__file__).resolve().parent.parent / 'data' / 'runtime' / 'router-temp'
    temporary.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ, TEMP=str(temporary), TMP=str(temporary))
    def command(*args):
        result = subprocess.run([cli, *args], env=environment, capture_output=True,
                                encoding='utf-8', errors='replace', timeout=45,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        if result.returncode:
            raise RuntimeError('Local routing runtime command failed')
        return result.stdout
    command('daemon', 'up')
    command('server', 'start', '--port', str(address.port), '--bind', '127.0.0.1')
    loaded = json.loads(command('ps', '--json'))
    if not any(model.get('identifier') == cfg['model'] for model in loaded):
        command('load', managed['model_key'], '--identifier', cfg['model'],
                '--gpu', managed.get('gpu', 'max'), '--context-length',
                str(managed.get('context_length', 2048)), '-y')
    # Cold-load and first-inference costs happen here, outside the one-second decision budget.
    headers = {'Authorization': 'Bearer ' + cfg['api_key']} if cfg.get('api_key') else {}
    with httpx.Client(timeout=5) as client:
        response = client.post(cfg['base_url'].rstrip('/') + '/v1/chat/completions', headers=headers,
                               json={'model': cfg['model'], 'messages': [{'role': 'user', 'content': 'Return 0. /no_think'}],
                                     **cfg.get('options', {})})
        response.raise_for_status()
    logger.info('Routing model warmed: %s', cfg['model'])
