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
"""
Create an encrypted VMDK based on an existing unencrypted VMDK.

Overview of the process:
    * Start a VM based on the metavisor VMDK.
    * Attach the unencrypted guest VMDK to the Encryptor instance.
    * The Bracket Encryptor copies the unencrypted root volume to a new
        encrypted volume that's 2x the size of the original.
    * Copy the new Metavisor VMDK and the encrypted guest VMDK
    * Create the output VM/OVF/2 VMDKs
    * Terminate the VM.

Before running brkt encrypt-vmdk, do "pip install pyvmomi".
"""

import logging
from brkt_cli.encryptor_service import (
    wait_for_encryptor_up,
    wait_for_encryption,
    ENCRYPTOR_STATUS_PORT
)
from brkt_cli.util import Deadline
from brkt_cli.esx.esx_service import (
    launch_mv_vm_from_s3,
    validate_local_mv_ovf
)
from brkt_cli.util import CRYPTO_XTS


log = logging.getLogger(__name__)


def create_ovf_image_from_mv_vm(vc_swc, enc_svc_cls, vm, guest_vmdk,
                                crypto_policy, vm_name=None, create_ovf=False,
                                create_ova=False, target_path=None,
                                image_name=None, ovftool_path=None,
                                user_data_str=None, serial_port_file_name=None,
                                status_port=ENCRYPTOR_STATUS_PORT,
                                static_ip=None):
    try:
        mv_vm_name = vc_swc.get_vm_name(vm)
        # clone the guest vmdk
        new_guest_vmdk_name = vc_swc.get_session_vmdk_name(guest_vmdk)
        source_disk = vc_swc.get_datastore_path(guest_vmdk)
        vc_swc.clone_disk(source_disk_name=source_disk,
                          dest_disk_name=new_guest_vmdk_name)
        # Reconfigure VM with more CPUs and memory
        vc_swc.reconfigure_vm_cpu_ram(vm)
        # Add datastore path to the guest vmdk
        # Attach guest vmdk
        vc_swc.add_disk(vm, filename=new_guest_vmdk_name, unit_number=2)
        if crypto_policy is None:
            crypto_policy = CRYPTO_XTS
        # Attach empty disk
        size = vc_swc.get_disk_size(vm, 2)
        if crypto_policy == CRYPTO_XTS:
            encrypted_guest_size = size + (6 * 1024*1024)
        else:
            encrypted_guest_size = (2 * size) + (6 * 1024*1024)
        vc_swc.add_disk(vm, disk_size=encrypted_guest_size, unit_number=1)
        # Add CDROM if required
        if vc_swc.cdrom:
            log.info("Adding CDROM configuration")
            vc_swc.add_cdrom(vm)
        # Configure Static IP for the encryptor VM
        if static_ip:
            vc_swc.configure_static_ip(vm, static_ip)
        # Reconfigure VM with serial port
        if serial_port_file_name:
            vc_swc.add_serial_port_to_file(vm, serial_port_file_name)
        # Power on the VM and wait for encryption
        vc_swc.power_on(vm)
        # Send user data
        vc_swc.send_userdata(vm, user_data_str)
        ip_addr = vc_swc.get_ip_address(vm)
        log.info("VM ip address is %s", ip_addr)
        # disconnect from vcenter
        vc_swc.disconnect()
        # wait for encryption to complete
        host_ips = [ip_addr]
        enc_svc = enc_svc_cls(host_ips, port=status_port)
        wait_for_encryptor_up(enc_svc, Deadline(600))
        wait_for_encryption(enc_svc)
        # reconnect to vcenter
        try:
            vc_swc.connect()
        except:
            log.error("Failed to re-connect to vCenter after encryption. "
                      "Please cleanup VM %s manually.", mv_vm_name)
            raise
        for retry in range(10):
            # This should not fail, if it fails, then retry
            # 10 times before giving up
            vm = vc_swc.find_vm(mv_vm_name)
            if vm:
                break
        # detach unencrypted guest root
        vc_swc.power_off(vm)
        vc_swc.detach_disk(vm, unit_number=0)
        vc_swc.detach_disk(vm, unit_number=2)
        # detach serial port
        if serial_port_file_name:
            vc_swc.delete_serial_port_to_file(vm, serial_port_file_name)
        if ((create_ovf is True) or (create_ova is True)):
            log.info("Creating images")
            if target_path is None:
                raise Exception("Cannot create ova/ovf as target path is None")
            ovf = vc_swc.export_to_ovf(vm, target_path, ovf_name=image_name)
            if create_ova is True:
                if ovftool_path is not None:
                    ova = vc_swc.convert_ovf_to_ova(ovftool_path, ovf)
                    print(ova)
            else:
                print(ovf)
        else:
            # clone the vm to create template
            if vc_swc.is_esx_host() is False:
                log.info("Creating the template VM")
                template_vm = vc_swc.clone_vm(vm, vm_name=vm_name,
                                              template=True)
                print(vc_swc.get_vm_name(template_vm))
        # Clean up encryptor VM in case of successful encryption
        vc_swc.set_teardown(False)
    except Exception:
        log.exception("Failed to encrypt the image")
        try:
            vc_swc.connect()
            vm = vc_swc.find_vm(mv_vm_name)
            vc_swc.power_off(vm)
            vc_swc.detach_disk(vm, unit_number=2)
            if serial_port_file_name:
                vc_swc.delete_serial_port_to_file(vm, serial_port_file_name)
        except:
            log.error("Failed to detach guest vmdk after encryption failure. "
                      "Please detach guest vmdk manually before cleaning "
                      "up VM %s.", mv_vm_name)
            vc_swc.set_teardown(True)
        raise
    finally:
        if vc_swc.no_teardown is False:
            if (vc_swc.is_esx_host() is True and
               create_ovf is False and
               create_ova is False):
                # Do not clean up encryptor VM as it will be
                # used as the clone VM
                log.info("Encrypted VM is %s", mv_vm_name)
            else:
                if vc_swc.connected() is False:
                    try:
                        vc_swc.connect()
                    except:
                        log.error("Failed to re-connect to vCenter after "
                                  "encryption. Please cleanup VM %s manually.",
                                  mv_vm_name)
                        raise
                    vm = None
                    if mv_vm_name:
                        vm = vc_swc.find_vm(mv_vm_name)
                if vm is not None:
                    vc_swc.destroy_vm(vm)
                try:
                    vc_swc.delete_disk(new_guest_vmdk_name)
                except Exception as e:
                    log.error("Failed to delete disk %s %s",
                              new_guest_vmdk_name, e)
        log.info("Done")


def encrypt_from_s3(vc_swc, enc_svc_cls, guest_vmdk, crypto_policy,
                    vm_name=None, create_ovf=False, create_ova=False,
                    target_path=None, image_name=None, ovftool_path=None,
                    ovf_name=None, download_file_list=None,
                    user_data_str=None, serial_port_file_name=None,
                    status_port=ENCRYPTOR_STATUS_PORT, cleanup=True,
                    static_ip=None):
    vm = None
    try:
        if (ovf_name is None or download_file_list is None):
            log.error("Cannot get metavisor OVF from S3")
            raise Exception("Invalid MV OVF")
        mv_vm_name = None
        if vc_swc.is_esx_host():
            mv_vm_name = vm_name
        vm = launch_mv_vm_from_s3(vc_swc, ovf_name,
                                  download_file_list, mv_vm_name,
                                  cleanup)
    except Exception as e:
        log.exception("Failed to launch metavisor OVF from S3 (%s)", e)
        if (vm is not None):
            vc_swc.destroy_vm(vm)
        raise
    create_ovf_image_from_mv_vm(vc_swc, enc_svc_cls, vm,
                                guest_vmdk, crypto_policy, vm_name,
                                create_ovf, create_ova, target_path,
                                image_name, ovftool_path, user_data_str,
                                serial_port_file_name, status_port, static_ip)


def encrypt_from_local_ovf(vc_swc, enc_svc_cls, guest_vmdk, crypto_policy,
                           vm_name=None, create_ovf=False, create_ova=False,
                           target_path=None, image_name=None,
                           ovftool_path=None, source_image_path=None,
                           ovf_image_name=None, user_data_str=None,
                           serial_port_file_name=None,
                           status_port=ENCRYPTOR_STATUS_PORT, static_ip=None):
    vm = None
    try:
        if ((source_image_path is None) or (ovf_image_name is None)):
            log.error("Metavisor OVF path needs to be specified")
            return
        # Launch OVF
        log.info("Launching VM from local OVF")
        # Normalize image name if required
        if not ovf_image_name.endswith('.ovf'):
            ovf_image_name = ovf_image_name + ".ovf"
        validate_local_mv_ovf(source_image_path, ovf_image_name)
        mv_vm_name = None
        if vc_swc.is_esx_host():
            mv_vm_name = vm_name
        vm = vc_swc.upload_ovf_to_vcenter(source_image_path,
                                          ovf_image_name,
                                          vm_name=mv_vm_name)
    except Exception as e:
        log.exception("Failed to launch from metavisor OVF (%s)", e)
        if (vm is not None):
            vc_swc.destroy_vm(vm)
        raise
    create_ovf_image_from_mv_vm(vc_swc, enc_svc_cls, vm,
                                guest_vmdk, crypto_policy, vm_name,
                                create_ovf, create_ova, target_path,
                                image_name, ovftool_path,
                                user_data_str, serial_port_file_name,
                                status_port, static_ip)


def encrypt_from_vmdk(vc_swc, enc_svc_cls, guest_vmdk, crypto_policy,
                      vm_name=None, create_ovf=False, create_ova=False,
                      target_path=None, image_name=None, ovftool_path=None,
                      metavisor_vmdk=None, user_data_str=None,
                      serial_port_file_name=None,
                      status_port=ENCRYPTOR_STATUS_PORT, static_ip=None):
    try:
        vm = None
        if (metavisor_vmdk is None):
            log.error("Metavisor VMDK is not specified")
            return
        # Add datastore path to the vmdk
        metavisor_vmdk_path = vc_swc.get_datastore_path(metavisor_vmdk)
        # Create a metavisor VM
        vm = vc_swc.create_vm()
        # Attach metavisor vmdk as root disk
        vc_swc.add_disk(vm, filename=metavisor_vmdk_path, unit_number=0)
        # Add CDROM if required
        if vc_swc.cdrom:
            log.info("Adding CDROM configuration")
            vc_swc.add_cdrom(vm)
    except Exception as e:
        log.exception("Failed to launch metavisor VMDK (%s)", e)
        if (vm is not None):
            vc_swc.destroy_vm(vm)
        raise
    create_ovf_image_from_mv_vm(vc_swc, enc_svc_cls, vm,
                                guest_vmdk, crypto_policy, vm_name,
                                create_ovf, create_ova, target_path,
                                image_name, ovftool_path,
                                user_data_str, serial_port_file_name,
                                status_port, static_ip)
