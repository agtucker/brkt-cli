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


def tags_to_dict(tags_json):
    d = {}
    if tags_json:
        for tag in tags_json:
            d[tag['Key']] = tag['Value']
    return d


def dict_to_tags(d):
    tags = list()
    for key, value in d.iteritems():
        tags.append({'Key': key, 'Value': value})
    return tags


def get_value(tags_json, key, default=None):
    return tags_to_dict(tags_json).get(key, default)


def set_value(tags_json, key, value):
    # Tag already exists.
    for t in tags_json:
        if t['Key'] == key:
            t['Value'] = value
            return

    # Set the tag for the first time.
    tags_json.append({'Key': key, 'Value': value})
