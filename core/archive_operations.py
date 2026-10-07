"""Small local maintenance entry points; no content or secrets in diagnostics."""
import logging
from core.discord_archive_sync import get_ingestor
from core.archive_search import create_search

log = logging.getLogger(__name__)


def index_once():
    try:
        ingestor = get_ingestor()
        if ingestor:
            return create_search(ingestor.archive, ingestor.policy).index_pending()
    except Exception as exc:
        log.warning('Archive index retry pending (%s)', type(exc).__name__)
    return 0
