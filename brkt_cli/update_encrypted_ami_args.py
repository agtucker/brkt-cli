import argparse


def setup_update_encrypted_ami(parser):
    parser.add_argument(
        'ami',
        metavar='AMI_ID',
        help='The AMI that will be encrypted'
    )
    parser.add_argument(
        '--updater-ami',
        metavar='UPDATER_AMI_ID',
        help='The metavisor updater AMI that will be used',
        dest='updater_ami',
        required=True
    )
    parser.add_argument(
        '--region',
        metavar='REGION',
        help='AWS region (e.g. us-west-2)',
        dest='region',
        default='us-west-2',
        required=True
    )
    parser.add_argument(
        '--zone',
        metavar='ZONE',
        help='AWS zone (e.g. us-west-2a)',
        dest='zone',
        default=None,
        required=False
    )
    parser.add_argument(
        '--encrypted-ami-name',
        metavar='NAME',
        dest='encrypted_ami_name',
        help='Specify the name of the generated encrypted AMI',
        required=False
    )
    # Optional EC2 SSH key pair name to use for launching the snapshotter
    # and encryptor instances.  This argument is hidden because it's only
    # used for development.
    parser.add_argument(
        '--key',
        metavar='KEY',
        help=argparse.SUPPRESS,
        dest='key_name'
    )
