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
import os
import unittest

from brkt_cli import encryptor_service

from brkt_cli import util

from brkt_cli.aws import (
    encrypt_ami, test_aws_service, update_ami
)
from brkt_cli.aws.test_aws_service import build_aws_service
from brkt_cli.test_encryptor_service import (
    DummyEncryptorService,
    FailedEncryptionService
)
from brkt_cli.util import CRYPTO_GCM


class TestRunUpdate(unittest.TestCase):

    def setUp(self):
        util.SLEEP_ENABLED = False

    def test_subnet_and_security_groups(self):
        """ Test that the subnet and security group ids are passed through
        to run_instance().
        """
        aws_svc, encryptor_image, guest_image = build_aws_service()
        encrypted_ami_id = encrypt_ami.encrypt(
            aws_svc=aws_svc,
            enc_svc_cls=DummyEncryptorService,
            image_id=guest_image.id,
            encryptor_ami=encryptor_image.id,
            crypto_policy=CRYPTO_GCM
        )

        self.call_count = 0

        def run_instance_callback(args):
            if args.image_id == encryptor_image.id:
                self.call_count += 1
                self.assertEqual('subnet-1', args.subnet_id)
                self.assertEqual(['sg-1', 'sg-2'], args.security_group_ids)

        aws_svc.run_instance_callback = run_instance_callback
        ami_id = update_ami(
            aws_svc, encrypted_ami_id, encryptor_image.id,
            'Test updated AMI',
            subnet_id='subnet-1', security_group_ids=['sg-1', 'sg-2'],
            enc_svc_class=DummyEncryptorService
        )

        self.assertEqual(1, self.call_count)
        self.assertIsNotNone(ami_id)

    def test_guest_instance_type(self):
        """ Test that the guest instance type is passed through
        to run_instance().
        """
        aws_svc, encryptor_image, guest_image = \
            test_aws_service.build_aws_service()
        encrypted_ami_id = encrypt_ami.encrypt(
            aws_svc=aws_svc,
            enc_svc_cls=DummyEncryptorService,
            image_id=guest_image.id,
            encryptor_ami=encryptor_image.id,
            crypto_policy=CRYPTO_GCM
        )

        def run_instance_callback(args):
            if args.image_id == encrypted_ami_id:
                self.assertEqual('t2.micro', args.instance_type)
            elif args.image_id == encryptor_image.id:
                self.assertEqual('m4.large', args.instance_type)
            else:
                self.fail('Unexpected image: ' + args.image_id)

        aws_svc.run_instance_callback = run_instance_callback
        update_ami(
            aws_svc, encrypted_ami_id, encryptor_image.id, 'Test updated AMI',
            subnet_id='subnet-1', security_group_ids=['sg-1', 'sg-2'],
            enc_svc_class=DummyEncryptorService, guest_instance_type='t2.micro'
        )

    def test_security_group_eventual_consistency(self):
        """ Test that we handle eventually consistency issues when creating
        a temporary security group.
        """
        aws_svc, encryptor_image, guest_image = build_aws_service()
        encrypted_ami_id = encrypt_ami.encrypt(
            aws_svc=aws_svc,
            enc_svc_cls=DummyEncryptorService,
            image_id=guest_image.id,
            encryptor_ami=encryptor_image.id,
            crypto_policy=CRYPTO_GCM
        )

        self.call_count = 0

        def run_instance_callback(args):
            if args.image_id == encryptor_image.id:
                self.call_count += 1
                if self.call_count < 3:
                    # Simulate eventual consistency error while creating
                    # security group.
                    raise test_aws_service.new_client_error(
                        'InvalidGroup.NotFound')

        aws_svc.run_instance_callback = run_instance_callback
        update_ami(
            aws_svc, encrypted_ami_id, encryptor_image.id,
            'Test updated AMI',
            enc_svc_class=DummyEncryptorService
        )
        self.assertEqual(3, self.call_count)

    def test_update_error_console_output(self):
        """ Test that when an update failure occurs, we write the
        console log to a temp file.
        """
        aws_svc, encryptor_image, guest_image = build_aws_service()

        encrypted_ami_id = encrypt_ami.encrypt(
            aws_svc=aws_svc,
            enc_svc_cls=DummyEncryptorService,
            image_id=guest_image.id,
            encryptor_ami=encryptor_image.id,
            crypto_policy=CRYPTO_GCM
        )

        # Create callbacks that make sure that we stop the updater
        # instance before collecting logs.
        self.updater_instance = None

        def run_instance_callback(args):
            if args.image_id == encryptor_image.id:
                self.updater_instance = args.instance

        self.updater_stopped = False

        def stop_instance_callback(instance):
            if (self.updater_instance and
                    instance.id == self.updater_instance.id):
                self.updater_stopped = True

        aws_svc.run_instance_callback = run_instance_callback
        aws_svc.stop_instance_callback = stop_instance_callback

        try:
            update_ami(
                aws_svc, encrypted_ami_id, encryptor_image.id,
                'Test updated AMI',
                enc_svc_class=FailedEncryptionService
            )
            self.fail('Update should have failed')
        except encryptor_service.EncryptionError as e:
            with open(e.console_output_file.name) as f:
                content = f.read()
                self.assertEquals(
                    test_aws_service.CONSOLE_OUTPUT_TEXT, content)
            os.remove(e.console_output_file.name)

        self.assertTrue(self.updater_stopped)
