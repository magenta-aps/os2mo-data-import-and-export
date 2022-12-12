import httpx
import lxml.etree
import xmltodict

from sdlon.date_utils import datetime_to_sd_date
from sdlon.models import SDGetDepartmentReq, SDGetDepartmentResp, SDAuth, SDDepartment

BASE_URL = "https://service.sd.dk/sdws/"


class SDClient:
    def __init__(self, sd_username: str, sd_password: str):
        self.username = sd_username
        self.password = sd_password

    def get_department(self, query_params: SDGetDepartmentReq) -> SDGetDepartmentResp:
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

        url = BASE_URL + type(query_params).__name__[2:][:-3] + "20111201"
        # TODO: make ENV for timeout
        r = httpx.get(
            url, params=params, auth=(self.username, self.password), timeout=120
        )

        # Nice for debugging
        # sd_xml_resp = lxml.etree.XML(r.text.split(">", maxsplit=1)[1])
        # xml = lxml.etree.tostring(sd_xml_resp, pretty_print=True).decode("utf-8")
        # print(xml)

        # TODO: handle XML errors
        xml_to_ordered_dict = xmltodict.parse(r.text)
        root_elem = xml_to_ordered_dict.get("GetDepartment20111201")

        departments = root_elem.get("Department")
        dep_list = departments if isinstance(departments, list) else [departments]

        return SDGetDepartmentResp(
            region_identifier=root_elem.get("RegionIdentifier"),
            region_uuid_identifier=root_elem.get("RegionUUIDIdentifier"),
            institution_identifier=root_elem.get("InstitutionIdentifier"),
            institution_uuid_identifier=root_elem.get("InstitutionUUIDIdentifier"),
            departments=[
                SDDepartment.parse_obj(dep) for dep in dep_list
            ]
        )
