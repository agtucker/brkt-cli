# Copyright 2015 Bracket Computing, Inc. All Rights Reserved.
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

from __future__ import print_function

import argparse
import logging
import re
import sys
from distutils.version import LooseVersion

import boto
import boto.ec2
import boto.vpc
import requests
from boto.exception import EC2ResponseError, NoAuthHandlerFound

from brkt_cli import aws_service
from brkt_cli import encrypt_ami
from brkt_cli import encrypt_ami_args
from brkt_cli import encryptor_service
from brkt_cli import gce_service
from brkt_cli import encrypt_gce_image
from brkt_cli import encrypt_gce_image_args
from brkt_cli import launch_gce_image
from brkt_cli import launch_gce_image_args
from brkt_cli import update_encrypted_ami_args
from brkt_cli import update_gce_image
from brkt_cli import update_encrypted_gce_image_args
from brkt_cli import util
from brkt_cli.proxy import Proxy
from brkt_cli.util import validate_dns_name_ip_address
from brkt_cli.validation import ValidationError
from encrypt_ami import (
    TAG_ENCRYPTOR,
    TAG_ENCRYPTOR_AMI,
    TAG_ENCRYPTOR_SESSION_ID)
from encryptor_service import BracketEnvironment
from update_ami import update_ami

VERSION = '0.9.15pre1'

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


def _validate_ntp_servers(ntp_servers):
    if ntp_servers is None:
        return
    for server in ntp_servers:
        if not validate_dns_name_ip_address(server):
            raise ValidationError(
                'Invalid ntp-server %s specified. '
                'Should be either a host name or an IPv4 address' % server)


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
            _validate_encryptor_ami(aws_svc, encryptor_ami_id)
        else:
            log.debug('Skipping validation')

        if values.encrypted_ami_name:
            filters = {'name': values.encrypted_ami_name}
            if aws_svc.get_images(filters=filters, owners=['self']):
                raise ValidationError(
                    'You already own an image named %s' %
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


def _parse_brkt_env(brkt_env_string):
    """ Parse the --brkt-env value.  The value is in the following format:

    api_host:port,hsmproxy_host:port

    :return: a BracketEnvironment object
    :raise: ValidationError if brkt_env is malformed
    """
    endpoints = brkt_env_string.split(',')
    if len(endpoints) != 2:
        raise ValidationError('brkt-env requires two values')

    def _parse_endpoint(endpoint):
        host_port_pattern = r'([^:]+):(\d+)$'
        m = re.match(host_port_pattern, endpoint)
        if not m:
            raise ValidationError('Malformed endpoint: %s' % endpoints[0])
        host = m.group(1)
        port = int(m.group(2))

        if not util.validate_dns_name_ip_address(host):
            raise ValidationError('Invalid hostname: ' + host)
        return host, port

    be = BracketEnvironment()
    (be.api_host, be.api_port) = _parse_endpoint(endpoints[0])
    (be.hsmproxy_host, be.hsmproxy_port) = _parse_endpoint(endpoints[1])
    return be


def command_launch_gce_image(values, log):
    gce_svc = gce_service.GCEService(values.project, None, log)
    launch_gce_image.launch(log,
                            gce_svc,
                            values.image,
                            values.instance_name,
                            values.zone,
                            values.delete_boot,
                            values.instance_type,
                            {})
    return 0


def command_update_encrypted_gce_image(values, log):
    session_id = util.make_nonce()
    gce_svc = gce_service.GCEService(values.project, session_id, log)
    encrypted_image_name = gce_service.get_image_name(values.encrypted_image_name, values.image)
    
    if not encrypted_image_name.islower():
        raise ValidationError('GCE image name must be in lower case')

    log.info('Starting updater session %s', gce_svc.get_session_id())

    brkt_env = None
    if values.brkt_env:
        brkt_env = _parse_brkt_env(values.brkt_env)

    # use pre-existing image
    if values.encryptor_image:
        encryptor = values.encryptor_image
    # create image from file in GCS bucket
    else:
        log.info('Retrieving encryptor image from GCS bucket')
        encryptor = 'encryptor-%s' % gce_svc.get_session_id()
        if values.image_file:
            gce_svc.get_latest_encryptor_image(values.zone,
                                               encryptor,
                                               values.bucket,
                                               image_file=values.image_file)
        else:
            gce_svc.get_latest_encryptor_image(values.zone,
                                               encryptor,
                                               values.bucket)

    encrypt_gce_image.validate_images(gce_svc, encrypted_image_name, encryptor, values.image)
    update_gce_image.update_gce_image(
        gce_svc=gce_svc,
        enc_svc_cls=encryptor_service.EncryptorService,
        image_id=values.image,
        encryptor_image=encryptor,
        encrypted_image_name=encrypted_image_name,
        zone=values.zone,
        brkt_env=brkt_env
    )
    return 0


def command_encrypt_gce_image(values, log):
    session_id = util.make_nonce()
    gce_svc = gce_service.GCEService(values.project, session_id, log)

    brkt_env = None
    if values.brkt_env:
        brkt_env = _parse_brkt_env(values.brkt_env)

    encrypted_image_name = gce_service.get_image_name(values.encrypted_image_name, values.image)
    if not encrypted_image_name.islower():
        raise ValidationError('GCE image name must be in lower case')
    # use pre-existing image
    if values.encryptor_image:
        encryptor = values.encryptor_image
    # create image from file in GCS bucket
    else:
        log.info('Retrieving encryptor image from GCS bucket')
        encryptor = 'encryptor-%s' % gce_svc.get_session_id()
        if values.image_file:
            gce_svc.get_latest_encryptor_image(values.zone,
                                               encryptor,
                                               values.bucket,
                                               image_file=values.image_file)
        else:
            gce_svc.get_latest_encryptor_image(values.zone,
                                               encryptor,
                                               values.bucket)

    encrypt_gce_image.validate_images(gce_svc, encrypted_image_name, encryptor, values.image)

    log.info('Starting encryptor session %s', gce_svc.get_session_id())
    encrypted_image_id = encrypt_gce_image.encrypt(
        gce_svc=gce_svc,
        enc_svc_cls=encryptor_service.EncryptorService,
        image_id=values.image,
        encryptor_image=encryptor,
        encrypted_image_name=encrypted_image_name,
        zone=values.zone,
        brkt_env=brkt_env
    )
    # Print the image name to stdout, in case the caller wants to process
    # the output.  Log messages go to stderr.
    print(encrypted_image_id)
    return 0


def _parse_proxies(*proxy_host_ports):
    """ Parse proxies specified on the command line.

    :param proxy_host_ports: a list of strings in "host:port" format
    :return: a list of Proxy objects
    :raise: ValidationError if any of the items are malformed
    """
    proxies = []
    for s in proxy_host_ports:
        m = re.match(r'([^:]+):(\d+)$', s)
        if not m:
            raise ValidationError('%s is not in host:port format' % s)
        host = m.group(1)
        port = int(m.group(2))
        if not util.validate_dns_name_ip_address(host):
            raise ValidationError('%s is not a valid hostname' % host)
        proxy = Proxy(host, port)
        proxies.append(proxy)

    return proxies


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
    _validate_ntp_servers(values.ntp_servers)

    _connect_and_validate(aws_svc, values, encryptor_ami)
    error_msg = _validate_guest_ami(aws_svc, values.ami)
    if error_msg:
        raise ValidationError(error_msg)

    log.info('Starting encryptor session %s', aws_svc.session_id)

    brkt_env = None
    if values.brkt_env:
        brkt_env = _parse_brkt_env(values.brkt_env)

    # Handle proxy config.
    proxy_config = None
    if values.proxy_config_file:
        path = values.proxy_config_file
        log.debug('Loading proxy config from %s', path)
        try:
            with open(path) as f:
                proxy_config = f.read()
        except IOError as e:
            log.debug('Unable to read %s', path, e)
            raise ValidationError('Unable to read %s' % path)
        proxy.validate_proxy_config(proxy_config)
    elif values.proxies:
        proxies = _parse_proxies(*values.proxies)
        proxy_config = proxy.generate_proxy_config(*proxies)

    encrypted_image_id = encrypt_ami.encrypt(
        aws_svc=aws_svc,
        enc_svc_cls=encryptor_service.EncryptorService,
        image_id=values.ami,
        encryptor_ami=encryptor_ami,
        encrypted_ami_name=values.encrypted_ami_name,
        subnet_id=values.subnet_id,
        security_group_ids=values.security_group_ids,
        brkt_env=brkt_env,
        ntp_servers=values.ntp_servers,
        proxy_config=proxy_config,
        guest_instance_type=values.guest_instance_type
    )
    # Print the AMI ID to stdout, in case the caller wants to process
    # the output.  Log messages go to stderr.
    print(encrypted_image_id)
    return 0


def _validate_ami(aws_svc, ami_id):
    """
    @return the Image object
    @raise ValidationError if the image doesn't exist
    """
    try:
        image = aws_svc.get_image(ami_id)
    except EC2ResponseError, e:
        if e.error_code.startswith('InvalidAMIID'):
            raise ValidationError(
                'Could not find ' + ami_id + ': ' + e.error_code)
        else:
            raise ValidationError(e.error_message)
    if not image:
        raise ValidationError('Could not find ' + ami_id)
    return image


def _validate_guest_ami(aws_svc, ami_id):
    """ Validate that we are able to encrypt this image.

    :return: None if the image is valid, or an error string if not
    """
    image = _validate_ami(aws_svc, ami_id)
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


def _validate_guest_encrypted_ami(aws_svc, ami_id, encryptor_ami_id):
    """ Validate that this image was encrypted by Bracket by checking
        tags.

    :raise: ValidationError if validation fails
    :return: the Image object
    """
    ami = _validate_ami(aws_svc, ami_id)

    # Is this encrypted by Bracket?
    tags = ami.tags
    expected_tags = (TAG_ENCRYPTOR,
                     TAG_ENCRYPTOR_SESSION_ID,
                     TAG_ENCRYPTOR_AMI)
    missing_tags = set(expected_tags) - set(tags.keys())
    if missing_tags:
        raise ValidationError(
            '%s is missing tags: %s' % (ami.id, ', '.join(missing_tags)))

    # See if this image was already encrypted by the given encryptor AMI.
    original_encryptor_id = tags.get(TAG_ENCRYPTOR_AMI)
    if original_encryptor_id == encryptor_ami_id:
        msg = '%s was already encrypted with Bracket Encryptor %s' % (
            ami.id,
            encryptor_ami_id
        )
        raise ValidationError(msg)

    return ami


def _validate_encryptor_ami(aws_svc, ami_id):
    """ Validate that the image exists and is a Bracket encryptor image.

    @raise ValidationError if validation fails
    """
    image = _validate_ami(aws_svc, ami_id)
    if 'brkt-avatar' not in image.name:
        raise ValidationError(
            '%s (%s) is not a Bracket Encryptor image' % (ami_id, image.name)
        )
    return None


def _get_updated_image_name(image_name, session_id):
    """ Generate a new name, based on the existing name of the encrypted
    image and the session id.

    @return the new name
    """
    # Replace session id in the image name.
    m = re.match('(.+) \(encrypted (\S+)\)', image_name)
    suffix = ' (encrypted %s)' % session_id
    if m:
        encrypted_ami_name = util.append_suffix(
            m.group(1), suffix, max_length=128)
    else:
        encrypted_ami_name = util.append_suffix(
            image_name, suffix, max_length=128)
    return encrypted_ami_name


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

    _validate_ntp_servers(values.ntp_servers)
    _connect_and_validate(aws_svc, values, encryptor_ami)

    encrypted_ami = values.ami
    if values.validate:
        guest_image = _validate_guest_encrypted_ami(
            aws_svc, encrypted_ami, encryptor_ami)
    else:
        log.info('Skipping AMI validation.')
        guest_image = aws_svc.get_image(encrypted_ami)

    mv_image = aws_svc.get_image(encryptor_ami)
    if (guest_image.virtualization_type !=
            mv_image.virtualization_type):
        log.error(
            'Virtualization type mismatch.  %s is %s, but encryptor %s is '
            '%s.',
            guest_image.id,
            guest_image.virtualization_type,
            mv_image.id,
            mv_image.virtualization_type
        )
        return 1

    encrypted_ami_name = values.encrypted_ami_name
    if encrypted_ami_name:
        # Check for name collision.
        filters = {'name': encrypted_ami_name}
        if aws_svc.get_images(filters=filters, owners=['self']):
            raise ValidationError(
                'You already own image named %s' % encrypted_ami_name)
    else:
        encrypted_ami_name = _get_updated_image_name(guest_image.name, nonce)
    log.debug('Image name: %s', encrypted_ami_name)
    aws_service.validate_image_name(encrypted_ami_name)

    brkt_env = None
    if values.brkt_env:
        brkt_env = _parse_brkt_env(values.brkt_env)

    # Initial validation done
    log.info('Updating %s with new metavisor %s', encrypted_ami, encryptor_ami)

    updated_ami_id = update_ami(
        aws_svc, encrypted_ami, encryptor_ami, encrypted_ami_name,
        subnet_id=values.subnet_id,
        security_group_ids=values.security_group_ids,
        ntp_servers=values.ntp_servers,
        brkt_env=brkt_env,
        guest_instance_type=values.guest_instance_type
    )
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

    # metavar indicates with subparsers will be shown
    # when the cli is invoked incorrectly or with -h/--help
    # this hides other subparsers that shouldn't be visible
    # to users
    subparsers = parser.add_subparsers(dest='subparser_name', metavar='{encrypt-ami,update-encrypted-ami}')

    encrypt_ami_parser = subparsers.add_parser(
        'encrypt-ami',
        description='Create an encrypted AMI from an existing AMI.'
    )
    encrypt_ami_args.setup_encrypt_ami_args(encrypt_ami_parser)

    encrypt_gce_image_parser = subparsers.add_parser('encrypt-gce-image')
    encrypt_gce_image_args.setup_encrypt_gce_image_args(encrypt_gce_image_parser)

    launch_gce_image_parser = subparsers.add_parser('launch-gce-image')
    launch_gce_image_args.setup_launch_gce_image_args(launch_gce_image_parser)

    update_gce_image_parser = subparsers.add_parser('update-gce-image')
    update_encrypted_gce_image_args.setup_update_gce_image_args(update_gce_image_parser)

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
        if values.subparser_name == 'launch-gce-image':
            log.info('Warning: GCE support is still in development.')
            return command_launch_gce_image(values, log)
        if values.subparser_name == 'encrypt-gce-image':
            log.info('Warning: GCE support is still in development.')
            return command_encrypt_gce_image(values, log)
        if values.subparser_name == 'update-gce-image':
            log.info('Warning: GCE support is still in development.')
            return command_update_encrypted_gce_image(values, log)
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
