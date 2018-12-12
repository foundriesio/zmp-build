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


def shortlog_has_sauce(shortlog, sauce):
    '''Check if a Git shortlog has a 'sauce tag'.

    :param shortlog: Git commit message shortlog, which might begin
                     with a "sauce tag" that looks like '[sauce <tag>] '
    :param sauce: String (or iterable of strings) indicating a source of
                  "sauce". This is organization-specific.

    For example, sauce="xyz" and the shortlog is:

    [xyz fromlist] area: something

    Then the return value is True. If the shortlog is any of these,
    the return value is False:

    area: something
    [abc fromlist] area: something
    [WIP] area: something
    '''
    if isinstance(sauce, str):
        sauce = '[' + sauce
    else:
        sauce = tuple('[' + s for s in sauce)

    return shortlog.startswith(sauce)


def shortlog_no_sauce(shortlog, sauce):
    '''Return a Git shortlog without a 'sauce tag'.

    :param shortlog: Git commit message shortlog, which might begin
                     with a "sauce tag" that looks like '[sauce <tag>] '
    :param sauce: String (or iterable of strings) indicating a source of
                  "sauce". This is organization-specific.

    For example, sauce="xyz" and the shortlog is:

    "[xyz fromlist] area: something"

    Then the return value is "area: something".

    As another example with the same sauce, if shortlog is "foo: bar",
    the return value is "foo: bar".
    '''
    if isinstance(sauce, str):
        sauce = '[' + sauce
    else:
        sauce = tuple('[' + s for s in sauce)

    if shortlog.startswith(sauce):
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
