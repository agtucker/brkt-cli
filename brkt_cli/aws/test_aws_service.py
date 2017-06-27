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
import ssl
import unittest
import uuid

from boto.exception import EC2ResponseError

import brkt_cli
import brkt_cli.aws
import brkt_cli.util
from brkt_cli.aws import aws_service
from brkt_cli.aws.model import (
    BlockDeviceType,
    BlockDeviceMapping,
    ConsoleOutput,
    Image,
    Instance,
    KeyPair,
    RegionInfo,
    SecurityGroup,
    Snapshot,
    Volume,
    VPC
)
from brkt_cli.validation import ValidationError

CONSOLE_OUTPUT_TEXT = 'Starting up.\nAll systems go!\n'


def new_id():
    return uuid.uuid4().hex[:6]


class TestException(Exception):
    pass


class RunInstanceArgs(object):
    def __init__(self):
        self.image_id = None
        self.instance_type = None
        self.ebs_optimized = None
        self.security_group_ids = None
        self.subnet_id = None
        self.user_data = None
        self.instance = None
        self.instance_profile_name = None


class DummyAWSService(aws_service.BaseAWSService):

    def __init__(self):
        super(DummyAWSService, self).__init__(new_id())
        self.instances = {}
        self.volumes = {}
        self.snapshots = {}
        self.transition_to_running = {}
        self.transition_to_completed = {}
        self.images = {}
        self.console_output_text = CONSOLE_OUTPUT_TEXT
        self.tagged_volumes = []
        self.subnets = {}
        self.security_groups = {}
        self.region = 'us-west-2'
        self.regions = [
            RegionInfo(name='us-west-2'),
            RegionInfo(name='eu-west-1')
        ]
        self.volumes = {}

        vpc = VPC()
        vpc.id = 'vpc-' + new_id()
        vpc.is_default = True
        self.default_vpc = vpc

        # Callbacks.
        self.run_instance_callback = None
        self.create_security_group_callback = None
        self.get_instance_callback = None
        self.get_volume_callback = None
        self.terminate_instance_callback = None
        self.create_snapshot_callback = None
        self.get_snapshot_callback = None
        self.delete_snapshot_callback = None
        self.stop_instance_callback = None
        self.create_tags_callback = None
        self.terminate_instance_callback = None
        self.delete_security_group_callback = None

    def get_regions(self):
        return self.regions

    def connect(self, region, key_name=None):
        self.region = region

    def run_instance(self,
                     image_id,
                     security_group_ids=None,
                     instance_type='c4.xlarge',
                     placement=None,
                     block_device_map=None,
                     subnet_id=None,
                     user_data=None,
                     ebs_optimized=True,
                     instance_profile_name=None):
        instance = Instance()
        instance.id = new_id()
        instance.image_id = image_id
        instance.root_device_name = '/dev/sda1'
        instance._state.code = 0
        instance._state.name = 'pending'

        # Create volumes based on block device data from the image.
        image = self.get_image(image_id)
        instance_bdm = BlockDeviceMapping()
        for device_name, bdm in image.block_device_mapping.iteritems():
            # Create a new volume and attach it to the instance.
            volume = Volume()
            volume.size = 8
            volume.id = new_id()
            self.volumes[volume.id] = volume

            bdt = BlockDeviceType(volume_id=volume.id, size=8)
            instance_bdm[device_name] = bdt

        instance.block_device_mapping = instance_bdm
        self.instances[instance.id] = instance

        if self.run_instance_callback:
            args = RunInstanceArgs()
            args.image_id = image_id
            args.instance_type = instance_type
            args.ebs_optimized = ebs_optimized
            args.security_group_ids = security_group_ids
            args.subnet_id = subnet_id
            args.user_data = user_data
            args.instance = instance
            args.instance_profile_name = instance_profile_name
            self.run_instance_callback(args)

        return instance

    def get_instance(self, instance_id):
        instance = self.instances[instance_id]
        if self.get_instance_callback:
            self.get_instance_callback(instance)

        # Transition from pending to running on subsequent calls.
        if instance.state == 'pending':
            if self.transition_to_running.get(instance_id):
                # We returned pending last time.  Transition to running.
                instance._state.code = 16
                instance._state.name = 'running'
                del(self.transition_to_running[instance_id])
            else:
                # Transition to running next time.
                self.transition_to_running[instance_id] = True
        return instance

    def create_tags(self, resource_id, name=None, description=None):
        if self.create_tags_callback:
            self.create_tags_callback(resource_id, name, description)
        pass

    def stop_instance(self, instance_id):
        instance = self.instances[instance_id]
        if self.stop_instance_callback:
            self.stop_instance_callback(instance)

        instance._state.code = 80
        instance._state.name = 'stopped'
        return instance

    def terminate_instance(self, instance_id):
        if self.terminate_instance_callback:
            self.terminate_instance_callback(instance_id)

        instance = self.instances[instance_id]
        if self.terminate_instance_callback:
            self.terminate_instance_callback(instance)

        instance._state.code = 48
        instance._state.name = 'terminated'
        return instance

    def get_volume(self, volume_id):
        volume = self.volumes[volume_id]
        if self.get_volume_callback:
            self.get_volume_callback(volume)
        return volume

    def get_volumes(self, tag_key=None, tag_value=None):
        if tag_key and tag_value:
            return self.tagged_volumes
        else:
            return []

    def get_snapshots(self, *snapshot_ids):
        return [self.get_snapshot(id) for id in snapshot_ids]

    def get_snapshot(self, snapshot_id):
        snapshot = self.snapshots[snapshot_id]

        # Transition from pending to completed on subsequent calls.
        if snapshot.status == 'pending':
            if self.transition_to_completed.get(snapshot_id):
                # We returned pending last time.  Transition to completed.
                snapshot.status = 'completed'
                del(self.transition_to_completed[snapshot_id])
            else:
                # Transition to completed next time.
                self.transition_to_completed[snapshot_id] = True

        if self.get_snapshot_callback:
            self.get_snapshot_callback(snapshot)

        return snapshot

    def create_snapshot(self, volume_id, name=None, description=None):
        snapshot = Snapshot()
        snapshot.id = new_id()
        snapshot.status = 'pending'
        self.snapshots[snapshot.id] = snapshot

        if self.create_snapshot_callback:
            self.create_snapshot_callback(volume_id, snapshot)

        return snapshot

    def attach_volume(self, vol_id, instance_id, device):
        instance = self.get_instance(instance_id)
        bdt = BlockDeviceType(volume_id=vol_id, size=8)
        instance.block_device_mapping[device] = bdt
        return True

    def create_image(self, instance_id, name, **kwargs):
        image = Image()
        image.id = instance_id
        image.block_device_mapping = kwargs['block_device_mapping']
        image.state = 'available'
        image.name = name
        image.description = 'This is a test'
        image.virtualization_type = 'hvm'
        image.root_device_name = '/dev/sda1'
        i = self.get_instance(instance_id)
        rdn = image.root_device_name
        # create_image creates this implicitly
        image.block_device_mapping[rdn] = i.block_device_mapping[rdn]
        self.images[image.id] = image
        return image.id

    def create_volume(self, size, zone, **kwargs):
        volume = Volume()
        volume.id = 'vol-' + new_id()
        volume.size = size
        volume.zone = zone
        volume.status = 'available'
        self.volumes[volume.id] = volume
        return volume

    def detach_volume(self, vol_id, **kwargs):
        self.volumes[vol_id].status = 'available'
        return True

    def delete_volume(self, volume_id):
        del(self.volumes[volume_id])

    def register_image(self,
                       block_device_map,
                       name=None,
                       description=None):
        image = Image()
        image.id = 'ami-' + new_id()
        image.block_device_mapping = block_device_map
        image.state = 'available'
        image.name = name
        image.description = description
        image.virtualization_type = 'hvm'
        image.root_device_name = '/dev/sda1'
        image.root_device_type = 'ebs'
        image.hypervisor = 'xen'
        self.images[image.id] = image
        return image.id

    def wait_for_image(self, image_id):
        pass

    def get_image(self, image_id, retry=False):
        image = self.images.get(image_id)
        if image:
            return image
        else:
            e = EC2ResponseError(None, None)
            e.error_code = 'InvalidAMIID.NotFound'
            raise e

    def get_images(self, filters=None, owners=None):
        # Only filtering by name is currently supported.
        name = filters.get('name', None)
        images = []
        if name:
            for i in self.images.values():
                if i.name == name:
                    images.append(i)
        return images

    def delete_snapshot(self, snapshot_id):
        del(self.snapshots[snapshot_id])
        if self.delete_snapshot_callback:
            self.delete_snapshot_callback(snapshot_id)

    def create_security_group(self, name, description, vpc_id=None):
        if self.create_security_group_callback:
            self.create_security_group_callback(vpc_id)
        sg = SecurityGroup()
        sg.id = 'sg-%s' % new_id()
        sg.vpc_id = vpc_id or self.default_vpc.id
        self.security_groups[sg.id] = sg
        return sg

    def get_security_group(self, sg_id, retry=False):
        return self.security_groups[sg_id]

    def add_security_group_rule(self, sg_id, **kwargs):
        pass

    def delete_security_group(self, sg_id):
        if self.delete_security_group_callback:
            self.delete_security_group_callback(sg_id)
        pass

    def get_key_pair(self, keyname):
        kp = KeyPair()
        kp.name = keyname
        return kp

    def get_console_output(self, instance_id):
        console_output = ConsoleOutput()
        console_output.output = self.console_output_text
        return console_output

    def get_subnet(self, subnet_id):
        return self.subnets[subnet_id]

    def get_default_vpc(self):
        return self.default_vpc

    def get_instance_attribute(self, instance_id, attribute, dry_run=False):
        if attribute == 'sriovNetSupport':
            return dict()
        return None

    def iam_role_exists(self, role):
        if role == 'brkt-objectstore':
            return True
        else:
            return False

    def modify_instance_attribute(self, instance_id, attribute, value, dry_run=False):
        if attribute == 'sriovNetSupport':
            return dict()
        return None

    def retry(self, function, error_code_regexp=None, timeout=None):
        return aws_service.retry_boto(
            function,
            error_code_regexp=error_code_regexp
        )


def build_aws_service():
    aws_svc = DummyAWSService()

    # Encryptor image
    bdm = BlockDeviceMapping()
    bdm['/dev/sda1'] = BlockDeviceType()
    bdm['/dev/sdg'] = BlockDeviceType()
    id = aws_svc.register_image(name='metavisor-0-0-1234', block_device_map=bdm)
    encryptor_image = aws_svc.get_image(id)

    # Guest image
    bdm = BlockDeviceMapping()
    bdm['/dev/sda1'] = BlockDeviceType(snapshot_id='snap-12345678')
    snapshot = Snapshot()
    snapshot.id = 'snap-12345678'
    aws_svc.snapshots[snapshot.id] = snapshot
    id = aws_svc.register_image(name='Guest image', block_device_map=bdm)
    guest_image = aws_svc.get_image(id)

    return aws_svc, encryptor_image, guest_image


class TestRetryBoto(unittest.TestCase):

    def setUp(self):
        self.num_calls = 0
        brkt_cli.util.SLEEP_ENABLED = False

    def _fail_for_n_calls(self, n, status=400):
        """ Raise EC2ResponseError the first n times that the method is
        called.
        """
        self.num_calls += 1
        if self.num_calls <= n:
            e = EC2ResponseError(status, None)
            e.error_code = 'InvalidInstanceID.NotFound'
            raise e

    def test_five_failures(self):
        """ Test that we handle failing 5 times and succeeding the 6th
        time.
        """
        function = aws_service.retry_boto(
            self._fail_for_n_calls,
            r'InvalidInstanceID\.NotFound',
            initial_sleep_seconds=0.0
        )
        function(5)

    def test_regexp_does_not_match(self):
        """ Test that we raise the underlying exception when the error code
        does not match.
        """
        function = aws_service.retry_boto(
            self._fail_for_n_calls,
            r'InvalidVolumeID.\NotFound',
            initial_sleep_seconds=0.0
        )
        with self.assertRaises(EC2ResponseError):
            function(1)

    def test_no_regexp(self):
        """ Test that we raise the underlying exception when the error code
        regexp is not specified.
        """
        function = aws_service.retry_boto(self._fail_for_n_calls)
        with self.assertRaises(EC2ResponseError):
            function(1)

    def test_503(self):
        """ Test that we retry when AWS returns a 503 status.
        """
        function = aws_service.retry_boto(
            self._fail_for_n_calls, initial_sleep_seconds=0.0)
        function(5, status=503)

    def test_ssl_error(self):
        """ Test that we retry on ssl.SSLError.  This is a case that was
        seen in the field.
        """

        def raise_ssl_error():
            self.num_calls += 1
            if self.num_calls <= 5:
                raise ssl.SSLError('Test')

        aws_service.retry_boto(raise_ssl_error, initial_sleep_seconds=0.0)()


class TestInstance(unittest.TestCase):

    def setUp(self):
        brkt_cli.util.SLEEP_ENABLED = False

    def test_wait_for_instance_terminated(self):
        """ Test waiting for an instance to terminate.
        """
        aws_svc, encryptor_image, guest_image = build_aws_service()
        instance = aws_svc.run_instance(guest_image.id)
        aws_svc.terminate_instance(instance.id)
        result = aws_service.wait_for_instance(
            aws_svc, instance.id, state='terminated', timeout=100)
        self.assertEquals(instance, result)

    def test_instance_error_state(self):
        """ Test that we raise an exception when an instance goes into
            an error state while we're waiting for it.
        """
        aws_svc, encryptor_image, guest_image = build_aws_service()
        instance = aws_svc.run_instance(guest_image.id)
        instance._state.name = 'error'
        try:
            aws_service.wait_for_instance(aws_svc, instance.id, timeout=100)
        except aws_service.InstanceError as e:
            self.assertTrue('error state' in e.message)

    def test_wait_for_instance_unexpectedly_terminated(self):
        """ Test that we handle the edge case when an instance is
            terminated on startup.
        """
        aws_svc, encryptor_image, guest_image = build_aws_service()
        instance = aws_svc.run_instance(guest_image.id)
        aws_svc.terminate_instance(instance.id)
        try:
            aws_service.wait_for_instance(
                aws_svc, instance.id, state='running', timeout=100)
        except aws_service.InstanceError as e:
            self.assertTrue('unexpectedly terminated' in e.message)


class TestCustomTags(unittest.TestCase):

    def test_tag_validation(self):
        # Key
        key = 'x' * 127
        self.assertEquals(key, aws_service.validate_tag_key(key))
        with self.assertRaises(ValidationError):
            aws_service.validate_tag_key(key + 'x')
        with self.assertRaises(ValidationError):
            aws_service.validate_tag_key('aws:foobar')

        # Value
        value = 'x' * 255
        self.assertEquals(value, aws_service.validate_tag_value(value))
        with self.assertRaises(ValidationError):
            aws_service.validate_tag_value(value + 'x')
        with self.assertRaises(ValidationError):
            aws_service.validate_tag_value('aws:foobar')


class TestVolume(unittest.TestCase):

    def setUp(self):
        brkt_cli.util.SLEEP_ENABLED = False
        self.num_calls = 0

    def test_wait_for_volume(self):
        aws_svc, encryptor_image, guest_image = build_aws_service()

        # Create a dummy volume.
        volume = Volume()
        volume.size = 8
        volume.id = new_id()
        volume.status = 'detaching'
        aws_svc.volumes[volume.id] = volume

        def transition_to_available(callback_volume):
            self.num_calls += 1
            self.assertEqual(volume, callback_volume)
            self.assertFalse(self.num_calls > 5)

            if self.num_calls == 5:
                volume.status = 'available'

        aws_svc.get_volume_callback = transition_to_available
        result = aws_service.wait_for_volume(aws_svc, volume.id)
        self.assertEqual(volume, result)


class TestSnapshotProgress(unittest.TestCase):

    def test_snapshot_progress_text(self):
        # One snapshot.
        s1 = Snapshot()
        s1.id = '1'
        s1.progress = u'25%'
        self.assertEqual(
            '1: 25%',
            aws_service._get_snapshot_progress_text([s1])
        )

        # Two snapshots.
        s2 = Snapshot()
        s2.id = '2'
        s2.progress = u'50%'

        self.assertEqual(
            '1: 25%, 2: 50%',
            aws_service._get_snapshot_progress_text([s1, s2])
        )
