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
import brkt_cli
import logging
import os
import subprocess
import getpass

from brkt_cli import _parse_proxies

from brkt_cli.subcommand import Subcommand

from brkt_cli import (
    encryptor_service,
    instance_config_args,
    util
)
from brkt_cli.game.gamify_decorator import gamify
from brkt_cli.instance_config import (
    INSTANCE_CREATOR_MODE,
    INSTANCE_UPDATER_MODE,
    INSTANCE_METAVISOR_MODE
)
from brkt_cli.instance_config_args import (
    instance_config_from_values,
    setup_instance_config_args
)
from brkt_cli.util import CRYPTO_XTS

from brkt_cli.esx import (
    encrypt_vmdk,
    encrypt_vmdk_args,
    esx_service,
    rescue_metavisor,
    rescue_metavisor_args,
    update_vmdk,
    update_encrypted_vmdk_args,
    encrypt_with_esx_host_args,
    update_with_esx_host_args,
    wrap_image,
    wrap_with_vcenter_args,
    wrap_with_esx_host_args,
)
from brkt_cli.validation import ValidationError

log = logging.getLogger(__name__)


def _check_env_vars_set(*var_names):
    for n in var_names:
        if not os.getenv(n):
            raise ValidationError("Environment variable %s is not set" % (n,))


def _get_vcenter_password(use_esx):
    if use_esx:
        vcenter_password = os.getenv('ESX_PASSWORD')
    else:
        vcenter_password = os.getenv('VCENTER_PASSWORD')
    if not vcenter_password:
        if use_esx:
            vcenter_password = getpass.getpass('Enter ESX password:')
        else:
            vcenter_password = getpass.getpass('Enter vCenter password:')
    return vcenter_password

@gamify
def run_encrypt(values, parsed_config, log, use_esx=False):
    session_id = util.make_nonce()
    if values.create_ovf or values.create_ova:
        # verify we have a valid output directory
        if values.target_path is None:
            raise ValidationError("Missing directory path to store "
                                  "final OVF/OVA images")
        if not os.path.exists(values.target_path):
            raise ValidationError("Target path %s not present",
                                  values.target_path)
        if values.create_ova:
            # verify ovftool is present
            try:
                cmd = [values.ovftool_path, '-v']
                subprocess.check_call(cmd)
            except:
                raise ValidationError("OVFtool not present. "
                                      "Cannot create OVA")
    else:
        if use_esx is False and values.template_vm_name is None:
            raise ValidationError("Missing template-vm-name for the "
                                  "template VM")
    if values.source_image_path is None and values.image_name is not None:
        raise ValidationError("Specify OVF image location with --ovf-source-directory")
    if use_esx:
        _check_env_vars_set('ESX_USER_NAME')
    else:
        _check_env_vars_set('VCENTER_USER_NAME')
    vcenter_password = _get_vcenter_password(use_esx)
    brkt_cli.validate_ntp_servers(values.ntp_servers)
    # Verify we have a valid launch token
    instance_config_args.get_launch_token(values, parsed_config)

    proxy = None
    if values.http_proxy:
        proxy = _parse_proxies(values.http_proxy)[0]

    # static ip configuration
    static_ip = None
    if not use_esx:
        if values.static_ip or values.static_gw or values.static_mask or \
           values.static_dns or values.static_dns_domain:
            static_ip = esx_service.StaticIPConfiguration(
                values.static_ip, values.static_mask, values.static_gw,
                values.static_dns, values.static_dns_domain)
            static_ip.validate()

    # Download images from S3
    try:
        if (values.encryptor_vmdk is None and
            values.source_image_path is None):
            (ovf, file_list) = \
                esx_service.download_ovf_from_s3(
                    values.bucket_name,
                    version=values.metavisor_version,
                    proxy=proxy
                )
            if ovf is None:
                raise ValidationError("Did not find MV OVF images")
    except Exception as e:
        raise ValidationError("Failed to download MV image from S3: ", e)
    # Connect to vCenter
    try:
        vc_swc = esx_service.initialize_vcenter(
            host=values.vcenter_host,
            user=os.getenv('ESX_USER_NAME') if use_esx else os.getenv('VCENTER_USER_NAME'),
            password=vcenter_password,
            port=values.vcenter_port,
            datacenter_name=None if use_esx else values.vcenter_datacenter,
            datastore_name=values.vcenter_datastore,
            esx_host=use_esx,
            cluster_name=None if use_esx else values.vcenter_cluster,
            no_of_cpus=values.no_of_cpus,
            memory_gb=values.memory_gb,
            session_id=session_id,
            network_name=values.network_name,
            nic_type=values.nic_type,
            verify=False if use_esx else values.validate,
            cdrom=values.cdrom,
        )
    except Exception as e:
        raise ValidationError("Failed to connect to vCenter: ", e)
    # Validate vCenter parameters
    vc_swc.validate_vcenter_params()
    # Validate that template does not already exist
    if values.template_vm_name:
        if vc_swc.find_vm(values.template_vm_name):
            raise ValidationError("VM with the same name as requested "
                                  "template VM name %s already exists" %
                                  values.template_vm_name)
    # Set tear-down
    vc_swc.set_teardown(values.no_teardown)
    # Set the disk-type
    if values.disk_type == "thin":
        vc_swc.set_thin_disk(True)
        vc_swc.set_eager_scrub(False)
    elif values.disk_type == "thick-lazy-zeroed":
        vc_swc.set_thin_disk(False)
        vc_swc.set_eager_scrub(False)
    elif values.disk_type == "thick-eager-zeroed":
        vc_swc.set_thin_disk(False)
        vc_swc.set_eager_scrub(True)
    else:
        raise ValidationError("Disk Type %s not correct. Can only be "
                              "thin, thick-lazy-zeroed or "
                              "thick-eager-zeroed" % (values.disk_type,))

    try:
        brkt_env = brkt_cli.brkt_env_from_values(values, parsed_config)
        lt = instance_config_args.get_launch_token(values, parsed_config)
        instance_config = instance_config_from_values(
            values,
            mode=INSTANCE_CREATOR_MODE,
            brkt_env=brkt_env,
            launch_token=lt
        )
        crypto_policy = values.crypto
        if crypto_policy is None:
            crypto_policy = CRYPTO_XTS
        instance_config.brkt_config['crypto_policy_type'] = crypto_policy
        user_data_str = vc_swc.create_userdata_str(instance_config,
            update=False, ssh_key_file=values.ssh_public_key_file)
        if (values.encryptor_vmdk is not None):
            # Create from MV VMDK
            encrypt_vmdk.encrypt_from_vmdk(
                vc_swc, encryptor_service.EncryptorService,
                values.vmdk, crypto_policy,
                vm_name=values.template_vm_name,
                create_ovf=values.create_ovf,
                create_ova=values.create_ova,
                target_path=values.target_path,
                image_name=values.encrypted_ovf_name,
                ovftool_path=values.ovftool_path,
                metavisor_vmdk=values.encryptor_vmdk,
                user_data_str=user_data_str,
                serial_port_file_name=values.serial_port_file_name,
                status_port=values.status_port,
                static_ip=static_ip
            )
        elif (values.source_image_path is not None):
            # Create from MV OVF in local directory
            encrypt_vmdk.encrypt_from_local_ovf(
                vc_swc, encryptor_service.EncryptorService,
                values.vmdk, crypto_policy,
                vm_name=values.template_vm_name,
                create_ovf=values.create_ovf,
                create_ova=values.create_ova,
                target_path=values.target_path,
                image_name=values.encrypted_ovf_name,
                ovftool_path=values.ovftool_path,
                source_image_path=values.source_image_path,
                ovf_image_name=values.image_name,
                user_data_str=user_data_str,
                serial_port_file_name=values.serial_port_file_name,
                status_port=values.status_port,
                static_ip=static_ip
            )
        else:
            # Create from MV OVF in S3
            encrypt_vmdk.encrypt_from_s3(
                vc_swc, encryptor_service.EncryptorService,
                values.vmdk, crypto_policy,
                vm_name=values.template_vm_name,
                create_ovf=values.create_ovf,
                create_ova=values.create_ova,
                target_path=values.target_path,
                image_name=values.encrypted_ovf_name,
                ovftool_path=values.ovftool_path,
                ovf_name=ovf,
                download_file_list=file_list,
                user_data_str=user_data_str,
                serial_port_file_name=values.serial_port_file_name,
                status_port=values.status_port,
                cleanup=values.cleanup,
                static_ip=static_ip
            )
        return 0
    except Exception as e:
        log.error("Failed to encrypt the guest VMDK: %s", e)
        return 1


@gamify
def run_update(values, parsed_config, log, use_esx=False):
    session_id = util.make_nonce()
    encrypted_ovf_name = None
    encrypted_ova_name = None
    if values.create_ovf or values.create_ova:
        # verify we have a valid input directory
        if values.target_path is None:
            raise ValidationError("Missing directory path to fetch "
                                  "encrypted OVF/OVA images from")
        if not os.path.exists(values.target_path):
            raise ValidationError("Target path %s not present",
                                  values.target_path)
        if values.create_ovf:
            name = os.path.join(values.target_path,
                                values.encrypted_ovf_name + ".ovf")
            if (os.path.exists(name) is False):
                raise ValidationError("Encrypted OVF image not found at "
                                      "%s", name)
            encrypted_ovf_name = values.encrypted_ovf_name
        else:
            encrypted_ova_name = values.encrypted_ovf_name
            name = os.path.join(values.target_path,
                                values.encrypted_ovf_name + ".ova")
            if (os.path.exists(name) is False):
                raise ValidationError("Encrypted OVA image not found at "
                                      "%s", name)
            # verify ovftool is present
            try:
                cmd = [values.ovftool_path, '-v']
                subprocess.check_call(cmd)
            except:
                raise ValidationError("OVFtool not present. "
                                      "Cannot process OVA")
    else:
        if use_esx:
            raise ValidationError("Cannot use template VMs for "
                                  "updation on a single ESX host")
        if (values.template_vm_name is None):
            raise ValidationError("Encrypted image not provided")
    if values.source_image_path is None and values.image_name is not None:
        raise ValidationError("Specify OVF image location with --ovf-source-directory")
    if use_esx:
        _check_env_vars_set('ESX_USER_NAME')
    else:
        _check_env_vars_set('VCENTER_USER_NAME')
    vcenter_password = _get_vcenter_password(use_esx)
    brkt_cli.validate_ntp_servers(values.ntp_servers)
    # Verify we have a valid launch token
    instance_config_args.get_launch_token(values, parsed_config)

    proxy = None
    if values.http_proxy:
        proxy = _parse_proxies(values.http_proxy)[0]

    # static ip configuration
    static_ip = None
    if not use_esx:
        if values.static_ip or values.static_gw or values.static_mask or \
           values.static_dns or values.static_dns_domain:
            static_ip = esx_service.StaticIPConfiguration(
                values.static_ip, values.static_mask, values.static_gw,
                values.static_dns, values.static_dns_domain)
            static_ip.validate()

    # Download images from S3
    try:
        if (values.encryptor_vmdk is None and
            values.source_image_path is None):
            (ovf_name, download_file_list) = \
                esx_service.download_ovf_from_s3(
                    values.bucket_name,
                    version=values.metavisor_version,
                    proxy=proxy
                )
            if ovf_name is None:
                raise ValidationError("Did not find MV OVF images")
    except Exception as e:
        raise ValidationError("Failed to download MV image from S3: ", e)
    # Connect to vCenter
    try:
        vc_swc = esx_service.initialize_vcenter(
            host=values.vcenter_host,
            user=os.getenv('ESX_USER_NAME') if use_esx else os.getenv('VCENTER_USER_NAME'),
            password=vcenter_password,
            port=values.vcenter_port,
            datacenter_name=None if use_esx else values.vcenter_datacenter,
            datastore_name=values.vcenter_datastore,
            esx_host=use_esx,
            cluster_name=None if use_esx else values.vcenter_cluster,
            no_of_cpus=values.no_of_cpus,
            memory_gb=values.memory_gb,
            session_id=session_id,
            network_name=values.network_name,
            nic_type=values.nic_type,
            verify=False if use_esx else values.validate,
        )
    except Exception as e:
        raise ValidationError("Failed to connect to vCenter: ", e)
    # Validate vCenter parameters
    vc_swc.validate_vcenter_params()
    if values.template_vm_name:
        if vc_swc.find_vm(values.template_vm_name) is None:
            raise ValidationError("Template VM %s not found" %
                                  values.template_vm_name)
    try:
        brkt_env = brkt_cli.brkt_env_from_values(values, parsed_config)
        lt = instance_config_args.get_launch_token(values, parsed_config)
        instance_config = instance_config_from_values(
            values,
            mode=INSTANCE_UPDATER_MODE,
            brkt_env=brkt_env,
            launch_token=lt
        )
        user_data_str = vc_swc.create_userdata_str(instance_config,
            update=True, ssh_key_file=values.ssh_public_key_file)
        if (values.encryptor_vmdk is not None):
            # Create from MV VMDK
            update_vmdk.update_from_vmdk(
                vc_swc, encryptor_service.EncryptorService,
                template_vm_name=values.template_vm_name,
                target_path=values.target_path,
                ovf_name=encrypted_ovf_name,
                ova_name=encrypted_ova_name,
                ovftool_path=values.ovftool_path,
                metavisor_vmdk=values.encryptor_vmdk,
                user_data_str=user_data_str,
                status_port=values.status_port,
                static_ip=static_ip
            )
        elif (values.source_image_path is not None):
            # Create from MV OVF in local directory
            update_vmdk.update_from_local_ovf(
                vc_swc, encryptor_service.EncryptorService,
                template_vm_name=values.template_vm_name,
                target_path=values.target_path,
                ovf_name=encrypted_ovf_name,
                ova_name=encrypted_ova_name,
                ovftool_path=values.ovftool_path,
                source_image_path=values.source_image_path,
                ovf_image_name=values.image_name,
                user_data_str=user_data_str,
                status_port=values.status_port,
                static_ip=static_ip
            )
        else:
            # Create from MV OVF in S3
            update_vmdk.update_from_s3(
                vc_swc, encryptor_service.EncryptorService,
                template_vm_name=values.template_vm_name,
                target_path=values.target_path,
                ovf_name=encrypted_ovf_name,
                ova_name=encrypted_ova_name,
                ovftool_path=values.ovftool_path,
                mv_ovf_name=ovf_name,
                download_file_list=download_file_list,
                user_data_str=user_data_str,
                status_port=values.status_port,
                cleanup=values.cleanup,
                static_ip=static_ip
            )
        return 0
    except:
        log.error("Failed to update encrypted VMDK");
        return 1


@gamify
def run_wrap_image(values, parsed_config, log, use_esx=False):
    session_id = util.make_nonce()

    if not use_esx and values.vm_name is None:
        raise ValidationError("Missing vm-name for the VM")
    if values.source_image_path is None and values.image_name is not None:
        raise ValidationError("Specify OVF image location with --ovf-source-directory")
    if use_esx:
        _check_env_vars_set('ESX_USER_NAME')
    else:
        _check_env_vars_set('VCENTER_USER_NAME')
    vcenter_password = _get_vcenter_password(use_esx)
    brkt_cli.validate_ntp_servers(values.ntp_servers)
    brkt_env = brkt_cli.brkt_env_from_values(values)
    if brkt_env is None:
        _, brkt_env = parsed_config.get_current_env()
    # Verify we have a valid launch token
    instance_config_args.get_launch_token(values, parsed_config)

    proxy = None
    if values.http_proxy:
        proxy = _parse_proxies(values.http_proxy)[0]

    # static ip configuration
    static_ip = None
    if not use_esx:
        if values.static_ip or values.static_gw or values.static_mask or \
           values.static_dns or values.static_dns_domain:
            static_ip = esx_service.StaticIPConfiguration(
                values.static_ip, values.static_mask, values.static_gw,
                values.static_dns, values.static_dns_domain)
            static_ip.validate()

    # Download images from S3
    try:
        if (values.encryptor_vmdk is None and
            values.source_image_path is None):
            (ovf, file_list) = \
                esx_service.download_ovf_from_s3(
                    values.bucket_name,
                    version=values.metavisor_version,
                    proxy=proxy
                )
            if ovf is None:
                raise ValidationError("Did not find MV OVF images")
    except Exception as e:
        raise ValidationError("Failed to download MV image from S3: ", e)

    # Connect to vCenter
    try:
        vc_swc = esx_service.initialize_vcenter(
            host=values.vcenter_host,
            user=os.getenv('ESX_USER_NAME') if use_esx else os.getenv('VCENTER_USER_NAME'),
            password=vcenter_password,
            port=values.vcenter_port,
            datacenter_name=None if use_esx else values.vcenter_datacenter,
            datastore_name=values.vcenter_datastore,
            esx_host=use_esx,
            cluster_name=None if use_esx else values.vcenter_cluster,
            no_of_cpus=values.no_of_cpus,
            memory_gb=values.memory_gb,
            session_id=session_id,
            network_name=values.network_name,
            nic_type=values.nic_type,
            verify=False if use_esx else values.validate,
        )
    except Exception as e:
        raise ValidationError("Failed to connect to vCenter: ", e)
    # Validate vCenter parameters
    vc_swc.validate_vcenter_params()
    # Validate that template does not already exist
    if values.vm_name:
        if vc_swc.find_vm(values.vm_name):
            raise ValidationError("VM with the same name as requested "
                                  "template VM name %s already exists" %
                                  values.vm_name)
    # Set the disk-type
    if values.disk_type == "thin":
        vc_swc.set_thin_disk(True)
        vc_swc.set_eager_scrub(False)
    elif values.disk_type == "thick-lazy-zeroed":
        vc_swc.set_thin_disk(False)
        vc_swc.set_eager_scrub(False)
    elif values.disk_type == "thick-eager-zeroed":
        vc_swc.set_thin_disk(False)
        vc_swc.set_eager_scrub(True)
    else:
        raise ValidationError("Disk Type %s not correct. Can only be "
                              "thin, thick-lazy-zeroed or "
                              "thick-eager-zeroed" % (values.disk_type,))

    try:
        brkt_env = brkt_cli.brkt_env_from_values(values, parsed_config)
        lt = instance_config_args.get_launch_token(values, parsed_config)
        instance_config = instance_config_from_values(
            values,
            mode=INSTANCE_METAVISOR_MODE,
            brkt_env=brkt_env,
            launch_token=lt)
        instance_config.brkt_config['allow_unencrypted_guest'] = True
        user_data_str = vc_swc.create_userdata_str(instance_config,
            update=False, ssh_key_file=values.ssh_public_key_file)
        if values.encryptor_vmdk is not None:
            # Create from MV VMDK
            wrap_image.wrap_from_vmdk(
                vc_swc, values.vmdk, vm_name=values.vm_name,
                metavisor_vmdk=values.encryptor_vmdk,
                user_data_str=user_data_str,
                static_ip=static_ip
            )
        elif values.source_image_path is not None:
            # Create from MV OVF in local directory
            wrap_image.wrap_from_local_ovf(
                vc_swc, values.vmdk, vm_name=values.vm_name,
                source_image_path=values.source_image_path,
                ovf_image_name=values.image_name,
                user_data_str=user_data_str,
                static_ip=static_ip
            )
        else:
            # Create from MV OVF in S3
            wrap_image.wrap_from_s3(
                vc_swc, values.vmdk, vm_name=values.vm_name,
                ovf_name=ovf, download_file_list=file_list,
                user_data_str=user_data_str, cleanup=values.cleanup,
                static_ip=static_ip
            )
        return 0
    except Exception as e:
        log.error("Failed to wrap the guest VMDK: %s", e)
        return 1


def run_rescue_metavisor(values, parsed_config, log):
    session_id = util.make_nonce()
    if values.protocol != 'http':
        raise ValidationError("Unsupported rescue protocol %s",
                              values.protocol)
    _check_env_vars_set('VCENTER_USER_NAME')
    vcenter_password = _get_vcenter_password(False)
    # Connect to vCenter
    try:
        vc_swc = esx_service.initialize_vcenter(
            host=values.vcenter_host,
            user=os.getenv('VCENTER_USER_NAME'),
            password=vcenter_password,
            port=values.vcenter_port,
            datacenter_name=values.vcenter_datacenter,
            datastore_name=values.vcenter_datastore,
            esx_host=False,
            cluster_name=values.vcenter_cluster,
            no_of_cpus=None,
            memory_gb=None,
            session_id=session_id,
            network_name=None,
            nic_type=None,
            verify=values.validate,
            cdrom=values.cdrom,
        )
    except Exception as e:
        raise ValidationError("Failed to connect to vCenter ", e)
    try:
        user_data_str = vc_swc.create_userdata_str(None,
            rescue_proto=values.protocol,
            rescue_url=values.url)
        rescue_metavisor.rescue_metavisor_vcenter(
            vc_swc, user_data_str, values.vm_name
        )
        return 0
    except Exception as e:
        log.exception("Failed to put Metavisor in rescue mode %s", e)
        return 1


class VMwareSubcommand(Subcommand):

    def name(self):
        return 'vmware'

    def register(self, subparsers, parsed_config):
        self.config = parsed_config
        vmware_parser = subparsers.add_parser(
            self.name(),
            description='VMware operations',
            help='VMware operations',
        )

        vmware_subparsers = vmware_parser.add_subparsers(
            dest='vmware_subcommand',
            # Hardcode the list, so that we don't expose internal subcommands.
            metavar=(
                '{encrypt-with-vcenter,encrypt-with-esx-host,'
                'update-with-vcenter,update-with-esx-host,'
                'wrap-with-vcenter, wrap-with-esx-host}'
            )
        )

        encrypt_with_vcenter_parser = vmware_subparsers.add_parser(
            'encrypt-with-vcenter',
            description=(
                'Create an encrypted VMDK from an existing VMDK using vCenter'
            ),
            help='Encrypt a VMDK using vCenter',
            formatter_class=brkt_cli.SortingHelpFormatter
        )
        encrypt_vmdk_args.setup_encrypt_vmdk_args(
            encrypt_with_vcenter_parser)
        setup_instance_config_args(encrypt_with_vcenter_parser, parsed_config)

        encrypt_with_esx_parser = vmware_subparsers.add_parser(
            'encrypt-with-esx-host',
            description=(
                'Create an encrypted VMDK from an existing VMDK on an ESX host'
            ),
            help='Encrypt a VMDK on a ESX host',
            formatter_class=brkt_cli.SortingHelpFormatter
        )
        encrypt_with_esx_host_args.setup_encrypt_with_esx_host_args(
            encrypt_with_esx_parser)
        setup_instance_config_args(encrypt_with_esx_parser, parsed_config)

        update_with_vcenter_parser = vmware_subparsers.add_parser(
            'update-with-vcenter',
            description=(
                'Update an encrypted VMDK with the latest Metavisor using vCenter'
            ),
            help='Update an encrypted VMDK using vCenter',
            formatter_class=brkt_cli.SortingHelpFormatter
        )
        update_encrypted_vmdk_args.setup_update_vmdk_args(
            update_with_vcenter_parser)
        setup_instance_config_args(update_with_vcenter_parser, parsed_config)

        update_with_esx_parser = vmware_subparsers.add_parser(
            'update-with-esx-host',
            description=(
                'Update an encrypted VMDK with the latest Metavisor on an ESX host'
            ),
            help='Update an encrypted VMDK on an ESX host',
            formatter_class=brkt_cli.SortingHelpFormatter
        )
        update_with_esx_host_args.setup_update_with_esx_host_args(
            update_with_esx_parser)
        setup_instance_config_args(update_with_esx_parser, parsed_config)

        wrap_with_vcenter_parser = vmware_subparsers.add_parser(
            'wrap-with-vcenter',
            description=(
                'Launch guest image wrapped with Bracket Metavsor using vCenter'
            ),
            help='Launch guest image wrapped with Bracket Metavsor using vCenter',
            formatter_class=brkt_cli.SortingHelpFormatter
        )
        wrap_with_vcenter_args.setup_wrap_with_vcenter_args(
            wrap_with_vcenter_parser)
        setup_instance_config_args(wrap_with_vcenter_parser, parsed_config)

        wrap_with_esx_host_parser = vmware_subparsers.add_parser(
            'wrap-with-esx-host',
            description=(
                'Launch guest image wrapped with Bracket Metavsor on ESX host'
            ),
            help='Launch guest image wrapped with Bracket Metavsor on ESX host',
            formatter_class=brkt_cli.SortingHelpFormatter
        )
        wrap_with_esx_host_args.setup_wrap_with_esx_host_args(
            wrap_with_esx_host_parser)
        setup_instance_config_args(wrap_with_esx_host_parser, parsed_config)

        rescue_metavisor_parser = vmware_subparsers.add_parser(
            # Don't specify the help field.  This is an internal command
            # which shouldn't show up in usage output.
            'rescue-metavisor',
            description=(
                'Upload a Metavisor VM cores and diagnostics to a URL'
            ),
            formatter_class=brkt_cli.SortingHelpFormatter
        )
        rescue_metavisor_args.setup_rescue_metavisor_args(
            rescue_metavisor_parser)
        setup_instance_config_args(rescue_metavisor_parser, parsed_config)

    def run(self, values):
        if values.vmware_subcommand == 'encrypt-with-vcenter':
            return run_encrypt(values, self.config, log)
        if values.vmware_subcommand == 'encrypt-with-esx-host':
            return run_encrypt(values, self.config, log, use_esx=True)
        if values.vmware_subcommand == 'update-with-vcenter':
            return run_update(values, self.config, log)
        if values.vmware_subcommand == 'update-with-esx-host':
            return run_update(values, self.config, log, use_esx=True)
        if values.vmware_subcommand == 'rescue-metavisor':
            return run_rescue_metavisor(values, self.config, log)
        if values.vmware_subcommand == 'wrap-with-vcenter':
            return run_wrap_image(values, self.config, log)
        if values.vmware_subcommand == 'wrap-with-esx-host':
            return run_wrap_image(values, self.config, log, use_esx=True)


def get_subcommands():
    return [VMwareSubcommand()]
