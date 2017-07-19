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
import logging
import re
import tempfile
import urllib2

import botocore
from botocore.exceptions import ClientError

from brkt_cli import instance_config_args

import brkt_cli
from brkt_cli import encryptor_service, util, mv_version
from brkt_cli.aws import (
    aws_service,
    encrypt_ami,
    encrypt_ami_args,
    wrap_image,
    wrap_image_args,
    share_logs,
    share_logs_args,
    update_encrypted_ami_args,
    boto3_tag)
from brkt_cli.aws.aws_constants import (
    TAG_ENCRYPTOR, TAG_ENCRYPTOR_SESSION_ID, TAG_ENCRYPTOR_AMI
)
from brkt_cli.aws.update_ami import update_ami
from brkt_cli.instance_config import (
    INSTANCE_CREATOR_MODE,
    INSTANCE_UPDATER_MODE,
    INSTANCE_METAVISOR_MODE,
)
from brkt_cli.instance_config_args import (
    instance_config_from_values,
    setup_instance_config_args
)
from brkt_cli.subcommand import Subcommand
from brkt_cli.util import (
    BracketError,
    CRYPTO_GCM,
    CRYPTO_XTS
)
from brkt_cli.validation import ValidationError

log = logging.getLogger(__name__)


ENCRYPTOR_AMIS_AWS_BUCKET = 'solo-brkt-prod-net'


def _handle_aws_errors(func):
    """Provides common error handling to subcommands that interact with AWS
    APIs.
    """
    def _do_handle_aws_errors(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except botocore.exceptions.NoCredentialsError:
            log.debug('', exc_info=1)
            raise ValidationError(
                'Please specify AWS credentials.  For more information, see '
                'http://boto3.readthedocs.io/en/latest/guide/'
                'configuration.html'
            )
        except ClientError as e:
            code, message = aws_service.get_code_and_message(e)
            if code == 'AuthFailure':
                log.debug('', exc_info=1)
                raise ValidationError(message)
            elif code in (
                    'InvalidKeyPair.NotFound',
                    'InvalidSubnetID.NotFound',
                    'InvalidGroup.NotFound'
            ):
                log.debug('', exc_info=1)
                log.error(message)
            elif code == 'UnauthorizedOperation':
                log.debug('', exc_info=1)
                log.error(message)
                log.error(
                    'Unauthorized operation.  Check the IAM policy for your '
                    'AWS account.'
                )
            else:
                raise

        return 1
    return _do_handle_aws_errors


@_handle_aws_errors
def run_share_logs(values):
    nonce = util.make_nonce()

    aws_svc = aws_service.AWSService(
        nonce,
        retry_timeout=values.retry_timeout,
        retry_initial_sleep_seconds=values.retry_initial_sleep_seconds
    )
    log.debug(
        'Retry timeout=%.02f, initial sleep seconds=%.02f',
        aws_svc.retry_timeout, aws_svc.retry_initial_sleep_seconds)

    if values.snapshot_id and values.instance_id:
        raise ValidationError("Only one of --instance-id or --snapshot-id "
                              "may be specified")

    if not values.snapshot_id and not values.instance_id:
        raise ValidationError("--instance-id or --snapshot-id "
                              "must be specified")

    if values.validate:
        # Validate the region before connecting.
        region_names = [r.name for r in aws_svc.get_regions()]
        if values.region not in region_names:
            raise ValidationError(
                'Invalid region %s.  Supported regions: %s.' %
                (values.region, ', '.join(region_names)))

    aws_svc.connect(values.region)
    logs_svc = share_logs.ShareLogsService()

    share_logs.share(
        aws_svc,
        logs_svc,
        instance_id=values.instance_id,
        snapshot_id=values.snapshot_id,
        region=values.region,
        bucket=values.bucket,
        path=values.path,
        subnet_id=values.subnet_id
    )
    return 0


@_handle_aws_errors
def run_wrap_image(values, config):
    nonce = util.make_nonce()

    aws_svc = aws_service.AWSService(
        nonce,
        retry_timeout=values.retry_timeout,
        retry_initial_sleep_seconds=values.retry_initial_sleep_seconds
    )
    log.debug(
        'Retry timeout=%.02f, initial sleep seconds=%.02f',
        aws_svc.retry_timeout, aws_svc.retry_initial_sleep_seconds)

    aws_svc.connect(values.region, key_name=values.key_name)

    if values.validate:
        guest_image = _validate_guest_ami(aws_svc, values.ami)
    else:
        guest_image = aws_svc.get_image(values.ami)

    if values.iam:
        if not aws_svc.iam_role_exists(values.iam):
            raise ValidationError('IAM role %s does not exist' % values.iam)

    metavisor_ami = values.encryptor_ami or _get_encryptor_ami(values.region,
                                                    values.metavisor_version)
    if values.validate:
        values.encrypted_ami_name = None
        _validate(aws_svc, values, metavisor_ami)
        brkt_cli.validate_ntp_servers(values.ntp_servers)

    brkt_env = brkt_cli.brkt_env_from_values(values, config)
    lt = instance_config_args.get_launch_token(values, config)
    instance_config = instance_config_from_values(
        values,
        mode=INSTANCE_METAVISOR_MODE,
        brkt_env=brkt_env,
        launch_token=lt)

    instance = wrap_image.launch_wrapped_image(
        aws_svc=aws_svc,
        image_id=guest_image.id,
        metavisor_ami=metavisor_ami,
        wrapped_instance_name=values.wrapped_instance_name,
        subnet_id=values.subnet_id,
        security_group_ids=values.security_group_ids,
        instance_type=values.instance_type,
        instance_config=instance_config,
        iam=values.iam
    )
    # Print the Instance ID to stdout, in case the caller wants to process
    # the output. Log messages go to stderr
    print instance.id
    return 0


# Get the AMI ID if the CLI is passed "ubuntu" instead of an AMI. This function grabs the official, non-AWS Marketplace
# version of the AMI from an API endpoint
def get_ubuntu_ami_id(stock_image_version, region):
    if not stock_image_version:
        stock_image_version = '16.04'
    resp = urllib2.urlopen("https://cloud-images.ubuntu.com/locator/ec2/releasesTable")
    resp_str = resp.read()
    resp.close()
    # This API endpoint returns faulty JSON with a trailing comma. This regex removes this trailing comma
    resp_str = re.sub(",[ \t\r\n]+}", "}", resp_str)
    resp_str = re.sub(",[ \t\r\n]+\]", "]", resp_str)
    ami_data = json.loads(resp_str)['aaData']
    ubuntu_ami = ''
    for ami in ami_data:
        if ami[0] == region and ami[2].startswith(stock_image_version) and ami[4] == 'hvm:ebs-ssd':
            # The API endpoint returns an HTML tag with the AMI ID in it. This regex just gets the AMI ID
            match_obj = re.match('^<.+>(ami-.+)</.+>$', ami[6])
            ubuntu_ami = match_obj.group(1)
    if not ubuntu_ami:
        raise ValidationError(
            'Could not find Ubuntu AMI version %s.' % stock_image_version)
    return ubuntu_ami


# Get the AMI ID if the CLI is passed "centos" instead of an AMI. This function grabs the official, AWS Marketplace
# version of the AMI. You need to have previously subscribed to the AMI in order for this to work
def get_centos_ami_id(stock_image_version, aws_svc):
    if not stock_image_version:
        stock_image_version = '7'
    if stock_image_version == '6':
        prod_code = '6x5jmcajty9edm3f211pqjfn2'
    elif stock_image_version == '7':
        prod_code = 'aw0evgkw8e5c1q413zgy5pjce'
    else:
        raise ValidationError('CentOS version must be 6 or 7.')
    images = aws_svc.get_images(product_code=prod_code)
    if len(images) == 0:
        raise ValidationError(
            'Unable to find AMI for CentOS %s.' % stock_image_version)
    return images[-1].id


@_handle_aws_errors
def run_encrypt(values, config, verbose=False):
    session_id = util.make_nonce()

    aws_svc = aws_service.AWSService(
        session_id,
        retry_timeout=values.retry_timeout,
        retry_initial_sleep_seconds=values.retry_initial_sleep_seconds
    )
    log.debug(
        'Retry timeout=%.02f, initial sleep seconds=%.02f',
        aws_svc.retry_timeout, aws_svc.retry_initial_sleep_seconds)

    aws_svc.connect(values.region, key_name=values.key_name)

    if values.validate:
        # Validate the region before connecting.
        _validate_region(aws_svc, values.region)

    # Keywords check
    guest_ami_id = values.ami
    if values.ami == 'ubuntu':
        guest_ami_id = get_ubuntu_ami_id(values.stock_image_version, values.region)
    elif values.ami == 'centos':
        guest_ami_id = get_centos_ami_id(values.stock_image_version, aws_svc)

    if values.validate:
        guest_image = _validate_guest_ami(aws_svc, guest_ami_id)
    else:
        guest_image = _validate_ami(aws_svc, guest_ami_id)
    encryptor_ami = values.encryptor_ami or _get_encryptor_ami(values.region,
                                                    values.metavisor_version)
    aws_tags = encrypt_ami.get_default_tags(session_id, encryptor_ami)
    command_line_tags = brkt_cli.parse_tags(values.aws_tags)
    aws_tags.update(command_line_tags)
    aws_svc.default_tags = aws_tags

    if values.validate:
        _validate(aws_svc, values, encryptor_ami)
        brkt_cli.validate_ntp_servers(values.ntp_servers)

    mv_image = aws_svc.get_image(encryptor_ami)
    if values.crypto is None:
        if mv_image.name.startswith('metavisor'):
            crypto_policy = CRYPTO_XTS
        elif mv_image.name.startswith('brkt-avatar'):
            crypto_policy = CRYPTO_GCM
        else:
            log.warn(
                "Unable to determine encryptor type for image %s. "
                "Default boot volume crypto policy to %s",
                mv_image.name, CRYPTO_XTS
            )
            crypto_policy = CRYPTO_XTS
    else:
        crypto_policy = values.crypto
        if crypto_policy == CRYPTO_XTS and not mv_image.name.startswith('metavisor'):
            raise ValidationError(
                'Unsupported crypto policy %s for encryptor %s' %
                crypto_policy, mv_image.name
            )

    brkt_env = brkt_cli.brkt_env_from_values(values, config)
    lt = instance_config_args.get_launch_token(values, config)
    instance_config = instance_config_from_values(
        values,
        mode=INSTANCE_CREATOR_MODE,
        brkt_env=brkt_env,
        launch_token=lt
    )

    if verbose:
        with tempfile.NamedTemporaryFile(
            prefix='user-data-',
            delete=False
        ) as f:
            log.debug('Writing instance user data to %s', f.name)
            f.write(instance_config.make_userdata())

    encrypted_image_id = encrypt_ami.encrypt(
        aws_svc=aws_svc,
        enc_svc_cls=encryptor_service.EncryptorService,
        image_id=guest_image.id,
        encryptor_ami=encryptor_ami,
        encrypted_ami_name=values.encrypted_ami_name,
        crypto_policy=crypto_policy,
        subnet_id=values.subnet_id,
        security_group_ids=values.security_group_ids,
        guest_instance_type=values.guest_instance_type,
        instance_config=instance_config,
        status_port=values.status_port,
        save_encryptor_logs=values.save_encryptor_logs,
        terminate_encryptor_on_failure=(
            values.terminate_encryptor_on_failure)
    )
    # Print the AMI ID to stdout, in case the caller wants to process
    # the output.  Log messages go to stderr.
    print(encrypted_image_id)
    return 0


@_handle_aws_errors
def run_update(values, config, verbose=False):
    nonce = util.make_nonce()

    aws_svc = aws_service.AWSService(
        nonce,
        retry_timeout=values.retry_timeout,
        retry_initial_sleep_seconds=values.retry_initial_sleep_seconds
    )
    log.debug(
        'Retry timeout=%.02f, initial sleep seconds=%.02f',
        aws_svc.retry_timeout, aws_svc.retry_initial_sleep_seconds)

    if values.validate:
        # Validate the region before connecting.
        _validate_region(aws_svc, values.region)

    aws_svc.connect(values.region, key_name=values.key_name)
    encrypted_image = _validate_ami(aws_svc, values.ami)
    encryptor_ami = values.encryptor_ami or _get_encryptor_ami(values.region,
                                                    values.metavisor_version)
    aws_tags = encrypt_ami.get_default_tags(nonce, encryptor_ami)
    command_line_tags = brkt_cli.parse_tags(values.aws_tags)
    aws_tags.update(command_line_tags)
    aws_svc.default_tags = aws_tags

    if values.validate:
        _validate_guest_encrypted_ami(
            aws_svc, encrypted_image.id, encryptor_ami)
        brkt_cli.validate_ntp_servers(values.ntp_servers)
        _validate(aws_svc, values, encryptor_ami)
        _validate_guest_encrypted_ami(
            aws_svc, encrypted_image.id, encryptor_ami)
    else:
        log.info('Skipping AMI validation.')

    mv_image = aws_svc.get_image(encryptor_ami)
    if (encrypted_image.virtualization_type != mv_image.virtualization_type):
        log.error(
            'Virtualization type mismatch.  %s is %s, but encryptor %s is '
            '%s.',
            encrypted_image.id,
            encrypted_image.virtualization_type,
            mv_image.id,
            mv_image.virtualization_type
        )
        return 1

    encrypted_ami_name = values.encrypted_ami_name
    if encrypted_ami_name:
        # Check for name collision.
        if aws_svc.get_images(name=encrypted_ami_name, owner_alias='self'):
            raise ValidationError(
                'You already own image named %s' % encrypted_ami_name)
    else:
        encrypted_ami_name = _get_updated_image_name(
            encrypted_image.name, nonce)
        log.debug('Image name: %s', encrypted_ami_name)
        aws_service.validate_image_name(encrypted_ami_name)

    # Initial validation done
    log.info(
        'Updating %s with new metavisor %s',
        encrypted_image.id, encryptor_ami
    )

    brkt_env = brkt_cli.brkt_env_from_values(values, config)
    lt = instance_config_args.get_launch_token(values, config)
    instance_config = instance_config_from_values(
        values,
        mode=INSTANCE_UPDATER_MODE,
        brkt_env=brkt_env,
        launch_token=lt
    )
    if verbose:
        with tempfile.NamedTemporaryFile(
            prefix='user-data-',
            delete=False
        ) as f:
            log.debug('Writing instance user data to %s', f.name)
            f.write(instance_config.make_userdata())

    updated_ami_id = update_ami(
        aws_svc, encrypted_image.id, encryptor_ami, encrypted_ami_name,
        subnet_id=values.subnet_id,
        security_group_ids=values.security_group_ids,
        guest_instance_type=values.guest_instance_type,
        updater_instance_type=values.updater_instance_type,
        instance_config=instance_config,
        status_port=values.status_port,
    )
    print(updated_ami_id)
    return 0


class AWSSubcommand(Subcommand):
    def __init__(self):
        self.config = None
        self.verbose = False

    def name(self):
        return 'aws'

    def setup_config(self, config):
        config.register_option(
            'aws.region',
            'The AWS region metavisors will be launched into')
        config.register_option(
            'aws.subnet',
            'The AWS subnet metavisors will be launched into'
        )

    def register(self, subparsers, parsed_config):
        self.config = parsed_config

        aws_parser = subparsers.add_parser(
            self.name(),
            description='AWS operations',
            help='AWS operations'
        )

        aws_subparsers = aws_parser.add_subparsers(
            dest='aws_subcommand',
            # Hardcode the list, so that we don't expose internal subcommands.
            metavar='{encrypt,update,wrap-guest-image}'
        )

        encrypt_ami_parser = aws_subparsers.add_parser(
            'encrypt',
            description='Create an encrypted AMI from an existing AMI.',
            help='Encrypt an AWS image',
            formatter_class=brkt_cli.SortingHelpFormatter
        )
        encrypt_ami_args.setup_encrypt_ami_args(
            encrypt_ami_parser, parsed_config)
        setup_instance_config_args(encrypt_ami_parser, parsed_config,
                                   mode=INSTANCE_CREATOR_MODE)
        encrypt_ami_parser.set_defaults(aws_subcommand='encrypt')

        share_logs_parser = aws_subparsers.add_parser(
            # Don't specify the help field.  This is an internal command
            # which shouldn't show up in usage output.
            'share-logs',
            description='Share logs from an existing encrypted instance.',
            formatter_class=brkt_cli.SortingHelpFormatter
        )
        share_logs_args.setup_share_logs_args(
            share_logs_parser, parsed_config)
        share_logs_parser.set_defaults(aws_subcommand='share-logs')

        update_encrypted_ami_parser = aws_subparsers.add_parser(
            'update',
            description=(
                'Update an encrypted AMI with the latest Metavisor '
                'release.'
            ),
            help='Update an encrypted AWS image',
            formatter_class=brkt_cli.SortingHelpFormatter
        )
        update_encrypted_ami_args.setup_update_encrypted_ami(
            update_encrypted_ami_parser, parsed_config)
        setup_instance_config_args(update_encrypted_ami_parser,
                                   parsed_config,
                                   mode=INSTANCE_UPDATER_MODE)
        update_encrypted_ami_parser.set_defaults(aws_subcommand='update')

        wrap_parser = aws_subparsers.add_parser(
            'wrap-guest-image',
            description=(
                'Launch guest image wrapped with Bracket Metavsor'
            ),
            help='Launch guest image wrapped with Bracket Metavisor'
        )
        wrap_image_args.setup_wrap_image_args(wrap_parser, parsed_config)
        setup_instance_config_args(wrap_parser, parsed_config,
                                   mode=INSTANCE_CREATOR_MODE)
        wrap_parser.set_defaults(aws_subcommand='wrap-guest-image')

    def debug_log_to_temp_file(self, values):
        return values.aws_subcommand in ('encrypt', 'update')

    def run(self, values):
        if not values.region:
            raise ValidationError(
                'Specify --region or set the aws.region config key')
        if values.aws_subcommand == 'encrypt':
            return run_encrypt(values, self.config, self.verbose)
        if values.aws_subcommand == 'update':
            return run_update(values, self.config, self.verbose)
        if values.aws_subcommand == 'share-logs':
            return run_share_logs(values)
        if values.aws_subcommand == 'wrap-guest-image':
            return run_wrap_image(values, self.config)


def get_subcommands():
    return [AWSSubcommand()]


def _validate_subnet_and_security_groups(aws_svc,
                                         subnet_id=None,
                                         security_group_ids=None):
    """ Verify that the given subnet and security groups all exist and are
    in the same subnet.

    :return True if all of the ids are valid and in the same VPC
    :raise ClientError or ValidationError if any of the ids are invalid
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
        (vpc_id,) = vpc_ids
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


def _validate_ami(aws_svc, ami_id):
    """
    :return the Image object
    :raise ValidationError if the image doesn't exist
    """
    try:
        image = aws_svc.get_image(ami_id)
    except ClientError as e:
        code, message = aws_service.get_code_and_message(e)
        if code.startswith('InvalidAMIID'):
            raise ValidationError(
                'Could not find ' + ami_id + ': ' + code)
        else:
            raise ValidationError(message)
    if not image:
        raise ValidationError('Could not find ' + ami_id)
    return image


def _validate_guest_ami(aws_svc, ami_id):
    """ Validate that we are able to encrypt this image.

    :return: the Image object
    :raise: ValidationError if the AMI id is invalid
    """
    image = _validate_ami(aws_svc, ami_id)
    tags = boto3_tag.tags_to_dict(image.tags)
    if tags and TAG_ENCRYPTOR in tags:
        raise ValidationError('%s is already an encrypted image' % ami_id)

    # Amazon's API only returns 'windows' or nothing.  We're not currently
    # able to detect individual Linux distros.
    if image.platform == 'windows':
        raise ValidationError('Windows is not a supported platform')

    if image.root_device_type != 'ebs':
        raise ValidationError('%s does not use EBS storage.' % ami_id)
    if image.hypervisor != 'xen':
        raise ValidationError(
            '%s uses hypervisor %s.  Only xen is supported' % (
                ami_id, image.hypervisor)
        )
    return image


def _validate_guest_encrypted_ami(aws_svc, ami_id, encryptor_ami_id):
    """ Validate that this image was encrypted by Bracket by checking
        tags.

    :raise: ValidationError if validation fails
    :return: the Image object
    """
    ami = _validate_ami(aws_svc, ami_id)

    # Is this encrypted by Bracket?
    tags = boto3_tag.tags_to_dict(ami.tags)
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

    :raise ValidationError if validation fails
    """
    image = _validate_ami(aws_svc, ami_id)
    if 'brkt-avatar' not in image.name and 'metavisor' not in image.name:
        raise ValidationError(
            '%s (%s) is not a Bracket Encryptor image' % (ami_id, image.name)
        )
    return None


def _validate(aws_svc, values, encryptor_ami_id):
    """ Validate command-line options

    :param aws_svc: the BaseAWSService implementation
    :param values: object that was generated by argparse
    """
    if values.encrypted_ami_name:
        aws_service.validate_image_name(values.encrypted_ami_name)

    try:
        if values.key_name:
            aws_svc.get_key_pair(values.key_name)

        _validate_subnet_and_security_groups(
            aws_svc, values.subnet_id, values.security_group_ids)
        _validate_encryptor_ami(aws_svc, encryptor_ami_id)

        if values.encrypted_ami_name:
            images = aws_svc.get_images(
                name=values.encrypted_ami_name,
                owner_alias='self')
            if images:
                raise ValidationError(
                    'You already own an image named %s' %
                    values.encrypted_ami_name
                )
    except ClientError as e:
        _, message = aws_service.get_code_and_message(e)
        raise ValidationError(message)


def _validate_region(aws_svc, region_name):
    """ Check that the specified region is a valid AWS region.

    :raise ValidationError if the region is invalid
    """
    region_names = [r.name for r in aws_svc.get_regions()]
    if region_name not in region_names:
        raise ValidationError(
            '%s does not exist.  AWS regions are %s' %
            (region_name, ', '.join(region_names))
        )


def _get_encryptor_ami(region_name, version):
    """ Read the list of AMIs from the AMI endpoint and return the AMI ID
    for the given region.

    :raise ValidationError if the region is not supported
    :raise BracketError if the list of AMIs cannot be read
    """
    bucket = ENCRYPTOR_AMIS_AWS_BUCKET
    amis_url = mv_version.get_amis_url(version, bucket)

    log.debug('Getting encryptor AMI list from %s', amis_url)
    r = urllib2.urlopen(amis_url)
    if r.getcode() not in (200, 201):
        raise BracketError(
            'Getting %s gave response: %s' % (amis_url, r.text))
    resp_json = json.loads(r.read())
    ami = resp_json.get(region_name)

    if not ami:
        regions = resp_json.keys()
        raise ValidationError(
            'Encryptor AMI is only available in %s' % ', '.join(regions))
    return ami


def _get_updated_image_name(image_name, session_id):
    """ Generate a new name, based on the existing name of the encrypted
    image and the session id.

    :return the new name
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
