#!/bin/bash
# convert {
#    "crontab.RUN_THIS_JOB":true,
#    "crontab.RUN_THAT_JOB":false,
# }
# into 
#    export RUN_THIS_JOB=true
#    export RUN_THAT_JOB=false
#
# use like:
#    source tools/prefixed_settings.sh
# defaults work li calling like:
#     SETTING_PREFIX=crontab \
#     CUSTOMER_SETTINGS=/opt/settings/customer-settings.json \
#     source tools/prefixed_settings.sh
#
export VENV=${VENV:=./.venv}
export SETTING_PREFIX=${SETTING_PREFIX:=crontab}
export CUSTOMER_SETTINGS=${CUSTOMER_SETTINGS:=/opt/settings/customer-settings.json}
. <(${VENV}/bin/python tools/prefixed_settings.py)
