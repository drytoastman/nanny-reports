#!/usr/bin/env python3

import base64
from collections import defaultdict
import csv
import dateutil.parser
import decimal
import io
import math
import os

from flask import g, Flask, redirect, render_template, url_for

from data import *
from calc import *

app = Flask("nanny-reports")

@app.url_value_preprocessor
def preprocessor(endpoint, values):
    if values is not None:
        g.year = int(values.pop('year', 0))

@app.url_defaults
def urldefaults(endpoint, values):
    if 'year' not in values and getattr(g, 'year') and current_app.url_map.is_endpoint_expecting(endpoint, 'year'):
        values['year'] = g.year

@app.route('/')
def yearselect():
    years = [x[-4:] for x in current_app.config.keys() if x.startswith('SPREADSHEET')]
    if len(years) == 1:
        return redirect(url_for('.index', year=years[0]))
    return render_template("year.html", years=years)

@app.route('/<int:year>')
def index():
    sconfig, periods = get_config_data()
    return render_template("selector.html", sconfig=sconfig, periods=periods)

@app.route('/<int:year>/tax')
def tax():
    sconfig, periods = get_config_data()
    taxtables = get_tax_data()
    lastp     = periods[-1].endDate()
    data      = dict()
    ndata     = dict()
    for name in sconfig.nannies:
        ndata[name] = nanny_calculate(sconfig, periods, taxtables, name, get_nanny_data(name))


    for child in sconfig.children:
        data[child] = dict()
        data[child]['scheduleH'] = defaultdict(decimal.Decimal)
        for nanny in sconfig.nannies:
            data[child][nanny] = dict()
            data[child][nanny]['gross']    = ndata[nanny][lastp]['sums'][child+' Gross YTD']
            data[child][nanny]['ss']       = ndata[nanny][lastp]['tax'][child+' SS1 YTD']
            data[child][nanny]['medicare'] = ndata[nanny][lastp]['tax'][child+' Medicare1 YTD']
            data[child][nanny]['fed']      = ndata[nanny][lastp]['tax'][child+' Fed YTD']

            data[child]['scheduleH']['gross']     += ndata[nanny][lastp]['sums'][child+' Gross YTD']
            data[child]['scheduleH']['futagross'] += min(ndata[nanny][lastp]['sums'][child+' Gross YTD'], sconfig.fed_unemployment_base)
            data[child]['scheduleH']['fed']       += ndata[nanny][lastp]['tax'][child+' Fed YTD']
            data[child]['scheduleH']['waunemp']   += ndata[nanny][lastp]['tax'][child+' WAUnemp YTD']

        data[child]['scheduleH']['ss']       = (data[child]['scheduleH']['gross']     * sconfig.social_security).quantize(CENTS)
        data[child]['scheduleH']['medicare'] = (data[child]['scheduleH']['gross']     * sconfig.medicare).quantize(CENTS)
        data[child]['scheduleH']['futa']     = (data[child]['scheduleH']['futagross'] * sconfig.fed_unemployment).quantize(CENTS)


    wadata = dict()
    csvdata = dict()
    for child in sconfig.children:
        wadata[child]  = {ii:{n:dict(hours=0,wages=0) for n in sconfig.nannies} for ii in range(1,5)}
        csvdata[child] = dict()

        for period in periods:
            quarter = math.ceil(period.payDate().month/3)
            pend    = period.endDate()
            for nanny in sconfig.nannies:
                wadata[child][quarter][nanny]['hours'] += ndata[nanny][period.endDate()]['hours'].get(child+' Gross', 0)
                wadata[child][quarter][nanny]['wages'] += ndata[nanny][period.endDate()]['sums'].get(child+' Gross', 0)

        for quarter in range(1,5):
            # Create the WA quarter reports
            esdoutput   = io.StringIO()
            leaveoutput = io.StringIO()
            esdwriter   = csv.writer(esdoutput,   quoting=csv.QUOTE_ALL)
            leavewriter = csv.writer(leaveoutput, quoting=csv.QUOTE_ALL)

            # Filter out employees with 0 wages
            wadata[child][quarter] = {k:v for k,v in wadata[child][quarter].items() if v['wages']}

            for nanny, res in wadata[child][quarter].items():
                first,last = nanny.split(' ')
                ssn = sconfig.ssn(nanny)
                res['hours'] = res['hours'].quantize(INTEG, rounding=decimal.ROUND_UP)
                res['wages'] = res['wages'].quantize(CENTS)

                esdwriter.writerow([ssn, last+','+first,  res['hours'], res['wages']])
                leavewriter.writerow([ssn, last, first, '', res['hours'], res['wages']])

            csvdata[child][quarter] = dict(esd=esdoutput.getvalue(), leave=leaveoutput.getvalue())

    return render_template('tax.html', sconfig=sconfig, data=data, wadata=wadata, csvdata=csvdata)


@app.route('/<int:year>/paystub/<enddate>/<nannyname>')
def paystub(enddate, nannyname):

    enddate   = dateutil.parser.parse(enddate.replace('_','/')) 

    sconfig, periods = get_config_data()
    taxtables = get_tax_data()
    ndata     = get_nanny_data(nannyname)
    results   = nanny_calculate(sconfig, periods, taxtables, nannyname, ndata)

    period  = next(p for p in periods if p.endDate() == enddate)
    hours   = [h for h in ndata.hours if period.startDate() <= h.date <= period.endDate()]
    reimb   = [r for r in ndata.reimbursements if period.startDate() <= r.date <= period.endDate()]
    psums   = results[enddate]
    rates   = period.rates(nannyname)

    return render_template('paystub.html', sums=psums, hours=hours, reimb=reimb, period=period, rates=rates, sconfig=sconfig, nanny=nannyname, children=sconfig.children)


def common_init():
    def dpercent(val):
        if val is None: return ""
        if type(val) is str: return val
        return "{:.2f}%".format(val*100)

    def dollar(val, twoplaces=False):
        if not val: return ""
        if type(val) is str: return val
        if isinstance(val, collections.Iterator): return ','.join(map(str,val))
        if twoplaces:
            return "${:,}".format(val.quantize(CENTS))
        return "${:,}".format(val)

    def h2(val):
        if not val: return ""
        return val.quantize(CENTS)

    def h3(val):
        if not val: return ""
        return val.quantize(THRENTS)

    app.config.from_envvar('SETTINGS_FILE')
    app.jinja_env.filters['dpercent'] = dpercent
    app.jinja_env.filters['dollar']   = dollar
    app.jinja_env.filters['h2']       = h2
    app.jinja_env.filters['h3']       = h3

if __name__ == "__main__":
    os.environ['FLASK_ENV'] = 'development'
    os.environ['SETTINGS_FILE'] = 'settings.cfg'
    common_init()
    app.run()
else:
    common_init()
