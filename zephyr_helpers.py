#!/usr/bin/env python3

# Copyright 2018 Open Source Foundries, Limited
# Copyright 2018 Foundries.io, Limited
# SPDX-License-Identifier: Apache-2.0

'''Helper module for analyzing a Zephyr tree.

Has some features that are only useful when analyzing a tree with
upstream and downstream commits as well.'''

import argparse
from collections import defaultdict, OrderedDict, namedtuple
import configparser
import logging
import json
import os
import re
import sys
from subprocess import check_output
import textwrap

import pygit2
import editdistance

from pygit2_helpers import shortlog_is_revert, shortlog_reverts_what, \
    shortlog_has_sauce, shortlog_no_sauce, commit_shortlog


# This list maps the 'area' a commit affects to a list of
# shortlog prefixes (the content before the first ':') in the Zephyr
# commit shortlogs that belong to it.
#
# The values are lists of case-insensitive regular expressions that
# are matched against the shortlog prefix of each commit. Matches are
# done with regex.fullmatch().
#
# Keep its definition sorted alphabetically by key.
#
# -----------------------------------------------------------
#     If you edit this, update test_zephyr_helpers.py
#     by adding a new test case for your changes!
# -----------------------------------------------------------
AREA_TO_SHORTLOG_RES = [
    ('Arches', ['arch(/.*)?', 'arc(/.*)?', 'arm(/.*)?', 'esp32(/.*)?',
                'native(/.*)?', 'native_posix', 'nios2(/.*)?', 'posix(/.*)?',
                'lpc(/.*)?', 'riscv(32)?(/.*)?', 'soc(/.*)?', 'x86(/.*)?',
                'xtensa(/.*)?']),
    ('Bluetooth', ['bluetooth', 'bt']),
    ('Boards', ['boards?(/.*)?', 'mimxrt1050_evk']),
    ('Build', ['build', 'clang(/.*)?', 'cmake', 'kconfig', 'gen_isr_tables?',
               'gen_syscall_header', 'genrest', 'isr_tables?',
               'ld', 'linker', 'menuconfig', 'size_report', 'toolchains?']),
    ('Continuous Integration', ['ci', 'coverage', 'sanitycheck', 'gitlint']),
    ('Cryptography', ['crypto', 'mbedtls']),
    ('Device Tree', ['dt', 'dts(/.*)?', 'dt-bindings',
                     'extract_dts_includes?']),
    ('Documentation', ['docs?(/.*)?', 'CONTRIBUTING.rst', 'doxygen']),
    ('Drivers', ['drivers?(/.*)?',
                 'adc', 'aio', 'can', 'clock_control', 'counter', 'crc',
                 'device([.]h)?', 'display', 'dma', 'entropy', 'eth',
                 'ethernet',
                 'flash', 'gpio', 'grove', 'hid', 'i2c', 'i2s',
                 'interrupt_controller', 'ipm', 'led_strip', 'led', 'netusb',
                 'pci', 'pinmux', 'pwm', 'rtc', 'sensors?(/.*)?', 'serial',
                 'shared_irq', 'spi', 'timer', 'uart', 'uart_pipe',
                 'usb(/.*)?', 'watchdog',
                 # Technically in subsys/ (or parts are), but treated
                 # as drivers
                 'console', 'random', 'storage']),
    ('External', ['ext(/.*)?', 'hal', 'stm32cube']),
    ('Firmware Update', ['dfu', 'mgmt']),
    ('Kernel',  ['kernel(/.*)?', 'poll', 'mempool', 'syscalls', 'work_q',
                 'init.h', 'userspace', 'k_queue', 'k_poll', 'app_memory']),
    ('Libraries', ['libc?', 'json', 'ring_buffer', 'lib(/.*)']),
    ('Logging', ['logging', 'logger', 'log']),
    ('Maintainers', ['CODEOWNERS([.]rst)?']),
    ('Miscellaneous', ['misc', 'release', 'shell', 'printk', 'version']),
    ('Networking', ['net(/.*)?', 'openthread', 'slip']),
    ('Power Management', ['power']),
    ('Samples', ['samples?(/.*)?']),
    ('Scripts', ['scripts?(/.*)?', 'coccinelle', 'runner', 'gen_syscalls.py',
                 'gen_syscall_header.py', 'kconfiglib', 'west']),
    ('Storage', ['fs(/.*)?', 'disks?', 'fcb', 'settings']),
    ('Testing', ['tests?(/.*)?', 'testing', 'unittest', 'ztest', 'tracing']),
    ]


def _invert_keys_val_list(kvs):
    for k, vs in kvs:
        for v in vs:
            yield v, k


# This 'inverts' the key/value relationship in AREA_TO_SHORTLOG_RES to
# make a list from shortlog prefix REs to areas.
SHORTLOG_RE_TO_AREA = [(re.compile(k, flags=re.IGNORECASE), v) for k, v in
                       _invert_keys_val_list(AREA_TO_SHORTLOG_RES)]


AREAS = [a for a, _ in AREA_TO_SHORTLOG_RES]


#
# Repository analysis
#

class InvalidRepositoryError(RuntimeError):
    pass


class UnknownCommitsError(RuntimeError):
    '''Commits with unknown areas are present.

    The exception arguments are an iterable of commits whose area
    was unknown.
    '''
    pass


class NoSuchDownstream(RuntimeError):
    '''Downstream ref is invalid.'''
    pass


class NoSuchUpstream(RuntimeError):
    '''Upstream ref is invalid.'''
    pass


def shortlog_area_prefix(shortlog):
    '''Get the prefix of a shortlog which describes its area.

    This returns the "raw" prefix as it appears in the shortlog. To
    canonicalize this to one of a known set of areas, use
    shortlog_area() instead. If no prefix is present, returns None.
    '''
    # Base case for recursion.
    if not shortlog:
        return None

    # 'Revert "foo"' should map to foo's area prefix.
    if shortlog_is_revert(shortlog):
        shortlog = shortlog_reverts_what(shortlog)
        return shortlog_area_prefix(shortlog)

    # If there is no ':', there is no area. Otherwise, the candidate
    # area is the substring up to the first ':'.
    if ':' not in shortlog:
        return None
    area, rest = [s.strip() for s in shortlog.split(':', 1)]

    # subsys: foo should map to foo's area prefix, etc.
    if area in ['subsys', 'include']:
        return shortlog_area_prefix(rest)

    return area


def shortlog_area(shortlog):
    '''Match a Zephyr commit shortlog to the affected area.

    If there is no match, returns None.'''
    area_pfx = shortlog_area_prefix(shortlog)

    if area_pfx is None:
        return None

    for test_regex, area in SHORTLOG_RE_TO_AREA:
        match = test_regex.fullmatch(area_pfx)
        if match:
            return area
    return None


def commit_area(commit):
    '''From a Zephyr commit, get its area.'''
    return shortlog_area(commit_shortlog(commit))


# ZephyrRepoAnalysis: represents results of analyzing upstream and downstream
# activity in a repository from given starting points. See
# ZephyrRepoAnalyzer.
#
# - upstream_area_counts: map from areas to total number of
#   new upstream patches (new means not reachable from `downstream_ref`)
#
# - upstream_area_patches: map from areas to chronological (most
#   recent first) list of new upstream patches
#
# - downstream_outstanding_patches: a map from shortlogs of non-mergeup
#   downstream out of tree commits that haven't been reverted yet to their
#   pygit2 commit objects
#
# - downstream_merged_patches: "likely merged" downstream patches; a map from
#   shortlogs of unreverted downstream patches to lists of new upstream
#   patches (pygit2 commit objects) sent by downstream contributors that have
#   similar shortlogs.
ZephyrRepoAnalysis = namedtuple('ZephyrRepoAnalysis',
                                ['upstream_area_counts',
                                 'upstream_area_patches',
                                 'upstream_commit_range',
                                 'downstream_outstanding_patches',
                                 'downstream_merged_patches'])


class ZephyrRepoAnalyzer:
    '''Utility class for analyzing a Zephyr repository.'''

    def __init__(self, repo_path, downstream_ref,
                 downstream_sauce, downstream_domain,
                 upstream_ref,
                 sha_to_area=None, area_by_shortlog=None,
                 edit_dist_threshold=3):
        if sha_to_area is None:
            sha_to_area = {}

        self.sha_to_area = sha_to_area
        '''map from Zephyr SHAs to known areas, when they can't be guessed'''

        self.area_by_shortlog = area_by_shortlog
        '''function from shortlog prefix to area, checked after sha_to_area'''

        self.repo_path = repo_path
        '''path to Zephyr repository being analyzed'''

        self.downstream_ref = downstream_ref
        '''ref (commit-ish) for downstream commit to start analysis from'''

        self.downstream_sauce = self._parse_sauce(downstream_sauce)
        '''string identifying a downstream sauce tag;
        if your sauce tags look like [xyz <tag>], use "xyz". this can
        also be a tuple of strings to find multiple sources of sauce.'''

        self.downstream_domain = downstream_domain
        '''domain name (like "@example.com") used by downstream committers;
        this can also be a tuple of domains.'''

        self.upstream_ref = upstream_ref
        '''ref (commit-ish) for upstream ref to start analysis from'''

        self.edit_dist_threshold = edit_dist_threshold
        '''commit shortlog edit distance to use when fuzzy-matching
        upstream and downstream patches'''

    def _parse_sauce(self, sauce):
        if isinstance(sauce, str):
            return sauce
        else:
            return tuple(sauce)

    def analyze(self):
        '''Analyze repository history.

        If this returns without raising an exception, the return value
        is a ZephyrRepoAnalysis.
        '''
        try:
            self.repo = pygit2.Repository(self.repo_path)
        except KeyError:
            # pygit2 raises KeyError when the current path is not a Git
            # repository.
            msg = "Can't initialize Git repository at {}"
            raise InvalidRepositoryError(msg.format(self.repo_path))

        #
        # Group all upstream commits by area, and collect patch counts.
        #
        upstream_new = self._new_upstream_only_commits()
        upstream_commit_range = (upstream_new[0], upstream_new[-1])
        upstream_area_patches = defaultdict(list)
        for c in upstream_new:
            area = self._check_known_area(c) or commit_area(c)
            upstream_area_patches[area].append(c)

        unknown_area = upstream_area_patches.get(None)
        if unknown_area:
            raise UnknownCommitsError(*unknown_area)

        upstream_area_counts = {}
        for area, patches in upstream_area_patches.items():
            upstream_area_counts[area] = len(patches)

        #
        # Analyze downstream portion of the tree.
        #
        downstream_only = self._all_downstream_only_commits()
        downstream_outstanding = OrderedDict()
        for c in downstream_only:
            if len(c.parents) > 1:
                # Skip all the mergeup commits.
                continue

            sl = commit_shortlog(c)

            if shortlog_is_revert(sl):
                # If a shortlog marks a revert, delete the original commit
                # from outstanding.
                what = shortlog_reverts_what(sl)
                if what not in downstream_outstanding:
                    logging.warning(
                        "%s was reverted, but isn't in downstream history",
                        what)
                    continue
                del downstream_outstanding[what]
            else:
                # Non-revert commits just get appended onto
                # downstream_outstanding, keyed by shortlog to make finding
                # them later in case they're reverted easier.
                #
                # We could try to support this by looking into the entire
                # revert message to find the "This reverts commit SHA"
                # text and computing reverts based on oid rather than
                # shortlog. That'd be more robust, but let's not worry
                # about it for now.
                if sl in downstream_outstanding:
                    msg = 'duplicated commit shortlogs ({})'.format(sl)
                    raise NotImplementedError(msg)

                # Emit a warning if we have a non-revert patch with an
                # incorrect sauce tag. (Downstream might carry reverts
                # of upstream patches as hotfixes, which we shouldn't
                # warn about.)
                if not shortlog_has_sauce(sl, self.downstream_sauce):
                    logging.warning('out of tree patch has bad sauce: %s %s',
                                    c.oid, sl)

                downstream_outstanding[sl] = c

        # Compute likely merged patches.
        upstream_downstream = [c for c in upstream_new if
                               c.author.email.endswith(
                                   self.downstream_domain)]
        likely_merged = OrderedDict()
        for downstream_sl, downstream_c in downstream_outstanding.items():
            def ed(upstream_commit):
                return editdistance.eval(
                    shortlog_no_sauce(downstream_sl, self.downstream_sauce),
                    commit_shortlog(upstream_commit))
            matches = [c for c in upstream_downstream if
                       ed(c) < self.edit_dist_threshold]
            if len(matches) != 0:
                likely_merged[downstream_sl] = matches

        return ZephyrRepoAnalysis(upstream_area_counts,
                                  upstream_area_patches,
                                  upstream_commit_range,
                                  downstream_outstanding,
                                  likely_merged)

    def _new_upstream_only_commits(self):
        '''Commits in `upstream_ref` history since merge base with
        `downstream_ref`'''
        try:
            downstream_oid = self.repo.revparse_single(self.downstream_ref).oid
        except KeyError:
            raise NoSuchDownstream(self.downstream_ref)
        try:
            upstream_oid = self.repo.revparse_single(self.upstream_ref).oid
        except KeyError:
            raise NoSuchUpstream(self.upstream_ref)

        merge_base = self.repo.merge_base(downstream_oid, upstream_oid)

        sort = pygit2.GIT_SORT_TOPOLOGICAL | pygit2.GIT_SORT_REVERSE
        walker = self.repo.walk(upstream_oid, sort)
        walker.hide(merge_base)

        return [c for c in walker]

    def _check_known_area(self, commit):
        sha = str(commit.oid)
        for k, v in self.sha_to_area.items():
            if sha.startswith(k):
                return v
        if self.area_by_shortlog:
            spfx = shortlog_area_prefix(commit_shortlog(commit))
            return self.area_by_shortlog(spfx)
        return None

    def _all_downstream_only_commits(self):
        '''Commits reachable from `downstream_ref`, but not `upstream_ref`'''
        # Note: pygit2 doesn't seem to have any ready-made rev-list
        # equivalent, so call out to git directly to get the commit SHAs,
        # then wrap them with pygit2 objects.
        cmd = ['git', 'rev-list', '--pretty=oneline', '--reverse',
               self.downstream_ref, '^{}'.format(self.upstream_ref)]
        output_raw = check_output(cmd, cwd=self.repo_path)
        output = output_raw.decode(sys.getdefaultencoding()).splitlines()

        ret = []
        for line in output:
            sha, _ = line.split(' ', 1)
            commit = self.repo.revparse_single(sha)
            ret.append(commit)

        return ret


#
# For running the module as a script.
#

# Command line options.
_VALUE_OPTIONS = ['downstream-email', 'downstream-sauce', 'downstream-ref',
                  'upstream-ref', 'format']
_Y_N_OPTIONS = ['loot', 'to-revert']
_OPTIONS = _VALUE_OPTIONS + _Y_N_OPTIONS

# Choices for the --format option.
_FORMATS = ['text', 'json', 'pretty-json']

# Default configuration file
_DEFAULT_CONFIG_FILE = '~/.config/zephyr_helpers.config'
# Configuration file keys whose values are comma-separated lists.
_CONFIG_LIST_OPTIONS = ['downstream-email', 'downstream-sauce']

# Defaults for options except the config file. These are used to
# populate the remaining arguments after we've taken them from the
# command line and any config files we find.
_DEFAULTS = {
    # Not all options have defaults. If we need one and neither the
    # config file nor the arguments specify it, error out.
    'downstream-ref': 'origin/master',
    'upstream-ref': 'upstream/master',
    'format': _FORMATS[0],
    'loot': True,
    'to-revert': True,
    }


def _dest(opt):
    return opt.replace('-', '_')


def _y_or_n_bool(arg):
    argl = arg.lower()
    if argl in ['y', 'yes']:
        return True
    elif argl in ['n', 'no']:
        return False
    else:
        raise argparse.ArgumentTypeError('{} is not "y" or "n"'.
                                         format(arg))


def load_config(to_try):
    parser = configparser.ConfigParser()
    for path in to_try:
        parser.read(path)  # This doesn't error out if it doesn't exist.

    try:
        config = parser['zephyr_helpers']
    except KeyError:
        return None

    ret = {k: config[k] for k in _VALUE_OPTIONS if k in config}

    for opt in _Y_N_OPTIONS:
        if opt not in config:
            continue

        try:
            val = _y_or_n_bool(config[opt])
        except argparse.ArgumentTypeError:
            logging.warning(
                'ignoring config file option {} value {}; should be y or n'.
                format(opt, config[0]))
            continue

        ret[opt] = val

    return ret


def _update_args_with_configs(args):
    default_config_file = os.path.expanduser(_DEFAULT_CONFIG_FILE)
    config = load_config([default_config_file] + args.config_file)

    if config is None:
        logging.debug('no valid config files found')
        return

    for opt in _OPTIONS:
        dest = _dest(opt)

        if getattr(args, dest) is not None or opt not in config:
            continue

        val = config[opt]
        if opt in _CONFIG_LIST_OPTIONS:
            val = [v.strip() for v in val.split(',')]
        setattr(args, dest, val)
        logging.debug('{}={}'.format(dest, val))


def _update_args_with_defaults(args):
    for opt in _OPTIONS:
        dest = _dest(opt)

        if getattr(args, dest) is not None or opt not in _DEFAULTS:
            continue

        setattr(args, dest, _DEFAULTS[opt])
        logging.debug('{}={}'.format(dest, _DEFAULTS[opt]))


def parse_args(argv):
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=textwrap.dedent('''\
        Zephyr helper command line interface.

        Allows analyzing a Zephyr repository and printing information
        about it. It's assumed that the repository is a local clone
        of a downstream Zephyr repository.'''),
        epilog=textwrap.dedent('''\
        Configuration
        -------------

        This script can also be given a configuration file. (The
        default location is ~/.config/zephyr_helpers.config.)

        The contents are a configparser-style file, which looks like
        this:

            [zephyr_helpers]
            some-option-no-leading-dashes = value

        For example:

            [zephyr_helpers]
            downstream-email = @your-domain.com
            downstream-sauce = your-sauce
            to-revert = n

        Values which can be given multiple times can be specified in
        the configuration file with commas as separators.

        Any options given on the command line override values taken
        from configuration file(s).

        Required Values
        ---------------

        The 'downstream-email' and 'downstream-sauce' values are
        required, and must be given using command line options or set
        in a configuration file.'''))

    #
    # IMPORTANT:
    #
    # If you add or remove options, keep _OPTIONS,
    # as well as zephyr_helpers.config.template, in sync!
    #

    group = parser.add_argument_group(
        'command-line only options (no config file equivalent):')
    group.add_argument('-c', '--config-file', action='append', default=[],
                       help='config file path, in .ini format; see below')
    group.add_argument('-v', '--verbose', action='store_true',
                       help='enable verbose debug logs (to stderr)')

    group = parser.add_argument_group('out of tree organization options')
    group.add_argument('-d', '--downstream-email', action='append',
                       help='''downstream email domain (e.g. @example.com)
                       to use when searching for upstream commits authored
                       by downstream organization members; may be given more
                       than once. case sensitive!''')
    group.add_argument('-s', '--downstream-sauce', action='append',
                       help='''downstream sauce string to use when
                       searching for to-revert commits; e.g. if your
                       sauce tags look like [xyz <tag>], use "xyz".
                       may be given more than once. doesn't affect
                       search for out of tree patches. case sensitive!''')

    group = parser.add_argument_group('repository configuration')
    group.add_argument('--dr', '--downstream-ref',
                       dest='downstream_ref',
                       help='''downstream git revision (commit-ish) to
                       analyze upstream differences with; default: {}'''.
                       format(_DEFAULTS['downstream-ref']))
    group.add_argument('--ur', '--upstream-ref',
                       dest='upstream_ref',
                       help='''upstream git revision (commit-ish) to
                       analyze downstream against; default: {}.'''.
                       format(_DEFAULTS['upstream-ref']))

    group = parser.add_argument_group('output configuration')
    group.add_argument('--loot', type=_y_or_n_bool,
                       help='''if y (the default), print "loot" (a list of
                       outstanding out of tree patches); set to n to
                       disable''')
    group.add_argument('--to-revert', type=_y_or_n_bool,
                       help='''if y (the default), print out of tree patches
                       "to revert" before the next mergeup, i.e. upstream
                       patches from a downstream email domain whose shortlogs
                       are a short edit distance from a "loot" patch.''')
    group.add_argument('--format', choices=_FORMATS,
                       help='output format; default: {}'.
                       format(_DEFAULTS['format']))

    parser.add_argument('repo', metavar='ZEPHYR', nargs='?',
                        help='''path to the zephyr repository; default:
                        current working directory.''')

    args = parser.parse_args(argv)

    logging.basicConfig(level=(logging.DEBUG if args.verbose
                               else logging.WARNING),
                        format='[%(levelname)s] [%(funcName)s] %(message)s')

    for opt in _OPTIONS:
        dest = _dest(opt)
        if not hasattr(args, dest) or getattr(args, dest) is None:
            continue
        logging.debug('{}={}'.format(dest, getattr(args, dest)))

    _update_args_with_configs(args)
    _update_args_with_defaults(args)

    if args.downstream_email is None:
        print('error:'
              '--downstream-email not given and not set in a config file',
              file=sys.stderr)
        parser.print_usage()
        sys.exit(1)
    if args.downstream_sauce is None:
        print('error:',
              '--downstream-sauce not given and not set in a config file',
              file=sys.stderr)
        parser.print_usage()
        sys.exit(1)

    return parser, args


def main():
    def unknown_area(ignored_area_prefix):
        return 'UNKNOWN'

    parser, args = parse_args(sys.argv[1:])

    repo_path = args.repo
    if repo_path is None:
        repo_path = os.getcwd()

    analyzer = ZephyrRepoAnalyzer(
        repo_path, args.downstream_ref, tuple(args.downstream_sauce),
        tuple(args.downstream_email), args.upstream_ref,
        area_by_shortlog=unknown_area)

    try:
        analysis = analyzer.analyze()
    except NoSuchDownstream:
        print('error: invalid downstream-ref: {}'.format(args.downstream_ref),
              file=sys.stderr)
        print('check command line arguments, repository, and config file(s).',
              file=sys.stderr)
        parser.print_usage()
        sys.exit(1)
    except NoSuchUpstream:
        print('error: invalid upstream-ref: {}'.format(args.upstream_ref),
              file=sys.stderr)
        print('check command line arguments, repository, and config file(s).',
              file=sys.stderr)
        parser.print_usage()
        sys.exit(1)

    outstanding = analysis.downstream_outstanding_patches
    likely_merged = analysis.downstream_merged_patches

    def print_loot_text():
        print('LOOT:')
        if outstanding:
            for sl, c in outstanding.items():
                print('{} {}'.format(c.oid, sl))
        else:
            print('<none>')

    def print_to_revert_text():
        print('To revert:')
        if likely_merged:
            for sl, commits in likely_merged.items():
                downstream_oid = outstanding[sl].oid
                print('{} {}'.format(downstream_oid, sl))
                if len(commits) > 1:
                    print('\tlikely merged upstream as one of:')
                    for c in commits:
                        print('\t{} {}'.format(c.oid, commit_shortlog(c)))
                else:
                    print('\tlikely merged upstream as:')
                    print('\t{} {}'.format(commits[0].oid,
                                           commit_shortlog(commits[0])))
        else:
            print('<none>')

    def loot_json_obj():
        # It's important that this is a list, to preserve order when
        # "rebasing" and when going through json.dumps().
        return [{'sha': str(c.oid), 'shortlog': sl}
                for sl, c in outstanding.items()]

    def to_revert_json_obj():
        # List ordering isn't that important here, but let's be consistent.
        ret = []
        for sl, commits in likely_merged.items():
            ret.append({'downstream': {'sha': str(outstanding[sl].oid),
                                       'shortlog': sl},
                        'upstream-matches': [{'sha': str(c.oid),
                                              'shortlog': commit_shortlog(c)}
                                             for c in commits]})
        return ret

    if args.format == 'text':
        if args.loot:
            print_loot_text()
        if args.to_revert:
            if args.loot:
                print()         # make some vertical space
            print_to_revert_text()
    elif args.format in ('json', 'pretty-json'):
        to_dump = {}
        if args.loot:
            to_dump['loot'] = loot_json_obj()
        if args.to_revert:
            to_dump['to-revert'] = to_revert_json_obj()

        indent = None if args.format == 'json' else 4
        print(json.dumps(to_dump, indent=indent))


if __name__ == '__main__':
    main()
