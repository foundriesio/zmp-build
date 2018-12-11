#!/usr/bin/env python3

# Copyright 2018 Open Source Foundries, Limited
# Copyright 2018 Foundries.io, Limited
# SPDX-License-Identifier: Apache-2.0

'''Zephyr "what's new"? script.

This is a helper script for understanding what's happened in Zephyr
since a particular point in time. It looks at changes in an "upstream"
Zephyr tree that are not present in a "downstream" tree, and outputs
information on the differences between them.

This information is useful for general understanding, for creating
mergeup commit messages, etc.
'''

import abc
import argparse
from datetime import date
from itertools import chain
import os
import platform
import shlex
from subprocess import check_output
import sys
import textwrap
import time

from pygit2_helpers import commit_shortsha, commit_shortlog
from zephyr_helpers import AREAS, ZephyrRepoAnalyzer, shortlog_area, \
    UnknownCommitsError


#
# Helpers
#

def get_user_full_name():
    '''Get logged-in user's full name from process context and system.'''
    # Currently Linux-only.
    if platform.system() != 'Linux':
        raise NotImplementedError()
    getent_cmd = ['getent', 'passwd', os.environ['USER']]
    gecos_field = check_output(getent_cmd).split(b':')[4]
    name = gecos_field.split(b',', 1)[0]
    return name.decode(sys.getdefaultencoding())


#
# Output formatting
#

class ZephyrOutputFormatter(abc.ABC):
    '''Abstract base class for output formatters.'''

    @classmethod
    @abc.abstractmethod
    def names(cls):
        '''Name(s) of the output format'''

    @classmethod
    def get_by_name(cls, name):
        '''Get an output formatter class by format name.'''
        for sub_cls in ZephyrOutputFormatter.__subclasses__():
            names = sub_cls.names()
            if isinstance(names, str):
                if name == names:
                    return sub_cls
            else:
                if name in names:
                    return sub_cls
        raise ValueError('no output formatter for {}'.format(name))

    @abc.abstractmethod
    def get_output(self, repo_analysis, context=None):
        '''Get formatted output from a repo analysis.

        For now, this must be print()able.'''


class ZephyrTextFormatMixin:
    '''Plain text output formatter mix-in class.
    '''

    def get_output(self, analysis, context=None):
        '''Render the output.'''
        if context is None:
            context = {}
        preamble = self.preamble(analysis, context)
        highlights = self._highlights(analysis, context)
        individual_changes = self._individual_changes(analysis, context)
        postamble = self.postamble(analysis, context)
        return '\n'.join(chain(preamble, highlights, individual_changes,
                               postamble))

    def preamble(self, analysis, context):
        '''Subclass override hook for introductory or preamble sections.

        Should return a list of lines.'''
        return []

    def postamble(self, analysis, context):
        '''Subclass override hook for closing or postamble sections.

        Should return a list of lines.'''
        return []

    def upstream_commit_line(self, commit, merge_day=False):
        '''Get a line about the given upstream commit.'''
        if merge_day:
            merged = self.commit_merge_day(commit)
            return '- {} {}, merged {}'.format(commit_shortsha(commit),
                                               commit_shortlog(commit),
                                               merged)
        else:
            return '- {} {}'.format(commit_shortsha(commit),
                                    commit_shortlog(commit))

    def commit_merge_day(self, commit):
        '''Get a locale-specific day the commit was merged.'''
        return time.strftime('%-d %B %Y', time.localtime(commit.commit_time))

    def _highlights(self, analysis, context):
        '''Create a mergeup commit log message template.

        Groups the iterable of upstream commits by area, dumping a message
        and exiting if any are unknown. Otherwise, returns a highlights
        template followed by the commit shortlogs grouped by area.

        The sha_to_area dict maps SHA prefixes to commit areas, and
        overrides the guesses otherwise made by this routine from the
        shortlog.
        '''
        first, last = analysis.upstream_commit_range
        return (['Highlights',
                 '==========',
                 '',
                 '<Top-level highlights go here>',
                 '',
                 'This {} covers the following inclusive commit range:'.format(self.names()[0]),  # noqa: E501
                 '',
                 self.upstream_commit_line(first, merge_day=True),
                 self.upstream_commit_line(last, merge_day=True),
                 ''])

    def _upstream_area_message(self, area, commits):
        '''Given an area and its commits, get mergeup commit text.'''
        return '\n'.join(
            ['{} ({}):'.format(area, len(commits)),
             ''] +
            list(self.upstream_commit_line(c) for c in commits) +
            [''])

    def _areas_summary(self, analysis):
        '''Get mergeup commit text summary for all areas.'''
        area_counts = analysis.upstream_area_counts
        total = sum(area_counts.values())

        def area_count_str_len(area):
            count = area_counts[area]
            return len(str(count))
        areas_sorted = sorted(area_counts)

        ret = [
            'Patches by area ({} patches total):'.format(total),
            '',
        ]
        for area in areas_sorted:
            patch_count = area_counts[area]
            ret.append('- {}: {}'.format(area, patch_count))
        ret.append('')

        return ret

    def _individual_changes(self, analysis, context):
        area_logs = {}
        for area, patches in analysis.upstream_area_patches.items():
            area_logs[area] = self._upstream_area_message(area, patches)

        return (
            ['Individual Changes',
             '==================',
             ''] +
            self._areas_summary(analysis) +
            [area_logs[area] for area in sorted(area_logs)])


class ZephyrMergeupFormatter(ZephyrTextFormatMixin, ZephyrOutputFormatter):
    '''Mergeup commit message format, plain text.

    This includes a summary of outstanding downstream patches, and may
    print warnings if there are likely reverted downstream commits'''

    @classmethod
    def names(cls):
        return ['mergeup', 'mergeup-message']

    def preamble(self, analysis, context):
        return [
            "[FIO mergeup] Merge 'zephyrproject-rtos/master' into 'osf-dev/master'",  # noqa: E501
            ''
            ]

    def postamble(self, analysis, context):
        outstanding = analysis.downstream_outstanding_patches
        likely_merged = analysis.downstream_merged_patches
        ret = []

        def addl(line, comment=False):
            if comment:
                if line:
                    ret.append('# {}'.format(line))
                else:
                    ret.append('#')
            else:
                ret.append(line)

        addl('Outstanding Downstream patches')
        addl('==============================')
        addl('')
        for sl, c in outstanding.items():
            addl('- {} {}'.format(commit_shortsha(c), sl))
        addl('')

        if not likely_merged:
            return ret

        addl('Likely merged downstream patches:', True)
        addl('IMPORTANT: You probably need to revert these and re-run!', True)
        addl('           Make sure to check the above as well; these are',
             True)
        addl("           guesses that aren't always right.", True)
        addl('', True)
        for sl, commits in likely_merged.items():
            addl('- "{}", likely merged as one of:'.format(sl), True)
            for c in commits:
                addl('\t- {} {}'.format(commit_shortsha(c),
                                        commit_shortlog(c)),
                     True)
            addl('', True)

        return ret


class ZephyrNewsletterFormatter(ZephyrTextFormatMixin, ZephyrOutputFormatter):
    '''Newsletter Markdown format, for blog posts.

    This doesn't include a summary of outstanding downstream commits.'''

    @classmethod
    def names(cls):
        return ['newsletter', 'news']

    def preamble(self, analysis, context):
        datestamp = date.today().strftime('%d %B %Y')
        datestamp_hugo = date.today().strftime('%Y-%m-%d')
        author = context.get('author', None) or get_user_full_name()

        return [
            # Hugo blogging front matter.
            '+++',
            'title = "Zephyr Development News {}"'.format(datestamp),
            'date = "{}"'.format(datestamp_hugo),
            'tags = ["zephyr"]',
            'categories = ["zephyr-news"]',
            'banner = "img/banners/zephyr.png"',
            'author = "{}"'.format(author),
            '+++',
            '',

            # Introductory boilerplate.
            'This is the {} newsletter tracking the latest'.format(datestamp),
            '[Zephyr](https://zephyrproject.org) development merged into the',
            '[mainline tree on',
            'GitHub](https://github.com/zephyrproject-rtos/zephyr).',
            '',
            '<!--more-->',
            '',
            '{{% toc %}}',  # toc is a foundries.io specific hugo shortcode
            '',
        ]

    def upstream_commit_line(self, commit, merge_day=False):
        '''Get a line about the given upstream commit.'''
        full_oid = str(commit.oid)
        link = ('https://github.com/zephyrproject-rtos/zephyr/commit/' +
                full_oid)
        if merge_day:
            merged = self.commit_merge_day(commit)
            return '- [{}]({}) {}, merged {}'.format(commit_shortsha(commit),
                                                     link,
                                                     commit_shortlog(commit),
                                                     merged)
        else:
            return '- [{}]({}) {}'.format(commit_shortsha(commit),
                                          link,
                                          commit_shortlog(commit))


def dump_unknown_commit_help(unknown_commits):
    msg = """\
    Error: can't build mergeup log message.

    The following commits have unknown areas:

    {}

    You can manually specify areas like so:

    {}

    Where each AREA is taken from the list:

    \t{}

    You can also update AREA_TO_SHORTLOG_RES in {}
    to permanently associate an area with this type of shortlog.
    """
    unknown_as_list = ['- {} {}'.format(commit_shortsha(c),
                                        commit_shortlog(c))
                       for c in unknown_commits]
    try_instead = chain((shlex.quote(a) for a in sys.argv),
                        ('--set-area={}:AREA'.format(commit_shortsha(c))
                         for c in unknown_commits))
    print(textwrap.dedent(msg).format('\n'.join(unknown_as_list),
                                      ' '.join(try_instead),
                                      '\n\t'.join(AREAS),
                                      __file__),
          file=sys.stderr)


def main(args):
    repo_path = args.repo
    if repo_path is None:
        repo_path = os.getcwd()

    analyzer = ZephyrRepoAnalyzer(repo_path, args.downstream_ref,
                                  ('OSF', 'FIO'),
                                  ('@opensourcefoundries.com',
                                   '@foundries.io'),
                                  args.upstream_ref,
                                  sha_to_area=args.sha_to_area,
                                  area_by_shortlog=args.area_by_shortlog)
    try:
        analysis = analyzer.analyze()
    except UnknownCommitsError as e:
        dump_unknown_commit_help(e.args)
        sys.exit(1)

    try:
        formatter_cls = ZephyrOutputFormatter.get_by_name(args.format)
    except ValueError as e:
        # TODO add some logic to print the choices
        print('Error:', '\n'.join(e.args), file=sys.stderr)
        sys.exit(1)

    formatter = formatter_cls()
    if args.format in ZephyrNewsletterFormatter.names():
        context = {'author': args.newsletter_author}
    else:
        context = None
    output = formatter.get_output(analysis, context=context)
    print(output)


if __name__ == '__main__':
    formats = tuple(
        chain.from_iterable(f.names() for f in
                            ZephyrOutputFormatter.__subclasses__()))
    parser = argparse.ArgumentParser(description='''Zephyr mergeup helper
                                     script. This script currently just
                                     prints the mergeup commit message.''')
    group = parser.add_argument_group('repository options')
    group.add_argument('--downstream-ref', default='osf-dev/master',
                       help='''downstream git revision (commit-ish) to analyze
                       upstream differences with. Default is osf-dev/master
                       [sic; this is a legacy from the OSF days].''')
    group.add_argument('--fio-ref', dest='downstream_ref',
                       help=argparse.SUPPRESS)  # For backwards compatibility
    group.add_argument('--upstream-ref', default='upstream/master',
                       help='''Upstream ref (commit-ish) whose differences
                       with --downstream-ref to analyze. Default is
                       upstream/master.''')
    group.add_argument('-A', '--set-area', default=[], action='append',
                       help='''Format is sha:Area; associates an area with
                       a commit SHA. Use --areas to print all areas.''')
    group.add_argument('-p', '--set-area-prefix', default=[], action='append',
                       help='''Format is prefix:Area; associates an area prefix
                       (which must be a literal string for now) to a given
                       area.''')

    group = parser.add_argument_group('output formatting options')
    group.add_argument('-f', '--format', default='newsletter',
                       choices=formats,
                       help='''Output format, default is "newsletter"''')
    group.add_argument('--newsletter-author',
                       help='Override newsletter author full name')

    group = parser.add_argument_group('miscellaneous options')
    group.add_argument('--areas', action='store_true',
                       help='''Print all areas that upstream commits are
                       grouped into in mergeup commit logs, and exit.''')
    group.add_argument('--self-test', action='store_true',
                       help=argparse.SUPPRESS)  # for backwards compatibility

    parser.add_argument('repo', nargs='?',
                        help='''Path to the zephyr repository. If not given,
                        the current working directory is assumed.''')

    args = parser.parse_args()

    if args.self_test:
        sys.exit('this is deprecated; use "py.test test_*.py" instead')
    if args.areas:
        print('\n'.join(AREAS))
        sys.exit(0)

    sha_to_area = dict()
    for sha_area in args.set_area:
        sha, area = sha_area.split(':')
        if area not in AREAS:
            print('Invalid area {} for commit {}.'.format(area, sha),
                  file=sys.stderr)
            print('Choices:', ', '.join(AREAS), file=sys.stderr)
            sys.exit(1)
        sha_to_area[sha] = area
    args.sha_to_area = sha_to_area

    if args.set_area_prefix:
        prefix_area_map = {}
        for pa in args.set_area_prefix:
            prefix, area = pa.split(':')
            prefix_area_map[prefix] = area

        def area_by_shortlog(prefix):
            return prefix_area_map.get(prefix)
    else:
        area_by_shortlog = None
    args.area_by_shortlog = area_by_shortlog

    main(args)
