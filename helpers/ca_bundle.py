import os
import tempfile

import certifi

INTERMEDIATES = ("sectigo_dv_r36.pem", "godaddy_g2.pem")
CERTS_DIR = os.path.join(os.path.dirname(__file__), "..", "core", "certs")

_bundle_path = None


def ca_bundle():
    global _bundle_path
    if _bundle_path and os.path.exists(_bundle_path):
        return _bundle_path

    parts = [open(certifi.where(), encoding="utf-8").read()]
    for name in INTERMEDIATES:
        with open(os.path.join(CERTS_DIR, name), encoding="utf-8") as handle:
            parts.append(handle.read())

    path = os.path.join(tempfile.gettempdir(), "secureflow_ca_bundle.pem")
    with open(path, "w", encoding="utf-8") as out:
        out.write("\n".join(parts))

    _bundle_path = path
    return path
