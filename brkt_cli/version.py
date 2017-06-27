# Copyright 2017 Bracket Computing, Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at
#
# https://github.com/brkt/brkt-cli/blob/master/LICENSE
#
# or in the "license" file accompanying this file. This file is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR
# CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and
# limitations under the License.

import json
import urllib2
from distutils.version import LooseVersion

import logging

import datetime

import iso8601

log = logging.getLogger(__name__)

VERSION = '1.0.11pre1'


def _is_version_supported(version, supported_versions):
    """ Return True if the given version string is at least as high as
    the earliest version string in supported_versions.
    """
    # We use LooseVersion because StrictVersion can't deal with patch
    # releases like 0.9.9.1.
    sorted_versions = sorted(
        supported_versions,
        key=lambda v: LooseVersion(v)
    )
    return LooseVersion(version) >= LooseVersion(sorted_versions[0])


def _is_later_version_available(version, supported_versions):
    """ Return True if the given version string is the latest supported
    version.
    """
    # We use LooseVersion because StrictVersion can't deal with patch
    # releases like 0.9.9.1.
    sorted_versions = sorted(
        supported_versions,
        key=lambda v: LooseVersion(v)
    )
    return LooseVersion(version) < LooseVersion(sorted_versions[-1])


def check_version():
    """ Check if this version of brkt-cli is still supported by checking
    our version against the versions available on PyPI.  If a
    later version is available, print a message to the console.

    :return True if this version is still supported
    """
    url = 'http://pypi.python.org/pypi/brkt-cli/json'
    log.debug('Getting supported brkt-cli versions from %s', url)

    try:
        resp = urllib2.urlopen(url, timeout=5.0)
        code = resp.getcode()
        if code / 100 != 2:
            raise Exception(
                'Error %d when opening %s' % (code, url))
        d = json.loads(resp.read())
        supported_versions = d['releases'].keys()
    except Exception as e:
        # If we can't get the list of versions from PyPI, print the error
        # and return true.  We don't want the version check to block people
        # from getting their work done.
        log.debug('', exc_info=1)
        msg = e.message or type(e).__name__
        log.info('Unable to load brkt-cli versions from PyPI: %s', msg)
        return True

    if not _is_version_supported(VERSION, supported_versions):
        log.error(
            'Version %s is no longer supported. '
            'Run "pip install --upgrade brkt-cli" to upgrade to the '
            'latest version.',
            VERSION
        )
        return False
    if _is_later_version_available(VERSION, supported_versions):
        log.info(
            'A new release of brkt-cli is available. '
            'Run "pip install --upgrade brkt-cli" to upgrade to the '
            'latest version.'
        )

    return True


def set_last_version_check_time(cfg, dt=None):
    """ Save the last version check date in config.
    :param dt a datetime object.  If None, use the current timestamp
    """
    if not dt:
        dt = datetime.datetime.now(tz=iso8601.UTC)
    cfg.set_internal_option('last-version-check-time', dt.isoformat())


def get_last_version_check_time(cfg):
    """ Return the timestamp of the last version check as a datetime, or
    None if it has not been saved in config.
    """
    s = cfg.get_internal_option('last-version-check-time')
    if not s:
        return None

    try:
        return iso8601.parse_date(s)
    except iso8601.ParseError as e:
        log.warn(
            'Unable to parse version check date "%s": %s',
            s, e.message)


def is_version_check_needed(cfg):
    """ Return True if the last version check stored in config is over a
    day ago.

    :param cfg a CLIConfig object
    """
    t = get_last_version_check_time(cfg)
    if not t:
        return True

    now = datetime.datetime.now(tz=iso8601.UTC)
    if now - t >= datetime.timedelta(days=1):
        return True
