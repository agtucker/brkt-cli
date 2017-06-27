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
import unittest
import zlib

import brkt_cli
import brkt_cli.aws
import brkt_cli.util
from brkt_cli.aws import wrap_image
from brkt_cli.aws.model import Subnet
from brkt_cli.aws.test_aws_service import build_aws_service
from brkt_cli.instance_config import (
    BRKT_CONFIG_CONTENT_TYPE,
    INSTANCE_METAVISOR_MODE,
)
from brkt_cli.instance_config_args import (
    instance_config_args_to_values,
    instance_config_from_values
)


class TestWrappedInstanceName(unittest.TestCase):

    def test_encrypted_image_suffix(self):
        """ Test that generated suffixes are unique.
        """
        s1 = wrap_image.get_wrapped_suffix()
        s2 = wrap_image.get_wrapped_suffix()
        self.assertNotEqual(s1, s2)


class TestException(Exception):
    pass


class TestRunEncryption(unittest.TestCase):

    def setUp(self):
        brkt_cli.util.SLEEP_ENABLED = False

    def test_smoke(self):
        """ Run the entire process and test that nothing obvious is broken.
        """
        aws_svc, encryptor_image, guest_image = build_aws_service()
        instance = wrap_image.launch_wrapped_image(
            aws_svc=aws_svc,
            image_id=guest_image.id,
            metavisor_ami=encryptor_image.id,
        )
        self.assertIsNotNone(instance)

    def test_wrapped_instance_name(self):
        """ Test that the wrapped instance is launched with the Metavisor AMI.
        """
        aws_svc, encryptor_image, guest_image = build_aws_service()

        name = 'Am I an unencrypted guest instance?'
        instance = wrap_image.launch_wrapped_image(
            aws_svc=aws_svc,
            image_id=guest_image.id,
            metavisor_ami=encryptor_image.id,
            wrapped_instance_name=name,
        )
        instance = aws_svc.get_instance(instance.id)
        self.assertEqual(encryptor_image.id, instance.image_id)

    def test_subnet_with_security_groups(self):
        """ Test that the subnet and security groups are passed to the
        calls to AWSService.run_instance().
        """

        def run_instance_callback(args):
            self.assertEqual('subnet-1', args.subnet_id)
            self.assertEqual(['sg-1', 'sg-2'], args.security_group_ids)

        aws_svc, encryptor_image, guest_image = build_aws_service()
        aws_svc.run_instance_callback = run_instance_callback
        wrap_image.launch_wrapped_image(
            aws_svc=aws_svc,
            image_id=guest_image.id,
            metavisor_ami=encryptor_image.id,
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

        wrap_image.launch_wrapped_image(
            aws_svc=aws_svc,
            image_id=guest_image.id,
            metavisor_ami=encryptor_image.id,
            subnet_id='subnet-1'
        )
        self.assertTrue(self.security_group_was_created)

    def test_default_instance_type(self):
        """ Test that we launch the wrapped guest as m4.large by default
        """
        aws_svc, encryptor_image, guest_image = build_aws_service()

        def run_instance_callback(args):
            if args.image_id == guest_image.id:
                self.assertEqual('m4.large', args.instance_type)
            elif args.image_id == encryptor_image.id:
                self.assertEqual('m4.large', args.instance_type)
            else:
                self.fail('Unexpected image id: ' + args.image_id)

        aws_svc.run_instance_callback = run_instance_callback
        wrap_image.launch_wrapped_image(
            aws_svc=aws_svc,
            image_id=guest_image.id,
            metavisor_ami=encryptor_image.id,
        )

    def test_guest_instance_type(self):
        """ Test that we use the specified instance type to launch the wrapped
        instance.
        """
        aws_svc, encryptor_image, guest_image = build_aws_service()

        def run_instance_callback(args):
            if args.image_id == guest_image.id:
                self.assertEqual('t2.micro', args.instance_type)

        aws_svc.run_instance_callback = run_instance_callback
        wrap_image.launch_wrapped_image(
            aws_svc=aws_svc,
            image_id=guest_image.id,
            metavisor_ami=encryptor_image.id,
            instance_type='t2.micro'
        )

    def test_iam_role(self):
        """ Test that the IAM role is passed to the calls to
        AWSService.run_instance().
        """
        aws_svc, encryptor_image, guest_image = build_aws_service()

        def run_instance_callback(args):
            self.assertEqual('valid', args.instance_profile_name)

        aws_svc.run_instance_callback = run_instance_callback
        wrap_image.launch_wrapped_image(
            aws_svc=aws_svc,
            image_id=guest_image.id,
            metavisor_ami=encryptor_image.id,
            iam='valid'
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

    def test_brkt_env_wrapped_instance(self):
        """ Test that we parse the brkt_env value and pass the correct
        values to user_data when launching the encryptor instance.
        """
        aws_svc, encryptor_image, guest_image = build_aws_service()

        api_host_port = 'api.example.com:777'
        hsmproxy_host_port = 'hsmproxy.example.com:888'
        network_host_port = 'network.example.com:999'
        values = instance_config_args_to_values('')
        values.unencrypted_guest = True
        brkt_env = brkt_cli.BracketEnvironment(
            api_host='api.example.com',
            api_port=777,
            hsmproxy_host='hsmproxy.example.com',
            hsmproxy_port=888,
            network_host='network.example.com',
            network_port=999
        )
        ic = instance_config_from_values(
            values, mode=INSTANCE_METAVISOR_MODE, brkt_env=brkt_env)

        def run_instance_callback(args):
            if args.image_id == encryptor_image.id:
                brkt_config = self._get_brkt_config_from_mime(args.user_data)
                d = json.loads(brkt_config)
                self.assertEquals(
                    api_host_port,
                    d['brkt']['api_host']
                )
                self.assertEquals(
                    hsmproxy_host_port,
                    d['brkt']['hsmproxy_host']
                )
                self.assertEquals(
                    network_host_port,
                    d['brkt']['network_host']
                )
                self.assertEquals(
                    'metavisor',
                    d['brkt']['solo_mode']
                )
                self.assertEquals(
                    True,
                    d['brkt']['allow_unencrypted_guest']
                )

        aws_svc.run_instance_callback = run_instance_callback
        wrap_image.launch_wrapped_image(
            aws_svc=aws_svc,
            image_id=guest_image.id,
            metavisor_ami=encryptor_image.id,
            wrapped_instance_name='Test wrapped instance',
            instance_config=ic
        )
