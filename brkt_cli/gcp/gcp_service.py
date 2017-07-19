#!/usr/bin/env python

import abc
import datetime
import json
import re
import socket
import tempfile
import time
import uuid

import brkt_cli.util
from brkt_cli.util import (
    append_suffix,
    BracketError,
    make_nonce
)
from googleapiclient import discovery, errors
from oauth2client.client import GoogleCredentials

from brkt_cli.validation import ValidationError


GCP_NAME_MAX_LENGTH = 63
LATEST_IMAGE = 'latest.image.tar.gz'


brkt_image_buckets = {
    'prod': 'brkt-prod-images',
    'stage': 'brkt-stage-images',
    'shared': 'brkt-shared-images'
}


def retry(function, timeout=15.0):
    return brkt_cli.util.retry(
        function, on=[socket.error, errors.HttpError], timeout=timeout)


def execute_gcp_api_call(gcp_object):
    return gcp_object.execute()


class InstanceError(BracketError):
    pass


class BaseGCPService(object):
    __metaclass__ = abc.ABCMeta

    def __init__(self, project, session_id, logger):
        self.log = logger
        self.project = project
        self.session_id = session_id
        self.gcp_res_uri = "https://www.googleapis.com/compute/v1/"
        self.disks = []
        self.instances = []

    @abc.abstractmethod
    def list_zones(self):
        pass

    @abc.abstractmethod
    def get_session_id(self):
        pass

    @abc.abstractmethod
    def disk_from_image(self, zone, image, name, image_project):
        pass

    @abc.abstractmethod
    def get_snapshot(self, name):
        pass

    @abc.abstractmethod
    def wait_snapshot(self, snapshot):
        pass

    @abc.abstractmethod
    def get_image(self, image):
        pass

    @abc.abstractmethod
    def get_network(self, nw):
        pass

    @abc.abstractmethod
    def image_exists(self, image):
        pass

    @abc.abstractmethod
    def project_exists(self, project):
        pass

    @abc.abstractmethod
    def delete_instance(self, zone, instance):
        pass

    @abc.abstractmethod
    def delete_image(self, image):
        pass

    @abc.abstractmethod
    def delete_disk(self, zone, disk):
        pass

    @abc.abstractmethod
    def wait_instance(self, name, zone):
        pass

    @abc.abstractmethod
    def get_instance_ip(self, name, zone):
        pass

    @abc.abstractmethod
    def get_private_ip(self, name, zone):
        pass

    @abc.abstractmethod
    def detach_disk(self, zone, instance, diskName):
        pass

    @abc.abstractmethod
    def wait_for_disk(self, zone, diskName):
        pass

    @abc.abstractmethod
    def get_disk_size(self, zone, diskName):
        pass

    @abc.abstractmethod
    def wait_for_detach(self, zone, diskName):
        pass

    @abc.abstractmethod
    def disk_exists(self, zone, name):
        pass

    @abc.abstractmethod
    def create_snapshot(self, zone, disk, snapshot_name):
        pass

    @abc.abstractmethod
    def delete_snapshot(self, snapshot_name):
        pass

    @abc.abstractmethod
    def disk_from_snapshot(self, zone, snapshot, name):
        pass

    @abc.abstractmethod
    def create_disk(self, zone, name, size):
        pass

    @abc.abstractmethod
    def create_gcp_image_from_disk(self, zone, image_name, disk_name):
        pass

    @abc.abstractmethod
    def create_gcp_image_from_file(self, zone, image_name, file_name, bucket):
        pass

    @abc.abstractmethod
    def wait_image(self, image_name):
        pass

    @abc.abstractmethod
    def get_younger(self, new, old):
        pass

    @abc.abstractmethod
    def get_image_file(self, bucket):
        pass

    @abc.abstractmethod
    def set_tags(self, zone, instance, tags):
        pass

    @abc.abstractmethod
    def get_tags_fingerprint(self, name, zone):
        pass

    @abc.abstractmethod
    def get_latest_encryptor_image(self,
                                   zone,
                                   image_name,
                                   bucket,
                                   image_file=None):
        pass

    @abc.abstractmethod
    def run_instance(self,
                     zone,
                     name,
                     image,
                     network,
                     disks,
                     metadata,
                     delete_boot,
                     block_project_ssh_keys,
                     instance_type,
                     image_project,
                     subnet=None,
                     tags=None):
        pass

    @abc.abstractmethod
    def get_disk(self, zone, disk_name):
        pass

    @abc.abstractmethod
    def cleanup(self, zone, encryptor_image, keep_encryptor=False):
        pass


class GCPService(BaseGCPService):
    def __init__(self, project, session_id, logger):
        super(GCPService, self).__init__(project, session_id, logger)
        self.credentials = GoogleCredentials.get_application_default()
        self.compute = discovery.build(
            'compute', 'v1', credentials=self.credentials)

        self.storage = discovery.build(
            'storage', 'v1', credentials=self.credentials)


    # Create bucket/check permissions 
    def check_bucket_name(self, bucket, project):
        try:
            body = {'name' : bucket}
            self.storage.buckets().insert(
                project=project, body=body).execute()

            return 
        except errors.HttpError as e:
            # Parse error message and return if user owns bucket
            # Error codes are not unique so message is used
            message = json.loads(e.content)["error"]["message"]
            if 'You already own this bucket' in message:
                return
            else:
                self.log.error("Bucket must be in project: %s", project)
                raise


    # Check if the file name uses valid syntax
    def validate_file_name(self, file):
        m = re.search(r'[*\[\]?#]', file)
        if m:
            raise ValidationError("File name can't contain '[',']','*','?' or '#'")
        return

    def check_bucket_file(self, bucket, file):
        try:
            existing_file = self.storage.objects().get(
                    bucket=bucket,
                    object=file).execute()
            if existing_file:
                raise ValidationError("File already exists")
        except errors.HttpError as e:
            code = e.resp.status
            if code == 404:
                return self.validate_file_name(file)
            else:
                raise ValidationError(e)

    def wait_bucket_file(self, bucket, file):
        for i in range(48):
            try:
                self.storage.objects().get(
                   bucket=bucket,
                   object=file).execute()
                return True
            except errors.HttpError as e:
                self.log.debug(e)
                code = e.resp.status
                if code == 404:
                    time.sleep(5)
                else:
                    self.log.warn("Failed uploading tar file")
                    self.log.debug("Failed uploading tar file: %s", e)
                    return False
        return False

    def get_public_image(self):
        # guest_os is a family
        self.log.info("Trying to get from image family...")
        request = self.compute.images().getFromFamily(
            project='ubuntu-os-cloud',
            family='ubuntu-1404-lts')
        public_image = request.execute(num_retries=5)
        return public_image['name']

    def cleanup(self, zone, encryptor_image, keep_encryptor=False):
        try:
            for instance in self.instances[:]:
                self.log.info('deleting instance %s' % instance)
                self.delete_instance(zone, instance)
            for disk in self.disks[:]:
                self.log.info('deleting disk %s' % disk)
                if self.disk_exists(zone, disk):
                    self.wait_for_detach(zone, disk)
                    self.delete_disk(zone, disk)
            if not keep_encryptor and encryptor_image:
                self.log.info('Deleting encryptor image %s' % encryptor_image)
                self.delete_image(encryptor_image)
        except:
            self.log.exception('Cleanup failed')

    def list_zones(self):
        zones = []
        zones_resp = self.compute.zones().list(project=self.project).execute()
        for item in zones_resp['items']:
            zones.append(item['name'])

        return zones

    def get_session_id(self):
        return self.session_id

    def get_snapshot(self, name):
        snap_req = self.compute.snapshots().get(
            project=self.project, snapshot=name)
        return retry(execute_gcp_api_call)(snap_req)

    def wait_snapshot(self, snapshot):
        while True:
            if self.get_snapshot(snapshot)['status'] == 'READY':
                return
            time.sleep(5)

    def get_network(self, nw):
        return self.compute.networks().get(project=self.project,
            network=nw).execute()

    def network_exists(self, nw):
        try:
            self.get_network(nw)
        except:
            return False
        return True

    def get_image(self, image, image_project=None):
        if image_project:
            return self.compute.images().get(project=image_project,
                image=image).execute()
        else:
            return self.compute.images().get(project=self.project,
               image=image).execute()

    def image_exists(self, image, image_project=None):
        try:
            self.get_image(image, image_project)
        except:
            return False
        return True

    def get_project(self, project):
        return self.compute.projects().get(project=project).execute()

    def project_exists(self, project=None):
        try:
            self.get_project(project)
        except errors.HttpError as e:
            code = json.loads(e.content)['error']['code']
            if code == 400 or code == 403 or code == 404:
                return False
            raise
        return True

    def delete_instance(self, zone, instance):
        if instance in self.instances:
            self.instances.remove(instance)
        return self.compute.instances().delete(project=self.project,
               zone=zone, instance=instance).execute()

    def delete_image(self, image):
        return self.compute.images().delete(project=self.project,
               image=image).execute()

    def delete_disk(self, zone, disk):
        # remove disk if we're tracking it
        if disk in self.disks:
            self.disks.remove(disk)
        return self.compute.disks().delete(project=self.project,
               zone=zone, disk=disk).execute()

    def wait_instance(self, name, zone):
        instance = self.compute.instances().list(project=self.project,
                zone=zone)
        while True:
            instance_data = retry(execute_gcp_api_call)(instance)
            if 'items' in instance_data:
                for i in instance_data['items']:
                    if name == i['name'] and i['status'] == 'RUNNING':
                        return
            self.log.info('Waiting for ' + name + ' to become ready')
            time.sleep(5)

    def get_instance_ip(self, name, zone):
        for i in range(60):
            time.sleep(5)
            instance_req = self.compute.instances().get(project=self.project,
                    zone=zone, instance=name)
            try:
                nw = 'networkInterfaces'
                instance = retry(execute_gcp_api_call)(instance_req)
                if instance[nw][0]['accessConfigs'][0]['natIP']:
                    return instance[nw][0]['accessConfigs'][0]['natIP']
            except:
                pass
        self.log.info("Couldn't find an IP address for this instance.")

    def get_private_ip(self, name, zone):
        nw = 'networkInterfaces'
        instance_req = self.compute.instances().get(project=self.project,
                zone=zone, instance=name)
        instance = retry(execute_gcp_api_call)(instance_req)
        if instance[nw][0]['networkIP']:
            return instance[nw][0]['networkIP']
        self.log.info("Couldn't find private IP address for this instance.")

    def write_serial_console_file(self, zone, instance):
        try:
            serial_port_out = self.compute.instances().getSerialPortOutput(
                    project=self.project,
                    instance=instance,
                    zone=zone).execute()
            if 'contents' in serial_port_out:
                with tempfile.NamedTemporaryFile(prefix='serial-console-',
                                                 suffix='-%s.out' % self.session_id,
                                                 delete=False) as t:
                    t.write(serial_port_out['contents'])
                return t.name
        except:
            self.log.exception('Unable to write serial console contents')

        return None


    def detach_disk(self, zone, instance, diskName):
        detach_req = self.compute.instances().detachDisk(project=self.project,
                instance=instance, zone=zone, deviceName=diskName)
        retry(execute_gcp_api_call)(detach_req)
        # wait for disk ready
        return self.wait_for_detach(zone, diskName)

    def wait_for_disk(self, zone, diskName):
        disk_req = self.compute.disks().get(zone=zone,
                                            project=self.project,
                                            disk=diskName)
        while True:
            self.log.info("Waiting for disk to become ready")
            if 'READY' == retry(execute_gcp_api_call)(disk_req)['status']:
                return
            time.sleep(10)

    def get_disk_size(self, zone, diskName):
        disk_info = self.compute.disks().get(zone=zone, project=self.project, disk=diskName).execute()
        return int(disk_info['sizeGb'])

    def wait_for_detach(self, zone, diskName):
        detach_req = self.compute.disks().get(zone=zone,
                                              project=self.project,
                                              disk=diskName)
        while True:
            resp = retry(execute_gcp_api_call)(detach_req)
            if "users" not in resp and resp != {}:
                self.log.info("Disk detach successful")
                return
            time.sleep(10)

    def disk_exists(self, zone, name):
        try:
            self.compute.disks().get(zone=zone, project=self.project,
                    disk=name).execute()
            return True
        except:
            return False

    def create_snapshot(self, zone, disk, snapshot_name):
        disk_url = "projects/%s/zones/%s/disks/%s" % (self.project, zone, disk)
        body = {'sourceDisk': disk_url, 'name': snapshot_name}
        self.compute.disks().createSnapshot(project=self.project, disk=disk, body=body, zone=zone).execute()

    def delete_snapshot(self, snapshot_name):
        self.compute.snapshots().delete(project=self.project, snapshot=snapshot_name).execute()

    def disk_from_image(self, zone, image, name, image_project=None):
        if self.disk_exists(zone, name):
            return
        project = self.project
        if image_project:
            project = image_project

        image_info = self.compute.images().get(project=project,
                                                 image=image).execute()
        base = "projects/%s/zones/%s" % (self.project, zone)
        body = {
                "name": name,
                "zone": base,
                "type": base + "/diskTypes/pd-ssd",
                "sourceImage": "projects/%s/global/images/%s" % (project, image),
                "sizeGb": image_info['diskSizeGb']
        }
        self.compute.disks().insert(project=self.project,
                zone=zone, body=body).execute()
        self.disks.append(name)

    def disk_from_snapshot(self, zone, snapshot, name):
        if self.disk_exists(zone, name):
            return
        snap_info = self.compute.snapshots().get(project=self.project,
                                                 snapshot=snapshot).execute()
        base = "projects/%s/zones/%s" % (self.project, zone)
        body = {
                "name": name,
                "zone": base,
                "type": base + "/diskTypes/pd-ssd",
                "sourceSnapshot": "projects/%s/global/snapshots/%s" % (self.project, snapshot),
                "sizeGb": snap_info['diskSizeGb']
        }
        self.compute.disks().insert(project=self.project,
                zone=zone, body=body).execute()
        self.disks.append(name)

    def create_disk(self, zone, name, size=25):
        if name not in self.disks:
            self.disks.append(name)
        if self.disk_exists(zone, name):
            return

        base = "projects/%s/zones/%s" % (self.project, zone)
        body = {
            "name": name,
            "zone": base,
            "type": base + "/diskTypes/pd-ssd",
            "sizeGb": str(size)
        }
        self.compute.disks().insert(project=self.project,
                zone=zone, body=body).execute()
        self.wait_for_disk(zone, name)

    def create_ssd_disk(self, zone):
        name = 'scratch-' + str(uuid.uuid4().hex)
        base = "projects/%s/zones/%s" % (self.project, zone)
        body = {
            "deviceName": name,
            "type": "SCRATCH",
            "autoDelete": True,
            "initializeParams": {
                "diskType": base + "/diskTypes/local-ssd"
            }
        }
        return body

    def create_gcp_image_from_disk(self, zone, image_name, disk_name):
        build_disk = "projects/%s/zones/%s/disks/%s" % (self.project,
                zone, disk_name)
        self.compute.images().insert(body={"rawdisk": {},
            "name": image_name,
            "sourceDisk": build_disk},
            project=self.project).execute()

    def create_gcp_image_from_file(self, zone, image_name, file_name, bucket):
        source = "https://storage.googleapis.com/%s/%s" % (bucket, file_name)
        self.compute.images().insert(
            body={
                "rawDisk": {
                    "source": source
                },
                "name": image_name,
            },
            project=self.project).execute()

    def wait_image(self, image_name):
        image_req = self.compute.images().get(image=image_name, project=self.project)
        while True:
            if retry(execute_gcp_api_call, timeout=30.0)(image_req)['status'] == 'READY':
                return
            time.sleep(10)

    def get_younger(self, new, old):
        new_time = datetime.datetime.strptime(new['timeCreated'],
                                              "%Y-%m-%dT%H:%M:%S.%fZ")
        old_time = datetime.datetime.strptime(old['timeCreated'],
                                              "%Y-%m-%dT%H:%M:%S.%fZ")
        if new_time > old_time:
            return new
        else:
            return old

    def get_image_file(self, bucket):
        files = []
        request = self.storage.objects().list(bucket=bucket)
        # Paginate thru' the list of files in the bucket
        while request:
            resp = request.execute()
            files += resp['items']
            request = self.storage.objects().list_next(request, resp)
        # if LATEST_IMAGE exists return that
        for f in files:
            if f['name'] == LATEST_IMAGE:
                return LATEST_IMAGE

        # else return the newest file that ends in .image.tar.gz
        youngest = {'timeCreated': '1992-10-01T01:50:02.942Z'}
        for f in files:
            if 'image.tar.gz' in f['name']:
                youngest = self.get_younger(f, youngest)

        return youngest['name']

    def get_latest_encryptor_image(self,
                                   zone,
                                   bucket,
                                   image_file=None):
        if bucket in brkt_image_buckets:
            bucket = brkt_image_buckets[bucket]

        image_name = 'encryptor-%s' % self.get_session_id()
        # if image_file is not provided try to get latest.image.tar.gz
        # if latest.image.tar.gz doesnt exist return the newest image
        if not image_file:
            image_file = self.get_image_file(bucket)
        self.create_gcp_image_from_file(zone,
                                        image_name,
                                        image_file,
                                        bucket)
        self.wait_image(image_name)
        self.encryptor_image = image_name
        return image_name

    def run_instance(self,
                     zone,
                     name,
                     image,
                     network='default',
                     disks=[],
                     metadata={},
                     delete_boot=True,
                     block_project_ssh_keys=False,
                     instance_type='n1-standard-4',
                     image_project=None,
                     subnet=None,
                     tags=None):

        if block_project_ssh_keys:
            if 'items' not in metadata:
                metadata['items'] = []
            metadata['items'].append({"key": "block-project-ssh-keys", 'value': 'true'})

        # if boot disk doesn't autodelete we need to track it
        if not delete_boot:
            self.disks.append(name)
        if image_project:
            source_disk_image = "projects/%s/global/images/%s" % (image_project,
                image)
        else:
            source_disk_image = "projects/%s/global/images/%s" % (self.project,
                image)
        machine_type = "zones/%s/machineTypes/%s" % (zone, instance_type)

        config = {
            'name': name,
            'machineType': machine_type,

            # Specify the boot disk and the image to use as a source.
            'disks': [
                {
                    'boot': True,
                    'autoDelete': delete_boot,
                    'initializeParams': {
                        'sourceImage': self.gcp_res_uri + source_disk_image,
                    },
                },
            ] + disks,

            # Specify a network interface with NAT to access the public
            # internet.
            'networkInterfaces': [{
                'network': 'global/networks/%s' % network,
                'accessConfigs': [
                    {'type': 'ONE_TO_ONE_NAT', 'name': 'External NAT'}
                ]
            }],

            # Allow the instance to access cloud storage and logging.
            'serviceAccounts': [{
                'email': 'default',
                'scopes': [
                    'https://www.googleapis.com/auth/devstorage.read_write',
                    'https://www.googleapis.com/auth/logging.write'
                ]
            }],

            # Metadata is readable from the instance and allows you to
            # pass configuration from deployment scripts to instances.
            'metadata': metadata
        }

        if subnet:
            subnetwork = "projects/%s/regions/%s/subnetworks/%s" % (
                self.project, zone[:-2], subnet)
            config['networkInterfaces'][0]['subnetwork'] = subnetwork

        instance_req = self.compute.instances().insert(
            project=self.project,
            zone=zone,
            body=config)
        retry(execute_gcp_api_call, timeout=30.0)(instance_req)
        if tags:
            self.log.info("Setting instance tags %s", tags)
            self.set_tags(zone, name, tags)
        self.wait_instance(name, zone)
        self.get_disk_size(zone, name)
        self.instances.append(name)

    def get_disk(self, zone, disk_name):
        source_disk = "projects/%s/zones/%s/disks/%s" % (self.project,
                            zone, disk_name)
        return {
            'boot': False,
            'autoDelete': False,
            'source': self.gcp_res_uri + source_disk,
        }

    def set_tags(self, zone, instance, tags):
        """
        Takes a list of tags (strings) as input, will override existing
        tags on the instance
        """
        fingerprint = self.get_tags_fingerprint(instance, zone)
        body = {"items": tags, "fingerprint": fingerprint}
        request = self.compute.instances().setTags(project=self.project,
                                                   zone=zone,
                                                   instance=instance,
                                                   body=body)
        retry(execute_gcp_api_call)(request)
        return

    def get_tags_fingerprint(self, name, zone):
        """
        Returns the fingerprint of the instance, this is used for all
        requests which modify or update metadata on the instance
        """
        request = self.compute.instances().get(project=self.project,
                                                instance=name,
                                                zone=zone)
        instance = execute_gcp_api_call(request)
        if instance['tags']['fingerprint']:
            return instance['tags']['fingerprint']

def gcp_metadata_from_userdata(brkt_data, extra_items=None):
    """ brkt_data is a JSON blob containing the brkt-config """
    items_list = [{'key': 'brkt', 'value': brkt_data}]
    if extra_items:
        items_list.extend(extra_items)
    return { 'items': items_list }


def get_image_name(encrypted_image_name, name):
    if encrypted_image_name:
        return encrypted_image_name

    nonce = make_nonce()
    # Replace nonce in image name
    m = re.match('(.+)\-encrypted\-', name)
    if m:
        encrypted_image_name = append_suffix(
                m.group(1),
                '-encrypted-%s' % (nonce,),
                GCP_NAME_MAX_LENGTH)
    else:
        encrypted_image_name = append_suffix(
                name,
                '-encrypted-%s' % (nonce,),
                GCP_NAME_MAX_LENGTH)
    return encrypted_image_name


def validate_images(gcp_svc, encrypted_image_name, encryptor, guest_image, image_project=None):
    # check that image to be updated exists
    if not gcp_svc.image_exists(guest_image, image_project):
        raise ValidationError('Guest image or image project invalid')

    # check that encryptor exists
    if encryptor and not gcp_svc.image_exists(encryptor):
        raise ValidationError('Encryptor image %s does not exist. Encryption failed.' % encryptor)

    # check that there is no existing image named encrypted_image_name
    if gcp_svc.image_exists(encrypted_image_name):
        raise ValidationError('An image already exists with name %s. Encryption Failed.' % encrypted_image_name)



def validate_image_name(name):
    """ Verify that the name is a valid GCP image name. Return the name
        if it is valid.

    : raises ValidationError if name is invalid
    """
    if not (name and len(name) <= 64):
        raise ValidationError(
            'Image or instance name may be at most 64 characters')

    m = re.match(r'[a-z0-9\-]*[a-z0-9]$', name)
    if not m:
        raise ValidationError(
            "Image or instance name must be lower case letters, numbers "
            "and hyphens and must end with a lower case letter or a number"
        )
    return name
