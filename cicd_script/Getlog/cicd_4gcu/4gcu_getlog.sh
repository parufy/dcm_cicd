#!/bin/bash

USER=${1}
PW=${2}
oc login -u ${USER} -p ${PW}
LOGDIR=`date +%Y%m%d%H%M_4G-Logs`
mkdir ${LOGDIR}
PODNAME=`kubectl get pods -o json | jq -r '.items[] | select(.metadata.name | test("")).metadata.name'`
for POD in ${PODNAME}
do
  kubectl logs ${POD} > ${LOGDIR}/${POD}.log
done

tar -zcvf ${LOGDIR}.tar.gz  ${LOGDIR}
rm -rf ${LOGDIR}

printf "%s\n" "${LOGDIR}"


