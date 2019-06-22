
import bisect
import dateutil.parser
import decimal
import json
import os
import types

from flask import current_app, g
from google.oauth2 import service_account
import googleapiclient.discovery

secretfile  = os.path.join(os.getcwd(), 'creds.json')
credentials = service_account.Credentials.from_service_account_file(secretfile, scopes=['https://www.googleapis.com/auth/spreadsheets.readonly'])


DEC0 = decimal.Decimal(0)

def str2dec(val):
    sval = val.replace(',', '').replace('%','').strip()
    ret = decimal.Decimal(sval)
    if val[-1] == '%':
        ret = ret/100
    return ret


class Config():
    def __init__(self, sheet):
        self.lists = dict(nanny=dict(), child=dict(), employer=dict(), sickaccum=dict())

        for r in sheet['values']:
            if r[0][-1].isdigit():
                name,num = r[0].split()
                num = int(num)
                if name == 'Nanny':
                    self.lists['nanny'][num] = r[1].split('\n')
                elif name == 'Child':
                    self.lists['child'][num] = r[1]
                elif name == 'Employer':
                    self.lists['employer'][num] = r[1]
                elif name == 'SickAccum':
                    self.lists['sickaccum'][num] = str2dec(r[1])
            else:
                name = r[0].replace(' ', '_').lower()
                val = str2dec(r[1])
                setattr(self, name, val)


    @property
    def nannies(self):
        return [x[0] for x in self.lists['nanny'].values()]

    def nannyidx(self, nanny):
        return next(k for k,v in self.lists['nanny'].items() if v[0] == nanny)

    def ssn(self, nanny):
        return self.lists['nanny'][self.nannyidx(nanny)][1]

    def sickaccum(self, nanny, hours):
        hoursper = self.lists['sickaccum'][self.nannyidx(nanny)]
        if hoursper:  return hours/hoursper
        return 0


    @property
    def children(self):
        return self.lists['child'].values()

    def childidx(self, child):
        return next(k for k,v in self.lists['child'].items() if v == child)

    def employer(self, child):
        return self.lists['employer'][self.childidx(child)]

    def ein(self, child):
        return self.employer(child).split()[-1]

    def ename(self, child):
        return self.employer(child).split()[0]


class PayPeriod():
    def __init__(self, header, data):
        self.data = dict()
        for name, val in zip(header, data):
            self.data[name] = val

    def startDate(self):         return dateutil.parser.parse(self.data['Start'])
    def endDate(self):           return dateutil.parser.parse(self.data['End'])
    def payDate(self):           return dateutil.parser.parse(self.data['PayDate'])
    def rates(self, name):       return list(map(str2dec, map(str.strip, self.data['{} Rates'.format(name)].split(','))))
    def withholding(self, name): return list(map(int, map(str.strip, self.data['{} Withholding'.format(name)].split(','))))
    def __repr__(self):          return str(self.__dict__)

    @classmethod
    def parseSheet(cls, sheet):
        return sorted([ cls(sheet['values'][0], r) for r in sheet['values'][1:] ], key=lambda x: x.endDate())


class TaxTables():
    def __init__(self, single, married):
        self.single  = self.parseSheet(single)
        self.married = self.parseSheet(married)

    def parseSheet(self, sheet):
        amounts = []
        allowances = []
        for row in sheet['values'][1:]:
            amounts.append(str2dec(row[0]))
            allowances.append(list(map(str2dec, row[1:])))

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
                self.data[name] = val and str2dec(val) or DEC0

    def date(self): return self.date
    def hours(self, name): return self.data.get(name, DEC0)
    def __repr__(self): return str(self.__dict__)

    @classmethod
    def parseSheet(cls, sheet):
        return [ cls(sheet['values'][1], r) for r in sheet['values'][2:] ]


class Reimbursement():
    def __init__(self, header, row):
        for name, val in zip(header, row):
            if name == 'Date': self.date = dateutil.parser.parse(val)
            elif name == 'Amount': self.amount = str2dec(val)
            elif name == 'Notes': self.notes = val
    def __repr__(self): return str(self.__dict__)

    @classmethod
    def parseSheet(cls, sheet):
        return [ cls(sheet['values'][1], r) for r in sheet['values'][2:] ]


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

