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
import abc
import json
import logging
import time
import datetime
import ssl
import atexit
import os
import signal
import hashlib
import requests
import socket
import struct
import xml.etree.ElementTree
from functools import wraps
from operator import attrgetter
from threading import Thread
from httplib import BadStatusLine

import boto3
from botocore.handlers import disable_signing
from pyVim import connect
from pyVmomi import vmodl
from pyVmomi import vim

from brkt_cli.util import (
    retry,
    RetryExceptionChecker,
    validate_ip_address,
    validate_dns_name_ip_address
)
from brkt_cli import crypto
from brkt_cli import mv_version
from brkt_cli.instance_config import INSTANCE_UPDATER_MODE
from brkt_cli.validation import ValidationError

log = logging.getLogger(__name__)
logging.getLogger('boto3').setLevel(logging.FATAL)
logging.getLogger('botocore').setLevel(logging.FATAL)
logging.getLogger('s3transfer').setLevel(logging.FATAL)


class TimeoutError(Exception):
    pass

def timeout(seconds=30, error_message="Timer expired"):
    def decorator(func):
        def _handle_timeout(signum, frame):
            raise TimeoutError(error_message)

        def wrapper(*args, **kwargs):
            signal.signal(signal.SIGALRM, _handle_timeout)
            signal.alarm(seconds)
            try:
                result = func(*args, **kwargs)
            finally:
                signal.alarm(0)
            return result

        return wraps(func)(wrapper)

    return decorator


def compute_sha1_of_file(filename):
    hash_sha1 = hashlib.sha1()
    with open(filename, 'rb') as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash_sha1.update(chunk)
    return hash_sha1.hexdigest()


class StaticIPConfiguration(object):
    def __init__(self, ip, mask, gw, dns, dns_domain):
        self.ip = ip
        self.mask = mask
        self.gw = gw
        self.dns = dns
        self.dns_domain = dns_domain

    def validate(self):
        if not self.ip:
            raise ValidationError("VM IP address for static IP not provided")
        if not self.mask:
            raise ValidationError("Subnet mask for static IP not provided")
        if not self.gw:
            raise ValidationError("Default router for static IP not provided")
        if not self.dns:
            raise ValidationError("DNS servers for static IP not provided")
        if not self.dns_domain:
            raise ValidationError("DNS domain for static IP not provided")
        if not validate_ip_address(self.ip):
            raise ValidationError("IP address %s is not in IP address format",
                                  self.ip)
        if not validate_ip_address(self.mask):
            raise ValidationError("Subnet mask %s is not in IP address format",
                                  self.mask)
        if self.mask == '255.255.255.255':
            raise ValidationError("Subnet mask cannot be /32")
        if not validate_ip_address(self.gw):
            raise ValidationError("Default router %s is not in IP address "
                                  "format", self.gw)
        for server in self.dns:
            if not validate_ip_address(server):
                raise ValidationError("DNS server %s is not in IP address "
                                      "format", server)
        if not validate_dns_name_ip_address(self.dns_domain):
            raise ValidationError("DNS domain %s is invalid ",
                                  self.dns_domain)
        # check both ip and default router in same subnet
        if ((struct.unpack('!I', socket.inet_pton(socket.AF_INET, self.ip))[0] &
           struct.unpack('!I', socket.inet_pton(socket.AF_INET, self.mask))[0])
           !=
           (struct.unpack('!I', socket.inet_pton(socket.AF_INET, self.gw))[0] &
           struct.unpack('!I', socket.inet_pton(socket.AF_INET, self.mask))[0])):
            raise ValidationError("Default router %s and IP address %s not in "
                                  "the same subnet", self.gw, self.ip)


class BaseVCenterService(object):
    __metaclass__ = abc.ABCMeta

    def __init__(self, host, user, password, port,
                 datacenter_name, datastore_name, esx_host,
                 cluster_name, no_of_cpus, memoryGB, session_id,
                 network_name, nic_type, verify=True, cdrom=False):
        self.host = host
        self.user = user
        self.password = password
        self.port = port
        self.datacenter_name = datacenter_name
        self.datastore_name = datastore_name
        self.datastore_path = "[" + datastore_name + "] "
        self.esx_host = esx_host
        self.cluster_name = cluster_name
        if self.esx_host:
            self.cluster_name = None
        self.no_of_cpus = no_of_cpus
        self.memoryGB = memoryGB
        self.session_id = session_id
        self.no_teardown = False
        self.si = None
        self.thindisk = True
        self.eagerscrub = False
        self.network_name = network_name
        self.nic_type = nic_type
        self.cdrom = cdrom
        self.verify = verify

    def is_esx_host(self):
        return self.esx_host

    def get_session_vmdk_name(self, vmdk_name):
        p = os.path.split(vmdk_name)
        new_vmdk_name = self.session_id + p[1]
        if len(p[0]) > 0:
            new_vmdk_name = os.path.join(p[0], self.session_id + p[1])
        f_name = self.get_datastore_path(new_vmdk_name)
        return f_name

    @abc.abstractmethod
    def connect(self):
        pass

    @abc.abstractmethod
    def disconnect(self):
        pass

    @abc.abstractmethod
    def connected(self):
        pass

    @abc.abstractmethod
    def validate_connection(self):
        pass

    @abc.abstractmethod
    def get_session_id(self):
        pass

    @abc.abstractmethod
    def get_datastore_path(self, vmdk_name):
        pass

    @abc.abstractmethod
    def validate_vcenter_params(self):
        pass

    @abc.abstractmethod
    def find_vm(self, vm_name):
        pass

    @abc.abstractmethod
    def power_on(self, vm):
        pass

    @abc.abstractmethod
    def power_off(self, vm):
        pass

    @abc.abstractmethod
    def destroy_vm(self, vm):
        pass

    @abc.abstractmethod
    def get_ip_address(self, vm):
        pass

    @abc.abstractmethod
    def create_vm(self, memoryGB=1, numCPUs=1, vm_name=None):
        pass

    @abc.abstractmethod
    def reconfigure_vm_cpu_ram(self, vm):
        pass

    @abc.abstractmethod
    def rename_vm(self, vm, new_name):
        pass

    @abc.abstractmethod
    def add_disk(self, vm, disk_size=12*1024*1024,
                 filename=None, unit_number=0):
        pass

    @abc.abstractmethod
    def detach_disk(self, vm, unit_number=2):
        pass

    @abc.abstractmethod
    def clone_disk(self, source_disk=None, source_disk_name=None,
                   dest_disk=None, dest_disk_name=None):
        pass

    @abc.abstractmethod
    def delete_disk(self, disk_name):
        pass

    @abc.abstractmethod
    def get_disk(self, vm, unit_number):
        pass

    @abc.abstractmethod
    def get_disk_size(self, vm, unit_number):
        pass

    @abc.abstractmethod
    def clone_vm(self, vm, powerOn=False, vm_name=None, template=False):
        pass

    @abc.abstractmethod
    def create_userdata_str(self, instance_config, update=False,
                            ssh_key_file=None,
                            rescue_proto=None, rescue_url=None):
        pass

    @abc.abstractmethod
    def send_userdata(self, vm, user_data_str):
        pass

    @abc.abstractmethod
    def keep_lease_alive(self, lease):
        pass

    @abc.abstractmethod
    def export_to_ovf(self, vm, target_path, ovf_name=None):
        pass

    @abc.abstractmethod
    def convert_ovf_to_ova(self, ovftool_path, ovf_path):
        pass

    @abc.abstractmethod
    def convert_ova_to_ovf(self, ovftool_path, ova_path):
        pass

    @abc.abstractmethod
    def get_ovf_descriptor(self, ovf_path):
        pass

    @abc.abstractmethod
    def upload_ovf_to_vcenter(self, target_path, ovf_name, vm_name=None):
        pass

    @abc.abstractmethod
    def get_vm_name(self, vm):
        pass

    @abc.abstractmethod
    def get_disk_name(self, disk):
        pass

    def set_teardown(self, no_teardown):
        self.no_teardown = no_teardown

    def set_thin_disk(self, thin_disk):
        self.thindisk = thin_disk

    def set_eager_scrub(self, eager_scrub):
        self.eagerscrub = eager_scrub

class VmodlExceptionChecker(RetryExceptionChecker):
    def __init__(self, message):
        self.message = None

    def is_expected(self, exception):
        if isinstance(exception, TimeoutError):
            log.info("vCenter connection timed out, trying again")
            return True
        if isinstance(exception, ssl.SSLError):
            log.info("SSL error, trying again")
            return True
        if isinstance(exception, requests.exceptions.ConnectionError) or \
           isinstance(exception, vim.fault.HostConnectFault):
            if ("10060" in str(exception)):
                # 10060 corresponds to Connection timed out in Windows
                log.info("Connection timeout error, retrying")
                return True
        if isinstance(exception, vmodl.MethodFault):
            if ("STREAM ioctl timeout" in exception.msg):
                log.info("Stream IOCTL timeout, retrying")
                return True
            if ("Device timeout" in exception.msg):
                log.info("Device timeout, retrying")
                return True
            if ("Timer expired" in exception.msg):
                log.info("Timer expired, retrying")
                return True
        return False


class VCenterService(BaseVCenterService):
    def __init__(self, host, user, password, port,
                 datacenter_name, datastore_name, esx_host,
                 cluster_name, no_of_cpus, memoryGB, session_id,
                 network_name, nic_type, verify, cdrom):
        super(VCenterService, self).__init__(
            host, user, password, port, datacenter_name, datastore_name,
            esx_host, cluster_name, no_of_cpus, memoryGB, session_id,
            network_name, nic_type, verify, cdrom)

    @timeout(30)
    def _s_connect(self):
        context = None
        try:
            # Check if python version has SSLContext
            context = ssl.SSLContext
        except:
            context = None
        if self.verify:
            context = None
        if context:
            # Change ssl context due to bug in pyvmomi
            context = ssl.SSLContext(ssl.PROTOCOL_SSLv23)
            context.verify_mode = ssl.CERT_NONE
            self.si = connect.SmartConnect(host=self.host,
                                           user=self.user,
                                           pwd=self.password,
                                           port=self.port,
                                           sslContext=context)
        else:
            self.si = connect.SmartConnect(host=self.host,
                                           user=self.user,
                                           pwd=self.password,
                                           port=self.port)
        atexit.register(connect.Disconnect, self.si)

    def __connect(self):
        context = None
        try:
            context = ssl.SSLContext
        except:
            context = None
        if (context is not None):
            # Change ssl context due to bug in pyvmomi
            context = ssl.SSLContext(ssl.PROTOCOL_SSLv23)
            context.verify_mode = ssl.CERT_NONE
            self.si = connect.SmartConnect(host=self.host,
                                           user=self.user,
                                           pwd=self.password,
                                           port=self.port,
                                           sslContext=context)
        else:
            self.si = connect.SmartConnect(host=self.host,
                                           user=self.user,
                                           pwd=self.password,
                                           port=self.port)
        atexit.register(connect.Disconnect, self.si)

    def connect(self):
        func = None
        try:
            if signal.SIGALRM:
                func = self._s_connect
            else:
                func = self.__connect
        except:
            func = self.__connect
        try:
            retry(func,
                  exception_checker=VmodlExceptionChecker(None),
                  timeout=1000,
                  initial_sleep_seconds=15)()
        except vmodl.MethodFault as error:
            log.exception("Caught vmodl fault : %s", error.msg)
            raise

        # set datastore name
        if self.datastore_name is None:
            content = self.si.RetrieveContent()
            datastore = self.__get_obj(content, [vim.Datastore], None)
            self.datastore_name = datastore.info.name
            self.datastore_path = "[" + self.datastore_name + "] "

    def disconnect(self):
        connect.Disconnect(self.si)
        self.si = None

    def connected(self):
        if self.si is None:
            return False
        return True

    def validate_connection(self):
        try:
            content = self.si.RetrieveContent()
            self.__get_obj(content, [vim.VirtualMachine], None)
        except BadStatusLine:
            # Connection has expired, reconnect
            self.si = None
            try:
                log.info("vCenter connection expired, reconnecting")
                self.connect()
            except:
                log.error("vCenter connection expired and failed "
                          "to reconnect")
                raise
        except:
            log.error("vCenter connection expired and failed to reconnect")
            raise

    def get_session_id(self):
        return self.session_id

    def get_datastore_path(self, vmdk_name):
        if vmdk_name is None:
            return None
        vmdk_path = self.datastore_path + vmdk_name
        return vmdk_path

    def __get_obj(self, content, vimtype, name):
        obj = None
        container = content.viewManager.CreateContainerView(
            content.rootFolder, vimtype, True)
        for c in container.view:
            try:
                if name:
                    if c.name == name:
                        obj = c
                        break
                else:
                    obj = c
                    break
            except:
                pass
        return obj

    def __wait_for_task(self, task):
        while True:
            if task.info.state == 'success':
                return task.info.result
            if task.info.state == 'error':
                raise Exception('Task failed to finish with error %s' %
                                task.info.error)

    def validate_vcenter_params(self):
        content = self.si.RetrieveContent()
        if self.datacenter_name:
            datacenter = self.__get_obj(content, [vim.Datacenter],
                                        self.datacenter_name)
            if not datacenter:
                raise ValidationError("Datacenter %s not found",
                                      self.datacenter_name)
        if self.datastore_name:
            datastore = self.__get_obj(content, [vim.Datastore],
                                       self.datastore_name)
            if not datastore:
                raise ValidationError("Datastore %s not found",
                                      self.datastore_name)
        if self.cluster_name:
            cluster = self.__get_obj(content, [vim.ComputeResource],
                                     self.cluster_name)
            if not cluster:
                raise ValidationError("Cluster %s not found",
                                      self.cluster_name)
        elif not self.esx_host:
            raise ValidationError("Cluster name required when using vCenter")

    def find_vm(self, vm_name):
        self.validate_connection()
        content = self.si.RetrieveContent()
        vm = self.__get_obj(content, [vim.VirtualMachine], vm_name)
        return vm

    def power_on(self, vm):
        self.validate_connection()
        if format(vm.runtime.powerState) == "poweredOn":
            return
        task = vm.PowerOnVM_Task()
        self.__wait_for_task(task)

    def power_off(self, vm):
        self.validate_connection()
        if format(vm.runtime.powerState) != "poweredOn":
            return
        task = vm.PowerOffVM_Task()
        self.__wait_for_task(task)

    def destroy_vm(self, vm):
        self.validate_connection()
        log.info("Destroying VM %s", vm.config.name)
        content = self.si.RetrieveContent()
        f = self.si.content.fileManager
        vm_disk_name = vm.config.name.replace(':', '_')
        self.power_off(vm)
        vm.UnregisterVM()
        if self.esx_host:
            vm_disk_url = "https://" + self.host + "/folder/" + vm_disk_name + "?dsName=" + self.datastore_name
            task = f.DeleteDatastoreFile_Task(vm_disk_url)
        else:
            vm_disk_name = self.datastore_path + vm_disk_name
            datacenter = self.__get_obj(content, [vim.Datacenter],
                                        self.datacenter_name)
            task = f.DeleteDatastoreFile_Task(vm_disk_name, datacenter)
        self.__wait_for_task(task)

    def get_ip_address(self, vm):
        self.validate_connection()
        retry = 0
        while (vm.guest.ipAddress is None):
            if retry > 60:
                raise Exception('Cannot get VMs IP address')
            time.sleep(10)
            retry = retry + 1
        return (vm.guest.ipAddress)

    def create_vm(self, memoryGB=1, numCPUs=1, vm_name=None):
        self.validate_connection()
        content = self.si.RetrieveContent()
        datacenter = self.__get_obj(content, [vim.Datacenter],
                                    self.datacenter_name)
        vmfolder = datacenter.vmFolder
        cluster = self.__get_obj(content, [vim.ComputeResource],
                                 self.cluster_name)
        pool = cluster.resourcePool
        timestamp = datetime.datetime.utcnow().isoformat() + 'Z'
        if not vm_name:
            vm_name = "VM-" + timestamp
        vmx_file = vim.vm.FileInfo(logDirectory=None,
                                   snapshotDirectory=None,
                                   suspendDirectory=None,
                                   vmPathName=self.datastore_path)
        dev_changes = []
        # Add SCSI controller
        controller = vim.vm.device.VirtualLsiLogicController()
        controller.key = -1
        controller.sharedBus = \
            vim.vm.device.VirtualSCSIController.Sharing.noSharing
        controller.hotAddRemove = True
        controller.busNumber = 0
        disk_spec = vim.vm.device.VirtualDeviceSpec()
        disk_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
        disk_spec.device = controller
        dev_changes.append(disk_spec)
        # Add network interface
        n_intf = vim.vm.device.VirtualVmxnet3()
        n_intf.key = -1
        n_intf.backing = vim.vm.device.VirtualEthernetCard.NetworkBackingInfo()
        n_intf.backing.deviceName = self.network_name
        disk_spec_2 = vim.vm.device.VirtualDeviceSpec()
        disk_spec_2.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
        disk_spec_2.device = n_intf
        dev_changes.append(disk_spec_2)
        # Create the VM
        config = vim.vm.ConfigSpec(name=vm_name,
                                   memoryMB=(memoryGB*1024),
                                   numCPUs=numCPUs,
                                   files=vmx_file,
                                   guestId='otherLinuxGuest64',
                                   version='vmx-11',
                                   deviceChange=dev_changes)
        task = vmfolder.CreateVM_Task(config=config, pool=pool)
        self.__wait_for_task(task)
        log.info("VM %s created", vm_name)
        content = self.si.RetrieveContent()
        vm = self.__get_obj(content, [vim.VirtualMachine], vm_name)
        return vm

    def reconfigure_vm_cpu_ram(self, vm):
        self.validate_connection()
        vm_name = vm.config.name
        spec = vim.vm.ConfigSpec(name=vm_name,
                                 memoryMB=(1024*int(self.memoryGB)),
                                 numCPUs=int(self.no_of_cpus))
        task = vm.ReconfigVM_Task(spec=spec)
        self.__wait_for_task(task)

    def configure_static_ip(self, vm, static_ip):
        self.validate_connection()
        log.info("Configuring static IP address")
        # Static IP configuration on MVs work only with a
        # power-on and then power-off of the VM
        self.power_on(vm)
        self.power_off(vm)
        adaptermap = vim.vm.customization.AdapterMapping()
        globalip = vim.vm.customization.GlobalIPSettings()
        adaptermap.adapter = vim.vm.customization.IPSettings()
        adaptermap.adapter.ip = vim.vm.customization.FixedIp()
        adaptermap.adapter.ip.ipAddress = static_ip.ip
        adaptermap.adapter.subnetMask = static_ip.mask
        adaptermap.adapter.gateway = static_ip.gw
        globalip.dnsServerList = static_ip.dns
        adaptermap.adapter.dnsDomain = static_ip.dns_domain
        fixedname = 'brkt-' + self.session_id
        ident = vim.vm.customization.LinuxPrep(
            domain=static_ip.dns_domain,
            hostName=vim.vm.customization.FixedName(name=fixedname))
        customspec = vim.vm.customization.Specification()
        customspec.identity = ident
        customspec.nicSettingMap = [adaptermap]
        customspec.globalIPSettings = globalip
        task = vm.Customize(spec=customspec)
        self.__wait_for_task(task)

    def rename_vm(self, vm, new_name):
        if self.esx_host:
            raise Exception("Cannot rename VM on ESX host")
        self.validate_connection()
        vm_disk_name = vm.config.name.replace(':', '_')
        vm_disk_name = self.datastore_path + vm_disk_name
        vm_new_name = new_name.replace(':', '_')
        dest_disk_name = self.datastore_path + vm_new_name
        # first rename the VM
        spec = vim.vm.ConfigSpec(name=new_name)
        task = vm.ReconfigVM_Task(spec=spec)
        self.__wait_for_task(task)
        # now move the files
        content = self.si.RetrieveContent()
        f = self.si.content.fileManager
        datacenter = self.__get_obj(content, [vim.Datacenter],
                                    self.datacenter_name)
        task = f.MoveDatastoreFile_Task(vm_disk_name, datacenter,
                                        dest_disk_name, datacenter)
        self.__wait_for_task(task)

    def add_serial_port_to_file(self, vm, filename):
        self.validate_connection()
        content = self.si.RetrieveContent()
        spec = vim.vm.ConfigSpec()
        port_spec = vim.vm.device.VirtualDeviceSpec()
        port_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
        sport = vim.vm.device.VirtualSerialPort()
        sport.key = -1
        sport.backing = vim.vm.device.VirtualSerialPort.FileBackingInfo()
        sport.backing.fileName = self.get_datastore_path(filename)
        datastore = self.__get_obj(content, [vim.Datastore],
                                   self.datastore_name)
        sport.backing.datastore = datastore
        port_spec.device = sport
        dev_changes = []
        dev_changes.append(port_spec)
        spec.deviceChange = dev_changes
        task = vm.ReconfigVM_Task(spec=spec)
        self.__wait_for_task(task)
        log.info("Console messages will be dumped to file %s", filename)

    def delete_serial_port_to_file(self, vm, filename):
        self.validate_connection()
        delete_device = None
        backing_filename = self.get_datastore_path(filename)
        for device in vm.config.hardware.device:
            if (isinstance(device, vim.vm.device.VirtualSerialPort)):
                if device.backing.fileName == backing_filename:
                    delete_device = device
        if (delete_device is None):
            return
        spec = vim.vm.ConfigSpec()
        dev_changes = []
        disk_spec = vim.vm.device.VirtualDeviceSpec()
        disk_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.remove
        disk_spec.device = delete_device
        dev_changes.append(disk_spec)
        spec.deviceChange = dev_changes
        task = vm.ReconfigVM_Task(spec=spec)
        self.__wait_for_task(task)
        log.info("Console message will no longer be dumped to file %s",
                 filename)

    def add_cdrom(self, vm=None):
        self.validate_connection()
        # Find the IDE controller
        ide_ctlr = None
        if vm:
            for dev in vm.config.hardware.device:
                if isinstance(dev, vim.vm.device.VirtualIDEController):
                    ide_ctlr = dev
                    break

        cd_spec = vim.vm.device.VirtualDeviceSpec()
        cd_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
        cd_spec.device = vim.vm.device.VirtualCdrom()
        cd_spec.device.key = -1
        cd_spec.device.backing = vim.vm.device.VirtualCdrom.RemotePassthroughBackingInfo()
        cd_spec.device.backing.deviceName = 'cdrom0'
        cd_spec.device.deviceInfo = vim.Description()
        cd_spec.device.deviceInfo.label = 'CD/DVD drive'
        cd_spec.device.connectable = vim.vm.device.VirtualDevice.ConnectInfo()
        cd_spec.device.connectable.startConnected = False
        cd_spec.device.connectable.allowGuestControl = False
        if ide_ctlr:
            cd_spec.device.controllerKey = ide_ctlr.key
        else:
            cd_spec.device.controllerKey = -1
        configSpec = vim.vm.ConfigSpec(deviceChange=[cd_spec])
        task = vm.Reconfigure(configSpec)
        self.__wait_for_task(task)

    def add_disk(self, vm, disk_size=12*1024*1024,
                 filename=None, unit_number=0):
        self.validate_connection()
        spec = vim.vm.ConfigSpec()
        controller = None
        for dev in vm.config.hardware.device:
            if isinstance(dev, vim.vm.device.VirtualSCSIController):
                controller = dev
                break
        if controller is None:
            raise Exception("Did not find SCSI controller in the "
                            "Encryptor VM %s" % (vm.config.name,))
        dev_changes = []
        disk_spec = vim.vm.device.VirtualDeviceSpec()
        disk_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
        disk_spec.device = vim.vm.device.VirtualDisk()
        disk_spec.device.backing = \
            vim.vm.device.VirtualDisk.FlatVer2BackingInfo()
        if filename is not None:
            disk_spec.device.backing.fileName = filename
            disk_spec.device.capacityInKB = -1
        else:
            disk_spec.device.capacityInKB = disk_size
            disk_spec.device.backing.thinProvisioned = self.thindisk
            if self.thindisk is False:
                disk_spec.device.backing.eagerlyScrub = self.eagerscrub
            disk_spec.fileOperation = "create"
        disk_spec.device.backing.diskMode = 'persistent'
        disk_spec.device.unitNumber = unit_number
        disk_spec.device.controllerKey = controller.key
        dev_changes.append(disk_spec)
        spec.deviceChange = dev_changes
        task = vm.ReconfigVM_Task(spec=spec)
        self.__wait_for_task(task)
        if (filename):
            log.info("%s disk added to VM %s", filename, vm.config.name)
        else:
            log.info("%dKB empty disk added to %s", disk_size, vm.config.name)

    def detach_disk(self, vm, unit_number=2):
        self.validate_connection()
        delete_device = None
        for device in vm.config.hardware.device:
            if (isinstance(device, vim.vm.device.VirtualDisk)):
                if (device.unitNumber == unit_number):
                    delete_device = device
        if (delete_device is None):
            log.error("No disk found at %d in VM %s to detach",
                      unit_number, vm.config.name)
            return
        spec = vim.vm.ConfigSpec()
        dev_changes = []
        disk_spec = vim.vm.device.VirtualDeviceSpec()
        disk_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.remove
        disk_spec.device = delete_device
        dev_changes.append(disk_spec)
        spec.deviceChange = dev_changes
        task = vm.ReconfigVM_Task(spec=spec)
        self.__wait_for_task(task)
        log.info("Disk at %d detached from VM %s",
                 unit_number, vm.config.name)
        return delete_device

    def clone_disk(self, source_disk=None, source_disk_name=None,
                   dest_disk=None, dest_disk_name=None):
        self.validate_connection()
        content = self.si.RetrieveContent()
        if source_disk_name is None:
            if source_disk is None:
                raise Exception("Cannot clone disk as source not specified")
            source_disk_name = source_disk.backing.fileName
        if (dest_disk_name is None):
            if (dest_disk is None):
                raise Exception("Cannot clone disk as destination "
                                "not specified")
            source = source_disk_name.split("/")
            dest = dest_disk.backing.fileName.split("/")
            dest_disk_name = source[0] + "/" + dest[1]
        log.info("Cloning disk %s to disk %s", source_disk_name, dest_disk_name)
        virtualDiskManager = self.si.content.virtualDiskManager
        if self.esx_host:
            s_name = source_disk_name
            d_name = dest_disk_name
            if self.datastore_path in source_disk_name:
                start = source_disk_name.find(self.datastore_path)
                s_name = source_disk_name[(start + len(self.datastore_path)):]
            if self.datastore_path in dest_disk_name:
                start = dest_disk_name.find(self.datastore_path)
                d_name = dest_disk_name[(start + len(self.datastore_path)):]
            source_disk_url = "https://" + self.host + "/folder/" + s_name + "?dsName=" + self.datastore_name
            dest_disk_url = "https://" + self.host + "/folder/" + d_name + "?dsName=" + self.datastore_name
            task = virtualDiskManager.CopyVirtualDisk(
                source_disk_url, None,
                dest_disk_url, None)
        else:
            datacenter = self.__get_obj(content, [vim.Datacenter],
                                        self.datacenter_name)
            task = virtualDiskManager.CopyVirtualDisk(
                source_disk_name,
                datacenter,
                dest_disk_name,
                datacenter)
        self.__wait_for_task(task)
        return dest_disk_name

    def delete_disk(self, disk_name):
        self.validate_connection()
        content = self.si.RetrieveContent()
        virtualDiskManager = self.si.content.virtualDiskManager
        if self.esx_host:
            d_name = disk_name
            if self.datastore_path in disk_name:
                start = disk_name.find(self.datastore_path)
                d_name = disk_name[(start + len(self.datastore_path)):]
            disk_url = "https://" + self.host + "/folder/" + d_name + "?dsName=" + self.datastore_name
            task = virtualDiskManager.DeleteVirtualDisk(disk_url, None)
        else:
            datacenter = self.__get_obj(content, [vim.Datacenter],
                                        self.datacenter_name)
            task = virtualDiskManager.DeleteVirtualDisk(disk_name, datacenter)
        self.__wait_for_task(task)

    def get_disk(self, vm, unit_number):
        self.validate_connection()
        for device in vm.config.hardware.device:
            if (isinstance(device, vim.vm.device.VirtualDisk)):
                if (device.unitNumber == unit_number):
                    return device
        return None

    def get_disk_size(self, vm, unit_number):
        self.validate_connection()
        for device in vm.config.hardware.device:
            if (isinstance(device, vim.vm.device.VirtualDisk)):
                if (device.unitNumber == unit_number):
                    s = (device.deviceInfo.summary.split())[0]
                    size = int(s.replace(',', ''))
                    return size
        raise Exception("Did not find disk at %d of VM %s" %
                        (unit_number, vm.config.name))

    def clone_vm(self, vm, powerOn=False, vm_name=None, template=False):
        self.validate_connection()
        if self.esx_host:
            log.error("Cannot create template VM when connected to ESX host")
            return None
        self.validate_connection()
        content = self.si.RetrieveContent()
        datacenter = self.__get_obj(content, [vim.Datacenter],
                                    self.datacenter_name)
        destfolder = datacenter.vmFolder
        cluster = self.__get_obj(content, [vim.ComputeResource],
                                 self.cluster_name)
        pool = cluster.resourcePool
        datastore = self.__get_obj(content, [vim.Datastore],
                                   self.datastore_name)
        relospec = vim.vm.RelocateSpec()
        relospec.datastore = datastore
        relospec.pool = pool
        clonespec = vim.vm.CloneSpec()
        clonespec.location = relospec
        clonespec.powerOn = powerOn
        clonespec.template = template
        if (vm_name is None):
            timestamp = datetime.datetime.utcnow().isoformat() + 'Z'
            vm_name = "template-vm-" + timestamp
        task = vm.Clone(folder=destfolder, name=vm_name, spec=clonespec)
        self.__wait_for_task(task)
        try:
            vm = self.__get_obj(content, [vim.VirtualMachine], vm_name)
            return vm
        except (BadStatusLine, vmodl.fault.ManagedObjectNotFound) as e:
            log.debug("VM %s not found with error %s, retrying", vm_name, e)
            pass
        self.disconnect()
        self.connect()
        vm = self.__get_obj(content, [vim.VirtualMachine], vm_name)
        return vm

    def create_userdata_str(self, instance_config, update=False,
                            ssh_key_file=None,
                            rescue_proto=None, rescue_url=None):
        try:
            brkt_config = {}
            if instance_config:
                brkt_config = instance_config.get_brkt_config()
            if update is True:
                instance_config.set_mode(INSTANCE_UPDATER_MODE)
            if ssh_key_file:
                with open(ssh_key_file, 'r') as f:
                    key_value = (f.read()).strip()
                    if not crypto.is_public_key(key_value):
                        raise ValidationError(
                            '%s is not a public key file' % ssh_key_file
                        )
                brkt_config['ssh-public-key'] = key_value
            if rescue_proto:
                brkt_config = dict()
                brkt_config['rescue'] = dict()
                brkt_config['rescue']['protocol'] = rescue_proto
                brkt_config['rescue']['url'] = rescue_url
            if instance_config:
                instance_config.set_brkt_config(brkt_config)
                return instance_config.make_userdata()
            return json.dumps({'brkt': brkt_config})
        except Exception as e:
            log.exception("Failed to create user-data %s" % e)
            raise

    def send_userdata(self, vm, user_data_str):
        self.validate_connection()
        spec = vim.vm.ConfigSpec()
        option_n = vim.option.OptionValue()
        spec.extraConfig = []
        option_n.key = 'guestinfo.bracket'
        option_n.value = user_data_str
        spec.extraConfig.append(option_n)
        task = vm.ReconfigVM_Task(spec)
        self.__wait_for_task(task)

    def keep_lease_alive(self, lease):
        while(True):
            time.sleep(5)
            try:
                if self.upload_ovf_complete:
                    return
                # Choosing arbitrary percentage to keep the lease alive.
                lease.HttpNfcLeaseProgress(50)
                if (lease.state == vim.HttpNfcLease.State.done):
                    return
                if (lease.state == vim.HttpNfcLease.State.error):
                    return
                # If the lease is released, we get an exception.
                # Returning to kill the thread.
            except:
                return

    def export_to_ovf(self, vm, target_path, ovf_name=None):
        self.validate_connection()
        if (os.path.exists(target_path) is False):
            raise Exception("OVF target path does not exist")
        if (ovf_name is None):
            timestamp = datetime.datetime.utcnow().isoformat() + 'Z'
            timestamp = timestamp.replace(':', '_')
            timestamp = timestamp.replace('.', '_')
            ovf_name = "Encrypted-Guest-OVF-" + timestamp
        ovf_file_name = ovf_name + ".ovf"
        lease = vm.ExportVm()
        while (True):
            hls = lease.state
            if (hls == vim.HttpNfcLease.State.ready):
                break
            if (hls == vim.HttpNfcLease.State.error):
                log.error("Lease not obtained to create OVF. "
                          "Error %s" % lease.error)
                raise Exception("Failed to get lease to create OVF")
        lease_info = lease.info
        lease_info.leaseTimeout = 10000
        dev_urls = lease_info.deviceUrl
        ovf_files = []
        self.upload_ovf_complete = False
        try:
            for url in dev_urls:
                devid = url.key
                devurl = url.url
                if self.esx_host:
                    host_name = "https://" + self.host
                    devurl = url.url.replace("https://*", host_name)
                file_name = url.url[url.url.rfind('/') + 1:]
                target_file = os.path.join(target_path, file_name)
                keepalive_thread = Thread(target=self.keep_lease_alive,
                                          args=(lease,), name="keepalive-export")
                keepalive_thread.daemon = True
                keepalive_thread.start()
                # Disable verification as VMDK download happens directly
                # from the ESX host.
                r = requests.get(devurl, stream=True, verify=False)
                with open(target_file, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=1024):
                        if chunk:
                            f.write(chunk)
                size = os.path.getsize(target_file)
                ovf_file = vim.OvfManager.OvfFile()
                ovf_file.deviceId = devid
                ovf_file.path = file_name
                ovf_file.size = size
                ovf_files.append(ovf_file)
            desc = vim.OvfManager.CreateDescriptorParams()
            desc.ovfFiles = ovf_files
            manager = self.si.content.ovfManager
            desc_result = manager.CreateDescriptor(vm, desc)
            ovf_path = os.path.join(target_path, ovf_file_name)
            with open(ovf_path, 'w') as f:
                f.write(desc_result.ovfDescriptor)
        except Exception as e:
            log.error("Exception while creating OVF %s" % e)
            raise
        finally:
            self.upload_ovf_complete = True
            lease.HttpNfcLeaseComplete()
        return ovf_path

    def convert_ovf_to_ova(self, ovftool_path, ovf_path):
        ova_list = list(ovf_path)
        ova_list[len(ova_list)-1] = 'a'
        ova_path = "".join(ova_list)
        ovftool_cmd = ovftool_path + " " + ovf_path + " " + ova_path
        os.system(ovftool_cmd)
        try:
            os.remove(ovf_path)
        except Exception as e:
            log.warn("Cannot delete OVF file %s (error %s). "
                     "Please delete it manually."
                     % (ovf_path, e))
        return ova_path

    def convert_ova_to_ovf(self, ovftool_path, ova_path):
        ovf_list = list(ova_path)
        ovf_list[len(ovf_list)-1] = 'f'
        ovf_path = "".join(ovf_list)
        ovftool_cmd = ovftool_path + " " + ova_path + " " + ovf_path
        os.system(ovftool_cmd)
        return ovf_path

    def get_ovf_descriptor(self, ovf_path):
        if os.path.exists(ovf_path):
            with open(ovf_path, 'r') as f:
                ovfd = f.read()
                f.close()
                return ovfd
        return None

    def upload_ovf_to_vcenter(self, target_path, ovf_name,
                              vm_name=None, validate_mf=True):
        self.validate_connection()
        vm = None
        content = self.si.RetrieveContent()
        manager = self.si.content.ovfManager
        ovf_path = os.path.join(target_path, ovf_name)
        if validate_mf:
            # Load checksums for each file
            mf_checksum = None
            if ovf_name.endswith('.ovf'):
                mf_file_name = ovf_name[:ovf_name.find(".ovf")] + "-brkt.mf"
            else:
                mf_file_name = ovf_name + '-brkt.mf'
            mf_path = os.path.join(target_path, mf_file_name)
            # Deprecate this code over time
            if not os.path.exists(mf_path):
                if ovf_name.endswith('.ovf'):
                    mf_file_name = ovf_name[:ovf_name.find(".ovf")] + ".mf"
                else:
                    mf_file_name = ovf_name + '.mf'
                mf_path = os.path.join(target_path, mf_file_name)
            # end deprecate code
            with open(mf_path, 'r') as mf_file:
                mf_checksum = json.load(mf_file)
            # Validate ovf file
            ovf_checksum = mf_checksum[(os.path.split(ovf_path))[1]]
            if ovf_checksum != compute_sha1_of_file(ovf_path):
                raise ValidationError("OVF file checksum does not match. "
                                      "Validate the Metavisor OVF image.")
        # Load the OVF file
        spec_params = vim.OvfManager.CreateImportSpecParams()
        ovfd = self.get_ovf_descriptor(ovf_path)
        e = xml.etree.ElementTree.fromstring(ovfd)
        for child in e:
            if "VirtualSystem" in child.tag:
                for child_2 in child:
                    if self.is_esx_host():
                        # Remove property section
                        if "ProductSection" in child_2.tag:
                            child.remove(child_2)
                    # Remove the network interface
                    if "VirtualHardwareSection" in child_2.tag:
                        for child_3 in child_2:
                            found = False
                            if "Item" in child_3.tag:
                                for child_4 in child_3:
                                    if "ElementName" in child_4.tag:
                                        if "Network adapter" in child_4.text:
                                            found = True
                            if found:
                                child_2.remove(child_3)
        ovfd = xml.etree.ElementTree.tostring(e)
        datacenter = self.__get_obj(content, [vim.Datacenter],
                                    self.datacenter_name)
        datastore = self.__get_obj(content, [vim.Datastore],
                                   self.datastore_name)
        cluster = self.__get_obj(content, [vim.ComputeResource],
                                 self.cluster_name)
        resource_pool = cluster.resourcePool
        destfolder = datacenter.vmFolder
        import_spec = manager.CreateImportSpec(ovfd,
                                               resource_pool,
                                               datastore,
                                               spec_params)
        timestamp = datetime.datetime.utcnow().isoformat() + 'Z'
        if vm_name is None:
            vm_name = "Encryptor-VM-" + timestamp
        if import_spec.importSpec is None or \
           import_spec.importSpec.configSpec is None:
           log.error("Import specification error %s warning %s",
                      import_spec.error, import_spec.warning)
           raise Exception("Cannot import OVF specification")
        import_spec.importSpec.configSpec.name = vm_name
        n_intf = vim.vm.device.VirtualVmxnet3()
        n_intf.key = -1
        n_intf.backing = vim.vm.device.VirtualEthernetCard.NetworkBackingInfo()
        if (self.nic_type == "DistributedVirtualPort"):
            pg_obj = self.__get_obj(content,
                                    [vim.dvs.DistributedVirtualPort],
                                    self.network_name)
            port_connection = vim.dvs.PortConnection()
            port_connection.portKey = pg_obj.key
            port_connection.switchUuid = pg_obj.dvsUuid
            n_intf.backing = \
                vim.vm.device.VirtualEthernetCard.DistributedVirtualPortBackingInfo()
            n_intf.backing.port = port_connection
        elif (self.nic_type == "DistributedVirtualPortGroup"):
            pg_obj = self.__get_obj(content,
                                    [vim.dvs.DistributedVirtualPortgroup],
                                    self.network_name)
            port_connection = vim.dvs.PortConnection()
            port_connection.portgroupKey = pg_obj.key
            port_connection.switchUuid = pg_obj.config.distributedVirtualSwitch.uuid
            n_intf.backing = \
                vim.vm.device.VirtualEthernetCard.DistributedVirtualPortBackingInfo()
            n_intf.backing.port = port_connection
        else:
            n_intf.backing.deviceName = self.network_name
        nw_spec = vim.vm.device.VirtualDeviceSpec()
        nw_spec.operation = vim.vm.device.VirtualDeviceSpec.Operation.add
        nw_spec.device = n_intf
        import_spec.importSpec.configSpec.deviceChange.append(nw_spec)
        lease = resource_pool.ImportVApp(import_spec.importSpec, destfolder)
        self.upload_ovf_complete = False
        while (True):
            hls = lease.state
            if (hls == vim.HttpNfcLease.State.ready):
                break
            if (hls == vim.HttpNfcLease.State.error):
                log.error("Lease not obtained to upload OVF. "
                          "Error %s" % lease.error)
                vm = self.__get_obj(content, [vim.VirtualMachine], vm_name)
                if vm:
                    self.destroy_vm(vm)
                raise Exception("Failed to get lease to upload OVF")
        keepalive_thread = Thread(target=self.keep_lease_alive, args=(lease,),
                                  name="keepalive-upload")
        keepalive_thread.daemon = True
        keepalive_thread.start()
        try:
            count = 0
            for device_url in lease.info.deviceUrl:
                d_file_name = (os.path.split(import_spec.fileItem[count].path))[1]
                file_path = os.path.join(target_path,
                                         import_spec.fileItem[count].path)
                if os.path.exists(file_path) is False:
                    # lets try getting the fine-name from url
                    file_name = device_url.url[device_url.url.rfind('/') + 1:]
                    file_path = os.path.join(target_path, file_name)
                if os.path.exists(file_path) is False:
                    log.error("Cannot find disk %s" % (device_url.url))
                    vm = self.__get_obj(content, [vim.VirtualMachine], vm_name)
                    if vm:
                        lease.HttpNfcLeaseComplete()
                        self.destroy_vm(vm)
                    raise Exception("Failed to find VMDKs for the Metavisor OVF")
                if validate_mf:
                    # Validate the checksum of the file
                    file_checksum = mf_checksum[d_file_name]
                    if file_checksum != compute_sha1_of_file(file_path):
                        raise ValidationError("Disk file %s checksum does not match. "
                                              "Validate the Metavisor OVF image."
                                              % d_file_name)
                count = count + 1
                dev_url = device_url.url
                if self.esx_host:
                    host_name = "https://" + self.host
                    dev_url = device_url.url.replace("https://*", host_name)
                headers = {"Content-Type" : "application/x-vnd.vmware-streamVmdk",
                           "Connection" : "Keep-Alive"}
                with open(file_path, 'rb') as f:
                    # Disable verification as VMDK upload happens directly
                    # to the ESX host.
                    retry(requests.post,
                          on=requests.exceptions.ConnectionError)(
                          dev_url, data=f, verify=False, headers=headers)
            vm = self.__get_obj(content, [vim.VirtualMachine], vm_name)
        except Exception as e:
            log.error("Exception while uploading OVF %s" % e)
            vm = self.__get_obj(content, [vim.VirtualMachine], vm_name)
            if vm:
                lease.HttpNfcLeaseComplete()
                self.destroy_vm(vm)
            raise
        finally:
            self.upload_ovf_complete = True
            lease.HttpNfcLeaseComplete()
        return vm

    def get_vm_name(self, vm):
        return vm.config.name

    def get_disk_name(self, disk):
        return disk.backing.fileName

def initialize_vcenter(host, user, password, port,
                       datacenter_name, datastore_name, esx_host,
                       cluster_name, no_of_cpus, memory_gb, session_id,
                       network_name, nic_type, verify=True, cdrom=False):
    vc_swc = VCenterService(host, user, password, port,
                            datacenter_name, datastore_name, esx_host,
                            cluster_name, no_of_cpus, memory_gb, session_id,
                            network_name, nic_type, verify, cdrom)
    vc_swc.connect()
    return vc_swc


def need_to_download_from_s3(file_list):
    """ Checks if the necessary files are already downloaded

        Returns: True or False to indicate whether the download
        is required or not
    """
    fetch_s3_objects = True
    vmdk_sha = ovf_sha = manifest = False
    for file_name in file_list:
        if not os.path.exists(file_name):
            break
        if '.mf' in file_name:
            with open(file_name) as manifest_data:
                manifest = json.load(manifest_data)
        if '.vmdk' in file_name:
            with open(file_name) as f:
                data = f.read()
                f.close()
            vmdk_sha = hashlib.sha1(data).hexdigest()
        if '.ovf' in file_name:
            with open(file_name) as f:
                data = f.read()
                f.close()
            ovf_sha = hashlib.sha1(data).hexdigest()
    if vmdk_sha and ovf_sha and manifest:
        if vmdk_sha in manifest.values() and ovf_sha in manifest.values():
            fetch_s3_objects = False

    return fetch_s3_objects


def download_ovf_from_s3(bucket_name, version=None, proxy=None):
    log.info("Fetching Metavisor OVF from S3")
    if bucket_name is None:
        log.error("Bucket-name is unknown, cannot get metavisor OVF")
        raise Exception("Invalid bucket-name")

    try:
        _environ = dict(os.environ)

        if proxy:
            #TODO(workaround) https://github.com/boto/boto3/issues/338
            os.environ["HTTP_PROXY"] = "http://%s:%d" % (proxy.host, proxy.port)
            os.environ["HTTPS_PROXY"] = "https://%s:%d" % (proxy.host, proxy.port)

        s3 = boto3.resource('s3')

        if not (set(['AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY']) <= set(os.environ)):
            s3.meta.client.meta.events.register('choose-signer.s3.*', disable_signing)

        mv = mv_version.get_version(version=version,
                                    bucket=bucket_name)

        bucket = s3.Bucket(bucket_name)
        prefix = mv + '/'
        blist = list(bucket.objects.filter(Prefix=prefix))

        ovfs = [ o for o in blist if o.key.endswith('.ovf') ]
        if ovfs:
            ovf_key = sorted(ovfs, key=attrgetter('last_modified'))[-1].key
            ovf_name = ovf_key[ovf_key.rfind('/')+1:]
            ovf_prefix = ovf_key[:ovf_key.rfind('/')+1]
            ovf_filenames = [ o.key[o.key.rfind('/')+1:]
                              for o in blist if o.key.startswith(ovf_prefix) ]
            if need_to_download_from_s3(ovf_filenames):
                for ovf_file in ovf_filenames:
                    bucket.download_file(os.path.join(ovf_prefix, ovf_file),
                                         os.path.join('./', ovf_file))
                log.info("Found OVF image: %s", ovf_name)
            else:
                log.info("Using previously downloaded OVF image: %s", ovf_name)
        else:
            log.error("No metavisor ovfs found in bucket %s", bucket_name)
            ovf_name = ovf_filenames = None

        os.environ.clear()
        os.environ.update(_environ)

        return (ovf_name, ovf_filenames)
    except Exception as e:
        log.exception("Exception downloading OVF from S3 %s" % e)
        raise


def launch_mv_vm_from_s3(vc_swc, ovf_name, download_file_list,
                         vm_name=None, cleanup=True):
    # Launch OVF
    log.info("Launching VM from OVF %s", ovf_name)
    vm = vc_swc.upload_ovf_to_vcenter("./", ovf_name, vm_name)
    # Clean up the downloaded files
    if cleanup:
        for file_name in download_file_list:
            os.remove(file_name)
    else:
        log.info("Keeping the downloaded OVF files")
    return vm


def validate_local_mv_ovf(source_image_path, ovf_image_name):
    if not os.path.exists(os.path.join(source_image_path, ovf_image_name)):
        if ".ovf.ovf" in ovf_image_name:
            log.info("Metavisor ovf image name should not "
                     "include .ovf extension")
        raise ValidationError("Metavisor OVF image file not found")
