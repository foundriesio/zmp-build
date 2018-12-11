# Copyright 2018 Open Source Foundries, Limited
# Copyright 2018 Foundries.io, Limited
# SPDX-License-Identifier: Apache-2.0

'''Helper module for analyzing a Zephyr tree.

Has some features that are only useful when analyzing a tree with
upstream and downstream commits as well.'''

from collections import defaultdict, OrderedDict, namedtuple
import re
import sys
from subprocess import check_output

import pygit2
import editdistance

from pygit2_helpers import shortlog_is_revert, shortlog_reverts_what, \
    shortlog_no_sauce, commit_shortlog


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
                    print('WARNING: {} was reverted,'.format(what),
                          "but isn't present in downstream history",
                          file=sys.stderr)
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
        downstream_oid = self.repo.revparse_single(self.downstream_ref).oid
        upstream_oid = self.repo.revparse_single(self.upstream_ref).oid

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
