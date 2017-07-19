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


def add_vcenter_host(parser, use_esx=False):
    if use_esx:
        arg = '--esx-host'
        help_str = 'IP address/DNS Name of the ESX host'
    else:
        arg = '--vcenter-host'
        help_str = 'IP address/DNS Name of the vCenter host'
    parser.add_argument(
        arg,
        help=help_str,
        dest="vcenter_host",
        metavar='DNS_NAME',
        required=True
    )


def add_vcenter_port(parser, use_esx=False):
    if use_esx:
        arg = '--esx-port'
        help_str = 'Port Number of the ESX host'
    else:
        arg = '--vcenter-port'
        help_str = 'Port Number of the vCenter Server'
    parser.add_argument(
        arg,
        help=help_str,
        metavar='N',
        dest="vcenter_port",
        default="443",
        required=False
    )


def add_vcenter_datacenter(parser, use_esx=False):
    if use_esx:
        arg = '--esx-datacenter'
        help_str = 'ESX Datacenter to use'
    else:
        arg = '--vcenter-datacenter'
        help_str = 'vCenter Datacenter to use'
    parser.add_argument(
        arg,
        help=help_str,
        dest="vcenter_datacenter",
        metavar='NAME',
        default=None,
        required=False
    )


def add_vcenter_datastore(parser, use_esx=False):
    if use_esx:
        arg = '--esx-datastore'
        help_str = 'ESX Datastore to use'
    else:
        arg = '--vcenter-datastore'
        help_str = 'vCenter Datastore to use'
    parser.add_argument(
        arg,
        help=help_str,
        dest="vcenter_datastore",
        metavar='NAME',
        default=None,
        required=False
    )


def add_vcenter_cluster(parser):
    parser.add_argument(
        "--vcenter-cluster",
        help="vCenter cluster to use",
        dest="vcenter_cluster",
        metavar='NAME',
        default=None,
        required=False
    )


def add_vcenter_network_name(parser, use_esx=False):
    if use_esx:
        arg = '--esx-network-name'
        help_str = 'ESX network name to use'
    else:
        arg = '--vcenter-network-name'
        help_str = 'vCenter network name to use'
    parser.add_argument(
        arg,
        help=help_str,
        dest="network_name",
        metavar='NAME',
        default="VM Network",
        required=False
    )


def add_cpu(parser, help="Number of CPUs to assign to the Encryptor VM"):
    parser.add_argument(
        "--cpu-count",
        help=help,
        metavar='N',
        dest="no_of_cpus",
        default="8",
        required=False
    )


def add_memory(parser, help="Memory to assign to the Encryptor VM"):
    parser.add_argument(
        "--memory",
        help=help,
        metavar='GB',
        dest="memory_gb",
        default="32",
        required=False
    )


def add_encrypted_image_name(
    parser,
    help="Specify the name of the generated OVF/OVA"
    ):
    parser.add_argument(
        '--encrypted-image-name',
        metavar='NAME',
        dest='encrypted_ovf_name',
        help=help,
        required=False
    )


def add_template_vm_name(parser, help="Specify the name of the output template VM"):
    parser.add_argument(
        '--template-vm-name',
        metavar='NAME',
        dest='template_vm_name',
        help=help,
        required=False
    )


def add_static_ip_address(
    parser,
    help="Specify the static IP address of the encryptor VM"
    ):
    parser.add_argument(
        '--static-ip-address',
        metavar='IP',
        dest='static_ip',
        help=help,
        required=False
    )


def add_static_subnet_mask(
    parser,
    help="Specify the static subnet mask of the encryptor VM"
    ):
    parser.add_argument(
        '--static-subnet-mask',
        metavar='IP',
        dest='static_mask',
        help=help,
        required=False
    )


def add_static_default_router(
    parser,
    help="Specify the static default router of the encryptor VM"
    ):
    parser.add_argument(
        '--static-default-router',
        metavar='IP',
        dest='static_gw',
        help=help,
        required=False
    )



def add_static_dns_domain(
    parser,
    help="Specify the static DNS domain of the encryptor VM"
    ):
    parser.add_argument(
        '--static-dns-domain',
        metavar='DNS_NAME',
        dest='static_dns_domain',
        help=help,
        required=False
    )


def add_static_dns_server(
    parser,
    help="Specify the static DNS server of the encryptor VM"
    ):
    parser.add_argument(
        '--static-dns-server',
        metavar='DNS_NAME',
        dest='static_dns',
        action='append',
        help=help,
        required=False
    )


def add_no_verify_cert(parser):
    parser.add_argument(
        '--no-verify-cert',
        dest='validate',
        action='store_false',
        default=True,
         help="Don't validate vCenter certificate"
        )


def add_encrypted_image_directory(
    parser,
    help="Directory to store the generated OVF/OVA image"
    ):
    parser.add_argument(
        '--encrypted-image-directory',
        metavar='NAME',
        dest='target_path',
        help=help,
        default=None,
        required=False
    )


def add_ovftool_path(parser):
    parser.add_argument(
        '--ovftool-path',
        metavar='PATH',
        dest='ovftool_path',
        help='ovftool executable path',
        default="ovftool",
        required=False
    )


def add_ovf_source_directory(parser):
    parser.add_argument(
        '--ovf-source-directory',
        metavar='PATH',
        dest='source_image_path',
        help='Local path to the OVF directory',
        default=None,
        required=False
    )


def add_metavisor_ovf_image_name(parser):
    parser.add_argument(
        '--metavisor-ovf-image-name',
        metavar='NAME',
        dest='image_name',
        help='Metavisor OVF name',
        default=None,
        required=False
    )


def add_metavisor_version(parser):
    parser.add_argument(
        '--metavisor-version',
        metavar='NAME',
        dest='metavisor_version',
        help='Metavisor version [e.g 1.2.12 ] (default: latest)',
        default=None,
        required=False
    )


def add_console_file_name(parser):
    parser.add_argument(
        '--console-file-name',
        metavar='NAME',
        dest='serial_port_file_name',
        help='File name to dump console messages to',
        default=None,
        required=False
    )


def add_disk_type(parser):
    parser.add_argument(
        '--disk-type',
        metavar='TYPE',
        dest='disk_type',
        choices=["thin", "thick-lazy-zeroed", "thick-eager-zeroed"],
        help='thin/thick-lazy-zeroed/thick-eager-zeroed',
        default='thin',
        required=False
    )


def add_nic_type(parser, help=argparse.SUPPRESS):
    parser.add_argument(
        '--nic-type',
        metavar='NAME',
        dest='nic_type',
        choices=["Port", "DistributedVirtualPort", "DistributedVirtualPortGroup"],
        help='Port/DistributedVirtualPort/DistributedVirtualPortGroup',
        default="Port",
        required=False
    )


# This argument is no longer required with the new command
# syntax. Leaving it around for backwards compatibility.
def add_use_esx_host(parser, help=argparse.SUPPRESS):
    parser.add_argument(
        '--use-esx-host',
        dest='esx_host',
        action='store_true',
        default=False,
        help=help
    )


# Optional HTTP Proxy argument which can be used in proxied environments
# Specifies the HTTP Proxy to use for S3/AWS connections
def add_http_s3_proxy(parser, help=argparse.SUPPRESS):
    parser.add_argument(
        '--http-s3-proxy',
        dest='http_proxy',
        metavar='HOST:PORT',
        default=None,
        help=help
    )



# Optional VMDK that's used to launch the encryptor instance.  This
# argument is hidden because it's only used for development.
def add_encryptor_vmdk(parser, help=argparse.SUPPRESS):
    parser.add_argument(
        '--encryptor-vmdk',
        metavar='VMDK-NAME',
        dest='encryptor_vmdk',
        help=help
    )


# Optional ssh-public key to be put into the Metavisor.
# Use only with debug instances.
# Hidden because it is used only for development.
def add_ssh_public_key(parser, help=argparse.SUPPRESS):
    parser.add_argument(
        '--ssh-public-key',
        metavar='PATH',
        dest='ssh_public_key_file',
        default=None,
        help=help
    )


# Optional no-teardown will not tear down the
# Encryptor/Updater VM in case of error.
# Hidden because it is used only for development.
def add_no_teardown(parser, help=argparse.SUPPRESS):
    parser.add_argument(
        '--no-teardown',
        dest='no_teardown',
        action='store_true',
        default=False,
        help=help
    )


# Optional bucket-name in case dev/qa need to use
# other internal buckets to fetch the MV image from
def add_bucket_name(parser, help=argparse.SUPPRESS):
    parser.add_argument(
        '--bucket-name',
        metavar='NAME',
        dest='bucket_name',
        help=help,
        default="solo-brkt-prod-ovf-image"
    )


# Optional argument for root disk crypto policy. The supported values
# currently are "gcm" and "xts" with "xts" being the default
def add_crypto_policy(parser, help=argparse.SUPPRESS):
    parser.add_argument(
        '--crypto-policy',
        dest='crypto',
        metavar='NAME',
        choices=[CRYPTO_GCM, CRYPTO_XTS],
        help=help,
        default=None
    )


# Optional argument to keep the downloaded artifacts. Can we used in
# cases where the same (downloaded) OVF is used for multiple
# encryption/update jobs
def add_no_cleanup(parser, help=argparse.SUPPRESS):
    parser.add_argument(
        '--no-cleanup',
        dest='cleanup',
        default=True,
        action='store_false',
        help=help
    )


# Optional argument to attach a CDROM device to the launched VM
# Used currently only for PCF deployments
def add_cdrom(parser, help=argparse.SUPPRESS):
    parser.add_argument(
        '--cdrom',
        dest='cdrom',
        default=False,
        action='store_true',
        help=help
    )
