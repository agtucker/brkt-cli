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
import email
import json
import os
import unittest
import zlib

from boto.exception import EC2ResponseError

import brkt_cli
import brkt_cli.aws
import brkt_cli.util
from brkt_cli import ValidationError, encryptor_service
from brkt_cli.aws import (
    aws_service, encrypt_ami, update_ami, test_aws_service
)
from brkt_cli.aws.aws_constants import TAG_ENCRYPTOR_SESSION_ID
from brkt_cli.aws.model import Subnet, Volume
from brkt_cli.aws.test_aws_service import build_aws_service
from brkt_cli.instance_config import (
    BRKT_CONFIG_CONTENT_TYPE,
    INSTANCE_UPDATER_MODE,
)
from brkt_cli.instance_config_args import (
    instance_config_args_to_values,
    instance_config_from_values
)
from brkt_cli.test_encryptor_service import (
    DummyEncryptorService,
    FailedEncryptionService
)
from brkt_cli.util import CRYPTO_GCM


class TestEncryptedImageName(unittest.TestCase):

    def test_encrypted_image_suffix(self):
        """ Test that generated suffixes are unique.
        """
        s1 = encrypt_ami.get_encrypted_suffix()
        s2 = encrypt_ami.get_encrypted_suffix()
        self.assertNotEqual(s1, s2)

    def test_name_validation(self):
        name = 'Test123 ()[]./-\'@_'
        self.assertEquals(name, aws_service.validate_image_name(name))
        with self.assertRaises(ValidationError):
            aws_service.validate_image_name(None)
        with self.assertRaises(ValidationError):
            aws_service.validate_image_name('ab')
        with self.assertRaises(ValidationError):
            aws_service.validate_image_name('a' * 129)
        for c in '?!#$%^&*~`{}\|"<>':
            with self.assertRaises(ValidationError):
                aws_service.validate_image_name('test' + c)


class TestException(Exception):
    pass


class TestRunEncryption(unittest.TestCase):

    def setUp(self):
        brkt_cli.util.SLEEP_ENABLED = False

    def test_smoke(self):
        """ Run the entire process and test that nothing obvious is broken.
        """
        aws_svc, encryptor_image, guest_image = build_aws_service()
        encrypted_ami_id = encrypt_ami.encrypt(
            aws_svc=aws_svc,
            enc_svc_cls=DummyEncryptorService,
            image_id=guest_image.id,
            encryptor_ami=encryptor_image.id,
            crypto_policy=CRYPTO_GCM
        )
        self.assertIsNotNone(encrypted_ami_id)

    def test_encryption_error_console_output_available(self):
        """ Test that when an encryption failure occurs, we write the
        console log to a temp file.
        """
        aws_svc, encryptor_image, guest_image = build_aws_service()

        # Create callbacks that make sure that we stop the encryptor
        # instance before collecting logs.
        self.encryptor_instance = None

        def run_instance_callback(args):
            if args.image_id == encryptor_image.id:
                self.encryptor_instance = args.instance

        self.encryptor_stopped = False

        def stop_instance_callback(instance):
            if (self.encryptor_instance and
                    instance.id == self.encryptor_instance.id):
                self.encryptor_stopped = True

        aws_svc.run_instance_callback = run_instance_callback
        aws_svc.stop_instance_callback = stop_instance_callback

        try:
            encrypt_ami.encrypt(
                aws_svc=aws_svc,
                enc_svc_cls=FailedEncryptionService,
                image_id=guest_image.id,
                encryptor_ami=encryptor_image.id,
                crypto_policy=CRYPTO_GCM
            )
            self.fail('Encryption should have failed')
        except encryptor_service.EncryptionError as e:
            with open(e.console_output_file.name) as f:
                content = f.read()
                self.assertEquals(test_aws_service.CONSOLE_OUTPUT_TEXT, content)
            os.remove(e.console_output_file.name)

        self.assertTrue(self.encryptor_stopped)

    def test_encryption_error_console_output_not_available(self):
        """ Test that we handle the case when encryption fails and console
        output is not available.
        """
        aws_svc, encryptor_image, guest_image = build_aws_service()
        aws_svc.console_output_text = None

        try:
            encrypt_ami.encrypt(
                aws_svc=aws_svc,
                enc_svc_cls=FailedEncryptionService,
                image_id=guest_image.id,
                encryptor_ami=encryptor_image.id,
                crypto_policy=CRYPTO_GCM
            )
            self.fail('Encryption should have failed')
        except encryptor_service.EncryptionError as e:
            self.assertIsNone(e.console_output_file)

    def test_delete_orphaned_volumes(self):
        """ Test that we clean up instance volumes that are orphaned by AWS.
        """
        aws_svc, encryptor_image, guest_image = build_aws_service()

        # Simulate a tagged orphaned volume.
        volume = Volume()
        volume.id = test_aws_service.new_id()
        aws_svc.volumes[volume.id] = volume
        aws_svc.tagged_volumes.append(volume)

        # Verify that lookup succeeds before encrypt().
        self.assertEqual(volume, aws_svc.get_volume(volume.id))
        self.assertEqual(
            [volume],
            aws_svc.get_volumes(
                tag_key=TAG_ENCRYPTOR_SESSION_ID, tag_value='123')
        )

        encrypt_ami.encrypt(
            aws_svc=aws_svc,
            enc_svc_cls=DummyEncryptorService,
            image_id=guest_image.id,
            encryptor_ami=encryptor_image.id,
            crypto_policy=CRYPTO_GCM
        )

        # Verify that the volume was deleted.
        self.assertIsNone(aws_svc.volumes.get(volume.id, None))

    def test_encrypted_ami_name(self):
        """ Test that the name is set on the encrypted AMI when specified.
        """
        aws_svc, encryptor_image, guest_image = build_aws_service()

        name = 'Am I an AMI?'
        image_id = encrypt_ami.encrypt(
            aws_svc=aws_svc,
            enc_svc_cls=DummyEncryptorService,
            image_id=guest_image.id,
            encryptor_ami=encryptor_image.id,
            encrypted_ami_name=name,
            crypto_policy=CRYPTO_GCM
        )
        ami = aws_svc.get_image(image_id)
        self.assertEqual(name, ami.name)

    def test_subnet_with_security_groups(self):
        """ Test that the subnet and security groups are passed to the
        calls to AWSService.run_instance().
        """
        self.call_count = 0

        def run_instance_callback(args):
            self.call_count += 1
            self.assertEqual('subnet-1', args.subnet_id)
            if self.call_count == 1:
                # Snapshotter.
                self.assertIsNone(args.security_group_ids)
            elif self.call_count == 2:
                # Encryptor.
                self.assertEqual(['sg-1', 'sg-2'], args.security_group_ids)
            else:
                self.fail('Unexpected number of calls to run_instance()')

        aws_svc, encryptor_image, guest_image = build_aws_service()
        aws_svc.run_instance_callback = run_instance_callback
        encrypt_ami.encrypt(
            aws_svc=aws_svc,
            enc_svc_cls=DummyEncryptorService,
            image_id=guest_image.id,
            encryptor_ami=encryptor_image.id,
            crypto_policy=CRYPTO_GCM,
            subnet_id='subnet-1',
            security_group_ids=['sg-1', 'sg-2']
        )

    def test_subnet_without_security_groups(self):
        """ Test that we create the temporary security group in the subnet
        that the user specified.
        """
        self.security_group_was_created = False

        def create_security_group_callback(vpc_id):
            self.security_group_was_created = True
            self.assertEqual('vpc-1', vpc_id)

        aws_svc, encryptor_image, guest_image = build_aws_service()
        aws_svc.create_security_group_callback = \
            create_security_group_callback

        subnet = Subnet()
        subnet.id = 'subnet-1'
        subnet.vpc_id = 'vpc-1'
        aws_svc.subnets = {subnet.id: subnet}

        encrypt_ami.encrypt(
            aws_svc=aws_svc,
            enc_svc_cls=DummyEncryptorService,
            image_id=guest_image.id,
            encryptor_ami=encryptor_image.id,
            crypto_policy=CRYPTO_GCM,
            subnet_id='subnet-1'
        )
        self.assertTrue(self.security_group_was_created)

    def test_instance_type(self):
        """ Test that we launch the guest as m4.large and the encryptor
        as c4.xlarge.
        """
        aws_svc, encryptor_image, guest_image = build_aws_service()

        def run_instance_callback(args):
            if args.image_id == guest_image.id:
                self.assertEqual('m4.large', args.instance_type)
                self.assertFalse(args.ebs_optimized)
            elif args.image_id == encryptor_image.id:
                self.assertEqual('c4.xlarge', args.instance_type)
                self.assertTrue(args.ebs_optimized)
            else:
                self.fail('Unexpected image id: ' + args.image_id)

        aws_svc.run_instance_callback = run_instance_callback
        encrypt_ami.encrypt(
            aws_svc=aws_svc,
            enc_svc_cls=DummyEncryptorService,
            image_id=guest_image.id,
            encryptor_ami=encryptor_image.id,
            crypto_policy=CRYPTO_GCM
        )

    def test_guest_instance_type(self):
        """ Test that we use the specified instance type to launch the guest
        instance.
        """
        aws_svc, encryptor_image, guest_image = build_aws_service()

        def run_instance_callback(args):
            if args.image_id == guest_image.id:
                self.assertEqual('t2.micro', args.instance_type)

        aws_svc.run_instance_callback = run_instance_callback
        encrypt_ami.encrypt(
            aws_svc=aws_svc,
            enc_svc_cls=DummyEncryptorService,
            image_id=guest_image.id,
            encryptor_ami=encryptor_image.id,
            crypto_policy=CRYPTO_GCM,
            guest_instance_type='t2.micro'
        )

    def test_terminate_guest(self):
        """ Test that we terminate the guest instance if an exception is
        raised while waiting for it to come up.
        """
        self.terminate_instance_called = False
        self.instance_id = None

        def get_instance_callback(instance):
            self.instance_id = instance.id
            raise TestException('Test')

        def terminate_instance_callback(instance):
            self.terminate_instance_called = True
            self.assertEqual(self.instance_id, instance.id)

        aws_svc, encryptor_image, guest_image = build_aws_service()
        aws_svc.get_instance_callback = get_instance_callback
        aws_svc.terminate_instance_callback = terminate_instance_callback

        try:
            encrypt_ami.encrypt(
                aws_svc=aws_svc,
                enc_svc_cls=DummyEncryptorService,
                image_id=guest_image.id,
                encryptor_ami=encryptor_image.id,
                crypto_policy=CRYPTO_GCM
            )
        except TestException:
            pass

        self.assertTrue(self.terminate_instance_called)

    def test_register_ami_hvm(self):
        """ Test the new (non-legacy) code path in register_ami().
        """
        aws_svc, encryptor_image, guest_image = build_aws_service()
        encryptor_instance = aws_svc.run_instance(encryptor_image.id)
        guest_instance = aws_svc.run_instance(guest_image.id)
        mv_bdm = encryptor_instance.block_device_mapping
        mv_root_volume_id = mv_bdm['/dev/sda1'].volume_id
        encrypt_ami._register_ami(
            aws_svc,
            encryptor_instance,
            encryptor_image,
            'Name',
            'Description',
            legacy=False,
            guest_instance=guest_instance,
            mv_root_id=mv_root_volume_id
        )

    def test_clean_up_root_snapshot(self):
        """ Test that we clean up the root snapshot if an exception is
        raised while waiting for it to complete.
        """
        aws_svc, encryptor_image, guest_image = build_aws_service()
        guest_instance = aws_svc.run_instance(guest_image.id)
        self.snapshot = None
        self.snapshot_was_deleted = False

        def get_snapshot_callback(snapshot):
            """ Simulate an exception being raised while waiting for the
            snapshot to complete.
            """
            raise TestException()

        def create_snapshot_callback(volume_id, snapshot):
            self.snapshot = snapshot

        def delete_snapshot_callback(snapshot_id):
            self.assertEqual(self.snapshot.id, snapshot_id)
            self.snapshot_was_deleted = True

        aws_svc.get_snapshot_callback = get_snapshot_callback
        aws_svc.create_snapshot_callback = create_snapshot_callback
        aws_svc.delete_snapshot_callback = delete_snapshot_callback

        with self.assertRaises(TestException):
            aws_service.snapshot_root_volume(
                aws_svc, guest_instance, guest_image.id)
        self.assertTrue(self.snapshot_was_deleted)

    def test_no_terminate_encryptor_on_failure(self):
        """ Test that when terminate_encryptor_on_failure=False, we terminate
        the encryptor only when encryption succeeds.
        """
        aws_svc, encryptor_image, guest_image = build_aws_service()

        self.guest_instance_id = None
        self.guest_terminated = False
        self.encryptor_instance_id = None
        self.encryptor_terminated = False
        self.security_group_deleted = False
        self.snapshot_deleted = False

        def run_instance_callback(args):
            if args.image_id == encryptor_image.id:
                self.encryptor_instance_id = args.instance.id
            if args.image_id == guest_image.id:
                self.guest_instance_id = args.instance.id

        def delete_snapshot_callback(snapshot_id):
            self.snapshot_deleted = True

        def terminate_instance_callback(instance_id):
            self.assertIsNotNone(self.encryptor_instance_id)
            if instance_id == self.encryptor_instance_id:
                self.encryptor_terminated = True
            if instance_id == self.guest_instance_id:
                self.guest_terminated = True

        def delete_security_group_callback(sg_id):
            self.security_group_deleted = True

        # Encryption succeeded.  Make sure the encryptor was terminated.
        aws_svc.run_instance_callback = run_instance_callback
        aws_svc.delete_snapshot_callback = delete_snapshot_callback
        aws_svc.terminate_instance_callback = terminate_instance_callback
        aws_svc.delete_security_group_callback = \
            delete_security_group_callback

        encrypt_ami.encrypt(
            aws_svc=aws_svc,
            enc_svc_cls=DummyEncryptorService,
            image_id=guest_image.id,
            encryptor_ami=encryptor_image.id,
            crypto_policy=CRYPTO_GCM,
            terminate_encryptor_on_failure=False
        )

        self.assertIsNotNone(self.encryptor_instance_id)
        self.assertTrue(self.snapshot_deleted)
        self.assertTrue(self.guest_terminated)
        self.assertTrue(self.encryptor_terminated)
        self.assertTrue(self.security_group_deleted)

        # Encryption failed.  Make sure the encryptor was not terminated.
        self.guest_instance_id = None
        self.guest_terminated = False
        self.encryptor_instance_id = None
        self.encryptor_terminated = False
        self.security_group_deleted = False
        self.snapshot_deleted = False

        with self.assertRaises(encryptor_service.EncryptionError):
            encrypt_ami.encrypt(
                aws_svc=aws_svc,
                enc_svc_cls=FailedEncryptionService,
                image_id=guest_image.id,
                encryptor_ami=encryptor_image.id,
                crypto_policy=CRYPTO_GCM,
                terminate_encryptor_on_failure=False
            )
        self.assertIsNotNone(self.encryptor_instance_id)
        self.assertTrue(self.snapshot_deleted)
        self.assertTrue(self.guest_terminated)
        self.assertFalse(self.encryptor_terminated)
        self.assertFalse(self.security_group_deleted)


_test_brkt_env = brkt_cli.BracketEnvironment(
    api_host='api.example.com',
    api_port=777,
    hsmproxy_host='hsmproxy.example.com',
    hsmproxy_port=888,
    network_host='network.example.com',
    network_port=999
)


class TestBrktEnv(unittest.TestCase):

    def setUp(self):
        brkt_cli.util.SLEEP_ENABLED = False

    def _get_brkt_config_from_mime(self, compressed_mime_data):
        """Look for a 'brkt-config' part in the multi-part MIME input"""
        data = zlib.decompress(compressed_mime_data, 16 + zlib.MAX_WBITS)
        msg = email.message_from_string(data)
        for part in msg.walk():
            if part.get_content_type() == BRKT_CONFIG_CONTENT_TYPE:
                return part.get_payload(decode=True)
        self.assertTrue(False, 'Did not find brkt-config part in userdata')

    def test_brkt_env_encrypt(self):
        """ Test that we parse the brkt_env value and pass the correct
        values to user_data when launching the encryptor instance.
        """
        crypto = CRYPTO_GCM
        aws_svc, encryptor_image, guest_image = build_aws_service()

        def run_instance_callback(args):
            if args.image_id == encryptor_image.id:
                brkt_config = self._get_brkt_config_from_mime(args.user_data)
                d = json.loads(brkt_config)
                self.assertEquals(
                    'api.example.com:777',
                    d['brkt']['api_host']
                )
                self.assertEquals(
                    'hsmproxy.example.com:888',
                    d['brkt']['hsmproxy_host']
                )
                self.assertEquals(
                    'network.example.com:999',
                    d['brkt']['network_host']
                )
                self.assertEquals(
                    crypto,
                    d['brkt']['crypto_policy_type']
                )

        values = instance_config_args_to_values('')
        values.crypto = crypto
        ic = instance_config_from_values(values, brkt_env=_test_brkt_env)
        aws_svc.run_instance_callback = run_instance_callback
        encrypt_ami.encrypt(
            aws_svc=aws_svc,
            enc_svc_cls=DummyEncryptorService,
            image_id=guest_image.id,
            encryptor_ami=encryptor_image.id,
            instance_config=ic,
            crypto_policy=CRYPTO_GCM
        )

    def test_brkt_env_update(self):
        """ Test that the Bracket environment is passed through to metavisor
        user data.
        """
        aws_svc, encryptor_image, guest_image = build_aws_service()
        encrypted_ami_id = encrypt_ami.encrypt(
            aws_svc=aws_svc,
            enc_svc_cls=DummyEncryptorService,
            image_id=guest_image.id,
            encryptor_ami=encryptor_image.id,
            crypto_policy=CRYPTO_GCM
        )

        values = instance_config_args_to_values('')
        ic = instance_config_from_values(
            values, mode=INSTANCE_UPDATER_MODE, brkt_env=_test_brkt_env)

        def run_instance_callback(args):
            if args.image_id == encryptor_image.id:
                brkt_config = self._get_brkt_config_from_mime(args.user_data)
                d = json.loads(brkt_config)
                self.assertEquals(
                    'api.example.com:777',
                    d['brkt']['api_host']
                )
                self.assertEquals(
                    'hsmproxy.example.com:888',
                    d['brkt']['hsmproxy_host']
                )
                self.assertEquals(
                    'network.example.com:999',
                    d['brkt']['network_host']
                )
                self.assertEquals(
                    'updater',
                    d['brkt']['solo_mode']
                )

        aws_svc.run_instance_callback = run_instance_callback
        update_ami(
            aws_svc, encrypted_ami_id, encryptor_image.id,
            'Test updated AMI',
            enc_svc_class=DummyEncryptorService,
            instance_config=ic
        )

    def test_security_group_eventual_consistency(self):
        """ Test that we handle eventually consistency issues when creating
        a temporary security group.
        """
        aws_svc, encryptor_image, guest_image = build_aws_service()

        self.call_count = 0

        def run_instance_callback(args):
            if args.image_id == encryptor_image.id:
                self.call_count += 1
                if self.call_count < 3:
                    # Simulate eventual consistency error while creating
                    # security group.
                    e = EC2ResponseError(None, None)
                    e.error_code = 'InvalidGroup.NotFound'
                    raise e

        aws_svc.run_instance_callback = run_instance_callback

        encrypt_ami.encrypt(
            aws_svc=aws_svc,
            enc_svc_cls=DummyEncryptorService,
            image_id=guest_image.id,
            encryptor_ami=encryptor_image.id,
            crypto_policy=CRYPTO_GCM
        )

        self.assertEquals(3, self.call_count)

    def test_clean_up_guest_instance(self):
        """ Test that we clean up the guest instance if an exception is
        raised inside run_guest_instance().
        """
        aws_svc, encryptor_image, guest_image = build_aws_service()

        self.guest_instance_id = None
        self.guest_terminated = False

        def run_instance_callback(args):
            if args.image_id == guest_image.id:
                self.guest_instance_id = args.instance.id

        def create_tags_callback(resource_id, name, description):
            if resource_id == self.guest_instance_id:
                raise TestException()

        def terminate_instance_callback(instance_id):
            if instance_id == self.guest_instance_id:
                self.guest_terminated = True

        aws_svc.run_instance_callback = run_instance_callback
        aws_svc.create_tags_callback = create_tags_callback
        aws_svc.terminate_instance_callback = terminate_instance_callback

        with self.assertRaises(TestException):
            encrypt_ami.encrypt(
                aws_svc=aws_svc,
                enc_svc_cls=DummyEncryptorService,
                image_id=guest_image.id,
                encryptor_ami=encryptor_image.id,
                crypto_policy=CRYPTO_GCM
            )

        self.assertTrue(self.guest_terminated)

    def test_clean_up_encryptor_instance(self):
        """ Test that we clean up the encryptor instance if an exception is
        raised inside _run_encryptor_instance().
        """
        aws_svc, encryptor_image, guest_image = build_aws_service()

        self.encryptor_instance_id = None
        self.encryptor_terminated = False

        def run_instance_callback(args):
            if args.image_id == encryptor_image.id:
                self.encryptor_instance_id = args.instance.id

        def create_tags_callback(resource_id, name, description):
            if resource_id == self.encryptor_instance_id:
                raise TestException()

        def terminate_instance_callback(instance_id):
            if instance_id == self.encryptor_instance_id:
                self.encryptor_terminated = True

        aws_svc.run_instance_callback = run_instance_callback
        aws_svc.create_tags_callback = create_tags_callback
        aws_svc.terminate_instance_callback = terminate_instance_callback

        with self.assertRaises(TestException):
            encrypt_ami.encrypt(
                aws_svc=aws_svc,
                enc_svc_cls=DummyEncryptorService,
                image_id=guest_image.id,
                encryptor_ami=encryptor_image.id,
                crypto_policy=CRYPTO_GCM
            )

        self.assertTrue(self.encryptor_terminated)
