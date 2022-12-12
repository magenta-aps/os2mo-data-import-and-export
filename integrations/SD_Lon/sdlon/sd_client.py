from typing import List

import httpx

from sdlon.date_utils import datetime_to_sd_date
from sdlon.models import SDGetDepartmentReq, SDGetDepartmentResp, SDAuth

BASE_URL = "https://service.sd.dk/sdws/"


def get_department(query_params: SDGetDepartmentReq, auth: SDAuth) -> List[SDGetDepartmentResp]:
    # TODO: generalize
    # TODO: handle request errors
    params = query_params.dict()
    params = {key: str(value) for key, value in params.items() if value is not None}
    params.update(
        {
            "ActivationDate": datetime_to_sd_date(query_params.ActivationDate),
            "DeactivationDate": datetime_to_sd_date(query_params.DeactivationDate),
        }
    )

    print(params)

    url = BASE_URL + type(query_params).__name__[2:][:-3] + "20111201"
    r = httpx.get(
        url, params=params, auth=(auth.username, auth.password.get_secret_value()),
        timeout=120
    )
    print(r.text)
