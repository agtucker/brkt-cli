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
import hashlib
import logging

log = logging.getLogger(__name__)

EC384_KEY_LEN_BYTES = 48

# If the length of the bytearray created is less than 48, pad it with leading
# zeros. EC384 are 384bits i.e. 48byte keys. so both x and y need to be 48bytes
# with leading zeros wherever required


def _long_to_byte_array(long_int, pad_to_len=EC384_KEY_LEN_BYTES):
    bys = bytearray()
    while long_int:
        long_int, r = divmod(long_int, 256)
        bys.insert(0, r)
    if len(bys) < pad_to_len:
        # pad with leading zeros
        bys = bytearray(pad_to_len - len(bys)) + bys
    return bys


def _long_to_base64(n):
    bys = _long_to_byte_array(n)
    if not bys:
        bys.append(0)
    s = base64.urlsafe_b64encode(bys).rstrip(b'=')
    return s.decode("ascii")


def get_jwk(x, y):
    """ Return the JSON web key representation of an ECDSA public key.

    :param x the X value of the ECDSA public key as a long integer
    :param y the Y value of hte ECDSA public key as a long integer
    :return the JSON web key as a string
    """
    return {
        'alg': 'ES384',
        'kty': 'EC',
        'crv': 'P-384',
        'x': _long_to_base64(x),
        'y': _long_to_base64(y)
    }


def get_thumbprint(x, y):
    """ Return the thumbprint of an ECDSA public key, as specified by
    RFC 7638.

    :param x the X value of the ECDSA public key as a long integer
    :param y the Y value of hte ECDSA public key as a long integer
    :return the thumbprint as a string
    """
    jwk = get_jwk(x, y)
    thumbprint_json = \
        '{"crv":"%(crv)s","kty":"%(kty)s","x":"%(x)s","y":"%(y)s"}' % jwk
    log.debug('Thumbprint JSON: %s', thumbprint_json)
    return hashlib.sha256(thumbprint_json).hexdigest()
