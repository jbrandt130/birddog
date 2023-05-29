#!/bin/bash -f

cd $HOME/root/alexweb
log=./var/log
touch $log
python archivescraper.py >> $log
./gen_report.sh >> $log

change=`git status -s reports | wc -l`
if [ $change -gt 0 ]; then
    echo something changed
    date=`date` 
    git commit -a -m "automated report update: $date"
    git push
else
    echo no change
fi
