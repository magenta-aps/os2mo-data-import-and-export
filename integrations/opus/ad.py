import requests
from tenacity import retry
from tenacity import stop_after_attempt
from tenacity import wait_fixed


class LdapADGUIDReader:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port

    @retry(wait=wait_fixed(120), stop=stop_after_attempt(3))
    def read_user(self, cpr: str) -> dict[str, str]:
        """
        Get the ADGUID via the LDAP integration. For now, we will return a dictionary
        to be compatible with the old integration to the AD.

        Args:
             cpr: The CPR of the person to get the AD info from

        Returns:
            Dictionary containing the ADGUID of the AD person.
        """

        # No error handling - if this fails, we will fail hard.
        r = requests.get(
            f"http://{self.host}:{self.port}/SD", params={"cpr_number": cpr}
        )

        return {"ObjectGuid": r.json().get("uuid")}
