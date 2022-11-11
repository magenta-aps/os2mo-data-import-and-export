#!/bin/bash
[ "${BASH_SOURCE[0]}" == "${0}" ] && JOB_RUNNER_MODE=running || JOB_RUNNER_MODE=sourced
[ "${JOB_RUNNER_MODE}" == "running" ] && set +x
export JOB_RUNNER_MODE
export DIPEXAR=${DIPEXAR:=$(realpath -L $(dirname $(realpath -L "${BASH_SOURCE}"))/..)}
export CUSTOMER_SETTINGS=${CUSTOMER_SETTINGS:=${DIPEXAR}/settings/settings.json}
export SETTINGS_FILE=$(basename ${CUSTOMER_SETTINGS})
export BACKUP_MAX_SECONDS_AGE=${BACKUP_MAX_SECONDS_AGE:=120}
export VENV=${VENV:=${DIPEXAR}/.venv}
export POETRYPATH=${POETRYPATH:=/home/$(whoami)/.local/bin/poetry}
export IMPORTS_OK=false
export EXPORTS_OK=false
export REPORTS_OK=false
export BACKUP_OK=true
export LC_ALL="C.UTF-8"

cd ${DIPEXAR}
source ${DIPEXAR}/tools/prefixed_settings.sh
cd ${DIPEXAR}

RUN_DB_BACKUP=${RUN_DB_BACKUP:-true}
RUN_MO_DATA_SANITY_CHECK=${RUN_MO_DATA_SANITY_CHECK:-true}

export PYTHONPATH=$PWD:$PYTHONPATH

# some logfiles can be truncated after backup as a primitive log rotation
# they should be appended to BACK_UP_AND_TRUNCATE
declare -a BACK_UP_AND_TRUNCATE=(
    ${DIPEXAR}/mo_integrations.log
)

# files that need to be backed up BEFORE running the jobs
# should be appended to BACK_UP_BEFORE_JOBS NOW - they can't
# be added inside the job functions

declare -a BACK_UP_BEFORE_JOBS=()
if [[ $RUN_DB_BACKUP == "true" ]]; then
    BACK_UP_BEFORE_JOBS+=(${SNAPSHOT_LORA})
fi
BACK_UP_BEFORE_JOBS+=(
    $(readlink ${CUSTOMER_SETTINGS})
    $(
        SETTING_PREFIX="mox_stsorgsync" source ${DIPEXAR}/tools/prefixed_settings.sh
        # backup mox_stsorgsync config only if file exists
        [ -f "${MOX_MO_CONFIG}" ] && echo ${MOX_MO_CONFIG}
    )
    $(
        SETTING_PREFIX="integrations.SD_Lon.import" source ${DIPEXAR}/tools/prefixed_settings.sh
        # backup run_db only if file exists - it will not exist on non-SD customers
        echo ${run_db}
    )
    $(
        SETTING_PREFIX="integrations.opus.import" source ${DIPEXAR}/tools/prefixed_settings.sh
        # backup run_db only if file exists - it will not exist on non-OPUS customers
        echo ${run_db}
    )
)

# files that need to be backed up AFTER running the jobs
# should be appended to BACK_UP_AFTER_JOBS
declare -a BACK_UP_AFTER_JOBS=(
    ${CRON_LOG_FILE}
    # 2 files only exists at SD customers running changed at/cpr_uuid
    # always take them if they are there
    $([ -f "${DIPEXAR}/cpr_mo_ad_map.csv" ] && echo "${DIPEXAR}/cpr_mo_ad_map.csv")
    $([ -f "${DIPEXAR}/settings/cpr_uuid_map.csv" ] && echo "${DIPEXAR}/settings/cpr_uuid_map.csv")
)

show_git_commit(){
    echo
    echo "    CRON_GIT_COMMIT=$(git show -s --format=%H)"
    echo
}

sanity_check_mo_data(){
    echo Performing sanity check on data
    ${VENV}/bin/python3 tools/check_data.py
}

imports_mox_db_clear(){
    echo running imports_mox_db_clear
    ${VENV}/bin/python3 tools/clear_mox_tables.py
}

move_backup_to_archive() {
    if [ -z $1 ]; then
        # This should never happen, but just in case...
        echo "Backup file argument must be provided to function"
        exit 3
    fi

    echo "Moving $1 to archive"

    local archive=${CRON_BACKUP}/sql_removed
    if [[ ! -d ${archive} ]]; then
        mkdir ${archive}
    fi
    mv $1 ${archive}
}

remove_db_from_backup() {
    if [ -z $1 ]; then
        # This should never happen, but just in case...
        echo "Backup file argument must be provided to function"
        exit 3
    fi

    echo "Removing database dump from $1"

    local folder=/tmp/dipex-temp-untar-folder
    rm -rf $folder
    mkdir $folder

    tar xzf $1 -C "$folder/"
    rm $folder/opt/docker/os2mo/database_snapshot/os2mo_database.sql
    rm $1

    cd $folder
    tar -czf $1 *
    cd $OLDPWD

    rm -rf $folder

    echo "Database dump removed from $1"
}

imports_test_ad_connectivity(){
    BACK_UP_AND_TRUNCATE+=(
        "${DIPEXAR}/test_connectivity.log"
    )
    echo running imports_test_ad_connectivity
    ${VENV}/bin/python3 -m integrations.ad_integration.test_connectivity --test-read-settings
}

imports_test_ad_connectivity_writer(){
    BACK_UP_AND_TRUNCATE+=(
        "${DIPEXAR}/test_connectivity.log"
    )
    echo running imports_test_ad_connectivity_writer
    ${VENV}/bin/python3 -m integrations.ad_integration.test_connectivity --test-write-settings
}

imports_test_sd_connectivity(){
    echo running imports_test_sd_connectivity
    BACK_UP_AND_TRUNCATE+=(
        "${DIPEXAR}/integrations/SD_Lon/test_sd_connectivity.log"
    )
    cd integrations/SD_Lon/
    ${POETRYPATH} run python -m sdlon.test_sd_connectivity
    EXIT_CODE=$?
    cd ../..
    return $EXIT_CODE
}

imports_sd_fix_departments(){
    BACK_UP_AND_TRUNCATE+=(
        "${DIPEXAR}/fix_sd_departments.log"
    )
    echo running imports_sd_fix_departments
    ${VENV}/bin/python3 integrations/SD_Lon/sd_fix_departments.py
}

imports_sd_changed_at(){
    echo running imports_sd_changed_at
    BACK_UP_AFTER_JOBS+=(
        ${DIPEXAR}/cpr_mo_ad_map.csv
        ${DIPEXAR}/settings/cpr_uuid_map.csv
    )
    cd integrations/SD_Lon/
    ${POETRYPATH} run python -m sdlon.sd_changed_at changed-at-cli
    EXIT_CODE=$?
    cd ../..
    return $EXIT_CODE
}

imports_opus_diff_import(){
    echo running opus_diff_import
    BACK_UP_AFTER_JOBS+=(
        ${DIPEXAR}/cpr_mo_ad_map.csv
        ${DIPEXAR}/settings/cpr_uuid_map.csv
    )
    ${VENV}/bin/python3 integrations/opus/opus_diff_import.py
}

imports_sd_update_primary(){
    BACK_UP_AND_TRUNCATE+=(
        "${DIPEXAR}/calculate_primary.log"
    )
    echo "updating primary engagements"
    ${VENV}/bin/python3 integrations/calculate_primary/calculate_primary.py --integration SD --recalculate-all || (
        # denne fejl skal ikke stoppe afviklingen, da en afbrudt kørsel blot kan gentages
        echo FEJL i updating primary engagements, men kører videre
    )
}


imports_ad_sync(){
    BACK_UP_AND_TRUNCATE+=(
        "${DIPEXAR}/ad_mo_sync.log"
    )
    echo running imports_ad_sync
    ${VENV}/bin/python3 -m integrations.ad_integration.ad_sync
}

imports_ballerup_apos(){
    echo running imports_ballerup_apos
    ${VENV}/bin/python3 integrations/ballerup/ballerup.py
}

imports_ballerup_udvalg(){
    BACK_UP_AND_TRUNCATE+=(
        "${DIPEXAR}/udvalg.log"
    )
    echo running imports_ballerup_udvalg
    ${VENV}/bin/python3 integrations/ballerup/udvalg_import.py
}

imports_ad_group_into_mo(){
    BACK_UP_AND_TRUNCATE+=(
        "${DIPEXAR}/external_ad_users.log"
    )
    echo running imports_ad_group_into_mo
    ${VENV}/bin/python3 -m integrations.ad_integration.import_ad_group_into_mo --full-sync
}

imports_kle_online(){
    BACK_UP_AND_TRUNCATE+=(
        "${DIPEXAR}/kle_online.log"
    )
    echo running imports_kle_online
    "${VENV}/bin/python3" os2mo_data_import/kle/kle_import.py
}

imports_opgavefordeler(){
    BACK_UP_AND_TRUNCATE+=(
        "${DIPEXAR}/opgavefordeler.log"
    )
    echo running imports_opgavefordeler
    "${VENV}/bin/python3" integrations/kle/kle_import_export.py
}

imports_aak_los(){
    echo "Running aak_los"
    "${VENV}/bin/python3" integrations/aarhus/los_import.py
}



exports_mox_rollekatalog(){
    export MOX_ROLLE_MAPPING="${DIPEXAR}/cpr_mo_ad_map.csv"
    export MOX_ROLLE_OS2MO_API_KEY=$SAML_TOKEN
    export MOX_ROLLE_LOG_FILE="${DIPEXAR}/exports_mox_rollekatalog.log"

    echo running exports_mox_rollekatalog
    BACK_UP_AND_TRUNCATE+=(
        "$MOX_ROLLE_LOG_FILE"
    )

    ${VENV}/bin/python3 exporters/os2rollekatalog/os2rollekatalog_integration.py
}

exports_os2sync(){
    BACK_UP_AND_TRUNCATE+=($(
        SETTING_PREFIX="os2sync" source ${DIPEXAR}/tools/prefixed_settings.sh
        echo ${log_file}
    ))
    echo running exports_os2sync
    cd exporters/os2sync_export
    ${POETRYPATH} run python -m os2sync_export
    EXIT_CODE=$?
    cd ../..
    return $EXIT_CODE
}

exports_mox_stsorgsync(){
    MOX_ERR_CODE=0
    BACK_UP_AND_TRUNCATE+=($(
        SETTING_PREFIX="mox_stsorgsync" source ${DIPEXAR}/tools/prefixed_settings.sh
        echo ${LOGFILE}
    ))
    echo running exports_mox_stsorgsync
    (
        # get VENV, MOX_MO_CONFIG and LOGFILE
        SETTING_PREFIX="mox_stsorgsync" source ${DIPEXAR}/tools/prefixed_settings.sh
        ${VENV}/bin/python3 -m mox_stsorgsync >> ${LOGFILE} 2>&1 || MOX_ERR_CODE=1
        echo "Last 50 lines from mox_stsorgsyncs log :"
        tail  -n 50  ${LOGFILE}
        echo "last 10 Errors from OS2sync"
        grep ERROR /var/log/os2sync/service.log | tail -n 10
        exit ${MOX_ERR_CODE}
    )
}

exports_cpr_uuid(){
    echo running exports_cpr_uuid
    BACK_UP_AND_TRUNCATE+=(
        "${DIPEXAR}/cpr_uuid_export.log"
    )
    (
        SETTING_PREFIX="cpr.uuid" source ${DIPEXAR}/tools/prefixed_settings.sh
        ${VENV}/bin/python3 exporters/cpr_uuid.py ${CPR_UUID_FLAGS}
    )
}

exports_viborg_emus(){
    BACK_UP_AND_TRUNCATE+=(
        emus_log.txt
    )
    echo running viborg_emus
    EMUS_FILENAME="tmp/emus_export.xml"

    ${VENV}/bin/python3 exporters/emus/lcdb_viborg_xml_emus.py ${EMUS_FILENAME}
    ${VENV}/bin/python3 exporters/emus/emus_sftp.py ${EMUS_FILENAME}
}

exports_viborg_eksterne(){
    echo "running viborgs eksterne"
    ${VENV}/bin/python3 exporters/viborg_eksterne/viborg_eksterne.py || exit 1
    (
        SETTING_PREFIX="mora.folder" source ${DIPEXAR}/tools/prefixed_settings.sh
        SETTING_PREFIX="integrations.ad" source ${DIPEXAR}/tools/prefixed_settings.sh
        SETTING_PREFIX="exports_viborg_eksterne" source ${DIPEXAR}/tools/prefixed_settings.sh
        system_user="${system_user%%@*}"
        [ -z "${query_export}" ] && exit 1
        [ -z "${system_user}" ] && exit 1
        [ -z "${password}" ] && exit 1
        [ -z "${destination_smb_share}" ] && exit 1
        [ -z "${destination_directory}" ] && exit 1
        [ -z "${outfile_basename}" ] && exit 1
        [ -z "${workgroup}" ] && exit 1

        cd ${query_export}
        smbclient -U "${system_user}%${password}"  \
            ${destination_smb_share} -m SMB2  \
            -W ${workgroup} --directory ${destination_directory} \
            -c 'put '${outfile_basename}''

        #smbclient -U "${system_user}%${password}"  \
        #    ${destination_smb_share} -m SMB2  \
        #    -W ${workgroup} --directory ${destination_directory} \
        #    -c 'del '${outfile_basename}''
    )
}

exports_ad_life_cycle(){
    BACK_UP_AND_TRUNCATE+=(
        "${DIPEXAR}/AD_life_cycle.log"
    )
    echo "running exports_ad_life_cycle"
    ${VENV}/bin/python3 -m integrations.ad_integration.ad_life_cycle --create-ad-accounts
}

exports_ad_life_cycle_disable_accounts(){
    BACK_UP_AND_TRUNCATE+=(
        "${DIPEXAR}/AD_life_cycle.log"
    )
    echo "running exports_ad_life_cycle_disable_accounts"
    ${VENV}/bin/python3 -m integrations.ad_integration.ad_life_cycle --disable-ad-accounts
}

exports_mo_to_ad_sync(){
    BACK_UP_AND_TRUNCATE+=(
        "${DIPEXAR}/mo_to_ad_sync.log"
    )
    echo "running exports_mo_to_ad_sync"
    ${VENV}/bin/python3 -m integrations.ad_integration.mo_to_ad_sync
}

exports_ad_enddate_fixer(){
    echo "Fixing enddates in AD of terminated engagements"
    ${VENV}/bin/python3 -m integrations.ad_integration.ad_fix_enddate
}

exports_plan2learn(){
    echo "running exports_plan2learn"
    declare -a CSV_FILES=(
	bruger
	leder
	engagement
	organisation
	stillingskode
    )
    ${VENV}/bin/python3 ${DIPEXAR}/exporters/plan2learn/plan2learn.py --lora

    (
        # get OUT_DIR and EXPORTS_DIR
        SETTING_PREFIX="mora.folder" source ${DIPEXAR}/tools/prefixed_settings.sh
	[ -z "$query_export" ] && exit 1
	for f in "${CSV_FILES[@]}"
	do
	    ${VENV}/bin/python3 ${DIPEXAR}/exporters/plan2learn/ship_files.py \
		   ${query_export}/plan2learn_${f}.csv ${f}.csv
	done
    )
}

exports_queries_ballerup(){
    echo appending ballerup exports logfile to BACK_UP_AND_TRUNCATE
    BACK_UP_AND_TRUNCATE+=($(
        SETTING_PREFIX="exporters.ballerup" source ${DIPEXAR}/tools/prefixed_settings.sh
        echo ${WORK_DIR}/export.log
    ))
    echo running exports_queries_ballerup
    (
        # get OUT_DIR and EXPORTS_DIR
        SETTING_PREFIX="exporters.ballerup" source ${DIPEXAR}/tools/prefixed_settings.sh
        [ -z "${EXPORTS_DIR}" ] && echo "EXPORTS_DIR not spec'ed for exports_queries_ballerup" && exit 1
        [ -z "${WORK_DIR}" ] && echo "WORK_DIR not spec'ed for exports_queries_ballerup" && exit 1
        [ -d "${WORK_DIR}" ] || mkdir "${WORK_DIR}"
        cd "${WORK_DIR}"
        ${VENV}/bin/python3 ${DIPEXAR}/exporters/ballerup.py > ${WORK_DIR}/export.log 2>&1
        local STATUS=$?
        cp "${WORK_DIR}"/*.csv "${EXPORTS_DIR}"
        return $STATUS
    )
}

exports_actual_state_export(){
    # kører en test-kørsel
    BACK_UP_AND_TRUNCATE+=(sql_export.log)

    cd exporters/
    ${POETRYPATH} run python -m sql_export.sql_export --resolve-dar
    EXIT_CODE=$?
    cd ..
    return $EXIT_CODE
}

exports_historic_sql_export(){
    BACK_UP_AND_TRUNCATE+=(sql_export.log)

    cd exporters/
    ${POETRYPATH} run python -m sql_export.sql_export --resolve-dar --historic
    EXIT_CODE=$?
    cd ..
    return $EXIT_CODE
}

exports_os2phonebook_export(){
    # kører en test-kørsel
    BACK_UP_AND_TRUNCATE+=(os2phonebook_export.log)
    ${VENV}/bin/python3 ${DIPEXAR}/exporters/os2phonebook/os2phonebook_export.py generate-json
    ${VENV}/bin/python3 ${DIPEXAR}/exporters/os2phonebook/os2phonebook_export.py transfer-json
}

exports_sync_mo_uuid_to_ad(){
    BACK_UP_AND_TRUNCATE+=(sync_mo_uuid_to_ad.log)
    ${VENV}/bin/python3 -m integrations.ad_integration.sync_mo_uuid_to_ad --sync-all
}

reports_viborg_managers(){
    ${VENV}/bin/python3 ${DIPEXAR}/reports/viborg_managers.py
}

reports_frederikshavn(){
    BACK_UP_AND_TRUNCATE+=(Frederikshavn_reports.log)
    ${VENV}/bin/python3 ${DIPEXAR}/customers/Frederikshavn/Frederikshavn_reports.py
    ${VENV}/bin/python3 ${DIPEXAR}/customers/Frederikshavn/employee_survey.py
}

reports_svendborg(){
    ${VENV}/bin/python3 ${DIPEXAR}/customers/Svendborg/svendborg_reports.py
}

reports_csv(){
    ${VENV}/bin/python3 ${DIPEXAR}/reports/shared_reports.py
}

exports_lc_for_jobs_db(){
    BACK_UP_AND_TRUNCATE+=(lc-for-jobs.log)
    SETTING_PREFIX="lc-for-jobs" source ${DIPEXAR}/tools/prefixed_settings.sh
    [ -z "${actual_db_name}" ] && echo "actual_db_name not specified" && exit 1
    db_file="${actual_db_name}.db"

    [ -f "${db_file}" ] && chmod 600 "${db_file}"
    cd exporters/
    ${POETRYPATH} run python -m sql_export.lc_for_jobs_db sql-export --resolve-dar
    local STATUS=$?
    cd ..
    [ -f "${db_file}" ] && chmod 400 "${db_file}"
    return $STATUS
}

exports_cache_loracache() {
    echo "Building cached LoRaCache"
    rm -f "${DIPEXAR}/tmp/!(*_historic).p"  # delete old non-historic pickle files

    cd exporters/
    ${POETRYPATH} run python -m sql_export.lora_cache --no-historic --resolve-dar
    EXIT_CODE=$?
    cd ..
    return $EXIT_CODE
}

exports_historic_cache_loracache() {
    echo "Building full historic cached LoRaCache"
    rm -f "${DIPEXAR}/tmp/*_historic.p"  # delete old pickle files

    cd exporters/
    ${POETRYPATH} run python -m sql_export.lora_cache --historic --resolve-dar
    EXIT_CODE=$?
    cd ..
    return $EXIT_CODE
}

exports_historic_skip_past_cache_loracache() {
    echo "Building historic WITHOUT past cached LoRaCache"
    rm -f "${DIPEXAR}/tmp/*_historic_skip_past.p"  # delete old pickle files

    cd exporters/
    ${POETRYPATH} run python -m sql_export.lora_cache --historic --skip-past --resolve-dar
    EXIT_CODE=$?
    cd ..
    return $EXIT_CODE
}


reports_viborg_managers(){
    ${VENV}/bin/python3 ${DIPEXAR}/reports/viborg_managers.py
}

reports_sd_db_overview(){
    echo running reports_sd_db_overview
    ${VENV}/bin/python3 integrations/rundb/db_overview.py --rundb-variable integrations.SD_Lon.import.run_db read-current-status
    local STATUS=$?
    return $STATUS
}

# read the run-job script et al
for module in tools/job-runner.d/*.sh; do
    #echo sourcing $module
    source $module
done

prometrics-git

# imports are typically interdependent: -e
imports(){
    [ "${BACKUP_OK}" == "false" ] \
        && echo ERROR: backup is in error - skipping imports \
        && return 1 # imports depend on backup

    if [ "${RUN_MOX_DB_CLEAR}" == "true" ]; then
        run-job imports_mox_db_clear || return 2
    fi

    if [ "${RUN_CHECK_AD_CONNECTIVITY}" == "true" ]; then
        run-job imports_test_ad_connectivity || return 2
    fi

    if [ "${RUN_CHECK_SD_CONNECTIVITY}" == "true" ]; then
        run-job imports_test_sd_connectivity || return 2
    fi

    if [ "${RUN_SD_FIX_DEPARTMENTS}" == "true" ]; then
        run-job imports_sd_fix_departments || return 2
    fi

    if [ "${RUN_SD_CHANGED_AT}" == "true" ]; then
        run-job imports_sd_changed_at || return 2
    fi

    if [ "${RUN_SD_UPDATE_PRIMARY}" == "true" ]; then
        run-job imports_sd_update_primary || return 2
    fi

    if [ "${RUN_OPUS_DIFF_IMPORT}" == "true" ]; then
        run-job imports_opus_diff_import || return 2
    fi

    if [ "${RUN_AD_SYNC}" == "true" ]; then
        run-job imports_ad_sync || return 2
    fi

    if [ "${RUN_BALLERUP_APOS}" == "true" ]; then
        run-job imports_ballerup_apos || return 2
    fi

    if [ "${RUN_BALLERUP_UDVALG}" == "true" ]; then
        run-job imports_ballerup_udvalg || return 2
    fi

    if [ "${RUN_AD_GROUP_INTO_MO}" == "true" ]; then
        run-job imports_ad_group_into_mo || return 2
    fi

    if [ "${RUN_KLE_ONLINE}" == "true" ]; then
        run-job imports_kle_online || return 2
    fi

    if [ "${RUN_OPGAVEFORDELER}" == "true" ]; then
        run-job imports_opgavefordeler || return 2
    fi

    if [ "${RUN_IMPORTS_AAK_LOS}" == "true" ]; then
        run-job imports_aak_los || return 2
    fi

}

prepare_exports(){
    [ "${IMPORTS_OK}" == "false" ] \
        && echo ERROR: imports are in error - skipping exports \
        && return 1 # exports depend on imports

    # these particular exports are not allowed to fail:
    if [ "${RUN_CPR_UUID}" == "true" ]; then
        run-job exports_cpr_uuid || return 2
    fi

    if [ "${RUN_CACHE_LORACACHE}" == "true" ]; then
        run-job exports_cache_loracache || return 2
    fi

    if [ "${RUN_CACHE_HISTORIC_LORACACHE}" == "true" ]; then
        run-job exports_historic_cache_loracache || return 2
    fi

    if [ "${RUN_CACHE_HISTORIC_SKIP_PAST_LORACACHE}" == "true" ]; then
        run-job exports_historic_skip_past_cache_loracache || return 2
    fi
    
    if [ "${RUN_LC_FOR_JOBS_DB_EXPORT}" == "true" ]; then
        run-job exports_lc_for_jobs_db || return 2
    fi
}

exports(){
    [ "${PREPARE_EXPORTS_OK}" == "false" ] \
        && echo "ERROR in preparing exports" \
        && return 1 
    # Remaining exports are independent an can be run concurrently
    
    if [ "${RUN_ACTUAL_STATE_EXPORT}" == "true" ]; then
        run-job exports_actual_state_export &
    fi

    if [ "${RUN_HISTORIC_SQL_EXPORT}" == "true" ]; then
        run-job exports_historic_sql_export &
    fi

    if [ "${RUN_OS2SYNC}" == "true" ]; then
        run-job exports_os2sync &
    fi

    if [ "${RUN_QUERIES_BALLERUP}" == "true" ]; then
        run-job exports_queries_ballerup &
    fi

    if [ "${RUN_EXPORT_EMUS}" == "true" ]; then
        run-job exports_viborg_emus &
    fi

    if [ "${RUN_EXPORTS_VIBORG_EKSTERNE}" == "true" ]; then
        run-job exports_viborg_eksterne &
    fi

    if [ "${RUN_EXPORTS_OS2MO_PHONEBOOK}" == "true" ]; then
        run-job exports_os2phonebook_export &
    fi

    if [ "${RUN_EXPORTS_MO_UUID_TO_AD}" == "true" ]; then
        run-job exports_sync_mo_uuid_to_ad &
    fi

    if [ "${RUN_EXPORTS_AD_LIFE_CYCLE}" == "true" ]; then
        run-job exports_ad_life_cycle &
    fi

    if [ "${RUN_EXPORTS_AD_LIFE_CYCLE_DISABLE_ACCOUNTS}" == "true" ]; then
        run-job exports_ad_life_cycle_disable_accounts || return 2
    fi

    if [ "${RUN_EXPORTS_MO_TO_AD_SYNC}" == "true" ]; then
        run-job exports_mo_to_ad_sync || return 2
    fi

    if [ "${RUN_AD_ENDDATE_FIXER}" == "true" ]; then
        run-job exports_ad_enddate_fixer || return 2
    fi

    if [ "${RUN_MOX_ROLLE}" == "true" ]; then
        run-job exports_mox_rollekatalog || return 2
    fi

    if [ "${RUN_PLAN2LEARN}" == "true" ]; then
        run-job exports_plan2learn || return 2
    fi

    if [ "${RUN_EXPORTS_TEST}" == "true" ]; then
        run-job exports_test || return 2
    fi

}

# reports are typically not interdependent
reports(){
    #set -x # debug log
    [ "${IMPORTS_OK}" == "false" ] \
        && echo ERROR: imports are in error - skipping reports \
        && return 1 # reports depend on imports

    if [ "${RUN_SD_DB_OVERVIEW}" == "true" ]; then
        run-job reports_sd_db_overview &
    fi

    if [ "${RUN_VIBORG_MANAGERS}" == "true" ]; then
        run-job reports_viborg_managers &
    fi

    if [ "${RUN_REPORTS_FREDERIKSHAVN}" == "true" ]; then
        run-job reports_frederikshavn &
    fi

    if [ "${RUN_REPORTS_SVENDBORG}" == "true" ]; then
        run-job reports_svendborg &
    fi

    if [ "${RUN_REPORTS_CSV}" == "true" ]; then
        run-job reports_csv &
    fi

}

pre_truncate_logfiles(){
    # logfiles are truncated before each run as
    [ -f "udvalg.log" ] && truncate -s 0 "udvalg.log"
}

pre_backup(){
    prometrics-job-start "pre_backup"

    temp_report=$(mktemp)
    # deduplicate
    BACK_UP_BEFORE_JOBS=($(printf "%s\n" "${BACK_UP_BEFORE_JOBS[@]}" | sort -u))
    for f in ${BACK_UP_BEFORE_JOBS[@]}
    do
        FILE_FAILED=false
        # try to append to tar file and report if not found
        tar -rf $BUPFILE "${f}" > ${temp_report} 2>&1 || FILE_FAILED=true
        if [ "${FILE_FAILED}" = "true" ]; then
            BACKUP_OK=false
            echo BACKUP ERROR
            cat ${temp_report}
        fi
    done
    rm ${temp_report}

    if [[ ${RUN_DB_BACKUP} == "true" ]]; then
        declare -i age=$(stat -c%Y ${BUPFILE})-$(stat -c%Y ${SNAPSHOT_LORA})
        if [[ ${age} -gt ${BACKUP_MAX_SECONDS_AGE} ]]; then
            BACKUP_OK=false
            echo "ERROR database snapshot is more than ${BACKUP_MAX_SECONDS_AGE} seconds old: $age"
            # Report failed execution of `pre_backup`
            prometrics-job-end "pre_backup" 1
            return 1
        fi
    fi

    # Report successful execution of `pre_backup`
    prometrics-job-end "pre_backup" 0
}

post_backup(){
    prometrics-job-start "post_backup"

    temp_report=$(mktemp)
    # deduplicate
    BACK_UP_AFTER_JOBS=($(printf "%s\n" "${BACK_UP_AFTER_JOBS[@]}" | sort -u))
    BACK_UP_AND_TRUNCATE=($(printf "%s\n" "${BACK_UP_AND_TRUNCATE[@]}" | sort -u))
    for f in ${BACK_UP_AFTER_JOBS[@]} ${BACK_UP_AND_TRUNCATE[@]}
    do
        FILE_FAILED=false
        # try to append to tar file and report if not found
        tar -rf $BUPFILE "${f}" > ${temp_report} 2>&1 || FILE_FAILED=true
        if [ "${FILE_FAILED}" = "true" ]; then
            BACKUP_OK=false
            echo BACKUP ERROR
            cat ${temp_report}
        fi
    done
    rm ${temp_report}
    echo
    echo listing preliminary backup archive
    echo ${BUPFILE}.gz
    tar -tvf ${BUPFILE}
    gzip  ${BUPFILE}

    echo truncating backed up logfiles
    for f in ${BACK_UP_AND_TRUNCATE[@]}
    do
        if [ -f "${f}" ]; then
            printf "truncating %s\n" "$f"
            truncate -s 0 "${f}"
        else
            printf "not truncating %s\n" "$f"
        fi
    done

    echo
    BACKUP_SAVE_DAYS=${BACKUP_SAVE_DAYS:=60}
    echo deleting backups older than "${BACKUP_SAVE_DAYS}" days
    bupsave=${CRON_BACKUP}/$(date +%Y-%m-%d-%H-%M-%S -d "-${BACKUP_SAVE_DAYS} days")-cron-backup.tar.gz
    for oldbup in ${CRON_BACKUP}/????-??-??-??-??-??-cron-backup.tar.gz
    do
        [ "${oldbup}" \< "${bupsave}" ] && (
            remove_db_from_backup $oldbup
            move_backup_to_archive $oldbup
        )
    done
    echo backup done # do not remove this line

    # Report execution of `post_backup`
    if [ "${BACKUP_OK}" = "true" ]; then
        prometrics-job-end "post_backup" 0
    else
        prometrics-job-end "post_backup" 1
    fi
}

if [ "${JOB_RUNNER_MODE}" == "running" -a "$#" == "0" ]; then
    
    # Dette er den sektion, der kaldes fra CRON (ingen argumenter)

    if [ ! -d "${VENV}" ]; then
        REASON="FATAL: python env not found"
        echo ${REASON}
        exit 2 # error
    fi

    if [ ! -d "${CRON_BACKUP}" ]; then
        REASON="FATAL: Backup directory non existing"
        echo ${REASON}
        exit 2
    fi

    if [[ ${RUN_DB_BACKUP} == "true" ]] && [[ ! -f "${SNAPSHOT_LORA}" ]]; then
        REASON="FATAL: Database snapshot does not exist"
        echo ${REASON}
        exit 2
    fi

    if [ -n "${SVC_USER}" -a -n "${SVC_KEYTAB}" ]; then
        [ -r "${SVC_KEYTAB}" ] || echo WARNING: cannot read keytab
        kinit ${SVC_USER} -k -t ${SVC_KEYTAB} || (
            REASON="WARNING: not able to refresh kerberos auth - authentication failure"
            echo ${REASON}
        )
    else
        REASON="WARNING: not able to refresh kerberos auth - username or keytab missing"
        echo ${REASON}
    fi

    # Vi sletter lora-cache-picklefiler og andet inden vi kører cronjobbet
    rm tmp/*.p 2>/dev/null || :

    export BUPFILE=${CRON_BACKUP}/$(date +%Y-%m-%d-%H-%M-%S)-cron-backup.tar

    pre_backup

    if [[ ${RUN_MO_DATA_SANITY_CHECK} == "true" ]]; then
        run-job sanity_check_mo_data || echo Sanity check failed
    else
        echo "Skipping MO data sanity check"
    fi
    imports && IMPORTS_OK=true
    prepare_exports && PREPARE_EXPORTS_OK=true
    exports &
    reports &
    echo

    post_backup
elif [ "${JOB_RUNNER_MODE}" == "running" ]; then
    if [ -n "$(grep $1\(\) $0)" ]; then
        echo "running single job function '$1'"
        run-job $1
    else
        echo "unknown job '$1'"
    fi
elif [ "${JOB_RUNNER_MODE}" == "sourced" ]; then
    # export essential functions
    export -f pre_backup post_backup reports_sd_db_overview
fi
