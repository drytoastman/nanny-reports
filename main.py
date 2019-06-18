#!/usr/bin/env python3

import bisect
import collections
import decimal
from functools import partial
import json
import logging
import os
import types

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
                val = list(map(str.strip, r[1].split(',')))
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
    def hours(self, name): return self.data.get(name, decimal.Decimal())
    def __repr__(self): return str(self.__dict__)

    @classmethod
    def parseSheet(cls, sheet):
        return [ cls(sheet['values'][0], r) for r in sheet['values'][1:] ]


class Reimbursement():
    def __init__(self, header, row):
        for name, val in zip(header, row):
            if name == 'Date': self.date = dateutil.parser.parse(val)
            elif name == 'Amount': self.amount = decimal.Decimal(val)
            elif name == 'Notes': self.notes = val
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
    if current_app.config['ENV'] == 'development' and os.path.isfile('config.json'):
        with open('config.json', 'r') as fp:
            config = json.load(fp)
    else:
        config = get_api().values().batchGet(spreadsheetId=current_app.config['SPREADSHEET_ID'], ranges=['Config', 'PayPeriods']).execute()
        if current_app.config['ENV'] == 'development':
            with open('config.json', 'w') as fp:
                json.dump(config, fp)
    return Config(config['valueRanges'][0]), PayPeriod.parseSheet(config['valueRanges'][1])


def get_tax_data():
    if current_app.config['ENV'] == 'development' and os.path.isfile('tax.json'):
        with open('tax.json', 'r') as fp:
            tax = json.load(fp)
    else:
        tax = get_api().values().batchGet(spreadsheetId=current_app.config['SPREADSHEET_ID'], ranges=['Single Bracket', 'Married Bracket']).execute()
        if current_app.config['ENV'] == 'development':
            with open('tax.json', 'w') as fp:
                json.dump(tax, fp)

    return TaxTables(*tax['valueRanges'])


def get_nanny_data(name):
    if current_app.config['ENV'] == 'development' and os.path.isfile('{}.json'.format(name)):
        with open('{}.json'.format(name), 'r') as fp:
            data = json.load(fp)
    else:
        data = get_api().values().batchGet(spreadsheetId=current_app.config['SPREADSHEET_ID'], ranges=['{} Hours'.format(name), '{} Reimbursements'.format(name)]).execute()
        if current_app.config['ENV'] == 'development':
            with open('{}.json'.format(name), 'w') as fp:
                json.dump(data, fp)

    return types.SimpleNamespace(hours = Hours.parseSheet(data['valueRanges'][0]), reimbursements = Reimbursement.parseSheet(data['valueRanges'][1]))


SING   = 0
BOTH   = 1
SINGOT = 2
BOTHOT = 3

def sr(index, hours, rates):
    return hours * rates[index]

def srh(index, hours, rates):
    return sr(index, hours, rates)/decimal.Decimal(2)

def hr(hours, rates):
    return (min(hours,8) * rates[BOTH]) + (max(hours-8,0) * rates[SING])

def hrh(hours, rates):
    return hr(hours, rates)/decimal.Decimal(2)


CENTS = decimal.Decimal('0.01')
def dround(val, idx):
    if val is None: return "-"
    return val.quantize(CENTS, rounding= (idx%2)!=0 and decimal.ROUND_DOWN or decimal.ROUND_UP)


def calculate_gross(sconfig, periods, taxtables):

    ret = dict()
    calcs = list()

    #      Input Hours Key,  Rate Function,          Destination Keys
    calcs.append(('Both',    partial(sr, BOTH),      ['Sum', 'Both']))
    calcs.append(('Both OT', partial(sr, BOTHOT),    ['Sum', 'Both OT']))
    calcs.append(('Sick',    hr,                     ['Sum', 'Sick']))
    calcs.append(('Holiday', hr,                     ['Sum', 'Holiday']))

    for child in sconfig.children:
        calcs.append(   ('Both',        partial(srh, BOTH),   [child + ' Sum', child + ' Both']))
        calcs.append(   ('Both OT',     partial(srh, BOTHOT), [child + ' Sum', child + ' Both OT']))
        calcs.append(   ('Sick',        hrh,                  [child + ' Sum', child + ' Sick']))
        calcs.append(   ('Holiday',     hrh,                  [child + ' Sum', child + ' Holiday']))
        calcs.append(   (child,         partial(sr, SING),    ['Sum', child + ' Sum', 'Single',    child]))
        calcs.append(   (child + ' OT', partial(sr, SINGOT),  ['Sum', child + ' Sum', 'Single OT', child + ' OT']))


    for name in sconfig.nannies:
        ndata = get_nanny_data(name)
        ret[name] = dict()

        for period in periods:
            start = period.startDate()
            end   = period.endDate()
            rates = period.rates(name)

            ret[name][end] = dict()
            p = ret[name][end]['hours'] = collections.defaultdict(decimal.Decimal)
            s = ret[name][end]['sums'] = collections.defaultdict(decimal.Decimal)

            for h in ndata.hours:
                if h.date > end: break

                # Full YTD calculations
                for hrkey, ratefunc, dkeys in calcs:
                    for dkey in dkeys:
                        p[dkey + ' YTD'] += h.hours(hrkey)
                        s[dkey + ' YTD'] += ratefunc(h.hours(hrkey), rates)

                if h.date < start: continue

                # Just current period calculations
                for hrkey, ratefunc, dkeys in calcs:
                    for dkey in dkeys:
                        p[dkey] += h.hours(hrkey)
                        s[dkey] += ratefunc(h.hours(hrkey), rates)


        ytd = collections.defaultdict(decimal.Decimal)
        for period in periods:
            start = period.startDate()
            end   = period.endDate()
            rates = period.rates(name)

            sums = ret[name][end]
            w4   = period.withholding(name)
            s    = sums['sums']
            t    = sums['tax'] = collections.defaultdict(decimal.Decimal)

            if s['Sum']:
                fed = taxtables.getTax(w4[0], w4[1], w4[2], s['Sum'])
                for child in sconfig.children:
                    fedtax   = ((s[child+' Sum'] / s['Sum']) * fed).quantize(CENTS)
                    medicare = (s[child+' Sum'] * sconfig.medicare).quantize(CENTS)
                    ss       = (s[child+' Sum'] * sconfig.social_security).quantize(CENTS)
                    waleave  = (s[child+' Sum'] * sconfig.family_leave).quantize(CENTS)
                    waunemp  = (s[child+' Sum'] * sconfig.wa_unemployment).quantize(CENTS)
                    fedunemp = (s[child+' Sum'] * sconfig.fed_unemployment).quantize(CENTS)

                    # rolling count
                    ytd['Fed']        += fedtax
                    ytd['SS']         += ss
                    ytd['Medicare']   += medicare
                    ytd['WALeave']    += waleave
                    ytd['WAUnemp']    += waunemp
                    ytd['FedUnemp']   += fedunemp
                    ytd[child+' Fed']      += fedtax
                    ytd[child+' SS']       += ss
                    ytd[child+' Medicare'] += medicare
                    ytd[child+' WALeave']  += waleave
                    ytd[child+' WAUnemp']  += waunemp
                    ytd[child+' FedUnemp'] += fedunemp

                    # child
                    t[child+' Fed']      = fedtax
                    t[child+' Medicare'] = medicare
                    t[child+' SS']       = ss
                    t[child+' WALeave']  = waleave
                    t[child+' WAUnemp']  = waunemp
                    t[child+' FedUnemp'] = fedunemp
                    for copy in ('Fed', 'SS', 'Medicare', 'WALeave', 'WAUnemp', 'FedUnemp'):
                        t['{} {} YTD'.format(child, copy)] = ytd['{} {}'.format(child, copy)]

                    # combined
                    t['Fed']        += fedtax
                    t['SS']         += ss
                    t['Medicare']   += medicare
                    t['WALeave']    += waleave
                    t['WAUnemp']    += waunemp
                    t['FedUnemp']   += fedunemp
                    for copy in ('Fed', 'SS', 'Medicare', 'WALeave', 'WAUnemp', 'FedUnemp'):
                        t[copy+' YTD'] = ytd[copy]


    return ret


def calculate_tax(sconfig, periods, taxtables, gross):

    for nanny in gross:

        for period in periods:
            sums = gross[nanny][period.endDate()]
            w4 = period.withholding(nanny)
            t = sums['tax'] = collections.defaultdict(decimal.Decimal)
            s = sums['sums']

            if sums['sums']['Sum']:
                fed = taxtables.getTax(w4[0], w4[1], w4[2], s['Sum'])
                for child in sconfig.children:
                    fedtax   = ((s[child+' Sum'] / s['Sum']) * fed).quantize(CENTS)
                    medicare = (s[child+' Sum'] * sconfig.medicare).quantize(CENTS)
                    ss       = (s[child+' Sum'] * sconfig.social_security).quantize(CENTS)

                    # child
                    t[child+' Medicare'] = medicare
                    t[child+' SS']       = ss
                    t[child+' Fed']      = fedtax

                    # rolling count
                    ytd['Fed']        += fedtax
                    ytd[child+' Fed'] += fedtax

                    # combined
                    t['Fed']            += fedtax
                    t['Fed YTD']        = ytd['Fed']
                    t[child+' Fed YTD'] = ytd[child+' Fed']


@app.route('/')
def index():

    sconfig, periods = get_config_data()
    taxtables = get_tax_data()

    gross = calculate_gross(sconfig, periods, taxtables)
#    calculate_tax(sconfig, periods, taxtables, gross)

    enddate = dateutil.parser.parse('6/16/19')
    nannyname = sconfig.nannies[0]

    period  = next(p for p in periods if p.endDate() == enddate)
    sums    = gross[nannyname][enddate]
    rates   = period.rates(nannyname)

    import pprint
    pprint.pprint(sums)

    return render_template('paystub.html', sums=sums, enddate=enddate, rates=rates, children=sconfig.children)


def common_init():
    app.config.from_envvar('SETTINGS_FILE')
    app.jinja_env.filters['dround'] = dround

if __name__ == "__main__":
    os.environ['FLASK_ENV'] = 'development'
    os.environ['SETTINGS_FILE'] = 'settings.cfg'
    common_init()
    app.run()
else:
    common_init()
