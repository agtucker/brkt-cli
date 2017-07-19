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
Create an Bracket wrapped based on an existing unencrypted AMI.

Overview of the process:
    * Obtain the Bracket (metavisor) image to be used
    * Obtain the root volume snapshot of the guest image
    * When lacking "create volume" permissions on the guest image
        create a local snapshot of the guest image
    * Configure the Bracket image to be launched with the guest
        root volume attached at /dev/sdf
    * Pass appropriate user-data to the Bracket image to indicate
        that the guest volume is unencrypted
    * Launch the Bracket image

Before running brkt encrypt-ami, set the AWS_ACCESS_KEY_ID and
AWS_SECRET_ACCESS_KEY environment variables, like you would when
running the AWS command line utility.
"""

import logging

from botocore.exceptions import ClientError

from brkt_cli.aws import aws_service, boto3_device
from brkt_cli.aws.aws_service import (
    EBS_OPTIMIZED_INSTANCES, wait_for_instance, run_guest_instance, clean_up,
    snapshot_root_volume)
from brkt_cli.instance_config import InstanceConfig
from brkt_cli.user_data import gzip_user_data
from brkt_cli.util import make_nonce, append_suffix

# End user-visible terminology.  These are resource names and descriptions
# that the user will see in his or her EC2 console.

# Security group names
NAME_INSTANCE_SECURITY_GROUP = 'Bracket Wrapped %(nonce)s'
DESCRIPTION_INSTANCE_SECURITY_GROUP = (
    "Allows SSH access to the Bracket wrapped instance.")

NAME_WRAPPED_IMAGE_SUFFIX = ' (wrapped %(nonce)s)'

INSTANCE_NAME_MAX_LENGTH = 128

log = logging.getLogger(__name__)


def get_wrapped_suffix():
    """ Return a suffix that will be appended to the wrapped instance name.
    The suffix is in the format "(wrapped 787ace7a)".
    """
    return NAME_WRAPPED_IMAGE_SUFFIX % {'nonce': make_nonce()}


def get_name_from_image(image):
    name = append_suffix(
        image.name,
        get_wrapped_suffix(),
        max_length=INSTANCE_NAME_MAX_LENGTH
    )
    return name


def create_instance_security_group(aws_svc, vpc_id=None):
    """ Creates a default security group to allow SSH access. This ensures
    that even if a security group is not specified in the arguments, the
    user can SSH in to the launched instance.
    """
    sg_name = NAME_INSTANCE_SECURITY_GROUP % {'nonce': make_nonce()}
    sg_desc = DESCRIPTION_INSTANCE_SECURITY_GROUP
    sg = aws_svc.create_security_group(sg_name, sg_desc, vpc_id=vpc_id)
    log.info('Created security group with id %s', sg.id)
    try:
        aws_svc.authorize_security_group_ingress(sg.id, port=22)
    except Exception as e:
        log.error('Failed adding security group rule to %s: %s', sg.id, e)
        clean_up(aws_svc, security_group_ids=[sg.id])

    aws_svc.create_tags(sg.id, name=sg_name, description=sg_desc)
    return sg


def launch_wrapped_image(aws_svc, image_id, metavisor_ami,
                         wrapped_instance_name=None, subnet_id=None,
                         security_group_ids=None, instance_type='m4.large',
                         instance_config=None, iam=None):
    guest_image = aws_svc.get_image(image_id)
    guest_root_device = boto3_device.get_device(
        guest_image.block_device_mappings,
        guest_image.root_device_name
    )
    guest_snapshot_id = guest_root_device['Ebs']['SnapshotId']
    mv_image = aws_svc.get_image(metavisor_ami)

    if wrapped_instance_name:
        instance_name = wrapped_instance_name
    else:
        instance_name = get_name_from_image(guest_image)

    try:
        aws_svc.get_snapshot(guest_snapshot_id)
    except ClientError as e:
        code, message = aws_service.get_code_and_message(e)
        if code == 'InvalidSnapshot.NotFound':
            log.info(
                "Insufficient permission to launch guest image. %s ",
                message
            )
            guest_instance = None
            try:
                guest_instance = run_guest_instance(aws_svc, image_id,
                    subnet_id=subnet_id, instance_type=instance_type)
                wait_for_instance(aws_svc, guest_instance.id)
                guest_snapshot_id, _, _, _, _ = snapshot_root_volume(
                    aws_svc, guest_instance, image_id)
            finally:
                if guest_instance:
                    clean_up(aws_svc, instance_ids=[guest_instance.id])
        else:
            log.error("Unable to wrap guest image", message)
            raise

    guest_unencrypted_root_dev = boto3_device.make_device(
        device_name='/dev/sdf',
        volume_type='gp2',
        snapshot_id=guest_snapshot_id,
        delete_on_termination=True
    )
    # Clean up the Metavisor root volume on termination
    mv_root_dev = boto3_device.make_device(
        device_name='/dev/sda1',
        delete_on_termination=True
    )
    bdm = [guest_unencrypted_root_dev, mv_root_dev]

    # Copy over the guest BlockDeviceMaping for ephemeral drives
    for dev in guest_image.block_device_mappings:
        virtual_name = dev.get('VirtualName')
        if virtual_name:
            log.info('Propagating block device mapping for %s at %s',
                     virtual_name, dev['DeviceName'])
            new_dev = boto3_device.make_device_for_image(dev)
            bdm.append(new_dev)

    if instance_config is None:
        instance_config = InstanceConfig()
    instance_config.brkt_config['allow_unencrypted_guest'] = True
    user_data = instance_config.make_userdata()
    compressed_user_data = gzip_user_data(user_data)
    try:
        if not security_group_ids:
            vpc_id = None
            if subnet_id:
                subnet = aws_svc.get_subnet(subnet_id)
                vpc_id = subnet.vpc_id
            temp_sg = create_instance_security_group(
                aws_svc, vpc_id=vpc_id)
            security_group_ids = [temp_sg.id]

        ebs_optimized = instance_type in EBS_OPTIMIZED_INSTANCES
        instance = aws_svc.run_instance(
            mv_image.id,
            instance_type=instance_type,
            security_group_ids=security_group_ids,
            user_data=compressed_user_data,
            placement=None,
            block_device_mappings=bdm,
            ebs_optimized=ebs_optimized,
            subnet_id=subnet_id,
            instance_profile_name=iam
        )
        aws_svc.create_tags(
            instance.id,
            name=instance_name
        )

        log.info('Launching wrapped guest instance %s', instance.id)
        instance = wait_for_instance(aws_svc, instance.id)
    finally:
        pass

    log.info('Done.')
    return instance
