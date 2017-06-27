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
import logging
import os

import brkt_cli
import brkt_cli.crypto
from brkt_cli import (
    encryptor_service,
    get_proxy_config,
    yeti)
from brkt_cli import argutil
from brkt_cli import brkt_jwt
from brkt_cli.config import CLIConfig
from brkt_cli.instance_config import (
    InstanceConfig,
    INSTANCE_CREATOR_MODE,
    INSTANCE_UPDATER_MODE
)
from brkt_cli.util import (
    get_domain_from_brkt_env
)
from brkt_cli.validation import ValidationError

log = logging.getLogger(__name__)


def setup_instance_config_args(parser, parsed_config,
                               mode=INSTANCE_CREATOR_MODE, brkt_tag=True):
    parser.add_argument(
        '--ntp-server',
        metavar='DNS_NAME',
        dest='ntp_servers',
        action='append',
        help=(
            'NTP server to sync Metavisor clock. May be specified multiple '
            'times.'
        )
    )

    proxy_group = parser.add_mutually_exclusive_group()
    proxy_group.add_argument(
        '--proxy',
        metavar='HOST:PORT',
        help='Proxy that Metavisor uses to talk to the Bracket service',
        dest='proxies',
        action='append'
    )
    proxy_group.add_argument(
        '--proxy-config-file',
        metavar='PATH',
        help='proxy.yaml file that defines the proxy configuration '
             'that metavisor uses to talk to the Bracket service',
        dest='proxy_config_file'
    )

    # TODO: put brkt-env and service-domain into a mutually exclusive
    # group. We can't do this while they're hidden because of a bug in
    # argparse:
    #
    # http://stackoverflow.com/questions/30499648/python-mutually-exclusive-arguments-complains-about-action-index
    #
    # brkt_env_group = parser.add_mutually_exclusive_group()

    # Optional yeti endpoints. Hidden because it's only used for
    # development. The value contains the hosts and ports of the RPC,
    # HSM proxy, Network RPC separated by commas:
    #
    # <rpc-host>:<rpc-port>,<hsmproxy-host>:<hsmproxy-port>,<network-host>:<network-port>
    parser.add_argument(
        '--brkt-env',
        dest='brkt_env',
        help=argparse.SUPPRESS
    )

    # Optional domain that runs the Yeti service
    # (e.g. stage.mgmt.brkt.com). Hidden because it's only used for
    # development.
    parser.add_argument(
        '--service-domain',
        metavar='DOMAIN',
        help=argparse.SUPPRESS
    )

    if mode in (INSTANCE_CREATOR_MODE, INSTANCE_UPDATER_MODE):
        parser.add_argument(
            '--status-port',
            metavar='PORT',
            dest='status_port',
            type=encryptor_service.status_port,
            default=encryptor_service.ENCRYPTOR_STATUS_PORT,
            help=(
                'Specify the port to receive http status of encryptor. Any '
                'port in range 1-65535 can be used except for port 81.'),
            required=False
        )

    # Optional CA cert file for Brkt MCP. When an on-prem MCP is used
    # (and thus, the MCP endpoints are provided in the --brkt-env arg), the
    # CA cert for the MCP root CA must be 'baked into' the encrypted AMI
    # or provided via userdata to a MV with an unencrypted guest root.
    parser.add_argument(
        '--ca-cert',
        metavar='PATH',
        dest='ca_cert',
        help=(
            'Certificate that Metavisor uses to communicate with a '
            'Customer Managed MCP.'
        )
    )

    token_group = parser.add_mutually_exclusive_group()
    token_group.add_argument(
        '--token',
        help=(
            'Token (JWT) that Metavisor uses to authenticate with the '
            'Bracket service.  Use the make-token subcommand to generate a '
            'token.'
        ),
        metavar='TOKEN',
        dest='token',
        default=parsed_config.get_option('token'),
        required=False
    )

    if brkt_tag:
        argutil.add_brkt_tag(token_group)


def instance_config_from_values(values=None, mode=INSTANCE_CREATOR_MODE,
                                brkt_env=None, launch_token=None):
    """ Return an InstanceConfig object, based on options specified on
    the command line and Metavisor mode.

    :param values an argparse.Namespace object
    :param mode the mode in which Metavisor is running
    :param brkt_env a BracketEnvironment object
    :param launch_token the token that Metavisor will use to authenticate
    with Yeti.
    """
    brkt_config = {}
    if not values:
        return InstanceConfig(brkt_config, mode)

    if mode in (INSTANCE_CREATOR_MODE, INSTANCE_UPDATER_MODE):
        if not brkt_env:
            raise Exception(
                'BracketEnvironment is required for %s mode' % mode)

        # We only monitor status when encrypting or updating.
        brkt_config['status_port'] = (
            values.status_port or
            encryptor_service.ENCRYPTOR_STATUS_PORT
        )

    add_brkt_env_to_brkt_config(brkt_env, brkt_config)

    if launch_token:
        brkt_config['identity_token'] = launch_token

    if values.ntp_servers:
        brkt_config['ntp_servers'] = values.ntp_servers

    log.debug('Parsed brkt_config %s', brkt_config)

    ic = InstanceConfig(brkt_config, mode)

    # Now handle the args that cause files to be added to brkt-files
    proxy_config = get_proxy_config(values)
    if proxy_config:
        ic.add_brkt_file('proxy.yaml', proxy_config)

    if 'ca_cert' in values and values.ca_cert:
        if not brkt_env:
            raise ValidationError(
                'Must specify --service-domain or --brkt-env when specifying '
                '--ca-cert.'
            )
        try:
            with open(values.ca_cert, 'r') as f:
                ca_cert_data = f.read()
        except IOError as e:
            raise ValidationError(e)

        brkt_cli.crypto.validate_cert(ca_cert_data)

        domain = get_domain_from_brkt_env(brkt_env)

        ca_cert_filename = 'ca_cert.pem.' + domain
        ic.add_brkt_file(ca_cert_filename, ca_cert_data)

    if 'guest_fqdn' in values and values.guest_fqdn:
        ic.add_brkt_file('vpn.yaml', 'fqdn: ' + values.guest_fqdn)

    return ic


def _yeti_service_from_brkt_env(brkt_env):
    root_url = 'https://%s:%d' % (
        brkt_env.public_api_host, brkt_env.public_api_port)
    token = (
        os.environ.get('BRKT_API_TOKEN')
    )
    return yeti.YetiService(root_url, token=token)


def _validate_yeti_service(yeti_service):
    """ Validate that the given YetiService is able to authenticate.

    :return the valid YetiService
    :raise ValidationError if the API token is not set in config, or if
    authentication with Yeti fails.
    """

    if not yeti_service.token:
        raise ValidationError(
            '$BRKT_API_TOKEN is not set. Run brkt auth to get an API token.')

    try:
        yeti_service.get_customer()
    except yeti.YetiError as e:
        if e.http_status == 401:
            raise ValidationError(
                '$BRKT_API_TOKEN is not authorized to access %s' %
                yeti_service.root_url)
        raise ValidationError(e.message)
    return yeti_service


def yeti_service_from_brkt_env(brkt_env):
    """ Return a YetiService object based on the given BracketEnvironment.

    :raise ValidationError if the API token is not set in config, or if
    authentication with Yeti fails.
    """
    y = _yeti_service_from_brkt_env(brkt_env)
    return _validate_yeti_service(y)


def get_yeti_service(root_url):
    """ Return a YetiService object based on the given Yeti public API
    root URL.

    :raise ValidationError if the API token is not set in config, or if
    authentication with Yeti fails.
    """
    token = (
        os.environ.get('BRKT_API_TOKEN')
    )
    y = yeti.YetiService(root_url, token=token)
    return _validate_yeti_service(y)


def get_launch_token(values, cli_config):
    """ Return the launch token either from values.token or from Yeti, in that
    order.  Assume that the values.token and values.brkt_tags fields exist.

    :raise ValidationError if an error occurs while talking to Yeti
    """
    token = values.token
    if not token:
        log.debug('Getting launch token from Yeti')
        brkt_env = brkt_cli.brkt_env_from_values(values, cli_config)
        y = yeti_service_from_brkt_env(brkt_env)
        tags = brkt_jwt.brkt_tags_from_name_value_list(values.brkt_tags)
        token = y.create_launch_token(tags=tags)

    return token


def instance_config_args_to_values(cli_args, mode=INSTANCE_CREATOR_MODE):
    """ Convenience function for testing instance_config settings

    :param cli_args: string with args separated by spaces
    :return the values object as returned from argparser.parse_args()
    """
    parser = argparse.ArgumentParser()
    config = CLIConfig()
    config.register_option(
        'token',
        'The default token to use when encrypting, updating, or launching'
        ' images')
    setup_instance_config_args(parser, config, mode)
    argv = cli_args.split()
    return parser.parse_args(argv)


def add_brkt_env_to_brkt_config(brkt_env, brkt_config):
    """ Add BracketEnvironment values to the config dictionary
    that will be passed to the metavisor via userdata.

    :param brkt_env a BracketEnvironment object
    :param brkt_config a dictionary that contains configuration data
    """
    if brkt_env:
        api_host_port = '%s:%d' % (brkt_env.api_host, brkt_env.api_port)
        hsmproxy_host_port = '%s:%d' % (
            brkt_env.hsmproxy_host, brkt_env.hsmproxy_port)
        network_host_port = '%s:%d' % (
            brkt_env.network_host, brkt_env.network_port)
        brkt_config['api_host'] = api_host_port
        brkt_config['hsmproxy_host'] = hsmproxy_host_port
        brkt_config['network_host'] = network_host_port