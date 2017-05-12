"""countcommits.py
Copyright (c) Microsoft Corporation. All rights reserved.
Licensed under the MIT License.
"""
# Audits commit counts as of a specified date, comparing the total count
# from the GitHub API against the total count in ghinsightsms on Data Lake.
import configparser
import csv
import datetime
import glob
import json
import os
import re
import sys

import requests

from shared import *

class _settings: #-----------------------------------------------------------<<<
    """This class exists to provide a namespace used for global settings.
    """
    requests_session = None # current session object from requests library
    last_ratelimit = 0 # API rate limit for the most recent API call
    last_remaining = 0 # remaining portion of rate limit after last API call

def audit_reports(asofdate, orgfilter): #------------------------------------<<<
    """Audit the total number of commits and issues by repo.

    asofdate = the date for which totals will be calculate. Typically this is
    the day prior to the current date, since the /TabularSource2 files are
    generated around the end of each business day Redmond time.

    orgfilter = list of orgs (lowercase) to include in generated audit_*
    files. If None, all orgs are included.

    Generates reports as CSV files in the data-verification folder.
    """

    # the following flags are used for testing, to avoid the need to do all
    # of the steps (some of which are time-consuming) every time. All flags
    #  should be True for a complete set of tests based on the latest data.
    download_datalake = True # download latest data files from Data Lake?
    generate_totals = True # re-generate repo totals from daily totals?
    report_commits = True # generate the commits report?
    report_issues = True # generate the issues report?

    # local file where Data Lake daily totals are stored ...
    local_dailytots = 'data/verification_activities_repo.csv'

    if download_datalake:
        print('Downloading daily totals from Data Lake ...')
        token, _ = azure_datalake_token('ghinsights')
        adls_account = setting('ghinsights', 'azure', 'adls-account')
        datalake_get_file(local_dailytots, \
            '/TabularSource2/verification_activities_repo.csv', adls_account, token)
        print('Downloading repo data from Data Lake ...')
        token, _ = azure_datalake_token('ghinsights')
        adls_account = setting('ghinsights', 'azure', 'adls-account')
        datalake_get_file('data/Repo.csv', '/TabularSource2/Repo.csv', \
            adls_account, token)

    if generate_totals:
        print('Aggregating daily totals into repo totals ...')
        dailytotals(local_dailytots, \
            'data-verification/repototals-' + asofdate + '.csv', asofdate)

    if report_commits:
        commit_report(asofdate, \
            'data-verification/audit_commits_' + asofdate + '.csv', orgfilter)

    if report_issues:
        issue_report(asofdate, \
            'data-verification/audit_issues_' + asofdate + '.csv', orgfilter)

def commit_report(asofdate, reportfile, orgfilter): #------------------------<<<
    """Generate a report summarizing commit counts."""

    # write header row to output file
    open(reportfile, 'w').write('org,repo,datalake,github\n')

    reporeader = csv.reader(open('data/Repo.csv', 'r', encoding='iso-8859-2'),
                            delimiter=',', quotechar='"')
    for values in reporeader:
        org, repo = values[11].split('/')

        if (orgfilter and not org.lower() in orgfilter) or \
            documentation_repo(repo):
            continue
        github_commits = commits_asofdate_github(org, repo, asofdate)
        datalake_commits = commits_asofdate_datalake(org, repo, asofdate)
        print(console_output(org, repo, asofdate, datalake_commits, github_commits))

        # add this data row to the output file
        open(reportfile, 'a').write( \
            ','.join([org, repo, str(datalake_commits), str(github_commits)]) + '\n')

def commits_asofdate_datalake(orgname, reponame, asofdate): #----------------<<<
    """Get a total # commits from a repo totals data file created by
    dailytotals(). (Fields: org/repo, issues, pullrequests, commits.)"""
    if not hasattr(_settings, 'repo_tot_commits'):
        # load dictionary first time this function is called
        _settings.repo_tot_commits = dict()
        for line in open('data-verification/repototals-' + asofdate + '.csv', 'r').readlines():
            orgrepo, _, _, commits = line.strip().split(',')
            _settings.repo_tot_commits[orgrepo.lower()] = int(commits)
    return _settings.repo_tot_commits.get( \
        orgname.lower() + '/' + reponame.lower(), 0)

def commits_asofdate_github(org, repo, asofdate): #--------------------------<<<
    """Return cumulative # of commits for an org/repo as of a date.

    This is an optimized approach that is based on the assumption that there
    are relatively few commits after asofdate. Performance should be good for
    recent asofdate values.
    """
    requests_session = requests.session()
    requests_session.auth = (setting('ghinsights', 'github', 'username'),
                             setting('ghinsights', 'github', 'pat'))
    v3api = {"Accept": "application/vnd.github.v3+json"}

    # handle first page
    endpoint = 'https://api.github.com/repos/' + org + '/' + repo + \
        '/commits?per_page=100&page=1'
    firstpage = requests_session.get(endpoint, headers=v3api)
    pagelinks = github_pagination(firstpage)
    totpages = int(pagelinks['lastpage'])
    lastpage_url = pagelinks['lastURL']
    jsondata = json.loads(firstpage.text)
    if 'git repository is empty' in str(jsondata).lower() or \
        'not found' in str(jsondata).lower():
        return 0
    commits_firstpage = len([commit for commit in jsondata \
        if commit['commit']['committer']['date'][:10] <= asofdate])

    if not lastpage_url:
        # just one page of results for this repo
        return commits_firstpage

    # handle last page
    lastpage = requests_session.get(lastpage_url, headers=v3api)
    commits_lastpage = len([commit for commit in json.loads(lastpage.text) \
        if commit['commit']['committer']['date'][:10] <= asofdate])
    if not commits_lastpage:
        return 0 # there are no commits before asofdate for this repo

    # scan back from first page to find start of the desired date range
    pageno = 1
    while jsondata[-1]['commit']['committer']['date'][:10] > asofdate:
        pageno += 1

        # convert endpoint into the endpoint for page # pageno ...
        endpoint = '&'.join(endpoint.split('&')[:-1]) + '&page=' + str(pageno)

        thispage = requests_session.get(endpoint, headers=v3api)
        jsondata = json.loads(thispage.text)
        commits_firstpage = len([commit for commit in jsondata \
            if commit['commit']['committer']['date'][:10] <= asofdate])

    return (totpages - pageno - 1) * 100 + commits_firstpage + commits_lastpage

def console_output(org, repo, asofdate, dl_count, gh_count):
    """Format a line of console output showing how counts compare.
    """
    if dl_count == gh_count:
        desc = '-------'
    elif dl_count > gh_count:
        desc = 'extra'
    else:
        desc = 'MISSING'
    return (org + '/' + repo).ljust(50) + ' - ' + asofdate + \
        ' - DataLake:{0:>6}, GitHub:{1:>6}'. \
        format(dl_count, gh_count) + ' ' + desc

def dailytotals(rawdata, totfile, asofdate): #-------------------------------<<<
    """Generate a file with total issues, pullrequests, commits for each repo
    as of specified date."""
    tot_issues = dict()
    tot_prs = dict()
    tot_commits = dict()
    myreader = csv.reader(open(rawdata, 'r'), delimiter=',', quotechar='"')
    for values in myreader:
        thedate = values[0]
        if thedate > asofdate:
            continue
        orgrepo = values[1]
        issues = int(values[2])
        prs = int(values[3])
        commits = int(values[4])
        if orgrepo in tot_issues:
            tot_issues[orgrepo] += issues
        else:
            tot_issues[orgrepo] = issues
        if orgrepo in tot_prs:
            tot_prs[orgrepo] += prs
        else:
            tot_prs[orgrepo] = prs
        if orgrepo in tot_commits:
            tot_commits[orgrepo] += commits
        else:
            tot_commits[orgrepo] = commits
    with open(totfile, 'w') as fhandle:
        for orgrepo in tot_issues:
            fhandle.write(','.join([orgrepo, str(tot_issues[orgrepo]), \
                str(tot_prs[orgrepo]), str(tot_commits[orgrepo])]) + '\n')

def issue_report(asofdate, reportfile, orgfilter): #-------------------------<<<
    """Generate a report summarizing issue counts."""

    # write header row to output file
    open(reportfile, 'w').write('org,repo,datalake,github\n')

    reporeader = csv.reader(open('data/Repo.csv', 'r', encoding='iso-8859-2'),
                            delimiter=',', quotechar='"')
    for values in reporeader:
        org, repo = values[11].split('/')

        if (orgfilter and not org.lower() in orgfilter) or \
            documentation_repo(repo):
            continue
        github_issues = issues_asofdate_github(org, repo, asofdate)
        datalake_issues = issues_asofdate_datalake(org, repo, asofdate)
        print(console_output(org, repo, asofdate, datalake_issues, github_issues))

        # add this data row to the output file
        open(reportfile, 'a').write( \
            ','.join([org, repo, str(datalake_issues), str(github_issues)]) + '\n')

def issues_asofdate_datalake(orgname, reponame, asofdate): #-----------------<<<
    """Get a total # issues from a repo totals data file created by
    dailytotals(). (Fields: org/repo, issues, pullrequests, commits.)"""
    if not hasattr(_settings, 'repo_tot_issues'):
        # load dictionary first time this function is called
        _settings.repo_tot_issues = dict()
        for line in open('data-verification/repototals-' + asofdate + '.csv', 'r').readlines():
            orgrepo, issues, _, _ = line.strip().split(',')
            _settings.repo_tot_issues[orgrepo.lower()] = int(issues)
    return _settings.repo_tot_issues.get( \
        orgname.lower() + '/' + reponame.lower(), 0)

def issues_asofdate_github(org, repo, asofdate): #---------------------------<<<
    """Return cumulative # of issues for an org/repo as of a date.

    This is an optimized approach that is based on the assumption that there
    are relatively few commits after asofdate. Performance should be good for
    recent asofdate values.
    """
    requests_session = requests.session()
    requests_session.auth = (setting('ghinsights', 'github', 'username'),
                             setting('ghinsights', 'github', 'pat'))
    v3api = {"Accept": "application/vnd.github.v3+json"}

    # handle first page
    endpoint = 'https://api.github.com/repos/' + org + '/' + repo + \
        '/issues?filter=all&state=all&per_page=100&page=1'
    firstpage = requests_session.get(endpoint, headers=v3api)
    pagelinks = github_pagination(firstpage)
    totpages = int(pagelinks['lastpage'])
    lastpage_url = pagelinks['lastURL']
    jsondata = json.loads(firstpage.text)

    if 'git repository is empty' in str(jsondata).lower() or \
        'not found' in str(jsondata).lower():
        return 0
    issues_firstpage = len([issue for issue in jsondata \
        if issue['created_at'][:10] <= asofdate])

    if not lastpage_url:
        # just one page of results for this repo
        return issues_firstpage

    # handle last page
    lastpage = requests_session.get(lastpage_url, headers=v3api)
    issues_lastpage = len([issue for issue in json.loads(lastpage.text) \
        if issue['created_at'][:10] <= asofdate])
    if not issues_lastpage:
        return 0 # there are no issues before asofdate for this repo

    # scan back from first page to find start of the desired date range
    pageno = 1
    while jsondata[-1]['created_at'][:10] > asofdate:
        pageno += 1

        # convert endpoint into the endpoint for page # pageno ...
        endpoint = '&'.join(endpoint.split('&')[:-1]) + '&page=' + str(pageno)

        thispage = requests_session.get(endpoint, headers=v3api)
        jsondata = json.loads(thispage.text)
        issues_firstpage = len([issue for issue in jsondata \
            if issue['created_at'][:10] <= asofdate])

    return (totpages - pageno - 1) * 100 + issues_firstpage + issues_lastpage

# code to be executed when running standalone
if __name__ == '__main__':
    # set console encoding to UTF8
    sys.stdout = open(sys.stdout.fileno(), mode='w', encoding='utf-8', buffering=1)

    audit_reports('2017-05-10', ['microsoft'])
