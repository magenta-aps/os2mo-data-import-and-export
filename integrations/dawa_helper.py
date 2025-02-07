from typing import Optional

from fastramqpi.os2mo_dar_client import AsyncDARClient


async def dawa_lookup(street_name: str, postal_code: str) -> Optional[str]:
    """Lookup an address object in DAWA and try to find an UUID for the address.

    Note: `street_name` is a misleading parameter name, as it should contain the street
          name along with house number, door, story, etc.

    Args:
        street_name: Address string without postal code, city or country.
        postal_code: Postal code for the street_name.

    Returns:
        DAWA UUID for the address, or None if it is not uniquely found.
    """
    combined_address_string = f"{street_name}, {postal_code}"

    dar_uuid = None
    try:
        adarclient = AsyncDARClient()
        async with adarclient:
            dar_reply = await adarclient.cleanse_single(combined_address_string)
            dar_uuid = dar_reply["id"]
    except Exception as exp:
        print(exp, " during dawa_lookup")
    return dar_uuid
