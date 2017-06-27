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

import unittest
from brkt_cli.validation import ValidationError
from brkt_cli.aws import share_logs
from brkt_cli.aws.model import BlockDeviceMapping, Instance, Snapshot, Volume
from boto.exception import S3ResponseError


class CurrentValue():
    def __init__(self):
        self.connection = {'root': Volume()}
        self.connection['root'].volume_id = None

class S3():
    def __init__(self):
        self.bucket = Bucket()

    def get_all_buckets(self):
        return [self.bucket]

    def get_bucket(self, bucket):
        if self.bucket.region == 'un-matching-region':
            raise S3ResponseError(400, 'reason', 'body')
        return self.bucket

class Bucket():
    def __init__(self):
        self.name = 'test-bucket'
        self.acp = ACP()
        self.region = 'matching-region'

    def get_key(self, path):
        if path == 'matching':
            return True
        if ShareLogsTestService.created is True:
            return True
        return False

    def get_acl(self):
        return self.acp

class ACP():
    def __init__(self):
        self.acl = ACL()

class ACL():
    def __init__(self):
        self.grant = Grant()
        self.grants = [self.grant]

class Grant():
    def __init__(self):
        self.permission = 'WRITE'
        self.uri = 'http://acs.amazonaws.com/groups/global/AllUsers'

# This class is used for testing ShareLogs
class ShareLogsTestService(share_logs.ShareLogsService):
    
    created = False

    def get_instance(self, instance_id):
        instance = Instance()
        instance.block_device_mapping = BlockDeviceMapping()
        instance.block_device_mapping.current_value = CurrentValue()
        instance._state.name = 'running'
        instance.root_device_name = 'root'
        instance.id = 'test-id'
        return instance

    def s3_connect(self):
        return S3()

    def create_snapshot(self, volume_id, name):
        snapshot = Snapshot()
        snapshot.id = 'test-id'
        snapshot.volume_size = '5'
        return snapshot

    def get_snapshots(self, snapshot_id):
        snapshot = Snapshot()
        snapshot.status = 'completed'
        return [snapshot]

    def run_instance(self, image_id, instance_type,
                     block_device_map, user_data, ebs_optimized, subnet_id):
        instance = Instance()
        ShareLogsTestService.created = True
        return instance


class TestShareLogs(unittest.TestCase):

    def test_path(self):
        aws_svc = ShareLogsTestService()
        paths = ['#path', '\path', '@path', '$path', 'path%']
        for p in paths:
            with self.assertRaises(ValidationError):
                aws_svc.validate_file_name(p)

        # These charictors are all valid
        path = "!-_'/.*()PaTh8"
        result = aws_svc.validate_file_name(path)
        self.assertEqual(result, 0)

    def test_bucket_file(self):
        aws_svc = ShareLogsTestService()
        s3 = S3()
        # Tests if user doesn't already own bucket
        result = aws_svc.check_bucket_file(
            "different-bucket", "file", "matching", s3)
        self.assertEqual(result, 0)
        # Tests if user owns bucket in wrong region
        s3.bucket.region = 'un-matching-region'
        with self.assertRaises(ValidationError):
            aws_svc.check_bucket_file(
                'test-bucket', "file", "unmatching", s3)
        s3.bucket.region = 'matching-region'
        # Tests if the bucket has a matching file
        with self.assertRaises(ValidationError):
            aws_svc.check_bucket_file(
                "test-bucket", "matching", "matching", s3)
        # # Tests if the bucket doesn't have write permission
        s3.bucket.acp.acl.grant.permission = 'READ'
        with self.assertRaises(ValidationError):
            aws_svc.check_bucket_file(
                "test-bucket", "file", "matching", s3)
        # Tests if the user owns a writeable bucket
        s3.bucket.acp.acl.grant.permission = 'WRITE'
        result2 = aws_svc.check_bucket_file(
            "test-bucket", "file", "matching", s3)
        self.assertEqual(result2, 1)

    def test_normal(self):
        aws_svc = ShareLogsTestService()
        logs_svc = ShareLogsTestService()
        instance_id = 'test-instance'
        snapshot_id = None
        region = 'us-west-2'
        bucket = 'test-bucket'
        path = 'test/path'

        share_logs.share(aws_svc, logs_svc, instance_id=instance_id,
            snapshot_id=snapshot_id, region=region, bucket=bucket, path=path, subnet_id=None)
