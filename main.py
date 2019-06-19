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
            elif name.startswith('employer'):
                val = r[1]
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


def dpercent(val):
    if val is None: return ""
    if type(val) is str: return val
    return "{:.2f}%".format(val*100)

def dollar(val):
    if not val: return ""
    if type(val) is str: return val
    if isinstance(val, collections.Iterator): return ','.join(map(str,val))
    #return "${:,}".format(val)
    return "${:,.2f}".format(val)

def nozero(val):
    if not val: return ""
    return val

CENTS = decimal.Decimal('0.01')

def calculate(sconfig, periods, taxtables, name, ndata):

    ret = dict()
    calcs = list()

    #      Input Hours Key,  Rate Function,          Destination Keys
    calcs.append(('Both',    partial(sr, BOTH),      ['Gross', 'Both']))
    calcs.append(('Both OT', partial(sr, BOTHOT),    ['Gross', 'Both OT']))
    calcs.append(('Sick',    hr,                     ['Gross', 'Sick']))
    calcs.append(('Holiday', hr,                     ['Gross', 'Holiday']))

    for child in sconfig.children:
        calcs.append(   ('Both',        partial(srh, BOTH),   [child + ' Gross', child + ' Both']))
        calcs.append(   ('Both OT',     partial(srh, BOTHOT), [child + ' Gross', child + ' Both OT']))
        calcs.append(   ('Sick',        hrh,                  [child + ' Gross', child + ' Sick']))
        calcs.append(   ('Holiday',     hrh,                  [child + ' Gross', child + ' Holiday']))
        calcs.append(   (child,         partial(sr, SING),    ['Gross', child + ' Gross', 'Single',    child]))
        calcs.append(   (child + ' OT', partial(sr, SINGOT),  ['Gross', child + ' Gross', 'Single OT', child + ' OT']))


    for period in periods:
        start = period.startDate()
        end   = period.endDate()
        rates = period.rates(name)

        ret[end] = dict()
        p = ret[end]['hours'] = collections.defaultdict(decimal.Decimal)
        s = ret[end]['sums'] = collections.defaultdict(decimal.Decimal)

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

        for r in ndata.reimbursements:
            if r.date > end: break
            # Full YTD calculations
            s['Reimbursements YTD'] += r.amount
            for child in sconfig.children:
                s[child+' Reimbursements YTD'] += r.amount/2

            if r.date < start: continue
            s['Reimbursements'] += r.amount
            for child in sconfig.children:
                s[child+' Reimbursements'] += r.amount/2


    ytd = collections.defaultdict(decimal.Decimal)
    for period in periods:
        start = period.startDate()
        end   = period.endDate()
        rates = period.rates(name)

        sums = ret[end]
        w4   = period.withholding(name)
        s    = sums['sums']
        t    = sums['tax'] = collections.defaultdict(decimal.Decimal)

        gross = s['Gross']
        fed   = taxtables.getTax(w4[0], w4[1], w4[2], gross)  # fed is calculated as 1 and then divided between employers
        for child in sconfig.children:
            childgross = s[child+' Gross']
            fedtax = 0
            if gross:
                fedtax = ((childgross / gross) * fed).quantize(CENTS)
            medicare  = ((childgross * sconfig.medicare).quantize(CENTS)/2).quantize(CENTS)
            ss        = ((childgross * sconfig.social_security).quantize(CENTS)/2).quantize(CENTS)
            waleave   = (childgross * sconfig.family_leave).quantize(CENTS)
            waunemp   = (childgross * sconfig.wa_unemployment).quantize(CENTS)
            fedunemp  = (childgross * sconfig.fed_unemployment).quantize(CENTS)

            employee = fedtax   + ss + medicare + waleave 
            employer = fedunemp + ss + medicare + waunemp

            # rolling count
            ytd['Fed']        += fedtax
            ytd['SS1']        += ss
            ytd['SS2']        += ss
            ytd['Medicare1']  += medicare
            ytd['Medicare2']  += medicare
            ytd['WALeave']    += waleave
            ytd['WAUnemp']    += waunemp
            ytd['FedUnemp']   += fedunemp
            ytd['EmployeeTax'] += employee
            ytd['EmployerTax'] += employer
            ytd[child+' Fed']       += fedtax
            ytd[child+' SS1']       += ss
            ytd[child+' SS2']       += ss
            ytd[child+' Medicare1'] += medicare
            ytd[child+' Medicare2'] += medicare
            ytd[child+' WALeave']  += waleave
            ytd[child+' WAUnemp']  += waunemp
            ytd[child+' FedUnemp'] += fedunemp
            ytd[child+' EmployeeTax'] += employee
            ytd[child+' EmployerTax'] += employer

            # child
            t[child+' Fed']       = fedtax
            t[child+' SS1']       = ss
            t[child+' SS2']       = ss
            t[child+' Medicare1'] = medicare
            t[child+' Medicare2'] = medicare
            t[child+' WALeave']   = waleave
            t[child+' WAUnemp']   = waunemp
            t[child+' FedUnemp']  = fedunemp
            t[child+' EmployeeTax'] = employee
            t[child+' EmployerTax'] = employer
            for copy in ('Fed', 'SS1', 'SS2', 'Medicare1', 'Medicare2', 'WALeave', 'WAUnemp', 'FedUnemp', 'EmployeeTax', 'EmployerTax'):
                t['{} {} YTD'.format(child, copy)] = ytd['{} {}'.format(child, copy)]

            # combined
            t['Fed']        += fedtax
            t['SS1']        += ss
            t['SS2']        += ss
            t['Medicare1']  += medicare
            t['Medicare2']  += medicare
            t['WALeave']    += waleave
            t['WAUnemp']    += waunemp
            t['FedUnemp']   += fedunemp
            t['EmployeeTax'] += employee
            t['EmployerTax'] += employer
            for copy in ('Fed', 'SS1', 'SS2', 'Medicare1', 'Medicare2', 'WALeave', 'WAUnemp', 'FedUnemp', 'EmployeeTax', 'EmployerTax'):
                t[copy+' YTD'] = ytd[copy]

    return ret


@app.route('/')
def index():

    sconfig, periods = get_config_data()
    enddate   = dateutil.parser.parse('6/16/19')
    nannyname = sconfig.nannies[0]

    taxtables = get_tax_data()
    ndata     = get_nanny_data(nannyname)
    results   = calculate(sconfig, periods, taxtables, nannyname, ndata)

    period  = next(p for p in periods if p.endDate() == enddate)
    hours   = [h for h in ndata.hours if period.startDate() <= h.date <= period.endDate()]
    reimb   = [r for r in ndata.reimbursements if period.startDate() <= r.date <= period.endDate()]
    psums   = results[enddate]
    rates   = period.rates(nannyname)

    ncalc = [('Combined', '', ''), ('Combined YTD', '', ' YTD')]
    for child in sconfig.children:
        ncalc.extend([(child, child+' ', ''), (child+' YTD', child+' ', ' YTD')])
    nets = dict()
    for dest, prefix, suffix in ncalc:
        nets[dest] = (psums['sums'][prefix+'Gross'+suffix] - psums['tax'][prefix+'EmployeeTax'+suffix] + psums['sums'][prefix+'Reimbursements'+suffix])

    return render_template('paystub.html', sums=psums, nets=nets, hours=hours, reimb=reimb, period=period, rates=rates, sconfig=sconfig, nanny=nannyname, children=sconfig.children)


def common_init():
    app.config.from_envvar('SETTINGS_FILE')
    app.jinja_env.filters['dpercent'] = dpercent
    app.jinja_env.filters['dollar']   = dollar
    app.jinja_env.filters['nozero']   = nozero

if __name__ == "__main__":
    os.environ['FLASK_ENV'] = 'development'
    os.environ['SETTINGS_FILE'] = 'settings.cfg'
    common_init()
    app.run()
else:
    common_init()
