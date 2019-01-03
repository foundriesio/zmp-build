#!/usr/bin/env python3

# SPDX-License-Identifier: Apache-2.0

import argparse
import os
from os.path import abspath
from xml.etree import ElementTree

from zephyr_tools.pygit2_helpers import repo_commits, \
    commit_shortsha, commit_shortlog


def project_data(pinned_manifest):
    return {
        elt.attrib['name']: {
            'revision': elt.attrib['revision'],
            'path': elt.attrib.get('path', elt.attrib['name']),
        }
        for elt in ElementTree.parse(pinned_manifest).getroot()
        if elt.tag == 'project'
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--zmp',
                        help='ZMP installation directory, default is cwd')
    parser.add_argument('start_manifest', metavar='start-manifest',
                        help='starting pinned manifest')
    parser.add_argument('end_manifest', metavar='end-manifest',
                        help='ending pinned manifest')

    args = parser.parse_args()

    start_manifest = args.start_manifest
    end_manifest = args.end_manifest
    zmp = args.zmp

    if zmp is None:
        zmp = abspath(os.getcwd())
    else:
        zmp = abspath(zmp)

    # Projects we care about for the purposes of release notes.
    projects = ['zephyr', 'mcuboot', 'dm-lwm2m', 'dm-hawkbit-mqtt']

    # Get 'revision' and 'path' dicts for each project in each
    # pinned manifest, keyed by name.
    start_data = project_data(start_manifest)
    end_data = project_data(end_manifest)

    notes_metadata = {}
    for p in projects:
        start_rev = start_data[p]['revision']
        end_rev = end_data[p]['revision']
        path = end_data[p]['path']  # end should have the entire
                                    # history. start might be gone.
        commits = repo_commits(path, start_rev, end_rev)
        ncommits = len(commits)

        if ncommits >= 2:
            sc, ec = commits[0], commits[-1]
            changes = '''\
{} patches total:

- start commit: {} ("{}").
- end commit: {} ("{}").
'''.format(ncommits,
           commit_shortsha(sc), commit_shortlog(sc),
           commit_shortsha(ec), commit_shortlog(ec))
        elif ncommits == 1:
            changes = 'One new commit: {} ("{}").'.format(
                commit_shortsha(commits[0]),
                commit_shortlog(commits[0]))
        else:
            changes = 'No changes.'

        notes_metadata[p] = {
            'path': path,  # assume it stays the same
            'start_revision': start_rev,
            'end_revision': end_rev,
            'commits': commits,
            'changes': changes,
            }

    print('''\
## Zephyr

{}

## MCUboot

{}

## dm-hawkbit-mqtt

{}

## dm-lwm2m

{}
'''.format(*[notes_metadata[p]['changes'] for p in projects]))


if __name__ == '__main__':
    main()
