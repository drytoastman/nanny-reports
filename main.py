#!/usr/bin/env python3

import json
import logging
import os

import dateutil.parser
from flask import current_app, Flask, redirect, render_template, request, session, url_for
from googleapiclient.discovery import build
from google.oauth2 import service_account

app = Flask("nanny-reports")
app.config.from_envvar('SETTINGS_FILE')

secretfile = os.path.join(os.getcwd(), 'creds.json')
credentials = service_account.Credentials.from_service_account_file(secretfile, scopes=['https://www.googleapis.com/auth/spreadsheets.readonly'])

class Config():
    def __init__(self, rows):
        for r in rows:
            setattr(self, r[0], r[1].split(','))

@app.route('/data')
def data():
    service = build('sheets', 'v4', credentials=credentials)
    sheet   = service.spreadsheets()

    config = sheet.values().batchGet(spreadsheetId=current_app.config['SPREADSHEET_ID'], ranges=['Config', 'PayPeriods', 'Single Bracket', 'Married Bracket']).execute()
    with open('config.json', 'w') as fp:
        json.dump(config, fp)

    sconfig = Config(config['valueRanges'][0]['values'])
    for name in sconfig.Nannies:
        data = sheet.values().batchGet(spreadsheetId=current_app.config['SPREADSHEET_ID'], ranges=['{} Hours'.format(name), '{} Reimbursements'.format(name)]).execute()
        with open('{}.json'.format(name), 'w') as fp:
            json.dump(data, fp)

    return "done"

@app.route('/')
def index():
    with open('data.json', 'r') as fp:
        result = json.load(fp)['valueRanges']

    startdate = dateutil.parser.parse('5/20/19')
    enddate = dateutil.parser.parse('6/2/19')
    rate1 = 21
    rate2 = 25

    c1total = 0.0
    c2total = 0.0
    btotal = 0.0
    for d, c1, c2, b in zip(*[d['values'] for d in result]):
        if not d: continue
        date   = dateutil.parser.parse(d[0])
        child1 = c1 and float(c1[0]) or 0.0
        child2 = c2 and float(c2[0]) or 0.0
        both   = b and float(b[0]) or 0.0

        if startdate <= date <= enddate:
            c1total += child1*rate1
            c2total += child2*rate1
            btotal  += both*rate2

    return "{} {} {}".format(c1total+(btotal/2), c2total+(btotal/2), (c1total+c2total+btotal))

if __name__ == "__main__":
    app.run()
