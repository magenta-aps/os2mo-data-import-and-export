import json
import logging


logger = logging.getLogger(__name__)


class Session:
    def raise_for_status(self):
        pass

    def get(self, *args, **kwargs):
        logger.info("GET %r %r", args, kwargs)
        return self

    def delete(self, *args, **kwargs):
        logger.info("DELETE %r %r", args, kwargs)
        return self

    def post(self, *args, **kwargs):
        logger.info("POST %r %r", args, json.dumps(kwargs))
        return self
