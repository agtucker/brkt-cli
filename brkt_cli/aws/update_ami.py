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
Create an encrypted AMI (with new metavisor) based
on an existing encrypted AMI.

Before running brkt updaet-encrypted-ami, set the AWS_ACCESS_KEY_ID and
AWS_SECRET_ACCESS_KEY environment variables, like you would when
running the AWS command line utility.
"""

import json
import logging
import os

from brkt_cli import encryptor_service
from brkt_cli.aws import boto3_device
from brkt_cli.aws.aws_constants import (
    NAME_GUEST_CREATOR, DESCRIPTION_GUEST_CREATOR, NAME_METAVISOR_UPDATER,
    DESCRIPTION_METAVISOR_UPDATER,
    NAME_METAVISOR_ROOT_SNAPSHOT, NAME_ENCRYPTED_ROOT_SNAPSHOT)
from brkt_cli.aws.aws_service import (
    wait_for_instance, wait_for_image, create_encryptor_security_group,
    clean_up,
    wait_for_volume_attached,
    stop_and_wait, log_exception_console)
from brkt_cli.encryptor_service import (
    wait_for_encryptor_up,
    wait_for_encryption
)
from brkt_cli.instance_config import (
    InstanceConfig,
    INSTANCE_UPDATER_MODE,
)
from brkt_cli.user_data import gzip_user_data
from brkt_cli.util import Deadline

log = logging.getLogger(__name__)

MV_ROOT_DEVICE_NAME = '/dev/sda1'
GUEST_ROOT_DEVICE_NAME = '/dev/sdf'


def update_ami(aws_svc, encrypted_ami, updater_ami, encrypted_ami_name,
               subnet_id=None, security_group_ids=None,
               enc_svc_class=encryptor_service.EncryptorService,
               guest_instance_type='m4.large',
               updater_instance_type='m4.large',
               instance_config=None,
               status_port=encryptor_service.ENCRYPTOR_STATUS_PORT):
    encrypted_guest = None
    updater = None
    new_mv_vol_id = None
    temp_sg_id = None
    if instance_config is None:
        instance_config = InstanceConfig(mode=INSTANCE_UPDATER_MODE)

    try:
        guest_image = aws_svc.get_image(encrypted_ami)

        # Step 1. Launch encrypted guest AMI
        # Use 'updater' mode to avoid chain loading the guest
        # automatically. We just want this AMI/instance up as the
        # base to create a new AMI and preserve license
        # information embedded in the guest AMI
        log.info("Launching encrypted guest/updater")

        instance_config.brkt_config['status_port'] = status_port

        encrypted_guest = aws_svc.run_instance(
            encrypted_ami,
            instance_type=guest_instance_type,
            ebs_optimized=False,
            subnet_id=subnet_id,
            user_data=json.dumps(instance_config.brkt_config))
        aws_svc.create_tags(
            encrypted_guest.id,
            name=NAME_GUEST_CREATOR,
            description=DESCRIPTION_GUEST_CREATOR % {'image_id': encrypted_ami}
        )
        # Run updater in same zone as guest so we can swap volumes

        user_data = instance_config.make_userdata()
        compressed_user_data = gzip_user_data(user_data)

        # If the user didn't specify a security group, create a temporary
        # security group that allows brkt-cli to get status from the updater.
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

        updater = run_instance(
            updater_ami,
            instance_type=updater_instance_type,
            user_data=compressed_user_data,
            ebs_optimized=False,
            subnet_id=subnet_id,
            placement=encrypted_guest.placement,
            security_group_ids=security_group_ids)
        aws_svc.create_tags(
            updater.id,
            name=NAME_METAVISOR_UPDATER,
            description=DESCRIPTION_METAVISOR_UPDATER,
        )
        wait_for_instance(aws_svc, encrypted_guest.id, state="running")
        log.info(
            "Launched guest %s, updater %s", encrypted_guest.id, updater.id)

        # Step 2. Wait for the updater to finish and stop the instances
        aws_svc.stop_instance(encrypted_guest.id)

        updater = wait_for_instance(aws_svc, updater.id, state="running")
        host_ips = []
        if updater.public_ip_address:
            host_ips.append(updater.public_ip_address)
        if updater.private_ip_address:
            host_ips.append(updater.private_ip_address)
            log.info('Adding %s to NO_PROXY environment variable' %
                 updater.private_ip_address)
            if os.environ.get('NO_PROXY'):
                os.environ['NO_PROXY'] += "," + \
                    updater.private_ip_address
            else:
                os.environ['NO_PROXY'] = updater.private_ip_address

        # Wait for the encryption service to start up, so that we know that
        # Metavisor is done initializing.
        enc_svc = enc_svc_class(host_ips, port=status_port)
        log.info('Waiting for updater service on %s (port %s on %s)',
                 updater.id, enc_svc.port, ', '.join(host_ips))
        try:
            wait_for_encryptor_up(enc_svc, Deadline(600))
        except:
            log.error('Unable to connect to encryptor instance.')
            raise

        try:
            wait_for_encryption(enc_svc)
        except Exception as e:
            # Stop the updater instance, to make the console log available.
            stop_and_wait(aws_svc, updater.id)
            log_exception_console(aws_svc, e, updater.id)
            raise

        aws_svc.stop_instance(updater.id)
        encrypted_guest = wait_for_instance(
            aws_svc, encrypted_guest.id, state="stopped")
        updater = wait_for_instance(aws_svc, updater.id, state="stopped")

        guest_bdm = encrypted_guest.block_device_mappings
        updater_bdm = updater.block_device_mappings
        new_bdm = list()

        # Step 3. Create block device mappings for the new image.  Preserve
        # volume properties that may get reset to their defaults while
        # updating block device mappings.
        for d in guest_bdm:
            name = d['DeviceName']
            vol_id = d['Ebs']['VolumeId']
            vol = aws_svc.get_volume(vol_id)
            new_dev = boto3_device.make_device_for_image(d)

            # Preserve volume type (YETI-942).
            log.debug(
                'Preserving volume type %s for disk %s',
                vol.volume_type,
                name
            )
            new_dev['Ebs']['VolumeType'] = vol.volume_type

            # Preserve IOPS for guest root volume (YETI-1334).
            if d == GUEST_ROOT_DEVICE_NAME and vol.volume_type == 'io1':
                log.debug(
                    'Preserving IOPS setting %s for %s',
                    vol.iops,
                    d
                )
                new_dev['Ebs']['Iops'] = vol.iops

            if name == MV_ROOT_DEVICE_NAME:
                new_dev['Ebs']['DeleteOnTermination'] = True
                new_dev['Ebs']['VolumeType'] = 'gp2'

            new_bdm.append(new_dev)

        # Step 4. Detach old BSD drive(s) and delete from encrypted guest
        old_mv_dev = boto3_device.get_device(guest_bdm, MV_ROOT_DEVICE_NAME)
        old_mv_vol_id = old_mv_dev['Ebs']['VolumeId']

        log.info(
            'Detaching old metavisor disk %s from %s',
            old_mv_vol_id,
            encrypted_guest.id
        )
        aws_svc.detach_volume(
            old_mv_vol_id,
            instance_id=encrypted_guest.id,
            force=True
        )
        aws_svc.delete_volume(old_mv_vol_id)

        # Step 5. Detach the Metavisor root from the updater instance.
        log.info('Detaching boot volume from %s', updater.id)
        new_mv_dev = boto3_device.get_device(updater_bdm, MV_ROOT_DEVICE_NAME)
        new_mv_vol_id = new_mv_dev['Ebs']['VolumeId']
        aws_svc.detach_volume(
            new_mv_vol_id,
            instance_id=updater.id,
            force=True
        )

        # Step 6. Attach new boot disk to guest instance.
        log.info(
            'Attaching new metavisor boot disk %s to %s',
            new_mv_vol_id,
            encrypted_guest.id
        )
        aws_svc.attach_volume(
            new_mv_vol_id,
            encrypted_guest.id,
            MV_ROOT_DEVICE_NAME
        )
        encrypted_guest = wait_for_volume_attached(
            aws_svc, encrypted_guest.id, MV_ROOT_DEVICE_NAME)

        # Step 7. Create new AMI.
        log.info("Creating new AMI")
        image = aws_svc.create_image(
            encrypted_guest.id,
            encrypted_ami_name,
            description=guest_image.description,
            no_reboot=True,
            block_device_mappings=new_bdm
        )
        image = wait_for_image(aws_svc, image.id)

        # Step 8. Tag the snapshots and image that we just created.
        mv_root_dev = boto3_device.get_device(
            image.block_device_mappings,
            MV_ROOT_DEVICE_NAME
        )
        guest_dev = boto3_device.get_device(
            image.block_device_mappings,
            GUEST_ROOT_DEVICE_NAME
        )
        aws_svc.create_tags(
            mv_root_dev['Ebs']['SnapshotId'],
            name=NAME_METAVISOR_ROOT_SNAPSHOT,
        )

        aws_svc.create_tags(
            guest_dev['Ebs']['SnapshotId'],
            name=NAME_ENCRYPTED_ROOT_SNAPSHOT,
        )
        aws_svc.create_tags(image.id)

        return image.id
    finally:
        instance_ids = set()
        volume_ids = set()
        sg_ids = set()

        if encrypted_guest:
            instance_ids.add(encrypted_guest.id)
        if updater:
            instance_ids.add(updater.id)
        if new_mv_vol_id:
            volume_ids.add(new_mv_vol_id)
        if temp_sg_id:
            sg_ids.add(temp_sg_id)

        clean_up(aws_svc,
                 instance_ids=instance_ids,
                 volume_ids=volume_ids,
                 security_group_ids=sg_ids)
