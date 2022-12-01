#!/bin/bash

# DIPEX' main cron job. 
# Takes a backup and then starts job-runner.sh

# Configuration
#--------------
# Absolute path to the job-runner.sh script
# SCRIPT=... (must be set via environmental variable).
set -e
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
SCRIPT=${SCRIPT:-${DIR}/job-runner.sh}
BACKUP_SCRIPT=${BACKUP_SCRIPT:-${DIR}/backup.sh}

# Enable DB backup per default (override in settings.json
# prefixed with "crontab" if needed)
RUN_DB_BACKUP=true

# Unix service account to run job-runner.sh under
RUNAS=${RUNAS:-sys_magenta_dipex}

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
source ${DIR}/prefixed_settings.sh
if [[ ${RUN_DB_BACKUP} == "true" ]]; then
    bash ${BACKUP_SCRIPT}
fi

# Run script
#-----------
su --shell /bin/bash --command "${SCRIPT} |& systemd-cat -t 'dipex:job-runner.sh'" ${RUNAS} 
