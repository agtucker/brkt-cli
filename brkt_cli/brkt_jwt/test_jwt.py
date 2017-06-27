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
import uuid
from datetime import datetime, timedelta

import iso8601
import jwt as pyjwt

import brkt_cli.crypto
from brkt_cli import util
from brkt_cli import brkt_jwt
from brkt_cli.crypto import test_crypto
from brkt_cli.validation import ValidationError

if brkt_cli.crypto.cryptography_library_available:
    _crypto = brkt_cli.crypto.from_private_key_pem(
        test_crypto.TEST_PRIVATE_KEY_PEM
    )


class TestGenerateJWT(unittest.TestCase):

    def __init__(self, *args, **kwargs):
        super(TestGenerateJWT, self).__init__(*args, **kwargs)

    def test_make_jwt(self):
        if not brkt_cli.crypto.cryptography_library_available:
            return

        # Generate the JWT.
        now = datetime.now(tz=iso8601.UTC).replace(microsecond=0)
        nbf = now + timedelta(days=1)
        exp = now + timedelta(days=7)
        customer = str(uuid.uuid4())

        jwt = brkt_jwt.make_jwt(
            _crypto,
            nbf=nbf,
            exp=exp,
            customer=customer,
            claims={'one': 1, 'two': 2}
        )
        brkt_cli.validate_jwt(jwt)
        after = datetime.now(tz=iso8601.UTC)

        # Check the header.
        header = brkt_jwt.get_header(jwt)
        self.assertEqual('JWT', header['typ'])
        self.assertEqual('ES384', header['alg'])
        self.assertTrue('kid' in header)

        # Check the payload
        payload = brkt_jwt.get_payload(jwt)
        self.assertTrue('jti' in payload)
        self.assertTrue(payload['iss'].startswith('brkt-cli'))
        self.assertEqual(customer, payload['customer'])
        self.assertEqual(1, payload['one'])
        self.assertEqual(2, payload['two'])

        iat = util.timestamp_to_datetime(payload['iat'])
        self.assertTrue(now <= iat <= after)

        nbf_result = util.timestamp_to_datetime(payload['nbf'])
        self.assertEqual(nbf, nbf_result)

        exp_result = util.timestamp_to_datetime(payload['exp'])
        self.assertEqual(exp, exp_result)

    def test_malformed(self):
        """ Test that we raise ValidationError when the JWT is malformed. """
        for bogus in ['abc', 'a.b', 'xyz.123.456']:
            with self.assertRaises(ValidationError):
                brkt_jwt.get_header(bogus)
            with self.assertRaises(ValidationError):
                brkt_jwt.get_payload(bogus)

    def test_validate_jwt_missing_kid(self):
        """ Test that validate_jwt() detects when kid is missing. """
        if not brkt_cli.crypto.cryptography_library_available:
            return

        jwt = pyjwt.encode({}, _crypto.private_key, algorithm='ES384')
        with self.assertRaises(ValidationError):
            brkt_cli.validate_jwt(jwt)

    def test_validate_name_value(self):
        brkt_jwt.validate_name_value('abc123_-', 'abc123_-')
        with self.assertRaises(ValidationError):
            brkt_jwt.validate_name_value('valid', 'invalid!')
        with self.assertRaises(ValidationError):
            brkt_jwt.validate_name_value('invalid!', 'valid')
        with self.assertRaises(ValidationError):
            brkt_jwt.validate_name_value('any', 'valid')
        with self.assertRaises(ValidationError):
            brkt_jwt.validate_name_value('valid', 'any')


class TestJWK(unittest.TestCase):

    def test_long_to_byte_array(self):
        l = long('deadbeef', 16)
        ba = brkt_jwt.jwk._long_to_byte_array(l, pad_to_len=4)
        self.assertEqual(bytearray.fromhex('deadbeef'), ba)
        ba = brkt_jwt.jwk._long_to_byte_array(l, pad_to_len=50)
        self.assertEqual(bytearray(46) + bytearray.fromhex('deadbeef'), ba)
        ba = brkt_jwt.jwk._long_to_byte_array(l)
        self.assertEqual(bytearray(44) + bytearray.fromhex('deadbeef'), ba)


class TestNameValueToDict(unittest.TestCase):

    def test_name_value_to_dict(self):
        self.assertEqual(
            {'a': 'b', 'c': 'd'},
            brkt_jwt.name_value_list_to_dict(['a=b', 'c=d'])
        )
        with self.assertRaises(ValidationError):
            brkt_jwt.name_value_list_to_dict(['a=b', 'a=c'])

    def test_brkt_tags_from_name_value_list(self):
        self.assertEqual(
            {'a': 'b', 'c': 'd'},
            brkt_jwt.brkt_tags_from_name_value_list(['a=b', 'c=d'])
        )

        with self.assertRaises(ValidationError):
            brkt_jwt.brkt_tags_from_name_value_list(['exp=1'])
