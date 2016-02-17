# Copyright 2015 Bracket Computing, Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at
#
# https://github.com/brkt/brkt-sdk-java/blob/master/LICENSE
#
# or in the "license" file accompanying this file. This file is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR
# CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and
# limitations under the License.

from __future__ import print_function

import argparse
from distutils.version import LooseVersion

import boto
import boto.ec2
import boto.vpc
import logging
import re
import sys

import requests
from boto.exception import EC2ResponseError, NoAuthHandlerFound

from brkt_cli import aws_service
from brkt_cli import encrypt_ami
from brkt_cli import encrypt_ami_args
from brkt_cli import update_encrypted_ami_args
from brkt_cli import encryptor_service
from brkt_cli import util
from brkt_cli.validation import ValidationError
from encrypt_ami import (
    TAG_ENCRYPTOR,
    TAG_ENCRYPTOR_AMI,
    TAG_ENCRYPTOR_SESSION_ID
)

from update_ami import update_ami

VERSION = '0.9.12pre1'

log = None


def _validate_subnet_and_security_groups(aws_svc,
                                         subnet_id=None,
                                         security_group_ids=None):
    """ Verify that the given subnet and security groups all exist and are
    in the same subnet.

    :return True if all of the ids are valid and in the same VPC
    :raise EC2ResponseError or ValidationError if any of the ids are invalid
    """
    vpc_ids = set()
    if subnet_id:
        # Validate the subnet.
        subnet = aws_svc.get_subnet(subnet_id)
        vpc_ids.add(subnet.vpc_id)

    if security_group_ids:
        # Validate the security groups.
        for id in security_group_ids:
            sg = aws_svc.get_security_group(id, retry=False)
            vpc_ids.add(sg.vpc_id)

    if len(vpc_ids) > 1:
        raise ValidationError(
            'Subnet and security groups must be in the same VPC.')

    if not subnet_id and vpc_ids:
        # Security groups were specified but subnet wasn't.  Make sure that
        # the security groups are in the default VPC.
        (vpc_id, ) = vpc_ids
        default_vpc = aws_svc.get_default_vpc()
        log.debug(
            'Default VPC: %s, security group VPC IDs: %s',
            default_vpc,
            vpc_ids
        )

        # Perform the check as long as there's a default VPC.  In
        # EC2-Classic, there is no default VPC and the vpc_id field is null.
        if vpc_id and default_vpc:
            if vpc_id != default_vpc.id:
                raise ValidationError(
                    'Security groups must be in the default VPC when '
                    'a subnet is not specified.'
                )


def _validate_region(aws_svc, region):
    """ Return the region specified on the command line.

    :raise ValidationError if the region is invalid
    """
    regions = [str(r.name) for r in aws_svc.get_regions()]
    if region not in regions:
        raise ValidationError(
            'Invalid region %s.  Must be one of %s.' %
            (region, str(regions)))
    return region


def _connect_and_validate(aws_svc, values, encryptor_ami_id):
    """ Connect to the AWS service and validate command-line options

    :param aws_svc: the BaseAWSService implementation
    :param values: object that was generated by argparse
    """
    if values.encrypted_ami_name:
        aws_service.validate_image_name(values.encrypted_ami_name)

    aws_svc.connect(values.region, key_name=values.key_name)

    try:
        if values.key_name:
            aws_svc.get_key_pair(values.key_name)

        if values.validate:
            _validate_subnet_and_security_groups(
                aws_svc, values.subnet_id, values.security_group_ids)

            error_msg = validate_encryptor_ami(aws_svc, encryptor_ami_id)
            if error_msg:
                raise ValidationError(error_msg)
        else:
            log.debug('Skipping validation')

        if values.encrypted_ami_name:
            filters = {'name': values.encrypted_ami_name}
            if aws_svc.get_images(filters=filters):
                raise ValidationError(
                        'There is already an image named %s' %
                        values.encrypted_ami_name
                )
    except EC2ResponseError as e:
        raise ValidationError(e.message)


def _parse_tags(tag_strings):
    """ Parse the tags specified on the command line.

    :param: tag_strings a list of strings in KEY=VALUE format
    :return: the tags as a dictionary
    :raise: ValidationError if any of the tags are invalid
    """
    if not tag_strings:
        return {}

    tags = {}
    for s in tag_strings:
        m = re.match(r'([^=]+)=(.+)', s)
        if not m:
            raise ValidationError('Tag %s is not in the format KEY=VALUE' % s)
        tags[m.group(1)] = m.group(2)
    return tags


def command_encrypt_ami(values, log):
    session_id = util.make_nonce()

    aws_svc = aws_service.AWSService(session_id)
    _validate_region(aws_svc, values.region)

    encryptor_ami = (
        values.encryptor_ami or
        encrypt_ami.get_encryptor_ami(values.region, hvm=values.hvm)
    )

    default_tags = encrypt_ami.get_default_tags(session_id, encryptor_ami)
    default_tags.update(_parse_tags(values.tags))
    aws_svc.default_tags = default_tags

    _connect_and_validate(aws_svc, values, encryptor_ami)
    error_msg = _validate_guest_ami(aws_svc, values.ami)
    if error_msg:
        raise ValidationError(error_msg)

    log.info('Starting encryptor session %s', aws_svc.session_id)

    encrypted_image_id = encrypt_ami.encrypt(
        aws_svc=aws_svc,
        enc_svc_cls=encryptor_service.EncryptorService,
        image_id=values.ami,
        encryptor_ami=encryptor_ami,
        encrypted_ami_name=values.encrypted_ami_name,
        subnet_id=values.subnet_id,
        security_group_ids=values.security_group_ids,
        brkt_env=values.brkt_env
    )
    # Print the AMI ID to stdout, in case the caller wants to process
    # the output.  Log messages go to stderr.
    print(encrypted_image_id)
    return 0


def _validate_guest_ami(aws_svc, ami_id):
    """ Validate that we are able to encrypt this image.

    :return: None if the image is valid, or an error string if not
    """
    try:
        image = aws_svc.get_image(ami_id)
    except EC2ResponseError, e:
        if e.error_code == 'InvalidAMIID.NotFound':
            return e.error_message
        else:
            raise

    if TAG_ENCRYPTOR in image.tags:
        return '%s is already an encrypted image' % ami_id

    # Amazon's API only returns 'windows' or nothing.  We're not currently
    # able to detect individual Linux distros.
    if image.platform == 'windows':
        return 'Windows is not a supported platform'

    if image.root_device_type != 'ebs':
        return '%s does not use EBS storage.' % ami_id
    if image.hypervisor != 'xen':
        return '%s uses hypervisor %s.  Only xen is supported' % (
            ami_id, image.hypervisor)
    return None


def _validate_guest_encrypted_ami(aws_svc, ami_id):
    """ Validate that this image was encrypted by Bracket by checking
        tags.
    :return: None if we recognize the image, or an error string if not
    """
    # Is this encrypted by Bracket?
    image = aws_svc.get_image(ami_id)
    tags = image.tags
    expected_tags = (TAG_ENCRYPTOR,
                     TAG_ENCRYPTOR_SESSION_ID,
                     TAG_ENCRYPTOR_AMI)
    missing_tags = set(expected_tags) - set(tags.keys())
    if missing_tags:
        return 'Missing tags %s' % str(missing_tags)
    return None


def validate_encryptor_ami(aws_svc, ami_id):
    try:
        image = aws_svc.get_image(ami_id)
    except EC2ResponseError, e:
        return e.error_message
    if 'brkt-avatar' not in image.name:
        return '%s (%s) is not a Bracket Encryptor image' % (
            ami_id, image.name)
    return None


def command_update_encrypted_ami(values, log):
    nonce = util.make_nonce()

    aws_svc = aws_service.AWSService(nonce)
    _validate_region(aws_svc, values.region)
    encryptor_ami = (
        values.encryptor_ami or
        encrypt_ami.get_encryptor_ami(values.region, hvm=values.hvm)
    )

    default_tags = encrypt_ami.get_default_tags(nonce, encryptor_ami)
    default_tags.update(_parse_tags(values.tags))
    aws_svc.default_tags = default_tags

    _connect_and_validate(aws_svc, values, encryptor_ami)

    encrypted_ami = values.ami
    if values.validate:
        guest_ami_error = _validate_guest_encrypted_ami(aws_svc, encrypted_ami)
        if guest_ami_error:
            raise ValidationError(
                'Encrypted AMI verification failed: %s' % guest_ami_error)
    else:
        log.info('skipping AMI verification')
    guest_image = aws_svc.get_image(encrypted_ami)
    mv_image = aws_svc.get_image(encryptor_ami)
    if (guest_image.virtualization_type !=
        mv_image.virtualization_type):
        log.error("Encryptor virtualization_type mismatch")
        return 1
    encrypted_ami_name = values.encrypted_ami_name
    if not encrypted_ami_name:
        # Replace nonce in AMI name
        name = guest_image.name
        m = re.match('(.+) \(encrypted (\S+)\)', name)
        if m:
            encrypted_ami_name = m.group(1) + ' (encrypted %s)' % (nonce,)
        else:
            encrypted_ami_name = name + ' (encrypted %s)' % (nonce,)
        filters = {'name': encrypted_ami_name}
        if aws_svc.get_images(filters=filters):
            raise ValidationError(
                    'There is already an image named %s' %
                     encrypted_ami_name
            )
    # Initial validation done
    log.info('Updating %s with new metavisor %s', encrypted_ami, encryptor_ami)

    updated_ami_id = update_ami(
        aws_svc, encrypted_ami, encryptor_ami, encrypted_ami_name,
        subnet_id=values.subnet_id,
        security_group_ids=values.security_group_ids,
        brkt_env=values.brkt_env)
    print(updated_ami_id)
    return 0


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


def main():
    parser = argparse.ArgumentParser(
        description='Command-line interface to the Bracket Computing service.'
    )
    parser.add_argument(
        '-v',
        '--verbose',
        dest='verbose',
        action='store_true',
        help='Print status information to the console'
    )
    parser.add_argument(
        '--version',
        action='version',
        version='brkt-cli version %s' % VERSION
    )
    parser.add_argument(
        '--no-check-version',
        dest='check_version',
        action='store_false',
        default=True,
        help="Don't check whether this version of brkt-cli is supported"
    )

    subparsers = parser.add_subparsers(dest='subparser_name')

    encrypt_ami_parser = subparsers.add_parser(
        'encrypt-ami',
        description='Create an encrypted AMI from an existing AMI.'
    )
    encrypt_ami_args.setup_encrypt_ami_args(encrypt_ami_parser)

    update_encrypted_ami_parser = \
        subparsers.add_parser(
            'update-encrypted-ami',
            description=(
                'Update an encrypted AMI with the latest Metavisor release.'
            )
        )
    update_encrypted_ami_args.setup_update_encrypted_ami(
        update_encrypted_ami_parser)

    argv = sys.argv[1:]
    values = parser.parse_args(argv)
    # Initialize logging.  Log messages are written to stderr and are
    # prefixed with a compact timestamp, so that the user knows how long
    # each operation took.
    if values.verbose:
        log_level = logging.DEBUG
    else:
        # Boto logs auth errors and 401s at ERROR level by default.
        boto.log.setLevel(logging.FATAL)
        log_level = logging.INFO
    # Set the log level of our modules explicitly.  We can't set the
    # default log level to INFO because we would see INFO messages from
    # boto and other 3rd party libraries in the command output.
    logging.basicConfig(format='%(asctime)s %(message)s', datefmt='%H:%M:%S')
    global log
    log = logging.getLogger(__name__)
    log.setLevel(log_level)
    aws_service.log.setLevel(log_level)
    encryptor_service.log.setLevel(log_level)

    if values.check_version:
        supported_versions = None

        try:
            url = 'http://pypi.python.org/pypi/brkt-cli/json'
            r = requests.get(url)
            if r.status_code / 100 != 2:
                raise Exception(
                    'Error %d when opening %s' % (r.status_code, url))
            supported_versions = r.json()['releases'].keys()
        except Exception as e:
            print(e, file=sys.stderr)
            print(
                'Version check failed.  You can bypass it with '
                '--no-check-version',
                file=sys.stderr
            )
            return 1

        if not _is_version_supported(VERSION, supported_versions):
            print(
                'Version %s is no longer supported.\n'
                'Run "pip install --upgrade brkt-cli" to upgrade to the '
                'latest version.' %
                VERSION,
                file=sys.stderr
            )
            return 1
        if _is_later_version_available(VERSION, supported_versions):
            print(
                'A new release of brkt-cli is available.\n'
                'Run "pip install --upgrade brkt-cli" to upgrade to the '
                'latest version.',
                file=sys.stderr
            )

    try:
        if values.subparser_name == 'encrypt-ami':
            return command_encrypt_ami(values, log)
        if values.subparser_name == 'update-encrypted-ami':
            return command_update_encrypted_ami(values, log)
    except ValidationError as e:
        print(e, file=sys.stderr)
    except NoAuthHandlerFound:
        msg = (
            'Unable to connect to AWS.  Are your AWS_ACCESS_KEY_ID and '
            'AWS_SECRET_ACCESS_KEY environment variables set?'
        )
        if values.verbose:
            log.exception(msg)
        else:
            log.error(msg)
    except EC2ResponseError as e:
        if e.error_code == 'AuthFailure':
            msg = 'Check your AWS login credentials and permissions'
            if values.verbose:
                log.exception(msg)
            else:
                log.error(msg + ': ' + e.error_message)
        elif e.error_code in (
                'InvalidKeyPair.NotFound',
                'InvalidSubnetID.NotFound',
                'InvalidGroup.NotFound'):
            if values.verbose:
                log.exception(e.error_message)
            else:
                log.error(e.error_message)
        elif e.error_code == 'UnauthorizedOperation':
            if values.verbose:
                log.exception(e.error_message)
            else:
                log.error(e.error_message)
            log.error(
                'Unauthorized operation.  Check the IAM policy for your '
                'AWS account.'
            )
        else:
            raise
    except util.BracketError as e:
        if values.verbose:
            log.exception(e.message)
        else:
            log.error(e.message)
    except KeyboardInterrupt:
        if values.verbose:
            log.exception('Interrupted by user')
        else:
            log.error('Interrupted by user')
    return 1


if __name__ == '__main__':
    exit_status = main()
    exit(exit_status)
