#!/bin/sh
set -eu

# Railway Volumes تُركّب بصلاحيات root؛ نضمن أن التطبيق يستطيع الكتابة إليها.
mkdir -p "${DATA_DIR:-/app/data}"
chown -R appuser:appuser "${DATA_DIR:-/app/data}"

# تمرير إشارات Railway مباشرة إلى عملية التطبيق عبر gosu وexec.
exec gosu appuser "$@"
