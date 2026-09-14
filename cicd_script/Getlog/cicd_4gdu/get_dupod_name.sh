#!/bin/bash

NAMESPACE=${1}
POD_NAME=`kubectl get pods -n ${NAMESPACE} | grep lte | awk '{{print $1}}'`
printf "%s\n" "${POD_NAME}"
