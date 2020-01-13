#!/bin/sh

## ------------------------
##  Variables
## ------------------------

DATE='/bin/date'
DBHOST='localhost'
DBUSER='dbdump'
DUMP='/usr/bin/pg_dump'
DUMPDATE=`${DATE} +%Y%m%d%H%M`
DUMPOPTS='-F p'  
DUMPROOT='/x1/db_dump/postgres'
LOCKFILE='/tmp/postgres.dump.lock'
PSQL='/usr/bin/psql'
ALLDBS=`psql -lqtA -F "\t" -R "\n"`;
DBLISTFILE="/tmp/postgres.dump.list"


## ------------------------
## Functions
## ------------------------

backup_db() {
  echo ""
  echo "Dumping ${DBNAME} DB..."
  time ${DUMP} ${DUMPOPTS} ${DBNAME} | gzip --rsyncable -9 > ${DUMPFILE}
}

check_lock() {
  if [ -f ${LOCKFILE} ]; then
    echo "A lockfile ${LOCKFILE} already exists.  This likely means a backup is still running";
  #  exit 1;
  fi
}

create_db_list() {
  echo ""
  echo "Creating list of DB's to dump..."
  echo ${ALLDBS} | grep -v template0 | cut -f 1 > ${DBLISTFILE}
  echo ${ALLDBS} | grep -v template0 | cut -f 1
  echo ""
  echo ""
}

create_dumpdir() {
  if [ ! -d ${DUMPDIR} ]; then
    echo "Creating new directory structure ( ${DUMPDIR} ) for new DB ${DBNAME}"
    mkdir -p ${DUMPDIR};
    echo ""
  fi
}

rsync_offsite() {
  DATE_BIN=/bin/date
  TODAY=`$DATE_BIN +%Y%m%d`
  FIVE_DAYS=`$DATE_BIN -d '5 days ago' +%Y%m%d`
  OLD_LOG=/root/rsynclogs/abi/backups-titan-postgres-$FIVE_DAYS.log
  RM_BIN=/bin/rm

  /etc/init.d/stunnel4 start
  sleep 5
  echo "rsyncing file ${DUMPFILE} offsite to abi ..."
  time \
  /usr/bin/rsync -rlRptz \
  --log-file=/root/rsynclogs/abi/backups-titan-postgres-$TODAY.log \
  --password-file=/root/.pw-abi \
  ${DUMPFILE} rsync://apb-titan@localhost:1873/titan/
  /etc/init.d/stunnel4 stop
  echo ""

  $RM_BIN -f $OLD_LOG
}


start_lock() {
  touch ${LOCKFILE}
}

stop_lock() {
 rm -f ${LOCKFILE}
}


## ------------------------
##  Main loop
## ------------------------

date
check_lock
start_lock
create_db_list

## Now lets loops over the DBs and do something
for DBNAME in `cat "${DBLISTFILE}"` ; do
  DUMPDIR="${DUMPROOT}/${DBNAME}"
  DUMPFILE="${DUMPDIR}/${DUMPDATE}.sql.gz"
  ## Temporary measure to track timings
  date
  create_dumpdir;
  backup_db
  sleep 5
  rsync_offsite
  sleep 5;
done

stop_lock
date
