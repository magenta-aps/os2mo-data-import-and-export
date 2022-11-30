#!/bin/bash

# Usage:
# 1. Create the folder /opt/cron/
# 2. Copy this script to /opt/cron/os2mo-data.sh
# 3. Add the script to root's crontab: "05 06 * SCRIPT=/.../job-runner.sh /opt/cron/os2mo-data.sh"
# 4. Verify

# Configuration
#--------------
# Absolute path to the job-runner.sh script
# SCRIPT=... (must be set via environmental variable).

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
SCRIPT=${SCRIPT:-${DIR}/job-runner.sh}
BACKUP_SCRIPT=${BACKUP_SCRIPT:-${DIR}/backup.sh}

# Enable DB backup per default (override in settings.json
# prefixed with "crontab" if needed)
RUN_DB_BACKUP=${RUNAS:-true}

# Unix service account to run job-runner.sh under
RUNAS=${RUNAS:-sys_magenta_dipex}

# Installation type for backup (docker, legacy or none)
INSTALLATION_TYPE=${INSTALLATION_TYPE:-docker}

# Preconditions
#--------------

# Check if the script exists
if [ ! -f "${SCRIPT}" ]; then
    echo "Unable to locate script in specified path: ${SCRIPT}"
    exit 1
fi

# Check if the user exists
if ! id "${RUNAS}" >/dev/null 2>&1; then
    echo "Unable to locate the specified runas user: ${RUNAS}"
    exit 1
fi

# Check for necessary tools
if ! [ -x "$(command -v jq)" ]; then
    echo "Unable to locate the 'jq' executable."
    echo "Try: sudo apt-get install jq"
    exit 1
fi

# Database snapshot
#------------------
if [ "${INSTALLATION_TYPE}" == "docker" ]; then
    (
        source ${DIR}/prefixed_settings.sh
        if [[ ${RUN_DB_BACKUP} == "true" ]]; then
            bash ${BACKUP_SCRIPT}
            EXIT_CODE=$?
            if [ ${EXIT_CODE} -ne 0 ]; then
                exit 1
            fi
        else
            echo "Skip DB snapshot due to e.g. lack of disk space"
        fi
    )
elif [ "${INSTALLATION_TYPE}" == "legacy" ]; then
    echo "Unsupported installation type: legacy"
    exit 1
elif [ "${INSTALLATION_TYPE}" == "none" ]; then
    echo "WARNING: No snapshotting configured"
else
    echo "Unknown installation type: ${INSTALLATION_TYPE}"
    exit 1
fi


# Run script
#-----------
su --shell /bin/bash --command "${SCRIPT} |& systemd-cat -t 'dipex:job-runner.sh'" ${RUNAS} 
