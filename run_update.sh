#!/bin/bash

cd $HOME/code/alexweb
log=./var/log
python=/usr/local/anaconda3/bin/python
mkdir -p var
touch $log
echo starting report update: `date` >> $log
echo shell is $SHELL >> $log
#source $HOME/.zshrc
env >> $log
$python archivescraper.py >> $log 2>&1
./gen_report.sh >> $log 2>&1

change=`git status -s reports | wc -l`
if [ $change -gt 0 ]; then
    echo something changed >> $log
    date=`date` 
    git commit -a -m "automated report update: $date"
    git push
else
    echo no change >> $log
fi
echo finished report update: `date` >> $log

