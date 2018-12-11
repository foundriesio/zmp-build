# Copyright 2018 Open Source Foundries, Limited
# SPDX-License-Identifier: Apache-2.0

from datetime import datetime, timezone, timedelta

import pygit2


def repo_commits(repository_path, start_sha, end_sha,
                 sort=pygit2.GIT_SORT_TIME | pygit2.GIT_SORT_TOPOLOGICAL,
                 filter=None):
    if filter is None:
        def filter(commit):
            return True

    repository = pygit2.init_repository(repository_path)
    sort = pygit2.GIT_SORT_TIME | pygit2.GIT_SORT_TOPOLOGICAL
    start = repository.revparse_single(start_sha).oid
    end = repository.revparse_single(end_sha).oid
    walker = repository.walk(end, sort)
    walker.hide(start)
    return [c for c in walker if filter(c)]


def shortlog_is_revert(shortlog):
    return shortlog.startswith('Revert ')


def shortlog_reverts_what(shortlog):
    revert = 'Revert '
    return shortlog[len(revert) + 1:-1]


def shortlog_no_sauce(shortlog):
    if shortlog.startswith(('[OSF', '[FIO')):
        return shortlog[shortlog.find(']')+1:].strip()
    else:
        return shortlog


def commit_date(commit):
    author_timestamp = float(commit.author.time)
    author_time_offset = commit.author.offset
    author_tz = timezone(timedelta(minutes=author_time_offset))
    return datetime.fromtimestamp(author_timestamp, author_tz)


def commit_shortsha(commit, len=8):
    '''Return a short version of the commit SHA.'''
    return str(commit.oid)[:len]


def commit_shortlog(commit):
    '''Return the first line in a commit's log message.'''
    return commit.message.splitlines()[0]


def commit_is_fio(commit):
    '''Returns True iff the commit is from an OSF/foundries.io email.'''
    email = commit.author.email
    return email.endswith(('@opensourcefoundries.com', '@foundries.io'))
