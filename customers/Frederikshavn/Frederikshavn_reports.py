import logging
import sys

from ra_utils.load_settings import load_settings
from raclients.upload import run_report_and_upload

from reports.query_actualstate import list_employees, list_MED_members, run_report

LOG_LEVEL = logging.DEBUG

logger = logging.getLogger("Frederikshavn_reports")

logging.basicConfig(
    format="%(levelname)s %(asctime)s %(name)s %(message)s",
    level=LOG_LEVEL,
    stream=sys.stdout,
)


if __name__ == "__main__":
    # Læs fra settings
    settings = load_settings()
    logger.debug("Running reports for Frederikshavn")

    run_report_and_upload(
        settings,
        "MED_medlemmer.xlsx",
        run_report,
        list_MED_members,
        "MED",
        {"løn": "Frederikshavn Kommune", "MED": "MED-organisationen"},
    )
    run_report_and_upload(
        settings,
        "Ansatte.xlsx",
        run_report,
        list_employees,
        "Ansatte",
        "Frederikshavn Kommune",
    )

    logger.debug("Employee report done.")
    logger.debug("All reports for Frederikshavn done")
