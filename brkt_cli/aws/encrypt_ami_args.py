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

import argparse
from brkt_cli.util import (
    CRYPTO_GCM,
    CRYPTO_XTS
)
from brkt_cli.aws import aws_args


def setup_encrypt_ami_args(parser, parsed_config):
    parser.add_argument(
        'ami',
        metavar='ID',
        help='The guest AMI that will be encrypted. This can be the AMI ID, "ubuntu", or "centos".'
    )
    parser.add_argument(
        '--stock-image-version',
        metavar='STOCK_IMAGE_VERSION',
        help='The version number when specifying "ubuntu" or "centos" instead of an AMI ID. The default versions are '
             'Ubuntu 16.04 and CentOS 7.'
    )
    parser.add_argument(
        '--encrypted-ami-name',
        metavar='NAME',
        dest='encrypted_ami_name',
        help='Specify the name of the generated encrypted AMI',
        required=False
    )
    parser.add_argument(
        '--guest-instance-type',
        metavar='TYPE',
        dest='guest_instance_type',
        help=(
            'The instance type to use when running the unencrypted guest '
            'instance'),
        default='m4.large'
    )
    aws_args.add_no_validate(parser)
    aws_args.add_region(parser, parsed_config)
    aws_args.add_security_group(parser)
    aws_args.add_subnet(parser, parsed_config)
    aws_args.add_aws_tag(parser)
    aws_args.add_metavisor_version(parser)
    aws_args.add_encryptor_ami(parser)
    aws_args.add_key(parser)
    aws_args.add_retry_timeout(parser)
    aws_args.add_retry_initial_sleep_seconds(parser)

    parser.add_argument(
        '--save-encryptor-logs',
        dest='save_encryptor_logs',
        action='store_true',
        help=argparse.SUPPRESS,
        default=False
    )

    # Optional argument for development: if encryption fails, keep the
    # encryptor running so that we can debug it.
    parser.add_argument(
        '--no-terminate-encryptor-on-failure',
        dest='terminate_encryptor_on_failure',
        action='store_false',
        default=True,
        help=argparse.SUPPRESS
    )
    # Optional argument for root disk crypto policy. The supported values
    # currently are "gcm" and "xts" with "gcm" being the default
    parser.add_argument(
        '--crypto-policy',
        dest='crypto',
        metavar='NAME',
        choices=[CRYPTO_GCM, CRYPTO_XTS],
        help=argparse.SUPPRESS,
        default=None
    )
