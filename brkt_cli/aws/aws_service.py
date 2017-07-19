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
import logging
import re
import ssl
import string
import tempfile
import time
from datetime import datetime

import boto3
from botocore.exceptions import ClientError

from brkt_cli import util, encryptor_service
from brkt_cli.aws import boto3_device, boto3_tag
from brkt_cli.aws.aws_constants import (
    NAME_ENCRYPTOR_SECURITY_GROUP,
    DESCRIPTION_ENCRYPTOR_SECURITY_GROUP, NAME_GUEST_CREATOR,
    DESCRIPTION_GUEST_CREATOR, NAME_LOG_SNAPSHOT, DESCRIPTION_LOG_SNAPSHOT,
    NAME_ORIGINAL_VOLUME, NAME_ORIGINAL_SNAPSHOT,
    DESCRIPTION_ORIGINAL_SNAPSHOT)
from brkt_cli.aws.model import RegionInfo
from brkt_cli.util import (
    Deadline, BracketError, sleep, make_nonce, pretty_print_json
)
from brkt_cli.validation import ValidationError

log = logging.getLogger(__name__)

EBS_OPTIMIZED_INSTANCES = ['c1.xlarge', 'c3.xlarge', 'c3.2xlarge',
                           'c3.4xlarge', 'c4.large', 'c4.xlarge',
                           'c4.2xlarge', 'c4.4xlarge', 'c4.8xlarge',
                           'd2.xlarge', 'd2.4xlarge', 'd2.8xlarge',
                           'g2.2xlarge', 'i2.xlarge', 'i2.2xlarge',
                           'i2.4xlarge', 'i3.16xlarge', 'm1.large',
                           'm1.xlarge', 'm1.2xlarge', 'm1.4xlarge',
                           'm2.2xlarge', 'm2.4xlarge', 'm3.xlarge',
                           'm3.2xlarge', 'm4.large', 'm4.xlarge',
                           'm4.2xlarge', 'm4.4xlarge', 'm4.10xlarge',
                           'm4.16xlarge', 'p2.xlarge', 'p2.8xlarge',
                           'p2.16xlarge', 'r3.xlarge', 'r3.2xlarge',
                           'r3.4large', 'r4.large', 'r4.xlarge',
                           'r4.2xlarge', 'r4.4xlarge', 'r4.8xlarge',
                           'r4.16xlarge', 'x1.16xlarge', 'x1.32xlarge']


class BaseAWSService(object):
    __metaclass__ = abc.ABCMeta

    def __init__(self, session_id):
        self.session_id = session_id

    @abc.abstractmethod
    def get_regions(self):
        pass

    @abc.abstractmethod
    def connect(self, region, key_name=None):
        pass

    @abc.abstractmethod
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
        pass

    @abc.abstractmethod
    def get_instance(self, instance_id):
        pass

    @abc.abstractmethod
    def create_tags(self, resource_id, name=None, description=None):
        pass

    @abc.abstractmethod
    def stop_instance(self, instance_id):
        pass

    @abc.abstractmethod
    def terminate_instance(self, instance_id):
        pass

    @abc.abstractmethod
    def get_volume(self, volume_id):
        pass

    @abc.abstractmethod
    def iam_role_exists(self, role):
        pass

    @abc.abstractmethod
    def get_volumes(self, tag_key=None, tag_value=None):
        pass

    @abc.abstractmethod
    def get_snapshots(self, *snapshot_ids):
        pass

    @abc.abstractmethod
    def get_snapshot(self, snapshot_id):
        pass

    @abc.abstractmethod
    def create_snapshot(self, volume_id, name=None, description=None):
        pass

    @abc.abstractmethod
    def create_volume(self,
                      size,
                      zone,
                      snapshot=None,
                      volume_type=None,
                      encrypted=False):
        pass

    @abc.abstractmethod
    def delete_volume(self, volume_id):
        """ Delete the given volume.
        :return: True if the volume was deleted
        """
        pass

    @abc.abstractmethod
    def get_image(self, image_id, retry=False):
        pass

    @abc.abstractmethod
    def get_images(self, name=None, owner_alias=None, product_code=None):
        pass

    @abc.abstractmethod
    def delete_snapshot(self, snapshot_id):
        pass

    @abc.abstractmethod
    def create_security_group(self, name, description, vpc_id=None):
        pass

    @abc.abstractmethod
    def get_security_group(self, sg_id, retry=False):
        pass

    @abc.abstractmethod
    def authorize_security_group_ingress(self, sg_id, port):
        pass

    @abc.abstractmethod
    def delete_security_group(self, sg_id):
        pass

    @abc.abstractmethod
    def get_key_pair(self, keyname):
        pass

    @abc.abstractmethod
    def get_console_output(self, instance_id):
        pass

    @abc.abstractmethod
    def get_subnet(self, subnet_id):
        pass

    def create_image(self,
                     instance_id,
                     name,
                     description=None,
                     no_reboot=True,
                     block_device_mapping=None):
        pass

    @abc.abstractmethod
    def detach_volume(self, vol_id, instance_id, force=True):
        pass

    @abc.abstractmethod
    def attach_volume(self, vol_id, instance_id, device):
        pass

    @abc.abstractmethod
    def get_default_vpc(self):
        pass

    @abc.abstractmethod
    def modify_instance_attribute(self, instance_id, attribute,
        value, dry_run=False):
        pass

    @abc.abstractmethod
    def retry(self, function, error_code_regexp=None, timeout=None):
        pass


class BotoRetryExceptionChecker(util.RetryExceptionChecker):

    def __init__(self, error_code_regexp=None):
        self.error_code_regexp = error_code_regexp

    def is_expected(self, exception):
        if isinstance(exception, ssl.SSLError):
            # We've seen this in the field.
            return True

        if not isinstance(exception, ClientError):
            return False

        error_code, _ = get_code_and_message(exception)
        if error_code == '503':
            # This can happen when the AWS request limit has been exceeded.
            return True

        if self.error_code_regexp:
            m = re.match(self.error_code_regexp, error_code)
            return bool(m)

        return False


def retry_boto(function, error_code_regexp=None, timeout=10.0,
               initial_sleep_seconds=0.25):
    """ Retry an AWS API call.  Handle known intermittent errors and expected
    error codes.
    """
    return util.retry(
        function,
        exception_checker=BotoRetryExceptionChecker(error_code_regexp),
        timeout=timeout,
        initial_sleep_seconds=initial_sleep_seconds
    )


class AWSService(BaseAWSService):

    def __init__(
            self,
            encryptor_session_id,
            default_tags=None,
            retry_timeout=10.0,
            retry_initial_sleep_seconds=0.25):
        super(AWSService, self).__init__(encryptor_session_id)

        self.default_tags = default_tags or {}
        self.retry_timeout = retry_timeout
        self.retry_initial_sleep_seconds = retry_initial_sleep_seconds

        # These will be initialized by connect().
        self.key_name = None
        self.region = None

        self.ec2 = None
        # Hardcode us-east-1 for the purpose of getting the list of regions.
        self.ec2client = boto3.client('ec2', region_name='us-east-1')

    def get_regions(self):
        """ Return the available regions as a list of RegionInfo. """
        regions = []
        for r in self.ec2client.describe_regions()['Regions']:
            ri = RegionInfo(name=r['RegionName'], endpoint=r['Endpoint'])
            regions.append(ri)
        return regions

    def connect(self, region, key_name=None):
        self.region = region
        self.key_name = key_name
        self.ec2 = boto3.resource('ec2', region_name=region)
        self.ec2client = boto3.client('ec2', region_name=region)

    def retry(self, function, error_code_regexp=None, timeout=None):
        """ Call the retry_boto function with this object's timeout and
        initial sleep time values.
        """
        timeout = timeout or self.retry_timeout
        return retry_boto(
            function,
            error_code_regexp,
            timeout=timeout,
            initial_sleep_seconds=self.retry_initial_sleep_seconds
        )

    def run_instance(self,
                     image_id,
                     security_group_ids=None,
                     instance_type='c4.xlarge',
                     placement=None,
                     block_device_mappings=None,
                     subnet_id=None,
                     user_data=None,
                     ebs_optimized=True,
                     instance_profile_name=None):
        try:
            kwargs = {
                'ImageId': image_id,
                'MaxCount': 1,
                'MinCount': 1
            }
            if security_group_ids:
                kwargs['SecurityGroupIds'] = security_group_ids
            if instance_type:
                kwargs['InstanceType'] = instance_type
            if placement:
                kwargs['Placement'] = placement
            if block_device_mappings:
                kwargs['BlockDeviceMappings'] = block_device_mappings
            if subnet_id:
                kwargs['SubnetId'] = subnet_id
            if user_data:
                kwargs['UserData'] = user_data
            if ebs_optimized is not None:
                kwargs['EbsOptimized'] = ebs_optimized
            if instance_profile_name:
                kwargs['IamInstanceProfile'] = instance_profile_name
            if self.key_name:
                kwargs['KeyName'] = self.key_name

            if log.isEnabledFor(logging.DEBUG):
                # User-data is long and can have binary content.
                kwargs_for_log = dict(kwargs)
                if user_data:
                    kwargs_for_log['UserData'] = '(%d bytes)' % len(user_data)
                j = pretty_print_json(kwargs_for_log)
                log.debug('Running instance: %s', j)

            run_instances = self.retry(
                self.ec2client.run_instances, )
            response = run_instances(**kwargs)
            instance_id = response['Instances'][0]['InstanceId']
            log.debug('Launched instance %s', instance_id)
            return self.get_instance(instance_id)
        except ClientError:
            log.debug('Failed to launch instance for %s', image_id)
            raise

    def get_instance(self, instance_id):
        instance = self.ec2.Instance(instance_id)
        load = self.retry(
            instance.load, r'InvalidInstanceID\.NotFound')
        load()
        return instance

    def create_tags(self, resource_id, name=None, description=None):
        d = dict(self.default_tags)
        if name:
            d['Name'] = name
        if description:
            d['Description'] = description
        log.debug(
            'Tagging %s with %s', resource_id, pretty_print_json(d))
        create_tags = self.retry(self.ec2client.create_tags, r'.*\.NotFound')
        create_tags(
            Resources=[resource_id],
            Tags=boto3_tag.dict_to_tags(d)
        )

    def stop_instance(self, instance_id):
        log.debug('Stopping instance %s', instance_id)
        stop_instances = self.retry(self.ec2client.stop_instances)
        stop_instances(InstanceIds=[instance_id])

    def terminate_instance(self, instance_id):
        log.debug('Terminating instance %s', instance_id)
        terminate_instances = self.retry(self.ec2client.terminate_instances)
        terminate_instances(InstanceIds=[instance_id])

    def get_volume(self, volume_id):
        volume = self.ec2.Volume(volume_id)
        load = self.retry(
            volume.load, r'InvalidVolume\.NotFound')
        load()
        return volume

    def get_volumes(self, tag_key=None, tag_value=None):
        filters = list()
        if tag_key and tag_value:
            filters = [{'Name': 'tag:%s' % tag_key, 'Values': [tag_value]}]

        f = self.retry(
            self.ec2.volumes.filter, r'InvalidVolume\.NotFound')
        volumes = f(Filters=filters)

        for volume in volumes:
            load = self.retry(volume.load, r'InvalidVolume\.NotFound')
            load()
        return list(volumes)

    def iam_role_exists(self, role):
        try:
            self.ec2client.get_instance_profile(InstanceProfileName=role)
        except ClientError as e:
            code, _ = get_code_and_message(e)
            if code != 'NoSuchEntity':
                raise
            return False
        return True

    def get_snapshots(self, *snapshot_ids):
        f = self.retry(
            self.ec2.snapshots.filter, r'InvalidSnapshot\.NotFound')
        snapshots = f(SnapshotIds=snapshot_ids)
        for snapshot in snapshots:
            load = self.retry(snapshot.load, r'InvalidSnapshot\.NotFound')
            load()
        return list(snapshots)

    def get_snapshot(self, snapshot_id):
        snapshot = self.ec2.Snapshot(snapshot_id)
        load = self.retry(snapshot.load, r'InvalidSnapshot\.NotFound')
        load()
        return snapshot

    def create_snapshot(self, volume_id, name=None, description=None):
        log.debug('Creating snapshot of %s', volume_id)
        create_snapshot = self.retry(self.ec2client.create_snapshot)
        kwargs = {
            'VolumeId': volume_id
        }
        if description:
            kwargs['Description'] = description
        response = create_snapshot(**kwargs)
        snapshot_id = response['SnapshotId']
        self.create_tags(snapshot_id, name=name)
        return self.get_snapshot(snapshot_id)

    def create_volume(self,
                      size,
                      zone,
                      snapshot_id=None,
                      volume_type=None,
                      encrypted=None):
        create_volume = self.retry(self.ec2client.create_volume)

        kwargs = {
            'AvailabilityZone': zone,
            'Size': size
        }
        if snapshot_id:
            kwargs['SnapshotId'] = snapshot_id
        if volume_type:
            kwargs['VolumeType'] = volume_type
        if encrypted:
            kwargs['Encrypted'] = encrypted

        log.debug('Creating volume: %s', pretty_print_json(kwargs))
        response = create_volume(**kwargs)
        volume_id = response['VolumeId']
        return self.get_volume(volume_id)

    def delete_volume(self, volume_id):
        log.debug('Deleting volume %s', volume_id)
        try:
            delete_volume = self.retry(
                self.ec2client.delete_volume, r'VolumeInUse')
            delete_volume(VolumeId=volume_id)
        except ClientError as e:
            code, _ = get_code_and_message(e)
            if code != 'InvalidVolume.NotFound':
                raise

        return True

    def get_images(self, name=None, owner_alias=None, product_code=None):
        filters = list()
        if name:
            filters.append({'Name': 'name', 'Values': [name]})
        if owner_alias:
            filters.append({'Name': 'owner-alias', 'Values': [owner_alias]})
        if product_code:
            filters.append({'Name': 'product-code', 'Values': [product_code]})

        images = self.ec2.images.filter(Filters=filters)
        for image in images:
            image.load()
        return list(images)

    def get_image(self, image_id, retry=False):
        image = self.ec2.Image(image_id)
        load = image.load
        if retry:
            load = self.retry(
                image.load, r'InvalidAMIID\.NotFound')

        load()
        return image

    def delete_snapshot(self, snapshot_id):
        delete_snapshot = self.retry(self.ec2client.delete_snapshot)
        return delete_snapshot(SnapshotId=snapshot_id)

    def create_security_group(self, name, description, vpc_id=None):
        kwargs = {
            'Description': description,
            'GroupName': name
        }
        if vpc_id:
            kwargs['VpcId'] = vpc_id

        create_security_group = self.retry(
            self.ec2client.create_security_group)
        log.debug('Creating security group: %s', pretty_print_json(kwargs))
        response = create_security_group(**kwargs)
        return self.get_security_group(response['GroupId'])

    def get_security_group(self, sg_id, retry=True):
        sg = self.ec2.SecurityGroup(sg_id)
        load = sg.load
        if retry:
            load = self.retry(
                sg.load, r'InvalidGroup\.NotFound')

        load()
        return sg

    def authorize_security_group_ingress(self, sg_id, port):
        log.debug('Authorizing ingress to %s on port %d', sg_id, port)
        authorize = self.retry(
            self.ec2client.authorize_security_group_ingress)
        authorize(
            GroupId=sg_id,
            FromPort=port,
            ToPort=port,
            CidrIp='0.0.0.0/0',
            IpProtocol='tcp'
        )

    def delete_security_group(self, sg_id):
        log.debug('Deleting %s', sg_id)
        delete_security_group = self.retry(
            self.ec2client.delete_security_group,
            r'InvalidGroup\.InUse|DependencyViolation'
        )
        delete_security_group(GroupId=sg_id)

    def get_key_pair(self, keyname):
        key_pair = self.ec2.KeyPair(keyname)
        load = self.retry(key_pair.load)
        load()
        return key_pair

    def get_console_output(self, instance_id):
        response = self.ec2client.get_console_output(
            InstanceId=instance_id)
        return response['Output']

    def get_subnet(self, subnet_id):
        subnet = self.ec2.Subnet(subnet_id)
        load = self.retry(subnet.load)
        load()
        return subnet

    def create_image(self,
                     instance_id,
                     name,
                     description=None,
                     no_reboot=True,
                     block_device_mappings=None):
        timeout = float(60 * 60)  # One hour.
        create_image = self.retry(
            self.ec2client.create_image,
            r'InvalidParameterValue',
            timeout=timeout
        )
        kwargs = {
            'InstanceId': instance_id,
            'Name': name
        }
        if description:
            kwargs['Description'] = description
        if no_reboot is not None:
            kwargs['NoReboot'] = no_reboot
        if block_device_mappings:
            kwargs['BlockDeviceMappings'] = block_device_mappings

        response = create_image(**kwargs)
        return self.get_image(response['ImageId'])

    def detach_volume(self, vol_id, instance_id, force=True):

        detach_volume = self.retry(self.ec2client.detach_volume)
        kwargs = {
            'VolumeId': vol_id
        }
        if instance_id:
            kwargs['InstanceId'] = instance_id
        if force is not None:
            kwargs['Force'] = force
        log.debug('Detaching volume: %s', kwargs)
        response = detach_volume(**kwargs)
        return self.get_volume(response['VolumeId'])

    def attach_volume(self, vol_id, instance_id, device_name):
        log.debug(
            'Attaching %s to %s at %s', vol_id, instance_id, device_name)
        attach_volume = self.retry(
            self.ec2client.attach_volume, r'VolumeInUse')
        response = attach_volume(
            VolumeId=vol_id,
            InstanceId=instance_id,
            Device=device_name
        )
        return self.get_volume(response['VolumeId'])

    def get_default_vpc(self):
        filter = self.retry(self.ec2.vpcs.filter)
        vpcs = filter(Filters=[{'Name': 'isDefault', 'Values': ['true']}])
        if vpcs:
            vpc = list(vpcs)[0]
            vpc.load()
            return vpc

        return None

    def modify_instance_attribute(self, instance_id, attribute,
                                  value, dry_run=False):
        log.debug('Setting %s for %s to %s', attribute, instance_id, value)
        modify_instance_attribute = self.retry(
            self.ec2client.modify_instance_attribute)
        modify_instance_attribute(
            InstanceId=instance_id,
            Attribute=attribute,
            Value=value
        )


def validate_image_name(name):
    """ Verify that the name is a valid EC2 image name.  Return the name
        if it's valid.

    :raises ValidationError if the name is invalid
    """
    if not (name and 3 <= len(name) <= 128):
        raise ValidationError(
            'Image name must be between 3 and 128 characters long')

    m = re.match(r'[A-Za-z0-9()\[\] ./\-\'@_]+$', name)
    if not m:
        raise ValidationError(
            "Image name may only contain letters, numbers, spaces, "
            "and the following characters: ()[]./-'@_"
        )
    return name


def validate_tag_key(key):
    """ Verify that the key is a valid EC2 tag key.

    :return: the key if it's valid
    :raises ValidationError if the key is invalid
    """
    if len(key) > 127:
        raise ValidationError(
            'Tag key cannot be longer than 127 characters'
        )
    if key.startswith('aws:'):
        raise ValidationError(
            'Tag key cannot start with "aws:"'
        )
    return key


def validate_tag_value(value):
    """ Verify that the value is a valid EC2 tag value.

    :return: the value if it's valid
    :raises ValidationError if the value is invalid
    """
    if len(value) > 255:
        raise ValidationError(
            'Tag value cannot be longer than 255 characters'
        )
    if value.startswith('aws:'):
        raise ValidationError(
            'Tag value cannot start with "aws:"'
        )
    return value


class VolumeError(BracketError):
    pass


def wait_for_volume(aws_svc, volume_id, timeout=600.0, state='available'):
    """ Wait for the volume to be in the specified state.

    :return the Volume object
    :raise VolumeError if the timeout is exceeded
    """
    log.debug(
        'Waiting for %s, timeout=%.02f, state=%s',
        volume_id, timeout, state)

    deadline = Deadline(timeout)
    sleep_time = 0.5
    while not deadline.is_expired():
        volume = aws_svc.get_volume(volume_id)
        log.debug('Volume %s state=%s', volume.id, volume.state)
        if volume.state == state:
            return volume
        util.sleep(sleep_time)
        sleep_time *= 2
    raise VolumeError(
        'Timed out waiting for %s to be in the %s state' %
        (volume_id, state)
    )


class SnapshotError(BracketError):
    pass


class InstanceError(BracketError):
    pass


def wait_for_instance(
        aws_svc, instance_id, timeout=600, state='running'):
    """ Wait for up to timeout seconds for an instance to be in the
    given state.  Sleep for 2 seconds between checks.

    :return: The Instance object
    :raises InstanceError if a timeout occurs or the instance unexpectedly
        goes into an error or terminated state
    """

    log.debug(
        'Waiting for %s, timeout=%d, state=%s',
        instance_id, timeout, state)

    deadline = Deadline(timeout)
    while not deadline.is_expired():
        instance = aws_svc.get_instance(instance_id)
        log.debug('Instance %s state=%s', instance.id, instance.state['Name'])
        if instance.state['Name'] == state:
            return instance
        if instance.state['Name'] == 'error':
            raise InstanceError(
                'Instance %s is in an error state.  Cannot proceed.' %
                instance_id
            )
        if state != 'terminated' and instance.state['Name'] == 'terminated':
            raise InstanceError(
                'Instance %s was unexpectedly terminated.' % instance_id
            )
        sleep(2)
    raise InstanceError(
        'Timed out waiting for %s to be in the %s state' %
        (instance_id, state)
    )


def stop_and_wait(aws_svc, instance_id):
    """ Stop the given instance and wait for it to be in the stopped state.
    If an exception is thrown, log the error and return.
    """
    try:
        aws_svc.stop_instance(instance_id)
        wait_for_instance(aws_svc, instance_id, state='stopped')
    except:
        log.exception(
            'Error while waiting for instance %s to stop', instance_id)


def wait_for_image(aws_svc, image_id):
    log.debug('Waiting for %s to become available.', image_id)
    image = None

    for i in range(180):
        sleep(5)
        image = aws_svc.get_image(image_id)
        log.debug('%s: %s', image.id, image.state)
        if image.state == 'available':
            return image
        if image.state == 'failed':
            raise BracketError('Image state became failed')

    raise BracketError(
        'Image failed to become available (%s)' % image.state)


def create_encryptor_security_group(aws_svc, vpc_id=None, status_port=\
                                    encryptor_service.ENCRYPTOR_STATUS_PORT):
    sg_name = NAME_ENCRYPTOR_SECURITY_GROUP % {'nonce': make_nonce()}
    sg_desc = DESCRIPTION_ENCRYPTOR_SECURITY_GROUP
    sg = aws_svc.create_security_group(sg_name, sg_desc, vpc_id=vpc_id)
    log.info('Created temporary security group with id %s', sg.id)
    try:
        aws_svc.authorize_security_group_ingress(sg.id, status_port)
    except Exception as e:
        log.error('Failed adding security group rule to %s: %s', sg.id, e)
        try:
            log.info('Cleaning up temporary security group %s', sg.id)
            aws_svc.delete_security_group(sg.id)
        except Exception as e2:
            log.warn('Failed deleting temporary security group: %s', e2)
        raise

    aws_svc.create_tags(sg.id)
    return sg


def run_guest_instance(aws_svc, image_id, subnet_id=None,
                       instance_type='m4.large'):
    instance = None

    try:
        instance = aws_svc.run_instance(
            image_id, subnet_id=subnet_id,
            instance_type=instance_type, ebs_optimized=False)
        log.info(
            'Launching instance %s to snapshot root disk for %s',
            instance.id, image_id)
        aws_svc.create_tags(
            instance.id,
            name=NAME_GUEST_CREATOR,
            description=DESCRIPTION_GUEST_CREATOR % {'image_id': image_id}
        )
    except:
        if instance:
            clean_up(aws_svc, instance_ids=[instance.id])
        raise

    return instance


def clean_up(aws_svc, instance_ids=None, volume_ids=None,
              snapshot_ids=None, security_group_ids=None):
    """ Clean up any resources that were created by the encryption process.
    Handle and log exceptions, to ensure that the script doesn't exit during
    cleanup.
    """
    instance_ids = instance_ids or []
    volume_ids = volume_ids or []
    snapshot_ids = snapshot_ids or []
    security_group_ids = security_group_ids or []

    # Delete instances and snapshots.
    terminated_instance_ids = set()
    for instance_id in instance_ids:
        try:
            log.info('Terminating instance %s', instance_id)
            aws_svc.terminate_instance(instance_id)
            terminated_instance_ids.add(instance_id)
        except ClientError as e:
            log.warn('Unable to terminate instance %s: %s', instance_id, e)
        except:
            log.exception('Unable to terminate instance %s', instance_id)

    for snapshot_id in snapshot_ids:
        try:
            log.info('Deleting snapshot %s', snapshot_id)
            aws_svc.delete_snapshot(snapshot_id)
        except ClientError as e:
            log.warn('Unable to delete snapshot %s: %s', snapshot_id, e)
        except:
            log.exception('Unable to delete snapshot %s', snapshot_id)

    # Wait for instances to terminate before deleting security groups and
    # volumes, to avoid dependency errors.
    for id in terminated_instance_ids:
        log.info('Waiting for instance %s to terminate.', id)
        try:
            wait_for_instance(aws_svc, id, state='terminated')
        except (ClientError, InstanceError) as e:
            log.warn(
                'An error occurred while waiting for instance to '
                'terminate: %s', e)
        except:
            log.exception(
                'An error occurred while waiting for instance '
                'to terminate'
            )

    # Delete volumes and security groups.
    for volume_id in volume_ids:
        try:
            log.info('Deleting volume %s', volume_id)
            aws_svc.delete_volume(volume_id)
        except ClientError as e:
            log.warn('Unable to delete volume %s: %s', volume_id, e)
        except:
            log.exception('Unable to delete volume %s', volume_id)

    for sg_id in security_group_ids:
        try:
            log.info('Deleting security group %s', sg_id)
            aws_svc.delete_security_group(sg_id)
        except ClientError as e:
            log.warn('Unable to delete security group %s: %s', sg_id, e)
        except:
            log.exception('Unable to delete security group %s', sg_id)


def log_exception_console(aws_svc, e, id):
    log.error(
        'Encryption failed.  Check console output of instance %s '
        'for details.',
        id
    )

    e.console_output_file = _write_console_output(aws_svc, id)
    if e.console_output_file:
        log.error(
            'Wrote console output for instance %s to %s',
            id, e.console_output_file.name
        )
    else:
        log.error(
            'Encryptor console output is not currently available.  '
            'Wait a minute and check the console output for '
            'instance %s in the EC2 Management '
            'Console.',
            id
        )


def snapshot_log_volume(aws_svc, instance_id):
    """ Snapshot the log volume of the given instance.

    :except SnapshotError if the snapshot goes into an error state
    """

    # Snapshot root volume.
    instance = aws_svc.get_instance(instance_id)
    device = boto3_device.get_device(instance.block_device_mappings, '/dev/sda1')
    vol = aws_svc.get_volume(boto3_device.get_volume_id(device))
    image = aws_svc.get_image(instance.image_id)
    snapshot = aws_svc.create_snapshot(
        vol.id,
        name=NAME_LOG_SNAPSHOT % {'instance_id': instance_id},
        description=DESCRIPTION_LOG_SNAPSHOT % {
            'instance_id': instance_id,
            'aws_account': image.owner_id,
            'timestamp': datetime.utcnow().strftime('%b %d %Y %I:%M%p UTC')
        }
    )
    log.info(
        'Creating snapshot %s of log volume for instance %s',
        snapshot.id, instance_id
    )

    try:
        wait_for_snapshots(aws_svc, snapshot.id)
    except:
        clean_up(aws_svc, snapshot_ids=[snapshot.id])
        raise
    return snapshot


def wait_for_volume_attached(aws_svc, instance_id, device):
    """ Wait until the device appears in the block device mapping of the
    given instance.
    :return: the Instance object
    """
    # Wait for attachment to complete.
    log.debug(
        'Waiting for %s in block device mapping of %s.',
        device,
        instance_id
    )

    found = False
    instance = None

    for _ in xrange(20):
        instance = aws_svc.get_instance(instance_id)
        device_names = boto3_device.get_device_names(
            instance.block_device_mappings)
        log.debug('Found devices: %s', device_names)
        if device in device_names:
            found = True
            break
        else:
            sleep(5)

    if not found:
        raise BracketError(
            'Timed out waiting for %s to attach to %s' %
            (device, instance_id)
        )

    return instance


def _write_console_output(aws_svc, instance_id):

    try:
        console_output = aws_svc.get_console_output(instance_id)
        if console_output.output:
            prefix = instance_id + '-'
            with tempfile.NamedTemporaryFile(
                    prefix=prefix, suffix='-console.txt', delete=False) as t:
                t.write(console_output.output)
            return t
    except:
        log.exception('Unable to write console output')

    return None


def wait_for_snapshots(aws_svc, *snapshot_ids):
    log.debug('Waiting for status "completed" for %s', str(snapshot_ids))
    last_progress_log = time.time()

    # Give AWS some time to propagate the snapshot creation.
    # If we create and get immediately, AWS may return 400.
    sleep(20)

    while True:
        snapshots = aws_svc.get_snapshots(*snapshot_ids)
        log.debug('%s', {s.id: s.state for s in snapshots})

        done = True
        error_ids = []
        for snapshot in snapshots:
            if snapshot.state == 'error':
                error_ids.append(snapshot.id)
            if snapshot.state != 'completed':
                done = False

        if error_ids:
            # Get rid of unicode markers in error the message.
            error_ids = [str(id) for id in error_ids]
            raise SnapshotError(
                'Snapshots in error state: %s.  Cannot continue.' %
                str(error_ids)
            )
        if done:
            return

        # Log progress if necessary.
        now = time.time()
        if now - last_progress_log > 60:
            log.info(_get_snapshot_progress_text(snapshots))
            last_progress_log = now

        sleep(5)


def _get_snapshot_progress_text(snapshots):
    elements = [
        '%s: %s' % (str(s.id), str(s.progress))
        for s in snapshots
    ]
    return ', '.join(elements)


def snapshot_root_volume(aws_svc, instance, image_id):
    """ Snapshot the root volume of the given AMI.

    :except SnapshotError if the snapshot goes into an error state
    """
    log.info(
        'Stopping instance %s in order to create snapshot', instance.id)
    aws_svc.stop_instance(instance.id)
    wait_for_instance(aws_svc, instance.id, state='stopped')

    # Snapshot root volume.
    instance = aws_svc.get_instance(instance.id)
    root_device_name = instance.root_device_name
    device_names = boto3_device.get_device_names(
        instance.block_device_mappings)

    if root_device_name not in device_names:
        # try stripping partition id
        root_device_name = string.rstrip(root_device_name, string.digits)

    root_device = boto3_device.get_device(
        instance.block_device_mappings, root_device_name)

    volume_id = boto3_device.get_volume_id(root_device)
    vol = aws_svc.get_volume(volume_id)
    aws_svc.create_tags(
        volume_id,
        name=NAME_ORIGINAL_VOLUME % {'image_id': image_id}
    )

    snapshot = aws_svc.create_snapshot(
        vol.id,
        name=NAME_ORIGINAL_SNAPSHOT,
        description=DESCRIPTION_ORIGINAL_SNAPSHOT % {'image_id': image_id}
    )
    log.info(
        'Creating snapshot %s of root volume for instance %s',
        snapshot.id, instance.id
    )

    try:
        wait_for_snapshots(aws_svc, snapshot.id)

        # Now try to detach the root volume.
        log.info('Detaching root volume from guest.')
        aws_svc.detach_volume(
            volume_id,
            instance_id=instance.id,
            force=True
        )
        wait_for_volume(aws_svc, volume_id)
        # And now delete it
        log.info('Deleting root volume %s', volume_id)
        aws_svc.delete_volume(volume_id)
    except:
        clean_up(aws_svc, snapshot_ids=[snapshot.id])
        raise

    iops = None
    if vol.volume_type == 'io1':
        iops = vol.iops

    ret_values = (
        snapshot.id, root_device_name, vol.size, vol.volume_type, iops)
    log.debug('Returning %s', str(ret_values))
    return ret_values


def get_code_and_message(client_error):
    """ Return the AWS error code and message from the given
    boto3 ClientError. """
    return (
        client_error.response['Error']['Code'],
        client_error.response['Error']['Message']
    )
