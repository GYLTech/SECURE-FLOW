from helpers.hc_portals.base import PortalCaseNotFound, PortalError
from helpers.hc_portals import ahc, aphc, bhc, dhc, ghc, mlhc, mphc, phhc, rhc, skhc

PORTALS = {
    module.STATE_CODE: module
    for module in (mphc, phhc, dhc, mlhc, ahc, aphc, rhc, ghc, skhc, bhc)
}


def portal_for(state_code):
    return PORTALS.get(str(state_code or "").strip())


__all__ = ["PORTALS", "portal_for", "PortalCaseNotFound", "PortalError"]
