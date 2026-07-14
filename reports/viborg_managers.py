#
# Copyright (c) 2017-2018, Magenta ApS
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
#

import datetime
import json
import logging
import os
import pathlib

from anytree import PostOrderIter
from anytree import PreOrderIter
from fastramqpi.raclients.upload import file_uploader

from exporters.utils.priority_by_class import choose_public_address
from integrations.mora_helper import MoraHelper

"""
Rapportens opdrag: Tæl lederes medarbejdere, og den har udviklet sig lidt:

* En chef kan aldrig være chef for sig selv, defor tælles chefen ikke med
  iblandt sine egne ansatte
* Totalt fastlønnede akkumuleres på alle niveauer
* Totalt timelønnede akkumuleres på alle niveauer
* Direkte antal fastlønnede akkumuleres hos først opadfundne chef
* Direkte antal timelønnede akkumuleres hos først opadfundne chef
* Ledere akkumuleres som underordnede hos først opadfundne chef
* Medarbejdere tælles kun en gang i samme træ, men kan godt tælles med i 2 undertræer

Rapportens output er en csv-fil, som placeres i os2mo's rapport-directory
  ${QUERY_EXPORT_DIR}
"""
cfg_file = pathlib.Path.cwd() / "settings" / "settings.json"
if not cfg_file.is_file():
    raise Exception("No setting file")
settings = json.loads(cfg_file.read_text())

MORA_BASE = settings["mora.base"]
MORA_ROOT_ORG_UNIT_NAME = settings["municipality.name"]
LOG_LEVEL = logging._nameToLevel.get(os.environ.get("LOG_LEVEL", "WARNING"), 20)


logging.basicConfig(
    format="%(levelname)s %(asctime)s %(name)s %(message)s",
    level=LOG_LEVEL,
)

logger = logging.getLogger("viborg_managers")
for i in logging.root.manager.loggerDict:
    if i in ["mora-helper", "viborg_managers"]:
        logging.getLogger(i).setLevel(LOG_LEVEL)
    else:
        logging.getLogger(i).setLevel(logging.WARNING)


def hourly_paid(engagement):
    "workers hourly paid are determined by user_key prefix"
    hp = engagement["user_key"][0] in ["8", "9"]
    if hp:
        logger.info(
            "engagement %s with user_key %s considered hourly paid",
            engagement["uuid"],
            engagement["user_key"],
        )
    return hp


def find_people(mh, nodes):
    """forberedelser. der laves et rapport-dictionary på hver node
    det er dictionaries, da det ikke er tal vi opbevarer,
    men medarbejder-uuid-nøgler
    """
    for node in PostOrderIter(nodes["root"]):
        node.report = {
            "department": {},
            "m_dir_salary": {},
            "m_dir_subord": {},
            "e_dir_salary": {},
            "e_dir_hourly": {},
            "e_tot_salary": {},
            "e_tot_hourly": {},
        }

    for node in PostOrderIter(nodes["root"]):
        report = node.report
        ou = mh.read_ou(node.name)
        report["department"] = ou["uuid"]

        """ find denne afdelings managere og registrer dem hver især
        med person-uuid - som nøgle
        gør også det nødvendige for at finde deres egen afdeling
        """
        mana = False
        for manager in mh._mo_lookup(ou["uuid"], "ou/{}/details/manager"):
            if not manager.get("person"):
                logger.info("skipping vacant manager %s", manager)
                continue  # vacant manager
            else:
                mana = manager["person"]["uuid"]

            payload = {
                "manager": manager,
            }
            report["m_dir_salary"].setdefault(mana, payload)

        """ find alle engagementer i denne afdeling og opbevar
        medarbejdernes uuider som nøgler i dicts for henholdsvis
        timelønnede og funktionærer
        """

        for engagement in mh._mo_lookup(ou["uuid"], "ou/{}/details/engagement"):
            empl_e = engagement["person"]["uuid"]
            empl_ou = engagement["org_unit"]
            if hourly_paid(engagement):
                report["e_dir_hourly"].setdefault(empl_e, empl_ou)
            else:
                report["e_dir_salary"].setdefault(empl_e, empl_ou)

        """ overfør de, som er ansat direkte i denne afdeling til
        afdelingens total- og tæl managers med i denne sammentælling
        """

        node.report["e_tot_salary"].update(report["m_dir_salary"])
        node.report["e_tot_salary"].update(report["e_dir_salary"])
        node.report["e_tot_hourly"].update(report["e_dir_hourly"])

        """ overfør nu alle medarbejderuuider, så de også indgår i parentens
            dict. PostOrderIter sikrer at alle tal propagerer hele vejen op
        """

        if node.parent:
            """ totale antal employees skal altid med hele vejen op
            både timelønnede og fastlønnede.
            """

            node.parent.report["e_tot_salary"].update(report["e_tot_salary"])
            node.parent.report["e_tot_hourly"].update(report["e_tot_hourly"])

            """ managers her tilføjes som underordnede ledere i overordnet afd.
            ligesom de figurerer som direkte ansatte i overordn. afdeling
            """

            node.parent.report["m_dir_subord"].update(report["m_dir_salary"])
            node.parent.report["e_dir_salary"].update(report["m_dir_salary"])

            if not mana:
                """ Hvis denne afdeling ikke har nogen leder skal ansatte og ledere
                skubbes opad til næste overordnede afdeling:
                    * underordn. ledere overføres som direkte underordn. ledere
                    * fastlønnede overføres som dir. fastlønnede
                    * timelønneede overføres som dir. timelønnede
                """

                node.parent.report["m_dir_subord"].update(report["m_dir_subord"])
                node.parent.report["e_dir_salary"].update(report["e_dir_salary"])
                node.parent.report["e_dir_hourly"].update(report["e_dir_hourly"])


def prepare_report(mh, nodes):
    fieldnames = [
        "Leder",
        "Egen afd",
        "Email",
        "Direkte funktionær",
        "Heraf ledere",
        "Direkte timeløn",
        "Direkte ialt",
        "Samlet funktionær",
        "Samlet timeløn",
        "Samlet ialt",
        "Opgjort pr",
    ]
    rows = []
    opgjort_pr = datetime.datetime.now().strftime("%d/%m/%Y")
    sort_order = 0

    priority_list = settings.get("emus.email.priority", [])
    # priority_list = settings.get("exports_viborg_eksterne.email.priority", [])

    for node in PreOrderIter(nodes["root"]):
        sort_order = sort_order + 1

        """ 'manager' is the person-uuid of the person who is manager in this dept
        'payload' contains his manager and engagement-objects
        """
        for manager, payload in node.report["m_dir_salary"].items():
            # Fetch all email addresses, and use the best one
            _emails = mh.get_e_addresses(manager, "EMAIL")
            _email = choose_public_address(_emails, priority_list)

            row = {
                "sort_order": sort_order,
                "manager_uuid": payload["manager"]["person"]["uuid"],
                "Leder": payload["manager"]["person"]["name"],
                "Egen afd": payload["manager"]["org_unit"]["name"],
                "Email": (_email.get("name", "") if _email is not None else "hemmelig"),
                "Direkte funktionær": len(
                    [
                        (k, v)
                        for k, v in node.report["e_dir_salary"].items()
                        if k != payload["manager"]["person"]["uuid"]
                    ]
                ),
                "Heraf ledere": len(
                    [
                        (k, v)
                        for k, v in node.report["m_dir_subord"].items()
                        if k != payload["manager"]["person"]["uuid"]
                    ]
                ),
                "Direkte timeløn": len(node.report["e_dir_hourly"]),
                "Samlet funktionær": len(
                    [
                        (k, v)
                        for k, v in node.report["e_tot_salary"].items()
                        if k != payload["manager"]["person"]["uuid"]
                    ]
                ),
                "Samlet timeløn": len(node.report["e_tot_hourly"]),
                "Opgjort pr": opgjort_pr,
            }
            row.update(
                {
                    "Direkte ialt": row["Direkte funktionær"] + row["Direkte timeløn"],
                    "Samlet ialt": row["Samlet funktionær"] + row["Samlet timeløn"],
                }
            )
            rows.append(row)

    return fieldnames, rows


def collapse_same_manager_more_departments(rows):
    # sort on manager_uuid, but remain sorted in preorder thereafter
    rows.sort(key=lambda x: (x["manager_uuid"], x["sort_order"]))
    returnrows = {}
    for row in rows:
        retrow = returnrows.setdefault(row["manager_uuid"], row)
        # same manager, different department?
        if row != retrow:
            retrow["Direkte funktionær"] += row["Direkte funktionær"]
            retrow["Heraf ledere"] += row["Heraf ledere"]
            retrow["Direkte timeløn"] += row["Direkte timeløn"]
            retrow["Samlet funktionær"] += row["Samlet funktionær"]
            retrow["Samlet timeløn"] += row["Samlet timeløn"]
            retrow["Direkte ialt"] += row["Direkte ialt"]
            retrow["Samlet ialt"] += row["Samlet ialt"]
    return sorted(returnrows.values(), key=lambda x: x["sort_order"])


def get_root_org_unit_uuid(mh, root_org_unit_name):
    root_org_unit_uuid = None
    org = mh.read_organisation()
    roots = mh.read_top_units(org)
    for root in roots:
        logger.debug("checking if %s is %s", root["name"], root_org_unit_name)
        if root["name"] == root_org_unit_name:
            root_org_unit_uuid = root["uuid"]
            break
    return root_org_unit_uuid


def main(
    report_outfile,
    root_org_unit_name=MORA_ROOT_ORG_UNIT_NAME,
    mh=MoraHelper(),
):
    root_org_unit_uuid = get_root_org_unit_uuid(mh, root_org_unit_name)

    if not root_org_unit_uuid:
        logger.error("%s not found in root-ous", root_org_unit_name)
        exit(1)

    logger.warning("caching all ou's, so program may seem unresponsive temporarily")
    nodes = mh.read_ou_tree(root_org_unit_uuid)
    find_people(mh, nodes)
    fieldnames, rows = prepare_report(mh, nodes)
    rows = collapse_same_manager_more_departments(rows)
    with file_uploader(settings, report_outfile) as filename:
        mh._write_csv(fieldnames, rows, filename)


if __name__ == "__main__":
    morah = MoraHelper(MORA_BASE)
    main(report_outfile="viborg_managers.csv", mh=morah)
