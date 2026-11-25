"""The device authorization flow shared by desktop and console workers."""

from pathlib import Path
from tempfile import TemporaryDirectory

from kindlegarten.cli import KindleError, is_amazon_callback
from kindlegarten.i18n import tr
from kindlegarten.library import library_cache_path
from kindlegarten.marketplaces import MARKETPLACES, extend_kindl_regions
from kindlegarten.network import amazon_client


def prepare_login(region):
    from kindl.auth import new_device_identity, pkce_pair
    from kindl.client import AmazonDeviceClient
    extend_kindl_regions()
    market = MARKETPLACES[region]
    serial, pid = new_device_identity()
    verifier, challenge = pkce_pair()
    return {
        "region": region, "site": market.site, "serial": serial, "pid": pid, "verifier": verifier,
        "url": AmazonDeviceClient.login_url(serial, challenge, region=region,
                                           language=market.locale),
    }


def saved_session_valid(state):
    return saved_account(state) is not None


def saved_account(state):
    from httpx import HTTPStatusError
    from kindl.auth import AccountStore

    try:
        account = AccountStore.load(state).get("ipad")
    except (KeyError, RuntimeError, TypeError, ValueError):
        return None
    if account is None:
        return None
    with amazon_client(account.credentials) as client:
        try:
            client.library(limit=1)
        except HTTPStatusError as error:
            if error.response.status_code in {401, 403}:
                return None
            raise
    identifier = account.credentials.directed_id
    return {"id": f"•••{identifier[-6:]}"} if identifier else {}


def register(state, authorization, callback):
    from kindl.auth import Account, AccountStore

    if not is_amazon_callback(callback, authorization["site"]):
        raise KindleError(tr("The pasted URL does not contain the expected Amazon authorization code."))
    with amazon_client(region=authorization["region"]) as client:
        credentials = client.register(callback, authorization["verifier"], authorization["serial"], authorization["pid"])
    with amazon_client(credentials) as client:
        client.library(limit=1)
    store = AccountStore()
    store.add(Account("ipad", credentials))
    with TemporaryDirectory(dir=state.parent) as temporary:
        verified = Path(temporary) / state.name
        store.save(verified)
        library_cache_path(state.parent, "amazon", authorization["region"]).unlink(missing_ok=True)
        verified.replace(state)
