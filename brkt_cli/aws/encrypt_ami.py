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

from boto.ec2.blockdevicemapping import (
    BlockDeviceMapping,
    EBSBlockDeviceType,
)
from boto.ec2.instance import InstanceAttribute
from boto.exception import EC2ResponseError

from brkt_cli import encryptor_service
from brkt_cli.aws import aws_service
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

# boto2 does not support this attribute, and this attribute needs to be
# queried for as metavisor does not support sriovNet
if 'sriovNetSupport' not in InstanceAttribute.ValidValues:
    InstanceAttribute.ValidValues.append('sriovNetSupport')


def get_default_tags(session_id, encryptor_ami):
    default_tags = {
        TAG_ENCRYPTOR: True,
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
        crypto_policy, security_group_ids=None, subnet_id=None, zone=None,
        instance_config=None,
        status_port=encryptor_service.ENCRYPTOR_STATUS_PORT):
    bdm = BlockDeviceMapping()

    if instance_config is None:
        instance_config = InstanceConfig()
    instance_config.brkt_config['crypto_policy_type'] = crypto_policy

    # Use gp2 for fast burst I/O copying root drive
    guest_unencrypted_root = EBSBlockDeviceType(
        volume_type='gp2',
        snapshot_id=snapshot,
        delete_on_termination=True)
    # Use gp2 for fast burst I/O copying root drive
    log.info('Launching encryptor instance with snapshot %s', snapshot)
    # They are creating an encrypted AMI instead of updating it
    # Use gp2 for fast burst I/O copying root drive
    guest_encrypted_root = EBSBlockDeviceType(
        volume_type='gp2',
        delete_on_termination=True)
    if crypto_policy == CRYPTO_XTS:
        guest_encrypted_root.size = root_size + 6
    else:
        guest_encrypted_root.size = 2 * root_size + 6

    # Use 'sd' names even though AWS maps these to 'xvd'
    # The AWS GUI only exposes 'sd' names, and won't allow
    # the user to attach to an existing 'sd' name in use, but
    # would allow conflicts if we used 'xvd' names here.
    bdm['/dev/sdf'] = guest_unencrypted_root
    bdm['/dev/sdg'] = guest_encrypted_root

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

        instance = run_instance(
            encryptor_image_id,
            security_group_ids=security_group_ids,
            user_data=compressed_user_data,
            placement=zone,
            block_device_map=bdm,
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
        bdm = instance.block_device_mapping
        aws_svc.create_tags(
            bdm['/dev/sda1'].volume_id, name=NAME_METAVISOR_ROOT_VOLUME)
        aws_svc.create_tags(
            bdm['/dev/sdg'].volume_id, name=NAME_ENCRYPTED_ROOT_VOLUME)
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
        encryptor_image, image_id=None, vol_type='', iops=None,
        legacy=False, save_encryptor_logs=True,
        status_port=encryptor_service.ENCRYPTOR_STATUS_PORT):
    # First wait for encryption to complete
    host_ips = []
    if encryptor_instance.ip_address:
        host_ips.append(encryptor_instance.ip_address)
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
    encryptor_bdm = encryptor_instance.block_device_mapping

    # Stop the encryptor instance.
    log.info('Stopping encryptor instance %s', encryptor_instance.id)
    aws_svc.stop_instance(encryptor_instance.id)
    wait_for_instance(aws_svc, encryptor_instance.id, state='stopped')

    # Set up new Block Device Mappings
    log.debug('Creating block device mapping')
    new_bdm = BlockDeviceMapping()
    if not vol_type or vol_type == '':
        vol_type = 'gp2'

    # The once empty disk is the one and only disk
    mv_root_id = encryptor_bdm['/dev/sdg'].volume_id

    if not legacy:
        log.info("Detaching new guest root %s" % (mv_root_id,))
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
        guest_bdm = guest_image.block_device_mapping
        for key in guest_bdm.keys():
            guest_vol = guest_bdm[key]
            if guest_vol.ephemeral_name:
                log.info('Propagating block device mapping for %s at %s' %
                         (guest_vol.ephemeral_name, key))
                new_bdm[key] = guest_vol

    return mv_root_id, new_bdm


def _register_ami(aws_svc, encryptor_instance, encryptor_image, name,
                  description, mv_bdm=None, legacy=False, guest_instance=None,
                  mv_root_id=None):
    if not mv_bdm:
        mv_bdm = BlockDeviceMapping()
    # Register the new AMI.
    if legacy:
        # The encryptor instance may modify its volume attachments while
        # running, so we update the encryptor instance's local attributes
        # before reading them.
        encryptor_instance = aws_svc.get_instance(encryptor_instance.id)
        guest_id = encryptor_instance.id
        # Explicitly detach/delete all but root drive
        bdm = encryptor_instance.block_device_mapping
        for d in ['/dev/sda2', '/dev/sda3', '/dev/sda4',
                  '/dev/sda5', '/dev/sdf', '/dev/sdg']:
            if not bdm.get(d):
                continue
            aws_svc.detach_volume(
                bdm[d].volume_id,
                instance_id=encryptor_instance.id,
                force=True
            )
            aws_service.wait_for_volume(aws_svc, bdm[d].volume_id)
            aws_svc.delete_volume(bdm[d].volume_id)
    else:
        guest_id = guest_instance.id
        root_device_name = guest_instance.root_device_name
        # Explicitly attach new mv root to guest instance
        log.info('Attaching %s to %s', mv_root_id, guest_instance.id)
        aws_svc.attach_volume(
            mv_root_id,
            guest_instance.id,
            root_device_name,
        )
        instance = wait_for_volume_attached(
            aws_svc, guest_instance.id, root_device_name)
        bdm = instance.block_device_mapping
        mv_bdm[root_device_name] = bdm[root_device_name]
        mv_bdm[root_device_name].delete_on_termination = True

    # Legacy:
    #   Create AMI from (stopped) MV instance
    # Non-legacy:
    #   Create AMI from original (stopped) guest instance. This
    #   preserves any billing information found in
    #   the identity document (i.e. billingProduct)
    ami = aws_svc.create_image(
        guest_id,
        name,
        description=description,
        no_reboot=True,
        block_device_mapping=mv_bdm
    )

    log.info('Registered AMI %s based on the snapshots.', ami)
    wait_for_image(aws_svc, ami)
    image = aws_svc.get_image(ami, retry=True)
    name = NAME_METAVISOR_ROOT_SNAPSHOT
    snap = image.block_device_mapping[image.root_device_name]
    aws_svc.create_tags(
        snap.snapshot_id,
        name=name,
        description=description
    )
    aws_svc.create_tags(ami)

    ami_info = {}
    ami_info['volume_device_map'] = []
    result_image = aws_svc.get_image(ami, retry=True)
    for attach_point, bdt in result_image.block_device_mapping.iteritems():
        if bdt.snapshot_id:
            bdt_snapshot = aws_svc.get_snapshot(bdt.snapshot_id)
            device_details = {
                'attach_point': attach_point,
                'description': bdt_snapshot.tags.get('Name', ''),
                'size': bdt_snapshot.volume_size
            }
            ami_info['volume_device_map'].append(device_details)

    ami_info['ami'] = ami
    ami_info['name'] = name
    return ami_info


def encrypt(aws_svc, enc_svc_cls, image_id, encryptor_ami, crypto_policy,
            encrypted_ami_name=None, subnet_id=None, security_group_ids=None,
            guest_instance_type='m4.large', instance_config=None,
            save_encryptor_logs=True,
            status_port=encryptor_service.ENCRYPTOR_STATUS_PORT,
            terminate_encryptor_on_failure=True):
    log.info('Starting encryptor session %s', aws_svc.session_id)

    encryptor_instance = None
    ami = None
    snapshot_id = None
    guest_instance = None
    temp_sg_id = None
    guest_image = aws_svc.get_image(image_id)
    mv_image = aws_svc.get_image(encryptor_ami)

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
    if not guest_image.block_device_mapping.get(root_device_name):
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

        net_sriov_attr = aws_svc.get_instance_attribute(guest_instance.id,
                                                        "sriovNetSupport")
        encryptor_instance, temp_sg_id = _run_encryptor_instance(
            aws_svc=aws_svc,
            encryptor_image_id=encryptor_ami,
            snapshot=snapshot_id,
            root_size=size,
            guest_image_id=image_id,
            crypto_policy=crypto_policy,
            security_group_ids=security_group_ids,
            subnet_id=subnet_id,
            zone=guest_instance.placement,
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
            mv_image,
            image_id=image_id,
            vol_type=vol_type,
            iops=iops,
            legacy=legacy,
            save_encryptor_logs=save_encryptor_logs,
            status_port=status_port
        )

        if net_sriov_attr.get("sriovNetSupport") != "simple":
            log.info('Enabling sriovNetSupport for guest instance %s',
                     guest_instance.id)
            try:
                ret = aws_svc.modify_instance_attribute(guest_instance.id,
                                                        "sriovNetSupport",
                                                        "simple")
                if ret:
                    log.info('sriovNetSupport enabled successfully')
                else:
                    log.info('Failed to enable sriovNetSupport')
            except EC2ResponseError as e:
                log.warn('Unable to enable sriovNetSupport for guest '
                         'instance %s with error %s', guest_instance.id, e)

        ami_info = _register_ami(
            aws_svc,
            encryptor_instance,
            mv_image,
            name,
            description,
            legacy=legacy,
            guest_instance=guest_instance,
            mv_root_id=mv_root_id,
            mv_bdm=mv_bdm
        )
        ami = ami_info['ami']
        log.info('Created encrypted AMI %s based on %s', ami, image_id)
    finally:
        instance_ids = []
        if guest_instance:
            instance_ids.append(guest_instance.id)

        terminate_encryptor = (
            encryptor_instance and
            (ami or terminate_encryptor_on_failure)
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
            except EC2ResponseError as e:
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

    log.info('Done.')
    return ami
