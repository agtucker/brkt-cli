# Copyright 2015 Bracket Computing, Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at
#
# https://github.com/brkt/brkt-sdk-java/blob/master/LICENSE
#
# or in the "license" file accompanying this file. This file is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR
# CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and
# limitations under the License.

"""
Create an encrypted AMI based on an existing unencrypted AMI.

Overview of the process:
    * Start an instance based on the unencrypted AMI.
    * Snapshot the root volume of the unencrypted instance.
    * Terminate the instance.
    * Start a Bracket Encryptor instance.
    * Attach the unencrypted root volume to the Encryptor instance.
    * The Bracket Encryptor copies the unencrypted root volume to a new
        encrypted volume that's 2x the size of the original.
    * Snapshot the Bracket Encryptor system volumes and the new encrypted
        root volume.
    * Create a new AMI based on the snapshots.
    * Terminate the Bracket Encryptor instance.
    * Delete the unencrypted snapshot.

Before running brkt encrypt-ami, set the AWS_ACCESS_KEY_ID and
AWS_SECRET_ACCESS_KEY environment variables, like you would when
running the AWS command line utility.
"""

import os
import logging
import re
import string
import tempfile
import time

import requests
from boto.exception import EC2ResponseError
from boto.ec2.blockdevicemapping import (
    BlockDeviceMapping,
    EBSBlockDeviceType,
)

from brkt_cli import encryptor_service
from brkt_cli.util import (
    BracketError,
    Deadline,
    make_nonce,
)

# End user-visible terminology.  These are resource names and descriptions
# that the user will see in his or her EC2 console.

# Snapshotter instance names.
NAME_SNAPSHOT_CREATOR = 'Bracket root snapshot creator'
DESCRIPTION_SNAPSHOT_CREATOR = \
    'Used for creating a snapshot of the root volume from %(image_id)s'

# Security group names
NAME_ENCRYPTOR_SECURITY_GROUP = 'Bracket Encryptor %(nonce)s'
DESCRIPTION_ENCRYPTOR_SECURITY_GROUP = (
    "Allows access to the encryption service.")

# Encryptor instance names.
NAME_ENCRYPTOR = 'Bracket volume encryptor'
DESCRIPTION_ENCRYPTOR = \
    'Copies the root snapshot from %(image_id)s to a new encrypted volume'

# Snapshot names.
NAME_ORIGINAL_SNAPSHOT = 'Bracket encryptor original volume'
DESCRIPTION_ORIGINAL_SNAPSHOT = \
    'Original unencrypted root volume from %(image_id)s'
NAME_ENCRYPTED_ROOT_SNAPSHOT = 'Bracket encrypted root volume'
NAME_METAVISOR_ROOT_SNAPSHOT = 'Bracket system root'
NAME_METAVISOR_GRUB_SNAPSHOT = 'Bracket system GRUB'
NAME_METAVISOR_LOG_SNAPSHOT = 'Bracket system log'
DESCRIPTION_SNAPSHOT = 'Based on %(image_id)s'

# Volume names.
NAME_ORIGINAL_VOLUME = 'Original unencrypted root volume from %(image_id)s'
NAME_ENCRYPTED_ROOT_VOLUME = 'Bracket encrypted root volume'
NAME_METAVISOR_ROOT_VOLUME = 'Bracket system root'
NAME_METAVISOR_GRUB_VOLUME = 'Bracket system GRUB'
NAME_METAVISOR_LOG_VOLUME = 'Bracket system log'

# Tag names.
TAG_ENCRYPTOR = 'BrktEncryptor'
TAG_ENCRYPTOR_SESSION_ID = 'BrktEncryptorSessionID'
TAG_ENCRYPTOR_AMI = 'BrktEncryptorAMI'
TAG_DESCRIPTION = 'Description'

NAME_ENCRYPTED_IMAGE = '%(original_image_name)s %(encrypted_suffix)s'
NAME_ENCRYPTED_IMAGE_SUFFIX = ' (encrypted %(nonce)s)'
SUFFIX_ENCRYPTED_IMAGE = (
    ' - based on %(image_id)s, encrypted by Bracket Computing'
)
DEFAULT_DESCRIPTION_ENCRYPTED_IMAGE = \
    'Based on %(image_id)s, encrypted by Bracket Computing'

SLEEP_ENABLED = True
AMI_NAME_MAX_LENGTH = 128

BRACKET_ENVIRONMENT = "prod"
ENCRYPTOR_AMIS_URL = "http://solo-brkt-%s-net.s3.amazonaws.com/amis.json"
ENCRYPTION_PROGRESS_TIMEOUT = 10 * 60  # 10 minutes

log = logging.getLogger(__name__)


class SnapshotError(BracketError):
    pass


class InstanceError(BracketError):
    pass


def get_default_tags(session_id, encryptor_ami):
    default_tags = {
        TAG_ENCRYPTOR: True,
        TAG_ENCRYPTOR_SESSION_ID: session_id,
        TAG_ENCRYPTOR_AMI: encryptor_ami
    }
    return default_tags


def _get_snapshot_progress_text(snapshots):
    elements = [
        '%s: %s' % (str(s.id), str(s.progress))
        for s in snapshots
    ]
    return ', '.join(elements)


def sleep(seconds):
    if SLEEP_ENABLED:
        time.sleep(seconds)


def _wait_for_instance(
        aws_svc, instance_id, timeout=300, state='running'):
    """ Wait for up to timeout seconds for an instance to be in the
        'running' state.  Sleep for 2 seconds between checks.
    :return: The Instance object, or None if a timeout occurred
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


def wait_for_encryptor_up(enc_svc, deadline):
    start = time.time()
    while not deadline.is_expired():
        if enc_svc.is_encryptor_up():
            log.debug(
                'Encryption service is up after %.1f seconds',
                time.time() - start
            )
            return
        sleep(5)
    raise BracketError('Unable to contact %s' % enc_svc.hostname)


class EncryptionError(BracketError):
    def __init__(self, message):
        super(EncryptionError, self).__init__(message)
        self.console_output_file = None


class UnsupportedGuestError(BracketError):
    pass


def wait_for_encryption(enc_svc,
                        progress_timeout=ENCRYPTION_PROGRESS_TIMEOUT):
    err_count = 0
    max_errs = 10
    start_time = time.time()
    last_log_time = start_time
    progress_deadline = Deadline(progress_timeout)
    last_progress = 0

    while err_count < max_errs:
        try:
            status = enc_svc.get_status()
            err_count = 0
        except Exception as e:
            log.warn("Failed getting encryption status: %s", e)
            err_count += 1
            sleep(10)
            continue

        state = status['state']
        percent_complete = status['percent_complete']
        log.debug('state=%s, percent_complete=%d', state, percent_complete)

        # Make sure that encryption progress hasn't stalled.
        if progress_deadline.is_expired():
            raise EncryptionError(
                'Waited for encryption progress for longer than %s seconds' %
                progress_timeout
            )
        if percent_complete > last_progress:
            last_progress = percent_complete
            progress_deadline = Deadline(progress_timeout)

        # Log progress once a minute.
        now = time.time()
        if now - last_log_time >= 60:
            log.info('Encryption is %d%% complete', percent_complete)
            last_log_time = now

        if state == encryptor_service.ENCRYPT_SUCCESSFUL:
            log.info('Encrypted root drive created.')
            return
        elif state == encryptor_service.ENCRYPT_FAILED:
            failure_code = status.get('failure_code')
            log.debug('failure_code=%s', failure_code)
            if failure_code == \
                    encryptor_service.FAILURE_CODE_UNSUPPORTED_GUEST:
                raise UnsupportedGuestError(
                    'The specified AMI uses an unsupported operating system')
            raise EncryptionError('Encryption failed')

        sleep(10)
    # We've failed to get encryption status for _max_errs_ consecutive tries.
    # Assume that the server has crashed.
    raise EncryptionError('Encryption service unavailable')


def _get_encrypted_suffix():
    """ Return a suffix that will be appended to the encrypted image name.
    The suffix is in the format "(encrypted 787ace7a)".  The nonce portion of
    the suffix is necessary because Amazon requires image names to be unique.
    """
    return NAME_ENCRYPTED_IMAGE_SUFFIX % {'nonce': make_nonce()}


def _append_suffix(name, suffix, max_length=None):
    """ Append the suffix to the given name.  If the appended length exceeds
    max_length, truncate the name to make room for the suffix.

    :return: The possibly truncated name with the suffix appended
    """
    if not suffix:
        return name
    if max_length:
        truncated_length = max_length - len(suffix)
        name = name[:truncated_length]
    return name + suffix


def get_encryptor_ami(region):
    bracket_env = os.getenv('BRACKET_ENVIRONMENT',
                            BRACKET_ENVIRONMENT)
    if not bracket_env:
        raise BracketError('No bracket environment found')
    bucket_url = ENCRYPTOR_AMIS_URL % (bracket_env)
    log.debug('Getting encryptor AMI list from %s', bucket_url)
    r = requests.get(bucket_url)
    if r.status_code not in (200, 201):
        raise BracketError(
            'Getting %s gave response: %s' % (bucket_url, r.text))
    ami = r.json().get(region)
    if not ami:
        raise BracketError('No AMI for %s returned.' % region)
    return ami


def _wait_for_image(amazon_svc, image_id):
    log.debug('Waiting for %s to become available.', image_id)
    for i in range(180):
        sleep(5)
        try:
            image = amazon_svc.get_image(image_id)
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


def wait_for_snapshots(svc, *snapshot_ids):
    log.debug('Waiting for status "completed" for %s', str(snapshot_ids))
    last_progress_log = time.time()

    # Give AWS some time to propagate the snapshot creation.
    # If we create and get immediately, AWS may return 400.
    sleep(20)

    while True:
        snapshots = svc.get_snapshots(*snapshot_ids)
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


def create_encryptor_security_group(aws_svc, vpc_id=None):
    sg_name = NAME_ENCRYPTOR_SECURITY_GROUP % {'nonce': make_nonce()}
    sg_desc = DESCRIPTION_ENCRYPTOR_SECURITY_GROUP
    sg = aws_svc.create_security_group(sg_name, sg_desc, vpc_id=vpc_id)
    log.info('Created temporary security group with id %s', sg.id)
    try:
        aws_svc.add_security_group_rule(
            sg.id, ip_protocol='tcp',
            from_port=encryptor_service.ENCRYPTOR_STATUS_PORT,
            to_port=encryptor_service.ENCRYPTOR_STATUS_PORT,
            cidr_ip='0.0.0.0/0')
    except Exception as e:
        log.error('Failed adding security group rule to %s: %s', sg.id, e)
        try:
            log.info('Cleaning up temporary security group %s', sg.id)
            aws_svc.delete_security_group(sg.id)
        except Exception as e2:
            log.warn('Failed deleting temporary security group: %s', e2)
        raise e

    aws_svc.create_tags(sg.id)
    return sg


def run_encryptor_instance(aws_svc, encryptor_image_id, snapshot, root_size,
                           guest_image_id, security_group_ids=None,
                           subnet_id=None, update_ami=False):
    bdm = BlockDeviceMapping()
    guest_unencrypted_root = EBSBlockDeviceType(
        volume_type='gp2',
        snapshot_id=snapshot,
        delete_on_termination=True)
    # Use gp2 for fast burst I/O copying root drive
    bdm['/dev/sda4'] = guest_unencrypted_root
    if not update_ami:
        log.info('Launching encryptor instance with snapshot %s', snapshot)
        # They are creating an encrypted AMI instead of updating it
        # Use gp2 for fast burst I/O copying root drive
        guest_encrypted_root = EBSBlockDeviceType(
            volume_type='gp2',
            delete_on_termination=True)
        guest_encrypted_root.size = 2 * root_size + 1
        bdm['/dev/sda5'] = guest_encrypted_root
    else:
        log.info('Launching encryptor instance for updating %s',
                 guest_image_id)
        guest_encrypted_root = EBSBlockDeviceType(
            volume_type='gp2',
            snapshot_id=snapshot,
            delete_on_termination=True)

        guest_encrypted_root.size = root_size
        bdm['/dev/sda5'] = guest_encrypted_root

    instance = aws_svc.run_instance(
        encryptor_image_id,
        security_group_ids=security_group_ids,
        block_device_map=bdm,
        subnet_id=subnet_id)
    aws_svc.create_tags(
        instance.id,
        name=NAME_ENCRYPTOR,
        description=DESCRIPTION_ENCRYPTOR % {'image_id': guest_image_id}
    )
    instance = _wait_for_instance(aws_svc, instance.id)
    log.info('Launched encryptor instance %s', instance.id)
    # Tag volumes.
    bdm = instance.block_device_mapping
    if not update_ami:
        aws_svc.create_tags(
            bdm['/dev/sda5'].volume_id, name=NAME_ENCRYPTED_ROOT_VOLUME)
    aws_svc.create_tags(
        bdm['/dev/sda2'].volume_id, name=NAME_METAVISOR_ROOT_VOLUME)
    aws_svc.create_tags(
        bdm['/dev/sda1'].volume_id, name=NAME_METAVISOR_GRUB_VOLUME)
    aws_svc.create_tags(
        bdm['/dev/sda3'].volume_id, name=NAME_METAVISOR_LOG_VOLUME)
    return instance


def run_snapshotter_instance(aws_svc, image_id, subnet_id=None, updater=False):
    instance = aws_svc.run_instance(image_id, subnet_id=subnet_id)
    if not updater:
        log.info(
            'Launching instance %s to snapshot root disk for %s',
            instance.id, image_id)
    else:
        log.info(
            'Launching instance %s to snapshot ' % instance.id +
            'metavisor volumes')
    aws_svc.create_tags(
        instance.id,
        name=NAME_SNAPSHOT_CREATOR,
        description=DESCRIPTION_SNAPSHOT_CREATOR % {'image_id': image_id}
    )
    return _wait_for_instance(aws_svc, instance.id)


def _snapshot_root_volume(aws_svc, instance, image_id):
    """ Snapshot the root volume of the given AMI.

    :except SnapshotError if the snapshot goes into an error state
    """
    log.info(
        'Stopping instance %s in order to create snapshot', instance.id)
    aws_svc.stop_instance(instance.id)
    _wait_for_instance(aws_svc, instance.id, state='stopped')

    # Snapshot root volume.
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
    wait_for_snapshots(aws_svc, snapshot.id)

    ret_values = (
        snapshot.id, root_dev, vol.size, root_vol.volume_type, root_vol.iops)
    log.debug('Returning %s', str(ret_values))
    return ret_values


def write_console_output(aws_svc, instance_id):

    try:
        console_output = aws_svc.get_console_output(instance_id)
        if console_output.output:
            prefix = instance_id + '-'
            with tempfile.NamedTemporaryFile(
                    prefix=prefix, suffix='.log', delete=False) as t:
                t.write(console_output.output)
            return t
    except:
        log.exception('Unable to write console output')

    return None


def terminate_instance(aws_svc, id, name, terminated_instance_ids):
    try:
        log.info('Terminating %s instance %s', name, id)
        aws_svc.terminate_instance(id)
        terminated_instance_ids.add(id)
    except Exception as e:
        log.warn('Could not terminate %s instance: %s', name, e)


def _clean_up(aws_svc, instance_ids=None, volume_ids=None,
              snapshot_ids=None, security_group_ids=None):
    """ Clean up any resources that were created by the encryption process.
    Handle and log exceptions, to ensure that the script doesn't exit during
    cleanup.
    """
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
            _wait_for_instance(aws_svc, id, state='terminated')
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


def register_new_ami(aws_svc,
                     snap_grub,
                     snap_bsd,
                     snap_log,
                     snap_guest,
                     vol_type,
                     iops,
                     image_id,
                     encryptor_ami=None,
                     encrypted_ami_name=None):
    # Registers the new encrypted AMI for a created or updated AMI
    # Set up new Block Device Mappings
    log.debug('Creating block device mapping')
    new_bdm = BlockDeviceMapping()
    dev_grub = EBSBlockDeviceType(volume_type='gp2',
                                  snapshot_id=snap_grub.id,
                                  delete_on_termination=True)
    dev_root = EBSBlockDeviceType(volume_type='gp2',
                                  snapshot_id=snap_bsd.id,
                                  delete_on_termination=True)
    dev_log = EBSBlockDeviceType(volume_type='gp2',
                                 snapshot_id=snap_log.id,
                                 delete_on_termination=True)
    if vol_type == '':
        vol_type = 'standard'
    dev_guest_root = EBSBlockDeviceType(volume_type=vol_type,
                                        snapshot_id=snap_guest.id,
                                        iops=iops,
                                        delete_on_termination=True)
    new_bdm['/dev/sda1'] = dev_grub
    new_bdm['/dev/sda2'] = dev_root
    new_bdm['/dev/sda3'] = dev_log
    new_bdm['/dev/sda5'] = dev_guest_root

    log.debug('Getting image %s', image_id)
    image = aws_svc.get_image(image_id)
    if image is None:
        raise BracketError("Can't find image %s" % image_id)

    # Propagate any ephemeral drive mappings to the soloized image
    guest_bdm = image.block_device_mapping
    for key in guest_bdm.keys():
        guest_vol = guest_bdm[key]
        if guest_vol.ephemeral_name:
            log.info('Propagating block device mapping for %s at %s' %
                     (guest_vol.ephemeral_name, key))
            new_bdm[key] = guest_vol
    if encryptor_ami:
        # We are creating an encrypted image for the first time
        encryptor_image = aws_svc.get_image(encryptor_ami)
        if encryptor_image is None:
            raise BracketError("Can't find image %s" % encryptor_ami)
        kernel_id = encryptor_image.kernel_id
    else:
        # We are updating an encrypted image. We use the same kernel id
        kernel_id = image.kernel_id
    # Register the new AMI.
    if encrypted_ami_name:
        name = encrypted_ami_name
    else:
        name = _append_suffix(
            image.name,
            _get_encrypted_suffix(),
            max_length=AMI_NAME_MAX_LENGTH
        )
    if image.description:
        suffix = SUFFIX_ENCRYPTED_IMAGE % {'image_id': image_id}
        description = _append_suffix(
            image.description, suffix, max_length=255)
    else:
        description = DEFAULT_DESCRIPTION_ENCRYPTED_IMAGE % {
            'image_id': image_id
        }

    try:
        ami = aws_svc.register_image(
            name=name,
            description=description,
            kernel_id=kernel_id,
            block_device_map=new_bdm
        )
        log.info('Registered AMI %s based on the snapshots.', ami)
    except EC2ResponseError, e:
        # Sometimes register_image fails with an InvalidAMIID.NotFound
        # error and a message like "The image id '[ami-f9fcf3c9]' does not
        # exist".  In that case, just go ahead with that AMI id. We'll
        # wait for it to be created later (in wait_for_image).
        log.info(
            'Attempting to recover from image registration error: %s', e)
        if e.error_code == 'InvalidAMIID.NotFound':
            # pull the AMI ID out of the exception message if we can
            m = re.search('ami-[a-f0-9]{8}', e.message)
            if m:
                ami = m.group(0)
                log.info('Recovered with AMI ID %s', ami)
        if not ami:
            raise
    _wait_for_image(aws_svc, ami)
    aws_svc.create_tags(ami)
    log.info('Created encrypted AMI %s based on %s', ami, image_id)
    return ami


def encrypt(aws_svc, enc_svc_cls, image_id, encryptor_ami,
            encrypted_ami_name=None, subnet_id=None,
            security_group_ids=None):
    encryptor_instance = None
    ami = None
    snapshot_id = None
    temp_sg_id = None
    snapshotter_instance = None
    terminated_instance_ids = set()

    try:
        snapshotter_instance = run_snapshotter_instance(
            aws_svc, image_id, subnet_id=subnet_id)
        snapshot_id, root_dev, size, vol_type, iops = _snapshot_root_volume(
            aws_svc, snapshotter_instance, image_id
        )
        terminate_instance(
            aws_svc,
            id=snapshotter_instance.id,
            name='snapshotter',
            terminated_instance_ids=terminated_instance_ids
        )
        snapshotter_instance = None

        if not security_group_ids:
            vpc_id = None
            if subnet_id:
                subnet = aws_svc.get_subnet(subnet_id)
                vpc_id = subnet.vpc_id
            temp_sg_id = create_encryptor_security_group(
                aws_svc, vpc_id=vpc_id).id
            security_group_ids = [temp_sg_id]

        encryptor_instance = run_encryptor_instance(
            aws_svc=aws_svc,
            encryptor_image_id=encryptor_ami,
            snapshot=snapshot_id,
            root_size=size,
            guest_image_id=image_id,
            security_group_ids=security_group_ids,
            subnet_id=subnet_id
        )

        host_ip = (
            encryptor_instance.ip_address or
            encryptor_instance.private_ip_address
        )
        enc_svc = enc_svc_cls(host_ip)
        log.info('Waiting for encryption service on %s at %s',
                 encryptor_instance.id, host_ip)
        wait_for_encryptor_up(enc_svc, Deadline(600))
        log.info('Creating encrypted root drive.')
        try:
            wait_for_encryption(enc_svc)
        except EncryptionError as e:
            log.error(
                'Encryption failed.  Check console output of instance %s '
                'for details.',
                encryptor_instance.id
            )

            e.console_output_file = write_console_output(
                aws_svc, encryptor_instance.id)
            if e.console_output_file:
                log.error(
                    'Wrote console output for instance %s to %s',
                    encryptor_instance.id,
                    e.console_output_file.name
                )
            else:
                log.error(
                    'Encryptor console output is not currently available.  '
                    'Wait a minute and check the console output for '
                    'instance %s in the EC2 Management '
                    'Console.',
                    encryptor_instance.id
                )
            raise e

        bdm = encryptor_instance.block_device_mapping

        # Stop the encryptor instance.  Wait for it to stop before
        # taking snapshots.
        log.info('Stopping encryptor instance %s', encryptor_instance.id)
        aws_svc.stop_instance(encryptor_instance.id)
        _wait_for_instance(aws_svc, encryptor_instance.id, state='stopped')

        description = DESCRIPTION_SNAPSHOT % {'image_id': image_id}

        # Snapshot volumes.
        snap_guest = aws_svc.create_snapshot(
            bdm['/dev/sda5'].volume_id,
            name=NAME_ENCRYPTED_ROOT_SNAPSHOT,
            description=description
        )
        snap_bsd = aws_svc.create_snapshot(
            bdm['/dev/sda2'].volume_id,
            name=NAME_METAVISOR_ROOT_SNAPSHOT,
            description=description
        )
        snap_grub = aws_svc.create_snapshot(
            bdm['/dev/sda1'].volume_id,
            name=NAME_METAVISOR_GRUB_SNAPSHOT,
            description=description
        )
        snap_log = aws_svc.create_snapshot(
            bdm['/dev/sda3'].volume_id,
            name=NAME_METAVISOR_LOG_SNAPSHOT,
            description=description
        )

        log.info(
            'Creating snapshots for the new encrypted AMI: %s, %s, %s, %s',
            snap_guest.id, snap_bsd.id, snap_grub.id, snap_log.id)

        wait_for_snapshots(
            aws_svc, snap_guest.id, snap_bsd.id, snap_grub.id, snap_log.id)

        terminate_instance(
            aws_svc,
            id=encryptor_instance.id,
            name='encryptor',
            terminated_instance_ids=terminated_instance_ids
        )
        encryptor_instance = None
        ami = register_new_ami(
            aws_svc,
            snap_grub,
            snap_bsd,
            snap_log,
            snap_guest,
            vol_type,
            iops,
            image_id,
            encryptor_ami=encryptor_ami,
            encrypted_ami_name=encrypted_ami_name)
    finally:
        instance_ids = []
        if snapshotter_instance:
            instance_ids.append(snapshotter_instance.id)
        if encryptor_instance:
            instance_ids.append(encryptor_instance.id)

        # Delete volumes explicitly.  They should get cleaned up during
        # instance deletion, but we've gotten reports that occasionally
        # volumes can get orphaned.
        volume_ids = None
        try:
            volumes = aws_svc.get_volumes(
                tag_key=TAG_ENCRYPTOR_SESSION_ID,
                tag_value=aws_svc.session_id
            )
            volume_ids = [v.id for v in volumes]
        except EC2ResponseError as e:
            log.warn('Unable to clean up orphaned volumes: %s', e)
        except Exception as e:
            log.exception('Unable to clean up orphaned volumes')

        sg_ids = []
        if temp_sg_id:
            sg_ids.append(temp_sg_id)
        snapshot_ids = []
        if snapshot_id:
            snapshot_ids.append(snapshot_id)

        _clean_up(
            aws_svc,
            instance_ids=instance_ids,
            volume_ids=volume_ids,
            snapshot_ids=snapshot_ids,
            security_group_ids=sg_ids
        )

    log.info('Done.')
    return ami