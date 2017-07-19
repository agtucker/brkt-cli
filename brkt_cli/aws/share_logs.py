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

import logging
import os
import boto
import re
import time

from brkt_cli.aws import aws_service, boto3_device
from brkt_cli.validation import ValidationError
from brkt_cli import util

log = logging.getLogger(__name__)


def share(aws_svc=None, logs_svc=None, instance_id=None, region=None,
          snapshot_id=None, bucket=None, path=None, subnet_id=None):

    log.info('Sharing logs')
    snapshot = None
    new_instance = None

    try:
        s3 = logs_svc.s3_connect()
        # Check bucket for file and bucket permissions
        bucket_exists = logs_svc.check_bucket_file(bucket, path, region, s3)

        if not bucket_exists:
            log.info('Creating new bucket')
            # If bucket isn't already owned create new one
            new_bucket = logs_svc.make_bucket(bucket, region, s3)
            # Reconnect with updated bucket list
            s3 = logs_svc.s3_connect()
            # Allow public write access to new bucket
            new_bucket.set_acl('public-read-write')

        if not snapshot_id:
            # Get instance from ID
            instance = aws_svc.get_instance(instance_id)
            # Find name of the root device
            root_name = instance.root_device_name
            # Get root volume ID
            root_dev = boto3_device.get_device(
                instance.block_device_mappings, root_name)
            # Create a snapshot of the root volume
            snapshot = aws_svc.create_snapshot(
                volume_id=root_dev['Ebs']['VolumeId'],
                name="temp-logs-snapshot"
            )
            # Wait for snapshot to post
            log.info('Waiting for snapshot...')
            aws_service.wait_for_snapshots(aws_svc, snapshot.id)

        else:  # Taking logs from a snapshot
            snapshot = aws_svc.get_snapshot(snapshot_id)

        # Split path name into path and file
        os.path.split(path)
        logs_file = os.path.basename(path)

        # Updates ACL on logs file object
        acl = '--no-sign-request --acl public-read-write'
        # Startup script for new instance
        # This creates logs file and copys to bucket
        amzn = '#!/bin/bash\n' + \
        'sudo mount -t ufs -o ro,ufstype=ufs2 /dev/xvdg4 /mnt\n' + \
        'sudo tar czvf /tmp/%s -C /mnt ./log ./crash\n' % (logs_file) + \
        'aws configure set default.s3.multipart_threshold 256MB\n' + \
        'aws s3 cp /tmp/%s s3://%s/%s %s\n' % (logs_file, bucket, path, acl)

        # Specifies volume to be attached to instance
        mv_disk = boto3_device.make_device(
            device_name='/dev/sdg',
            volume_type='gp2',
            snapshot_id=snapshot.id,
            delete_on_termination=True,
            volume_size=snapshot.volume_size
        )
        bdm = [mv_disk]

        # Images taken on 4/3/2017 from:
        # https://aws.amazon.com/amazon-linux-ami/
        IMAGES_BY_REGION = {
            "us-east-1": "ami-0b33d91d",
            "us-east-2": "ami-c55673a0",
            "us-west-1": "ami-165a0876",
            "us-west-2": "ami-f173cc91",
            "ap-south-1": "ami-f9daac96",
            "ap-northeast-2": "ami-dac312b4",
            "ap-southeast-1": "ami-dc9339bf",
            "ap-southeast-2": "ami-1c47407f",
            "ap-northeast-1": "ami-56d4ad31",
            "eu-central-1": "ami-af0fc0c0",
            "eu-west-1": "ami-70edb016",
            "eu-west-2": "ami-f1949e95",
        }

        image_id = IMAGES_BY_REGION[region]

        # Launch new instance, with volume and startup script

        new_instance = aws_svc.run_instance(
            image_id, instance_type='m4.large', block_device_mappings=bdm,
            user_data=amzn, ebs_optimized=False, subnet_id=subnet_id)

        # wait for instance to launch
        log.info('Waiting for instance...')
        aws_service.wait_for_instance(aws_svc, new_instance.id)

        # wait for file to upload
        log.info('Waiting for file to upload')
        logs_svc.wait_bucket_file(bucket, path, region, s3)
        log.info('Deleting new snapshot and instance')

    finally:
        if snapshot and new_instance:
            aws_service.clean_up(aws_svc, instance_ids=[new_instance.id],
                snapshot_ids=[snapshot.id])
        if snapshot and not new_instance:
            aws_service.clean_up(aws_svc, snapshot_ids=[snapshot.id])


class ShareLogsService():

    def wait_bucket_file(self, bucket, path, region, s3):
        bucket = s3.get_bucket(bucket)
        for i in range(40):
            if bucket.get_key(path):
                log.info('Logs available at https://s3-%s.amazonaws.com/%s/%s'
                    % (region, bucket.name, path))
                return
            else:
                time.sleep(7)
        raise util.BracketError("Can't upload logs file")

    def make_bucket(self, bucket, region, s3):
        # Since bucket isn't owned, create it
        return s3.create_bucket(bucket, location=region)

    def check_bucket_file(self, bucket, path, region, s3):
        # go through all owned buckets
        for b in s3.get_all_buckets():
            # Check if users owns a bucket matching input
            if bucket == b.name:
                try:
                    bucket = s3.get_bucket(bucket)
                except boto.exception.S3ResponseError as e:
                    code = e.status
                    # If bucket is owned, but in wrong region
                    if code == 400:
                        raise ValidationError("Bucket must be in %s" % region)
                    raise
                logs_file = bucket.get_key(path)
                # check for a matching file in bucket
                if logs_file:
                    raise ValidationError(
                        "File already exists, delete and retry")
                # check that everyone has write access to bucket
                acp = bucket.get_acl()
                for grant in acp.acl.grants:
                    perm = grant.permission
                    uri = grant.uri
                    if perm == 'WRITE' and uri == 'http:' + \
                        '//acs.amazonaws.com/groups/global/AllUsers':
                        # check that file name is valid
                        self.validate_file_name(path)
                        return True
                raise ValidationError("Bucket permissions invalid:" +
                    "Everyone must have 'Write' object access")
        return False

    def validate_file_name(self, path):
        """
        Verify that the name is a valid object key name.
        :raises ValidationError if the name is invalid
        """
        m = re.match(r'[A-Za-z0-9()\!._/\-\'\*]+$', path)
        if not m:
            raise ValidationError(
                "path may only contain letters, numbers, "
                "and the following characters: !-_.*'()"
            )
        return 0

    def s3_connect(self):
        return boto.connect_s3()
