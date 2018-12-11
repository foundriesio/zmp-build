# Copyright 2018 Open Source Foundries, Limited
# SPDX-License-Identifier: Apache-2.0

from datetime import datetime, timezone, timedelta

import pygit2


def repo_commits(repository_path, start_sha, end_sha,
                 sort=pygit2.GIT_SORT_TIME | pygit2.GIT_SORT_TOPOLOGICAL,
                 filter=None):
    '''Return a sorted, filtered list of Git commits in a range.

    :param repository_path: Path to Git repository to use
    :param start_sha: Initial SHA to start walking from
    :param end_sha: Final SHA to stop walking at
    :param sort: Sorting algorithm to use; default is
                 pygit2.GIT_SORT_TIME | pygit2.GIT_SORT_TOPOLOGICAL
    :param filter: Predicate that returns True to keep a commit in the
                   range, and False to drop it. The default, None,
                   returns all commits in the range.
    '''
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
    '''Return True if and only if the shortlog starts with 'Revert '.

    :param shortlog: Git commit message shortlog.'''
    return shortlog.startswith('Revert ')


def shortlog_reverts_what(shortlog):
    '''If the shortlog is a revert, returns shortlog of what it reverted.

    :param shortlog: Git commit message shortlog

    For example, if shortlog is:

    'Revert "some_area: this turned out to be a bad idea"'

    The return value is 'some_area: this turned out to be a bad idea';
    i.e. the double quotes are also stripped.
    '''
    revert = 'Revert '
    return shortlog[len(revert) + 1:-1]


def shortlog_no_sauce(shortlog):
    '''Return a Git shortlog without a 'sauce tag'.

    :param shortlog: Git commit message shortlog

    If the shortlog starts with "[OSF" or "[FIO", returns
    the contents of the shortlog after the first ']'.

    Otherwise, returns the shortlog unaltered.

    For example, "[FIO toup] area: something" returns "area: something".
    As another example, "foo: bar" returns "foo: bar".
    '''
    if shortlog.startswith(('[OSF', '[FIO')):
        return shortlog[shortlog.find(']')+1:].strip()
    else:
        return shortlog


def commit_date(commit):
    '''Returns a datetime corresponding to the commit's author timestamp.

    :param commit: pygit2 commit object

    The returned datetime object is returned in the current locale,
    with an offset from the commit's author offset.
    '''
    author_timestamp = float(commit.author.time)
    author_time_offset = commit.author.offset
    author_tz = timezone(timedelta(minutes=author_time_offset))
    return datetime.fromtimestamp(author_timestamp, author_tz)


def commit_shortsha(commit, len=8):
    '''Return a short version of the commit SHA.

    :param commit: pygit2 commit object
    :param len: Number of leading characters in the SHA to return.
                The default is 8.
    '''
    return str(commit.oid)[:len]


def commit_shortlog(commit):
    '''Return the first line in a commit's log message.

    :param commit: pygit2 commit object'''
    return commit.message.splitlines()[0]


def commit_is_fio(commit):
    '''Returns True iff the commit is from an OSF/foundries.io email.

    :param commit: pygit2 commit object'''
    email = commit.author.email
    return email.endswith(('@opensourcefoundries.com', '@foundries.io'))
