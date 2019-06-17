#!/usr/bin/env python3

import bisect
import decimal
import json
import logging
import os

import dateutil.parser
from flask import current_app, Flask, g, redirect, render_template, request, session, url_for
import googleapiclient.discovery
from google.oauth2 import service_account


secretfile  = os.path.join(os.getcwd(), 'creds.json')
credentials = service_account.Credentials.from_service_account_file(secretfile, scopes=['https://www.googleapis.com/auth/spreadsheets.readonly'])
app = Flask("nanny-reports")

class Config():
    def __init__(self, sheet):
        for r in sheet['values']:
            name = r[0].replace(' ', '_').lower()
            if name in ('children', 'nannies'):
                val = r[1].split()
            else:
                val = decimal.Decimal(r[1])
            setattr(self, name, val)


class PayPeriod():
    def __init__(self, header, data):
        self.data = dict()
        for name, val in zip(header, data):
            self.data[name] = val

    def startDate(self):         return dateutil.parser.parse(self.data['Start'])
    def endDate(self):           return dateutil.parser.parse(self.data['End'])
    def payDate(self):           return dateutil.parser.parse(self.data['PayDate'])
    def rates(self, name):       return list(map(decimal.Decimal, map(str.strip, self.data['{} Rates'.format(name)].split(','))))
    def withholding(self, name): return list(map(int, map(str.strip, self.data['{} Withholding'.format(name)].split(','))))
    def __repr__(self):          return str(self.__dict__)

    @classmethod
    def parseSheet(cls, sheet):
        return [ cls(sheet['values'][0], r) for r in sheet['values'][1:] ]


class TaxTables():
    def __init__(self, single, married):
        self.single  = self.parseSheet(single)
        self.married = self.parseSheet(married)

    def parseSheet(self, sheet):
        amounts = []
        allowances = []
        for row in sheet['values'][1:]:
            amounts.append(decimal.Decimal(row[0]))
            allowances.append(list(map(decimal.Decimal, row[1:])))

        return (amounts, allowances)

    def getTax(self, married, allow, extra, gross):
        if married:
            amounts = self.married[0]
            allowances = self.married[1]
        else:
            amounts = self.single[0]
            allowances = self.single[1]

        return allowances[bisect.bisect_left(amounts, gross)-1][allow] + extra


class Hours():
    def __init__(self, header, row):
        self.data = dict()
        for name, val in zip(header, row):
            if name == 'Day':
                pass
            elif name == 'Date':
                self.date = dateutil.parser.parse(val)
            else:
                self.data[name] = val and decimal.Decimal(val) or decimal.Decimal(0)

    def date(self): return self.date
    def both(self): return self.data['Both']
    def bothot(self): return self.data['Both OT']
    def child(self, name): return self.data[name]
    def childot(self, name): return self.data[name + ' OT']
    def __repr__(self): return str(self.__dict__)

    @classmethod
    def parseSheet(cls, sheet):
        return [ cls(sheet['values'][0], r) for r in sheet['values'][1:] ]


def get_api():
    if not hasattr(g, 'api'):
        service = googleapiclient.discovery.build('sheets', 'v4', credentials=credentials)
        g.api   = service.spreadsheets()
    return g.api


def get_config_data():
    config  = None

    if current_app.config['ENV'] == 'development' and os.path.isfile('config.json'):
        with open('config.json', 'r') as fp:
            config = json.load(fp)
    else:
        config = get_api().values().batchGet(spreadsheetId=current_app.config['SPREADSHEET_ID'], ranges=['Config', 'PayPeriods', 'Single Bracket', 'Married Bracket']).execute()
        if current_app.config['ENV'] == 'development':
            with open('config.json', 'w') as fp:
                json.dump(config, fp)

    sconfig   = Config(config['valueRanges'][0])
    periods   = PayPeriod.parseSheet(config['valueRanges'][1])
    taxtables = TaxTables(*config['valueRanges'][2:])

    return sconfig, periods, taxtables


def get_nanny_data(name):
    data = None

    if current_app.config['ENV'] == 'development' and os.path.isfile('{}.json'.format(name)):
        with open('{}.json'.format(name), 'r') as fp:
            data = json.load(fp)
    else:
        data = get_api().values().batchGet(spreadsheetId=current_app.config['SPREADSHEET_ID'], ranges=['{} Hours'.format(name), '{} Reimbursements'.format(name)]).execute()
        if current_app.config['ENV'] == 'development':
            with open('{}.json'.format(name), 'w') as fp:
                json.dump(data, fp)

    return data


@app.route('/')
def index():

    sconfig, periods, taxtables = get_config_data()

    return render_template('test.html', hours=Hours.parseSheet())

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
        child1 = c1 and decimal.Decimal(c1[0]) or 0.0
        child2 = c2 and decimal.Decimal(c2[0]) or 0.0
        both   = b and decimal.Decimal(b[0]) or 0.0

        if startdate <= date <= enddate:
            c1total += child1*rate1
            c2total += child2*rate1
            btotal  += both*rate2

    return "{} {} {}".format(c1total+(btotal/2), c2total+(btotal/2), (c1total+c2total+btotal))



if __name__ == "__main__":
    os.environ['FLASK_ENV'] = 'development'
    os.environ['SETTINGS_FILE'] = 'settings.cfg'
    app.config.from_envvar('SETTINGS_FILE')
    app.run()
else:
    app.config.from_envvar('SETTINGS_FILE')
