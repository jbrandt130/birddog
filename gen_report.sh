#!/bin/bash -f

latest=`ls -t var | head -1`
echo Updating report using var/$latest
( cd "var/$latest"; wc -l `ls` ) > reports/RecordCounts.txt
( cd "var/$latest"; cat `ls` ) > reports/ModificationDates.csv

( cd reports ; wc -l `ls` )
echo Done
