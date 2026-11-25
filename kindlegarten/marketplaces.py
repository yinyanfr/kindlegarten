"""Kindle storefronts and their sign-in settings."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Marketplace:
    name: str
    site: str
    locale: str


MARKETPLACES = {
    "jp": Marketplace("Japan", "amazon.co.jp", "ja_JP"),
    "us": Marketplace("United States", "amazon.com", "en_US"),
    "uk": Marketplace("United Kingdom", "amazon.co.uk", "en_GB"),
    "de": Marketplace("Germany", "amazon.de", "de_DE"),
    "fr": Marketplace("France", "amazon.fr", "fr_FR"),
    "it": Marketplace("Italy", "amazon.it", "it_IT"),
    "es": Marketplace("Spain", "amazon.es", "es_ES"),
    "nl": Marketplace("Netherlands", "amazon.nl", "nl_NL"),
    "ca": Marketplace("Canada", "amazon.ca", "en_CA"),
    "au": Marketplace("Australia", "amazon.com.au", "en_AU"),
    "in": Marketplace("India", "amazon.in", "en_IN"),
    "br": Marketplace("Brazil", "amazon.com.br", "pt_BR"),
    "mx": Marketplace("Mexico", "amazon.com.mx", "es_MX"),
}
REGIONS_BY_SITE = {market.site: code for code, market in MARKETPLACES.items()}


def extend_kindl_regions():
    # The bundled backend defines US and JP. Keep those entries and register the additional stores.
    from kindl.regions import REGIONS, Region

    for code, market in MARKETPLACES.items():
        site = market.site
        origin = f"https://www.{site}"
        REGIONS.setdefault(code, Region(
            code=code,
            site=site,
            signin_url=f"{origin}/ap/signin",
            return_to=f"{origin}/",
            assoc_handle=f"amzn_mazama_desktop_{code}",
            identity_host=f"api.{site}",
            cookie_domain=f".{site}",
        ))
