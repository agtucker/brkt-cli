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


def get_device(block_device_mappings, device_name):
    """ Return the block device with the given name.
    """
    for bdm in block_device_mappings:
        if bdm['DeviceName'] == device_name:
            # Return a copy of the dictionary, so that the caller doesn't
            # inadvertently modify the source data.
            return dict(bdm)
    return None


def get_device_names(block_device_mappings):
    """ Return all of the device names for the given block device
    mappings. """
    return [d['DeviceName'] for d in block_device_mappings]


def make_device(device_name=None, virtual_name=None, encrypted=None,
                delete_on_termination=None, iops=None, snapshot_id=None,
                volume_size=None, volume_type=None, no_device=None,
                volume_id=None):
    """ Return a dictionary that represents a boto3 block device. """
    d = dict()
    ebs = dict()

    if device_name:
        d['DeviceName'] = device_name
    if virtual_name:
        d['VirtualName'] = virtual_name
    if no_device:
        d['NoDevice'] = no_device

    if encrypted is not None:
        ebs['Encrypted'] = encrypted
    if delete_on_termination is not None:
        ebs['DeleteOnTermination'] = delete_on_termination
    if iops:
        ebs['Iops'] = iops
    if snapshot_id:
        ebs['SnapshotId'] = snapshot_id
    if volume_size:
        ebs['VolumeSize'] = volume_size
    if volume_type:
        ebs['VolumeType'] = volume_type
    if volume_id:
        ebs['VolumeId'] = volume_id

    d['Ebs'] = ebs
    return d


def get_volume_id(device):
    ebs = device.get('Ebs')
    if ebs:
        return ebs.get('VolumeId')
    return None


def get_snapshot_id(device):
    ebs = device.get('Ebs')
    if ebs:
        return ebs.get('SnapshotId')
    return None


def make_device_for_image(source_device):
    """ Return a copy of the given device that only contains the fields
    required for creating an image.  This removes other fields that cause
    the call to create_image() to fail.
    """
    return make_device(
        device_name=source_device.get('DeviceName'),
        virtual_name=source_device.get('VirtualName'),
        encrypted=source_device.get('Encrypted'),
        delete_on_termination=source_device.get('DeleteOnTermination'),
        iops=source_device.get('Iops'),
        snapshot_id=source_device.get('SnapshotId'),
        volume_size=source_device.get('VolumeSize'),
        volume_type=source_device.get('VolumeType'),
        no_device=source_device.get('NoDevice')
    )
