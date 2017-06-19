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

import base64
import inspect
import os
import unittest

from brkt_cli import (
    brkt_env_from_domain,
    make_user_data
)
from brkt_cli.config import CLIConfig
from brkt_cli.instance_config_args import instance_config_args_to_values


class TestMakeUserData(unittest.TestCase):

    def setUp(self):
        super(TestMakeUserData, self).setUp()
        name_fields = self.id().split('.')
        self.test_name = name_fields[-1]
        my_filename = inspect.getfile(inspect.currentframe())
        my_dir = os.path.dirname(my_filename)
        self.testdata_dir = os.path.join(my_dir, 'testdata')
        self.maxDiff = None # show full diff with knowngood multi-line strings

    def check_output_vs_knowngood(self, output, knowngood_file):
        self.assertTrue(os.path.exists(knowngood_file), msg=
            'knowngood file does not exist: %s\ntest_output:\n---\n%s\n---\n'
            % (knowngood_file, output)
        )
        with open(knowngood_file, 'r') as f:
            knowngood = f.read()

        self.assertMultiLineEqual(output, knowngood)

    def run_cmd(self, values):
        config = CLIConfig()
        env = brkt_env_from_domain('foo.com')
        config.set_env('test', env)
        config.set_current_env('test')

        output = make_user_data.make(values, config)

        knowngood_file = os.path.join(self.testdata_dir,
                                      self.test_name + ".out")
        self.check_output_vs_knowngood(output, knowngood_file)
        return output

    def _init_values(self):
        values = instance_config_args_to_values('')
        values.make_user_data_brkt_files = None
        values.make_user_data_guest_fqdn = None
        values.make_user_data_guest_files = None
        values.unencrypted_guest = False
        values.ssh_public_key_file = None
        values.base64 = False
        return values

    def test_token_and_one_brkt_file(self):
        values = self._init_values()
        values.token = 'THIS_IS_NOT_A_JWT'
        infile = os.path.join(self.testdata_dir, 'logging.yaml')
        values.make_user_data_brkt_files = [ infile ]
        self.run_cmd(values)

    def test_add_one_brkt_file(self):
        values = self._init_values()
        infile = os.path.join(self.testdata_dir, 'logging.yaml')
        values.make_user_data_brkt_files = [ infile ]
        self.run_cmd(values)

    def test_add_one_binary_brkt_file(self):
        values = self._init_values()
        infile = os.path.join(self.testdata_dir, 'rand_bytes.bin')
        values.make_user_data_brkt_files = [ infile ]
        self.run_cmd(values)

    def test_proxy_and_one_brkt_file(self):
        values = self._init_values()
        values.proxies = [ '10.2.3.4:3128' ]
        infile = os.path.join(self.testdata_dir, 'colors.json')
        values.make_user_data_brkt_files = [ infile ]
        self.run_cmd(values)

    def test_add_two_brkt_files(self):
        values = self._init_values()
        infile1 = os.path.join(self.testdata_dir, 'logging.yaml')
        infile2 = os.path.join(self.testdata_dir, 'colors.json')
        values.make_user_data_brkt_files = [ infile1, infile2 ]
        self.run_cmd(values)

    def test_guest_fqdn(self):
        values = self._init_values()
        values.make_user_data_guest_fqdn = 'instance.foo.bar.com'
        self.run_cmd(values)

    def test_guest_userdata(self):
        values = self._init_values()
        infile = os.path.join(self.testdata_dir, 'user-script')
        values.make_user_data_guest_files = [ infile + ':x-shellscript' ]
        self.run_cmd(values)
        
    def test_brkt_files_with_guest_userdata(self):
        values = self._init_values()
        values.token = 'THIS_IS_NOT_A_JWT'
        infile = os.path.join(self.testdata_dir, 'logging.yaml')
        values.make_user_data_brkt_files = [ infile ]
        infile1 = os.path.join(self.testdata_dir, 'user-script')
        infile2 = os.path.join(self.testdata_dir, 'cloud-config')
        values.make_user_data_guest_files = [
            infile1 + ':x-shellscript', infile2 + ':cloud-config' ]
        self.run_cmd(values)

    def test_ssh_public_key(self):
        values = self._init_values()
        values.ssh_public_key_file = os.path.join(
            self.testdata_dir, 'pub-key')
        self.run_cmd(values)

    def test_unencrypted_guest(self):
        values = self._init_values()
        values.unencrypted_guest = True
        self.run_cmd(values)

    def test_encrypted_guest_and_service_domain(self):
        values = self._init_values()
        values.service_domain = 'stage.mgmt.brkt.com'
        self.run_cmd(values)

    def test_encrypted_guest_and_service_domain_and_proxy(self):
        values = self._init_values()
        values.service_domain = 'stage.mgmt.brkt.com'
        values.proxies = ['10.2.3.4:3128']
        self.run_cmd(values)

    def test_base64(self):
        values = self._init_values()
        values.base64 = True
        # Throw in a brkt-file, for good measure.
        infile = os.path.join(self.testdata_dir, 'logging.yaml')
        values.make_user_data_brkt_files = [ infile ]
        output = self.run_cmd(values)
        decoded_output = base64.b64decode(output)
        decoded_knowngood_file = os.path.join(self.testdata_dir,
                                      self.test_name + ".out.decoded")
        self.check_output_vs_knowngood(decoded_output,
                                       decoded_knowngood_file)
