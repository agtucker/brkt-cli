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
Create an encrypted AMI based on an existing unencrypted AMI.

Overview of the process:
    * Start an instance based on the unencrypted guest AMI.
    * Stop that instance
    * Snapshot the root volume of the unencrypted instance.
    * Start a Bracket Encryptor instance.
    * Attach the unencrypted root volume to the Encryptor instance.
    * The Bracket Encryptor copies the unencrypted root volume to a new
        encrypted volume that's 2x the size of the original.
    * Detach the Bracket Encryptor root volume
    * Snapshot the Bracket Encryptor system volumes and the new encrypted
        root volume.
    * Attach the Bracket Encryptor root volume to the stopped guest instance
    * Create a new AMI based on the snapshots and stopped guest instance.
    * Terminate the Bracket Encryptor instance.
    * Terminate the original guest instance.
    * Delete the unencrypted snapshot.

Before running brkt encrypt-ami, set the AWS_ACCESS_KEY_ID and
AWS_SECRET_ACCESS_KEY environment variables, like you would when
running the AWS command line utility.
"""
import logging
import os

from botocore.exceptions import ClientError

from brkt_cli import encryptor_service, util
from brkt_cli.aws import aws_service, boto3_device
from brkt_cli.aws.aws_constants import (
    NAME_ENCRYPTOR, DESCRIPTION_ENCRYPTOR,
    NAME_METAVISOR_ROOT_SNAPSHOT, NAME_ENCRYPTED_ROOT_VOLUME,
    NAME_METAVISOR_ROOT_VOLUME, NAME_ENCRYPTED_IMAGE_SUFFIX,
    SUFFIX_ENCRYPTED_IMAGE, DEFAULT_DESCRIPTION_ENCRYPTED_IMAGE,
    TAG_ENCRYPTOR, TAG_ENCRYPTOR_SESSION_ID, TAG_ENCRYPTOR_AMI
)
from brkt_cli.aws.aws_service import (
    wait_for_instance, stop_and_wait,
    wait_for_image, create_encryptor_security_group, run_guest_instance,
    clean_up, log_exception_console, snapshot_log_volume,
    wait_for_volume_attached, snapshot_root_volume)
from brkt_cli.instance_config import InstanceConfig
from brkt_cli.user_data import gzip_user_data
from brkt_cli.util import (
    BracketError,
    Deadline,
    make_nonce,
    append_suffix,
    CRYPTO_XTS
)

log = logging.getLogger(__name__)

AMI_NAME_MAX_LENGTH = 128


def get_default_tags(session_id, encryptor_ami):
    default_tags = {
        TAG_ENCRYPTOR: 'True',
        TAG_ENCRYPTOR_SESSION_ID: session_id,
        TAG_ENCRYPTOR_AMI: encryptor_ami
    }
    return default_tags


def get_encrypted_suffix():
    """ Return a suffix that will be appended to the encrypted image name.
    The suffix is in the format "(encrypted 787ace7a)".  The nonce portion of
    the suffix is necessary because Amazon requires image names to be unique.
    """
    return NAME_ENCRYPTED_IMAGE_SUFFIX % {'nonce': make_nonce()}


def _get_name_from_image(image):
    name = append_suffix(
        image.name,
        get_encrypted_suffix(),
        max_length=AMI_NAME_MAX_LENGTH
    )
    return name


def _get_description_from_image(image):
    if image.description:
        suffix = SUFFIX_ENCRYPTED_IMAGE % {'image_id': image.id}
        description = append_suffix(
            image.description, suffix, max_length=255)
    else:
        description = DEFAULT_DESCRIPTION_ENCRYPTED_IMAGE % {
            'image_id': image.id
        }
    return description


def _run_encryptor_instance(
        aws_svc, encryptor_image_id, snapshot, root_size, guest_image_id,
        crypto_policy, security_group_ids=None, subnet_id=None, placement=None,
        instance_config=None,
        status_port=encryptor_service.ENCRYPTOR_STATUS_PORT):

    if instance_config is None:
        instance_config = InstanceConfig()
    instance_config.brkt_config['crypto_policy_type'] = crypto_policy

    # Use 'sd' names even though AWS maps these to 'xvd'
    # The AWS GUI only exposes 'sd' names, and won't allow
    # the user to attach to an existing 'sd' name in use, but
    # would allow conflicts if we used 'xvd' names here.

    # Use gp2 for fast burst I/O copying root drive
    guest_unencrypted_root = boto3_device.make_device(
        device_name='/dev/sdf',
        volume_type='gp2',
        snapshot_id=snapshot,
        delete_on_termination=True
    )

    # Use gp2 for fast burst I/O copying root drive
    log.info('Launching encryptor instance with snapshot %s', snapshot)
    # They are creating an encrypted AMI instead of updating it
    # Use gp2 for fast burst I/O copying root drive

    guest_encrypted_root_size = 2 * root_size + 6
    if crypto_policy == CRYPTO_XTS:
        guest_encrypted_root_size = root_size + 6

    guest_encrypted_root = boto3_device.make_device(
        device_name='/dev/sdg',
        volume_type='gp2',
        volume_size=guest_encrypted_root_size,
        delete_on_termination=True
    )

    # If security groups were not specified, create a temporary security
    # group that allows us to poll the metavisor for encryption progress.
    temp_sg_id = None
    instance = None

    try:
        run_instance = aws_svc.run_instance

        if not security_group_ids:
            vpc_id = None
            if subnet_id:
                subnet = aws_svc.get_subnet(subnet_id)
                vpc_id = subnet.vpc_id
            temp_sg_id = create_encryptor_security_group(
                aws_svc, vpc_id=vpc_id, status_port=status_port).id
            security_group_ids = [temp_sg_id]

            # Wrap with a retry, to handle eventual consistency issues with
            # the newly-created group.
            run_instance = aws_svc.retry(
                aws_svc.run_instance,
                error_code_regexp='InvalidGroup\.NotFound'
            )

        user_data = instance_config.make_userdata()
        compressed_user_data = gzip_user_data(user_data)

        bdm = [guest_unencrypted_root, guest_encrypted_root]
        instance = run_instance(
            encryptor_image_id,
            security_group_ids=security_group_ids,
            user_data=compressed_user_data,
            placement=placement,
            block_device_mappings=bdm,
            subnet_id=subnet_id
        )
        aws_svc.create_tags(
            instance.id,
            name=NAME_ENCRYPTOR,
            description=DESCRIPTION_ENCRYPTOR % {'image_id': guest_image_id}
        )
        log.info('Launching encryptor instance %s', instance.id)
        instance = wait_for_instance(aws_svc, instance.id)

        # Tag volumes.
        mv_root_device = boto3_device.get_device(
            instance.block_device_mappings, '/dev/sda1')
        encrypted_root_device = boto3_device.get_device(
            instance.block_device_mappings, '/dev/sdg')
        aws_svc.create_tags(
            boto3_device.get_volume_id(
                mv_root_device), name=NAME_METAVISOR_ROOT_VOLUME)
        aws_svc.create_tags(
            boto3_device.get_volume_id(encrypted_root_device),
            name=NAME_ENCRYPTED_ROOT_VOLUME
        )
    except:
        cleanup_instance_ids = []
        cleanup_sg_ids = []
        if instance:
            cleanup_instance_ids = [instance.id]
        if temp_sg_id:
            cleanup_sg_ids = [temp_sg_id]
        clean_up(
            aws_svc,
            instance_ids=cleanup_instance_ids,
            security_group_ids=cleanup_sg_ids
        )
        raise

    return instance, temp_sg_id


def _terminate_instance(aws_svc, id, name, terminated_instance_ids):
    try:
        log.info('Terminating %s instance %s', name, id)
        aws_svc.terminate_instance(id)
        terminated_instance_ids.add(id)
    except Exception as e:
        log.warn('Could not terminate %s instance: %s', name, e)


def _snapshot_encrypted_instance(
        aws_svc, enc_svc_cls, encryptor_instance,
        image_id=None, vol_type=None, iops=None,
        legacy=False, save_encryptor_logs=True,
        status_port=encryptor_service.ENCRYPTOR_STATUS_PORT):
    # First wait for encryption to complete
    host_ips = []
    if encryptor_instance.public_ip_address:
        host_ips.append(encryptor_instance.public_ip_address)
    if encryptor_instance.private_ip_address:
        host_ips.append(encryptor_instance.private_ip_address)
        log.info('Adding %s to NO_PROXY environment variable' %
                 encryptor_instance.private_ip_address)
        if os.environ.get('NO_PROXY'):
            os.environ['NO_PROXY'] += "," + \
                encryptor_instance.private_ip_address
        else:
            os.environ['NO_PROXY'] = encryptor_instance.private_ip_address

    enc_svc = enc_svc_cls(host_ips, port=status_port)
    log.info('Waiting for encryption service on %s (port %s on %s)',
             encryptor_instance.id, enc_svc.port, ', '.join(host_ips))
    try:
        encryptor_service.wait_for_encryptor_up(enc_svc, Deadline(600))
    except:
        log.error('Unable to connect to encryptor instance.')
        raise

    try:
        log.info('Creating encrypted root drive.')
        encryptor_service.wait_for_encryption(enc_svc)
    except (BracketError, encryptor_service.EncryptionError) as e:
        # Stop the encryptor instance, to make the console log available.
        stop_and_wait(aws_svc, encryptor_instance.id)

        log_exception_console(aws_svc, e, encryptor_instance.id)
        if save_encryptor_logs:
            log.info('Saving logs from encryptor instance in snapshot')
            log_snapshot = snapshot_log_volume(aws_svc, encryptor_instance.id)
            log.info('Encryptor logs saved in snapshot %(snapshot_id)s. '
                     'Run `brkt share-logs --region %(region)s '
                     '--snapshot-id %(snapshot_id)s` '
                     'to share this snapshot with Bracket support' %
                     {'snapshot_id': log_snapshot.id,
                      'region': aws_svc.region})
        raise

    log.info('Encrypted root drive is ready.')
    # The encryptor instance may modify its volume attachments while running,
    # so we update the encryptor instance's local attributes before reading
    # them.
    encryptor_instance = aws_svc.get_instance(encryptor_instance.id)

    # Stop the encryptor instance.
    log.info('Stopping encryptor instance %s', encryptor_instance.id)
    aws_svc.stop_instance(encryptor_instance.id)
    wait_for_instance(aws_svc, encryptor_instance.id, state='stopped')

    # Set up new Block Device Mappings
    log.debug('Creating block device mapping')
    if not vol_type:
        vol_type = 'gp2'

    # The once empty disk is the one and only disk
    mv_root_dev = boto3_device.get_device(
        encryptor_instance.block_device_mappings, '/dev/sdg')
    mv_root_id = boto3_device.get_volume_id(mv_root_dev)

    if not legacy:
        log.info('Detaching Metavisor root from Encryptor.')
        aws_svc.detach_volume(
            mv_root_id,
            instance_id=encryptor_instance.id,
            force=True
        )
        aws_service.wait_for_volume(aws_svc, mv_root_id)
        aws_svc.create_tags(
            mv_root_id, name=NAME_METAVISOR_ROOT_VOLUME)

    if image_id:
        log.debug('Getting image %s', image_id)
        guest_image = aws_svc.get_image(image_id)
        if guest_image is None:
            raise BracketError("Can't find image %s" % image_id)

        # Propagate any ephemeral drive mappings to the soloized image
        guest_device_names = boto3_device.get_device_names(
            guest_image.block_device_mappings)
        for guest_dev_name in guest_device_names:
            guest_dev = boto3_device.get_device(
                guest_image.block_device_mappings, guest_dev_name)
            vn = guest_dev.get('VirtualName')
            if vn:
                log.info('Propagating block device mapping for %s at %s',
                         vn, guest_dev_name)
                new_bdm.append(guest_dev)

    return mv_root_id, new_bdm


def _register_ami(aws_svc, encryptor_instance, name,
                  description, mv_bdm=None, legacy=False, guest_instance=None,
                  mv_root_id=None):
    # mv_bdm contains the encrypted guest volume.
    if not mv_bdm:
        mv_bdm = list()
    # Register the new AMI.

    if legacy:
        # The encryptor instance may modify its volume attachments while
        # running, so we update the encryptor instance's local attributes
        # before reading them.
        encryptor_instance = aws_svc.get_instance(encryptor_instance.id)
        guest_id = encryptor_instance.id
        # Explicitly detach/delete all but root drive
        for device_name in ['/dev/sda2', '/dev/sda3', '/dev/sda4',
                  '/dev/sda5', '/dev/sdf', '/dev/sdg']:
            dev = boto3_device.get_device(
                encryptor_instance.block_device_mappings, device_name)
            if not dev:
                continue
            volume_id = boto3_device.get_volume_id(dev)
            aws_svc.detach_volume(
                volume_id,
                instance_id=encryptor_instance.id,
                force=True
            )
            aws_service.wait_for_volume(aws_svc, volume_id)
            aws_svc.delete_volume(volume_id)
    else:
        guest_id = guest_instance.id
        root_device_name = guest_instance.root_device_name

        log.info('Attaching new Metavisor root.')

        # Explicitly attach new mv root to guest instance
        aws_svc.attach_volume(
            mv_root_id,
            guest_instance.id,
            root_device_name,
        )

        guest_instance = wait_for_volume_attached(
            aws_svc, guest_instance.id, root_device_name)
        root_dev = boto3_device.get_device(
            guest_instance.block_device_mappings, root_device_name)
        root_dev_copy = boto3_device.make_device_for_image(root_dev)
        root_dev_copy['Ebs']['DeleteOnTermination'] = True
        mv_bdm.append(root_dev_copy)

    guest_instance = aws_svc.get_instance(guest_id)

    # Legacy:
    #   Create AMI from (stopped) MV instance
    # Non-legacy:
    #   Create AMI from original (stopped) guest instance. This
    #   preserves any billing information found in
    #   the identity document (i.e. billingProduct)
    image = aws_svc.create_image(
        guest_id,
        name,
        description=description,
        no_reboot=True,
        block_device_mappings=mv_bdm
    )

    log.info('Registered %s based on the snapshots.', image.id)
    image = wait_for_image(aws_svc, image.id)

    snap_dev = boto3_device.get_device(
        image.block_device_mappings, image.root_device_name)
    aws_svc.create_tags(
        boto3_device.get_snapshot_id(snap_dev),
        name=NAME_METAVISOR_ROOT_SNAPSHOT,
        description='Test snapshot'
    )
    aws_svc.create_tags(image.id)
    return image


def _print_bdm(context, resource):
    print context, util.pretty_print_json(resource.block_device_mappings)


def encrypt(aws_svc, enc_svc_cls, image_id, encryptor_ami, crypto_policy,
            encrypted_ami_name=None, subnet_id=None, security_group_ids=None,
            guest_instance_type='m4.large', instance_config=None,
            save_encryptor_logs=True,
            status_port=encryptor_service.ENCRYPTOR_STATUS_PORT,
            terminate_encryptor_on_failure=True):
    log.info('Starting encryptor session %s', aws_svc.session_id)

    encryptor_instance = None
    snapshot_id = None
    guest_instance = None
    temp_sg_id = None
    guest_image = aws_svc.get_image(image_id)
    mv_image = aws_svc.get_image(encryptor_ami)
    encrypted_image = None

    # Normal operation is both encryptor and guest match
    # on virtualization type, but we'll support a PV encryptor
    # and a HVM guest (legacy)
    log.debug('Guest type: %s Encryptor type: %s',
              guest_image.virtualization_type, mv_image.virtualization_type)
    if guest_image.virtualization_type != 'hvm':
        raise BracketError(
            'Unsupported virtualization type: %s' %
            guest_image.virtualization_type
        )
    legacy = False
    root_device_name = guest_image.root_device_name
    root_dev = boto3_device.get_device(
        guest_image.block_device_mappings, root_device_name)

    if not root_dev:
            log.warn("AMI must have root_device_name in block_device_mapping "
                     "in order to preserve guest OS license information")
            legacy = True
    if guest_image.root_device_name != "/dev/sda1":
        log.warn("Guest Operating System license information will not be "
                 "preserved because the root disk is attached at %s "
                 "instead of /dev/sda1", guest_image.root_device_name)
        legacy = True

    try:
        guest_instance = run_guest_instance(
            aws_svc,
            image_id,
            subnet_id=subnet_id,
            instance_type=guest_instance_type
        )

        wait_for_instance(aws_svc, guest_instance.id)
        snapshot_id, root_dev, size, vol_type, iops = snapshot_root_volume(
            aws_svc, guest_instance, image_id
        )

        guest_instance = aws_svc.get_instance(guest_instance.id)
        encryptor_instance, temp_sg_id = _run_encryptor_instance(
            aws_svc=aws_svc,
            encryptor_image_id=encryptor_ami,
            snapshot=snapshot_id,
            root_size=size,
            guest_image_id=image_id,
            crypto_policy=crypto_policy,
            security_group_ids=security_group_ids,
            subnet_id=subnet_id,
            placement=guest_instance.placement,
            instance_config=instance_config,
            status_port=status_port
        )

        log.debug('Getting image %s', image_id)
        image = aws_svc.get_image(image_id)
        if image is None:
            raise BracketError("Can't find image %s" % image_id)
        if encrypted_ami_name:
            name = encrypted_ami_name
        else:
            name = _get_name_from_image(image)
        description = _get_description_from_image(image)

        mv_root_id, mv_bdm = _snapshot_encrypted_instance(
            aws_svc,
            enc_svc_cls,
            encryptor_instance,
            image_id=image_id,
            vol_type=vol_type,
            iops=iops,
            legacy=legacy,
            save_encryptor_logs=save_encryptor_logs,
            status_port=status_port
        )

        guest_instance = aws_svc.get_instance(guest_instance.id)

        if guest_instance.sriov_net_support != "simple":
            log.info('Enabling sriovNetSupport for guest instance %s',
                     guest_instance.id)
            try:
                aws_svc.modify_instance_attribute(
                    guest_instance.id,
                    "sriovNetSupport",
                    "simple")
                log.info('sriovNetSupport enabled successfully')
            except ClientError as e:
                log.warn('Unable to enable sriovNetSupport for guest '
                         'instance %s with error %s', guest_instance.id, e)

        encrypted_image = _register_ami(
            aws_svc,
            encryptor_instance,
            name,
            description,
            legacy=legacy,
            guest_instance=guest_instance,
            mv_root_id=mv_root_id,
            mv_bdm=mv_bdm
        )
        log.info('Created encrypted AMI %s based on %s',
                 encrypted_image.id, image_id)
        return encrypted_image.id
    finally:
        instance_ids = []
        if guest_instance:
            instance_ids.append(guest_instance.id)

        terminate_encryptor = (
            encryptor_instance and
            (encrypted_image or terminate_encryptor_on_failure)
        )

        if terminate_encryptor:
            instance_ids.append(encryptor_instance.id)
        elif encryptor_instance:
            log.info('Not terminating encryptor instance %s',
                     encryptor_instance.id)

        # Delete volumes explicitly.  They should get cleaned up during
        # instance deletion, but we've gotten reports that occasionally
        # volumes can get orphaned.
        #
        # We can't do this if we're keeping the encryptor instance around,
        # since its volumes will still be attached.
        volume_ids = None
        if terminate_encryptor:
            try:
                volumes = aws_svc.get_volumes(
                    tag_key=TAG_ENCRYPTOR_SESSION_ID,
                    tag_value=aws_svc.session_id
                )
                volume_ids = [v.id for v in volumes]
            except ClientError as e:
                log.warn('Unable to clean up orphaned volumes: %s', e)
            except:
                log.exception('Unable to clean up orphaned volumes')

        sg_ids = []
        if temp_sg_id and terminate_encryptor:
            sg_ids.append(temp_sg_id)

        snapshot_ids = []
        if snapshot_id:
            snapshot_ids.append(snapshot_id)

        clean_up(
            aws_svc,
            instance_ids=instance_ids,
            volume_ids=volume_ids,
            snapshot_ids=snapshot_ids,
            security_group_ids=sg_ids
        )
