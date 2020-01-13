#!/bin/sh

source common.conf

if [ $# -lt 1 ]; then
	echo Usage: $0 email@address '[email@address...]'
	exit 1
fi

cd $LISTS_DIR

for i in */
do
(
cd $i
for j in  in *private/
do
if [ -d `pwd`/$j/allow/subscribers ]; then
	for m in $*
	do
		ezmlm-sub `pwd`/$j allow $m  && echo Subscribed $m to $i/$j/allow
	done
fi
done
)
done

