import ssl

import requests
from requests.adapters import HTTPAdapter

from helpers.ca_bundle import ca_bundle


class LegacyRenegotiationAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        context = ssl.create_default_context(cafile=ca_bundle())
        context.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)
        kwargs["ssl_context"] = context
        return super().init_poolmanager(*args, **kwargs)


def legacy_session(origin):
    session = requests.Session()
    session.mount(origin, LegacyRenegotiationAdapter())
    return session
