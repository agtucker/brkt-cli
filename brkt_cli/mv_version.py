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

"""Utilities for discovering metavisor version"""

import logging
import re
from distutils.version import LooseVersion

import boto3
from botocore.handlers import disable_signing


log = logging.getLogger(__name__)
logging.getLogger('boto3').setLevel(logging.FATAL)
logging.getLogger('botocore').setLevel(logging.FATAL)
logging.getLogger('s3transfer').setLevel(logging.FATAL)


AWS_PROD_BUCKET_DEFAULT='solo-brkt-prod-net'
RE_METAVISOR_VERSION=re.compile(r'(metavisor)-(\d+)-(\d+)-(\d+)-(g.*?)(-.*)?$')
RE_MAJOR_VERSION_ARG=re.compile(r'^(\d+)$')
RE_MINOR_VERSION_ARG=re.compile(r'^(\d+)-(\d+)$')
RE_PATCH_VERSION_ARG=re.compile(r'^(\d+)-(\d+)-(\d+)$')


class MetavisorVersionNotFoundError(Exception):
    pass


def get_s3_versions(bucket):
    """Return available metavisor versions in S3 bucket

    Return a list of possible metavisor versions from provided AWS bucket.

    Args:
       bucket: AWS bucket where metavisor versions are pubished (str)

    Returns:
       versions: List of metavisor version prefix's for AWS bucket (list)

    """
    log.debug('Fetching Metavisor version from S3')
    versions = []

    # Check for dev bucket
    if bucket == 'packages.int.brkt.net':
        version_prefix = 'metavisor/metavisor-'
    else:
        version_prefix = 'metavisor-'

    s3 = boto3.resource('s3')
    s3.meta.client.meta.events.register('choose-signer.s3.*', disable_signing)
    client = boto3.client('s3')
    paginator = client.get_paginator('list_objects')
    page_iterator = paginator.paginate(Bucket=bucket,
                                       Delimiter='/',
                                       Prefix=version_prefix)
    for page in page_iterator:
        if page.get('CommonPrefixes'):
            versions += \
                [ v.get('Prefix').rstrip('/') for v in page.get('CommonPrefixes') ]

    return versions


def get_version(version, bucket):
    """Return a single published metavisor version

    Returns the latest version of metavisor if no specific version is
    provided.

    Args:
       version: Semantic or exact match metavisor version (str)
       bucket: AWS bucket where metavisor versions are pubished (str)

    Returns:
       mversion: Metavisor version prefix for AWS bucket (str)

    """
    mversion = None
    mv_regex = version_regex(version)

    versions = get_s3_versions(bucket)
    mversions = sorted([LooseVersion(v) for v in versions], reverse=True)
    for mv in mversions:
        vcandidate = re.search(mv_regex, mv.vstring)
        if vcandidate:
            # Return exact version matches (e.g. -debug builds)
            if mv_regex.pattern.rstrip('$') == version:
                mversion = mv.vstring
                break
            # Return only official metavisor versions (e.g. no suffix)
            # ('metavisor', '1', '3', '106', 'g0cbdb5597', None)
            mvsearch = re.search(RE_METAVISOR_VERSION, mv.vstring)
            if mvsearch.groups()[-1] == None:
                mversion = mv.vstring
                break

    if not mversion:
        log.exception("Exception determining metavisor version in %s (%s)",
                      bucket, mv_regex.pattern)
        raise MetavisorVersionNotFoundError()

    log.info("Found metavisor version %s", mversion)

    return mversion


def get_amis_url(version, bucket):
    """Return url to amis.json for specified metavisor version

    Args:
       version: Semantic or exact match metavisor version (str)
       bucket: AWS bucket where metavisor versions are pubished (str)

    Returns:
       url: Location to amis.json for given metavisor version (str)

    """
    mversion = get_version(version, bucket)
    url = 'http://%(bucket)s.s3.amazonaws.com/%(mversion)s/amis.json' % locals()

    return url


def version_regex(version):
    """Return a metavisor long version regex for a given version string

    Transforms short semantic version strings (e.g. 1, 1.2, 1.2.3) into
    metavisor long form version strings. If no supported semantic version
    string is provided, default to an exact match.

    Args:
        version: Semantic or exact match version (str)

    Returns:
        version_regex: Compiled regex object (_sre.SRE_Pattern)
    """

    if not version:
        return  RE_METAVISOR_VERSION

    # Be lenient with . vs - version input
    version = version.replace('.','-')

    if re.search(RE_PATCH_VERSION_ARG, version):
        vr = r'metavisor-%s-(g.*?)(-.*)?$' % version
    elif re.search(RE_MINOR_VERSION_ARG, version):
        vr = r'metavisor-%s-(\d+)-(g.*?)(-.*)?$' % version
    elif re.search(RE_MAJOR_VERSION_ARG, version):
        vr = r'metavisor-%s-(\d+)-(\d+)-(g.*?)(-.*)?$' % version
    else:
        vr = r'%s$' % version

    return re.compile(vr)
