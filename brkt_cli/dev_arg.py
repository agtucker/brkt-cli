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
from argparse import SUPPRESS


def add_hidden_argument(parser, dev_help, *args, **kwargs):
    if not dev_help:
        kwargs['help'] = SUPPRESS
    elif 'help' in kwargs:
        kwargs['help'] = kwargs['help'] + ' (hidden)'
    parser.add_argument(
        *args,
        **kwargs
    )