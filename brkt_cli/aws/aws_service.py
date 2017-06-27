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
import time

import string
import tempfile
from datetime import datetime

import boto
import boto.sts
import boto.vpc
import boto.iam
from boto.exception import EC2ResponseError, BotoServerError

from brkt_cli import util, encryptor_service
from brkt_cli.aws.aws_constants import (
    NAME_ENCRYPTOR_SECURITY_GROUP,
    DESCRIPTION_ENCRYPTOR_SECURITY_GROUP, NAME_GUEST_CREATOR,
    DESCRIPTION_GUEST_CREATOR, NAME_LOG_SNAPSHOT, DESCRIPTION_LOG_SNAPSHOT,
    NAME_ORIGINAL_VOLUME, NAME_ORIGINAL_SNAPSHOT,
    DESCRIPTION_ORIGINAL_SNAPSHOT)
from brkt_cli.util import Deadline, BracketError, sleep, make_nonce
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
        :raise: EC2ResponseError if an error occurred
        """
        pass

    @abc.abstractmethod
    def get_image(self, image_id, retry=False):
        pass

    @abc.abstractmethod
    def get_images(self, filters=None, owners=None):
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
    def add_security_group_rule(self, sg_id, **kwargs):
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
    def detach_volume(self, vol_id, instance_id=None, force=True):
        pass

    @abc.abstractmethod
    def attach_volume(self, vol_id, instance_id, device):
        pass

    @abc.abstractmethod
    def get_default_vpc(self):
        pass

    @abc.abstractmethod
    def get_instance_attribute(self, instance_id, attribute, dry_run=False):
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
        if not isinstance(exception, BotoServerError):
            return False
        if exception.status == 503:
            # This can happen when the AWS request limit has been exceeded.
            return True
        if self.error_code_regexp:
            m = re.match(self.error_code_regexp, exception.error_code)
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


def _get_first_element(list, error_status):
    """ Return the first element in the list.  If the list is empty, raise
    an EC2ResponseError with the given error status.  This is a workaround
    for the case where the AWS API erroneously returns an empty list instead
    of an error.
    """
    if list:
        return list[0]
    else:
        raise EC2ResponseError(
            error_status, 'AWS API returned an empty response')


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
        self.conn = None

    def get_regions(self):
        return boto.vpc.regions()

    def connect(self, region, key_name=None):
        self.region = region
        self.key_name = key_name
        self.conn = boto.vpc.connect_to_region(region)

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
                     block_device_map=None,
                     subnet_id=None,
                     user_data=None,
                     ebs_optimized=True,
                     instance_profile_name=None):
        if security_group_ids is None:
            security_group_ids = []
        log.debug(
            'run_instance: %s, security groups=%s, subnet=%s, '
            'type=%s',
            image_id, security_group_ids, subnet_id, instance_type
        )

        try:
            run_instances = self.retry(self.conn.run_instances)
            reservation = run_instances(
                image_id=image_id,
                placement=placement,
                key_name=self.key_name,
                instance_type=instance_type,
                block_device_map=block_device_map,
                security_group_ids=security_group_ids,
                subnet_id=subnet_id,
                ebs_optimized=ebs_optimized,
                user_data=user_data,
                instance_profile_name=instance_profile_name
            )
            instance = reservation.instances[0]
            log.debug('Launched instance %s', instance.id)
            return instance
        except EC2ResponseError:
            log.debug('Failed to launch instance for %s', image_id)
            raise

    def get_instance(self, instance_id):
        get_only_instances = self.retry(
            self.conn.get_only_instances, r'InvalidInstanceID\.NotFound')
        instances = get_only_instances([instance_id])
        return _get_first_element(instances, 'InvalidInstanceID.NotFound')

    def create_tags(self, resource_id, name=None, description=None):
        tags = dict(self.default_tags)
        if name:
            tags['Name'] = name
        if description:
            tags['Description'] = description
        log.debug('Tagging %s with %s', resource_id, tags)
        create_tags = self.retry(self.conn.create_tags, r'.*\.NotFound')
        create_tags([resource_id], tags)

    def stop_instance(self, instance_id):
        log.debug('Stopping instance %s', instance_id)
        stop_instances = self.retry(self.conn.stop_instances)
        instances = stop_instances([instance_id])
        return instances[0]

    def terminate_instance(self, instance_id):
        log.debug('Terminating instance %s', instance_id)
        terminate_instances = self.retry(self.conn.terminate_instances)
        terminate_instances([instance_id])

    def get_volume(self, volume_id):
        get_all_volumes = self.retry(
            self.conn.get_all_volumes, r'InvalidVolume\.NotFound')
        volumes = get_all_volumes(volume_ids=[volume_id])
        return _get_first_element(volumes, 'InvalidVolume.NotFound')

    def get_volumes(self, tag_key=None, tag_value=None):
        filters = {}
        if tag_key and tag_value:
            filters['tag:%s' % tag_key] = tag_value

        get_all_volumes = self.retry(self.conn.get_all_volumes)
        return get_all_volumes(filters=filters)

    def iam_role_exists(self, role):
        conn = boto.iam.connect_to_region(self.region)
        try:
            conn.get_instance_profile(role)
        except BotoServerError as e:
            if e.error_code != 'NoSuchEntity':
                raise
            return False
        return True

    def get_snapshots(self, *snapshot_ids):
        get_all_snapshots = self.retry(
            self.conn.get_all_snapshots, r'InvalidSnapshot\.NotFound')
        return get_all_snapshots(snapshot_ids)

    def get_snapshot(self, snapshot_id):
        snapshots = self.get_snapshots(snapshot_id)
        return _get_first_element(snapshots, 'InvalidSnapshot.NotFound')

    def create_snapshot(self, volume_id, name=None, description=None):
        log.debug('Creating snapshot of %s', volume_id)
        create_snapshot = self.retry(self.conn.create_snapshot)
        snapshot = create_snapshot(volume_id, description)
        self.create_tags(snapshot.id, name=name)
        return snapshot

    def create_volume(self,
                      size,
                      zone,
                      snapshot=None,
                      volume_type=None,
                      encrypted=None):
        create_volume = self.retry(self.conn.create_volume)
        return create_volume(
            size,
            zone,
            snapshot=snapshot,
            volume_type=volume_type,
            encrypted=encrypted)

    def delete_volume(self, volume_id):
        log.debug('Deleting volume %s', volume_id)
        try:
            delete_volume = self.retry(
                self.conn.delete_volume, r'VolumeInUse')
            delete_volume(volume_id)
        except EC2ResponseError as e:
            if e.error_code != 'InvalidVolume.NotFound':
                raise
        return True

    def get_images(self, filters=None, owners=None):
        get_all_images = self.retry(self.conn.get_all_images)
        return get_all_images(filters=filters, owners=owners)

    def get_image(self, image_id, retry=False):
        get_image = self.conn.get_image
        if retry:
            get_image = self.retry(
                self.conn.get_image, r'InvalidAMIID\.NotFound')

        return get_image(image_id)

    def delete_snapshot(self, snapshot_id):
        delete_snapshot = self.retry(self.conn.delete_snapshot)
        return delete_snapshot(snapshot_id)

    def create_security_group(self, name, description, vpc_id=None):
        log.debug(
            'Creating security group: name=%s, description=%s',
            name, description
        )
        if vpc_id:
            log.debug('Using %s', vpc_id)

        create_security_group = self.retry(self.conn.create_security_group)
        return create_security_group(
            name, description, vpc_id=vpc_id
        )

    def get_security_group(self, sg_id, retry=True):
        get_all_security_groups = self.conn.get_all_security_groups
        if retry:
            get_all_security_groups = self.retry(
                self.conn.get_all_security_groups, r'InvalidGroup\.NotFound')

        groups = get_all_security_groups(group_ids=[sg_id])
        return _get_first_element(groups, 'InvalidGroup.NotFound')

    def add_security_group_rule(self, sg_id, **kwargs):
        kwargs['group_id'] = sg_id
        authorize_security_group = self.retry(
            self.conn.authorize_security_group)
        ok = authorize_security_group(**kwargs)
        if not ok:
            raise Exception('Unknown error while adding security group rule')

    def delete_security_group(self, sg_id):
        delete_security_group = self.retry(
            self.conn.delete_security_group,
            r'InvalidGroup\.InUse|DependencyViolation'
        )
        ok = delete_security_group(group_id=sg_id)
        if not ok:
            raise Exception('Unknown error while deleting security group')

    def get_key_pair(self, keyname):
        get_all_key_pairs = self.retry(self.conn.get_all_key_pairs)
        key_pairs = get_all_key_pairs(keynames=[keyname])
        return _get_first_element(key_pairs, 'InvalidKeyPair.NotFound')

    def get_console_output(self, instance_id):
        return self.conn.get_console_output(instance_id)

    def get_subnet(self, subnet_id):
        subnets = self.conn.get_all_subnets(subnet_ids=[subnet_id])
        return _get_first_element(subnets, 'InvalidSubnetID.NotFound')

    def create_image(self,
                     instance_id,
                     name,
                     description=None,
                     no_reboot=True,
                     block_device_mapping=None):
        timeout = float(60 * 60)  # One hour.
        create_image = self.retry(
            self.conn.create_image, r'InvalidParameterValue', timeout=timeout)
        return create_image(
            instance_id,
            name,
            description=description,
            no_reboot=no_reboot,
            block_device_mapping=block_device_mapping
        )

    def detach_volume(self, vol_id, instance_id=None, force=True):
        detach_volume = self.retry(self.conn.detach_volume)
        return detach_volume(
            vol_id, instance_id=instance_id, force=force)

    def attach_volume(self, vol_id, instance_id, device):
        attach_volume = self.retry(self.conn.attach_volume, r'VolumeInUse')
        return attach_volume(vol_id, instance_id, device)

    def get_default_vpc(self):
        get_all_vpcs = self.retry(self.conn.get_all_vpcs)
        vpcs = get_all_vpcs(filters={'is-default': 'true'})
        if len(vpcs) > 0:
            return vpcs[0]
        return None

    def get_instance_attribute(self, instance_id, attribute, dry_run=False):
        get_instance_attribute = self.retry(self.conn.get_instance_attribute)
        return get_instance_attribute(
            instance_id,
            attribute,
            dry_run=dry_run
        )

    def modify_instance_attribute(self, instance_id, attribute,
                                  value, dry_run=False):
        modify_instance_attribute = self.retry(self.conn.modify_instance_attribute)
        return modify_instance_attribute(
            instance_id,
            attribute,
            value,
            dry_run=dry_run
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
        if volume.status == state:
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
        log.debug('Instance %s state=%s', instance.id, instance.state)
        if instance.state == state:
            return instance
        if instance.state == 'error':
            raise InstanceError(
                'Instance %s is in an error state.  Cannot proceed.' %
                instance_id
            )
        if state != 'terminated' and instance.state == 'terminated':
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
    for i in range(180):
        sleep(5)
        try:
            image = aws_svc.get_image(image_id)
        except EC2ResponseError, e:
            if e.error_code == 'InvalidAMIID.NotFound':
                log.debug('AWS threw a NotFound, ignoring')
                continue
            else:
                log.warn('Unknown AWS error: %s', e)
        # These two attributes are optional in the response and only
        # show up sometimes. So we have to getattr them.
        reason = repr(getattr(image, 'stateReason', None))
        code = repr(getattr(image, 'code', None))
        log.debug("%s: %s reason: %s code: %s",
                  image.id, image.state, reason, code)
        if image.state == 'available':
            break
        if image.state == 'failed':
            raise BracketError('Image state became failed')
    else:
        raise BracketError(
            'Image failed to become available (%s)' % (image.state,))


def create_encryptor_security_group(aws_svc, vpc_id=None, status_port=\
                                    encryptor_service.ENCRYPTOR_STATUS_PORT):
    sg_name = NAME_ENCRYPTOR_SECURITY_GROUP % {'nonce': make_nonce()}
    sg_desc = DESCRIPTION_ENCRYPTOR_SECURITY_GROUP
    sg = aws_svc.create_security_group(sg_name, sg_desc, vpc_id=vpc_id)
    log.info('Created temporary security group with id %s', sg.id)
    try:
        aws_svc.add_security_group_rule(
            sg.id, ip_protocol='tcp',
            from_port=status_port,
            to_port=status_port,
            cidr_ip='0.0.0.0/0')
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
        except EC2ResponseError as e:
            log.warn('Unable to terminate instance %s: %s', instance_id, e)
        except:
            log.exception('Unable to terminate instance %s', instance_id)

    for snapshot_id in snapshot_ids:
        try:
            log.info('Deleting snapshot %s', snapshot_id)
            aws_svc.delete_snapshot(snapshot_id)
        except EC2ResponseError as e:
            log.warn('Unable to delete snapshot %s: %s', snapshot_id, e)
        except:
            log.exception('Unable to delete snapshot %s', snapshot_id)

    # Wait for instances to terminate before deleting security groups and
    # volumes, to avoid dependency errors.
    for id in terminated_instance_ids:
        log.info('Waiting for instance %s to terminate.', id)
        try:
            wait_for_instance(aws_svc, id, state='terminated')
        except (EC2ResponseError, InstanceError) as e:
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
        except EC2ResponseError as e:
            log.warn('Unable to delete volume %s: %s', volume_id, e)
        except:
            log.exception('Unable to delete volume %s', volume_id)

    for sg_id in security_group_ids:
        try:
            log.info('Deleting security group %s', sg_id)
            aws_svc.delete_security_group(sg_id)
        except EC2ResponseError as e:
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
    bdm = instance.block_device_mapping

    log_vol = bdm["/dev/sda1"]
    vol = aws_svc.get_volume(log_vol.volume_id)
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
        bdm = instance.block_device_mapping
        log.debug('Found devices: %s', bdm.keys())
        if device in bdm:
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
        log.debug('%s', {s.id: s.status for s in snapshots})

        done = True
        error_ids = []
        for snapshot in snapshots:
            if snapshot.status == 'error':
                error_ids.append(snapshot.id)
            if snapshot.status != 'completed':
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
    root_dev = instance.root_device_name
    bdm = instance.block_device_mapping

    if root_dev not in bdm:
        # try stripping partition id
        root_dev = string.rstrip(root_dev, string.digits)
    root_vol = bdm[root_dev]
    vol = aws_svc.get_volume(root_vol.volume_id)
    aws_svc.create_tags(
        root_vol.volume_id,
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
        log.info('Detaching root volume %s from %s',
                 root_vol.volume_id, instance.id)
        aws_svc.detach_volume(
            root_vol.volume_id,
            instance_id=instance.id,
            force=True
        )
        wait_for_volume(aws_svc, root_vol.volume_id)
        # And now delete it
        log.info('Deleting root volume %s', root_vol.volume_id)
        aws_svc.delete_volume(root_vol.volume_id)
    except:
        clean_up(aws_svc, snapshot_ids=[snapshot.id])
        raise

    iops = None
    if vol.type == 'io1':
        iops = vol.iops

    ret_values = (
        snapshot.id, root_dev, vol.size, vol.type, iops)
    log.debug('Returning %s', str(ret_values))
    return ret_values
