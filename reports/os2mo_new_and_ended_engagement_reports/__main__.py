# from reports.os2mo_new_and_ended_engagement_reports.config import get_engagement_settings
# from reports.os2mo_new_and_ended_engagement_reports.config import setup_gql_client
# from reports.os2mo_new_and_ended_engagement_reports.get_engagements import display_engagements
# from reports.os2mo_new_and_ended_engagement_reports.get_engagements import write_file
#
#
# def main() -> None:
#
#     started_engagements_data_in_csv = convert_person_and_engagement_data_to_csv(
#         details_of_started_engagements, started=True
#     )
#     # Converting details on ended engagements to csv.
#     ended_engagements_data_in_csv = convert_person_and_engagement_data_to_csv(
#         details_of_ended_engagements, ended=True
#     )
#
#     # Generating a file on newly established engagements.
#     write_file(
#         started_engagements_data_in_csv,
#         "reports/os2mo_new_and_ended_engagement_reports/testing_started_engagement_csv.csv",
#     )
#     # Generating a file  on ended engagements.
#     write_file(
#         ended_engagements_data_in_csv,
#         "reports/os2mo_new_and_ended_engagement_reports/testing_ended_engagement_csv.csv",
#     )
#     settings = get_engagement_settings()
#     settings.start_logging_based_on_settings()
#     gql_session = setup_gql_client(settings=settings)
#
#     display_engagements(settings, gql_session)
#
#
# if __name__ == "__main__":
#     settings = get_engagement_settings()
#     settings.start_logging_based_on_settings()
#     gql_session = setup_gql_client(settings=settings)
#
#     display_engagements(settings, gql_session)
#     main()
