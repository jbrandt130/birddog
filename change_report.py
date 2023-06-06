import pandas as pd
#import numpy as np
#import urllib.request, urllib.parse, urllib.error
import io
import re
import json
import datetime
import os
import random
#from time import sleep
import argparse 

data_dir = './var'

report_dir_pattern = re.compile('[0-9]+-[0-9]+-[0-9]+ [0-9]+:[0-9]+:[0-9]+.[0-9]+')

with open('archives.json') as f:
    archive_list = json.load(f)

def all_reports():
    dirs = os.listdir(data_dir)
    return list(sorted(filter(lambda x: re.match(report_dir_pattern, x) is not None, dirs), reverse=True))

def find_report(key, reports):
    if type(key) == int:
        if key < 0 or key >= len(reports):
            raise IndexError
        return key
    elif type(key) == str:
        if re.match('[0-9]+$', key) is not None:
            return find_report(int(key), reports)
        key = re.compile(key)
        for i, item in enumerate(reports):
            if re.search(key, item) is not None:
                return i
    raise ValueError

def form_date(df):
    df['date'] = df['year'] + ':' + df['month'] + ':' + df['day'] + ':' + df['hour']
    return df

def load_report(fname):
    #print('load_report', fname)
    df = pd.read_csv(f'{data_dir}/{fname}', 
                     names=['type', 'id', 'year', 'month', 'day', 'hour'],
                     dtype=str)
    df = form_date(df)
    df = df.drop(columns=['year', 'month', 'day', 'hour'])
    df = df.replace({0: ''})
    df = df.fillna('?')
    return df.set_index(['id'])

def check_changes(df1, df2):
    df = df1.join(df2, how='outer', lsuffix='1', rsuffix='2').fillna('')
    return df[df['date1'] != df['date2']]

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Alexweb Wiki Change Report')
    parser.add_argument('-t', '--target', help='target report for comparison (default: latest)', default=0)
    parser.add_argument('-r', '--reference', help='reference report for comparison (default: previous)', default=1)
    parser.add_argument('-l', '--list', action='store_true', help='list all available reports')
    args = parser.parse_args()

    #print("Running archive change report")
    reports = all_reports()

    if args.list:
        for i, report in enumerate(reports):
            print(f'[{i}] {report}')
    else:
        reference = find_report(args.reference, reports)
        target = find_report(args.target, reports)

        print('Change report:')
        print(f'    Target:    [{target}] {reports[target]}')
        print(f'    Reference: [{reference}] {reports[reference]}')

        reference = reports[reference]
        target = reports[target]

        for archive in archive_list:
            #print(f'Archive: {archive}')
            target_report = None
            reference_report = None
            try:
                target_report = load_report(f'{target}/{archive}.csv')
            except FileNotFoundError as e:
                print(f'No target report for {archive}.')
                pass
            try:
                reference_report = load_report(f'{reference}/{archive}.csv')
            except FileNotFoundError as e:
                print(f'No reference report for {archive}.')
                pass
            if target_report is not None and reference_report is not None:
                change = check_changes(reference_report, target_report)
                if len(change) > 0:
                    added = change[change['date1'] == '']
                    removed = change[change['date2'] == '']
                    updated = change[(change['date1'] != '') & (change['date2'] != '')]

                    for index, row in removed.iterrows():
                        print(f"MISSING {index} ({row['type1']}): {row['date1']}")
                    for index, row in added.iterrows():
                        print(f"ADDED {index} ({row['type2']}): {row['date2']}")
                    for index, row in updated.iterrows():
                        print(f"UPDATED {index} ({row['type2']}): {row['date1']} --> {row['date2']}")
            elif reference_report is not None:
                    for index, row in reference_report.iterrows():
                        print(f"MISSING {index} ({row['type']}): {row['date']}")
            elif target_report is not None:
                    for index, row in target_report.iterrows():
                        print(f"ADDED {index} ({row['type']}): {row['date']}")


