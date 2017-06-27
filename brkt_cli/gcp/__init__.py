import brkt_cli
import logging
import os
from brkt_cli.subcommand import Subcommand

from brkt_cli import encryptor_service, instance_config_args, util
from brkt_cli.instance_config import (
    INSTANCE_CREATOR_MODE,
    INSTANCE_METAVISOR_MODE,
    INSTANCE_UPDATER_MODE,
)
from brkt_cli.instance_config_args import (
    instance_config_from_values,
    setup_instance_config_args
)
from brkt_cli.gcp import (
    encrypt_gcp_image,
    encrypt_gcp_image_args,
    gcp_service,
    launch_gcp_image,
    launch_gcp_image_args,
    update_gcp_image,
    update_encrypted_gcp_image_args,
    wrap_gcp_image,
    wrap_gcp_image_args,
    share_logs_gcp_args,
)
from brkt_cli.validation import ValidationError

log = logging.getLogger(__name__)


def run_encrypt(values, config):
    session_id = util.make_nonce()
    gcp_svc = gcp_service.GCPService(values.project, session_id, log)
    check_args(values, gcp_svc, config)

    encrypted_image_name = gcp_service.get_image_name(
        values.encrypted_image_name, values.image)
    gcp_service.validate_image_name(encrypted_image_name)
    if values.validate:
        gcp_service.validate_images(gcp_svc,
                                    encrypted_image_name,
                                    values.encryptor_image,
                                    values.image,
                                    values.image_project)
        if values.gcp_tags:
            validate_tags(values.gcp_tags)

    if not values.verbose:
        logging.getLogger('googleapiclient').setLevel(logging.ERROR)

    log.info('Starting encryptor session %s', gcp_svc.get_session_id())

    brkt_env = brkt_cli.brkt_env_from_values(values, config)
    lt = instance_config_args.get_launch_token(values, config)
    ic = instance_config_from_values(
        values,
        mode=INSTANCE_CREATOR_MODE,
        brkt_env=brkt_env,
        launch_token=lt
    )
    encrypted_image_id = encrypt_gcp_image.encrypt(
        gcp_svc=gcp_svc,
        enc_svc_cls=encryptor_service.EncryptorService,
        image_id=values.image,
        encryptor_image=values.encryptor_image,
        encrypted_image_name=encrypted_image_name,
        zone=values.zone,
        instance_config=ic,
        crypto_policy=values.crypto,
        image_project=values.image_project,
        keep_encryptor=values.keep_encryptor,
        image_file=values.image_file,
        image_bucket=values.bucket,
        network=values.network,
        subnetwork=values.subnetwork,
        status_port=values.status_port,
        cleanup=values.cleanup,
        gcp_tags=values.gcp_tags
    )
    # Print the image name to stdout, in case the caller wants to process
    # the output.  Log messages go to stderr.
    print(encrypted_image_id)
    return 0


def run_update(values, config):
    session_id = util.make_nonce()
    gcp_svc = gcp_service.GCPService(values.project, session_id, log)
    check_args(values, gcp_svc, config)

    encrypted_image_name = gcp_service.get_image_name(
        values.encrypted_image_name, values.image)
    gcp_service.validate_image_name(encrypted_image_name)
    if values.validate:
        gcp_service.validate_images(gcp_svc,
                                    encrypted_image_name,
                                    values.encryptor_image,
                                    values.image)
        if values.gcp_tags:
            validate_tags(values.gcp_tags)
    if not values.verbose:
        logging.getLogger('googleapiclient').setLevel(logging.ERROR)

    log.info('Starting updater session %s', gcp_svc.get_session_id())

    brkt_env = brkt_cli.brkt_env_from_values(values, config)
    lt = instance_config_args.get_launch_token(values, config)
    ic = instance_config_from_values(
        values,
        mode=INSTANCE_UPDATER_MODE,
        brkt_env=brkt_env,
        launch_token=lt
    )
    updated_image_id = update_gcp_image.update_gcp_image(
        gcp_svc=gcp_svc,
        enc_svc_cls=encryptor_service.EncryptorService,
        image_id=values.image,
        encryptor_image=values.encryptor_image,
        encrypted_image_name=encrypted_image_name,
        zone=values.zone,
        instance_config=ic,
        keep_encryptor=values.keep_encryptor,
        image_file=values.image_file,
        image_bucket=values.bucket,
        network=values.network,
        subnetwork=values.subnetwork,
        status_port=values.status_port,
        cleanup=values.cleanup,
        gcp_tags=values.gcp_tags
    )

    print(updated_image_id)
    return 0


def run_launch(values, config):
    gcp_svc = gcp_service.GCPService(values.project, None, log)
    if values.ssd_scratch_disks > 8:
        raise ValidationError("Maximum of 8 SSD scratch disks are supported")

    # Use the token in the image unless a token or tags were specified on
    # the command line.
    lt = None
    if values.token or values.brkt_tags:
        lt = instance_config_args.get_launch_token(values, config)

    instance_config = instance_config_from_values(
        values, mode=INSTANCE_METAVISOR_MODE, launch_token=lt)
    if values.startup_script:
        extra_items = [{
            'key': 'startup-script',
            'value': values.startup_script
        }]
    else:
        extra_items = None
    brkt_userdata = instance_config.make_userdata()
    metadata = gcp_service.gcp_metadata_from_userdata(
        brkt_userdata, extra_items=extra_items)
    if not values.verbose:
        logging.getLogger('googleapiclient').setLevel(logging.ERROR)

    if values.instance_name:
        gcp_service.validate_image_name(values.instance_name)


    if values.gcp_tags:
        validate_tags(values.gcp_tags)

    encrypted_instance_id = launch_gcp_image.launch(log,
                            gcp_svc,
                            values.image,
                            values.instance_name,
                            values.zone,
                            values.delete_boot,
                            values.instance_type,
                            values.network,
                            values.subnetwork,
                            metadata,
                            values.ssd_scratch_disks,
                            values.gcp_tags)
    print(encrypted_instance_id)
    return 0


def run_wrap_image(values, config):
    session_id = util.make_nonce()
    gcp_svc = gcp_service.GCPService(values.project, session_id, log)

    if values.ssd_scratch_disks > 8:
        raise ValidationError("Maximum of 8 SSD scratch disks are supported")

    brkt_env = brkt_cli.brkt_env_from_values(values, config)
    lt = instance_config_args.get_launch_token(values, config)
    instance_config = instance_config_from_values(
        values,
        mode=INSTANCE_METAVISOR_MODE,
        brkt_env=brkt_env,
        launch_token=lt)
    if values.startup_script:
        extra_items = [{
            'key': 'startup-script',
            'value': values.startup_script
        }]
    else:
        extra_items = None
    instance_config.brkt_config['allow_unencrypted_guest'] = True
    brkt_userdata = instance_config.make_userdata()
    metadata = gcp_service.gcp_metadata_from_userdata(
        brkt_userdata, extra_items=extra_items)

    if values.instance_name:
        gcp_service.validate_image_name(values.instance_name)
    if values.gcp_tags:
        validate_tags(values.gcp_tags)
    if not values.verbose:
        logging.getLogger('googleapiclient').setLevel(logging.ERROR)
    
    wrapped_instance = wrap_gcp_image.wrap_guest_image(
        gcp_svc=gcp_svc,
        image_id=values.image,
        encryptor_image=values.encryptor_image,
        zone=values.zone,
        metadata=metadata,
        instance_name=values.instance_name,
        image_project=values.image_project,
        image_file=values.image_file,
        image_bucket=values.bucket,
        instance_type=values.instance_type,
        network=values.network,
        subnet=values.subnetwork,
        cleanup=values.cleanup,
        ssd_disks=values.ssd_scratch_disks,
        gcp_tags=values.gcp_tags
    )
    # Print the instance name to stdout in case the caller want to process
    # the output. Log messages go to stderr
    print(wrapped_instance)
    return 0


def run_sharelogs(values, config):
    session_id = util.make_nonce()
    gcp_svc = gcp_service.GCPService(values.project, session_id, log)
    return share_logs(values, gcp_svc)


def share_logs(values, gcp_svc):
    snapshot_name = 'brkt-diag-snapshot-' + gcp_svc.get_session_id()
    instance_name = 'brkt-diag-instance-' + gcp_svc.get_session_id()
    disk_name = 'sdb-' + gcp_svc.get_session_id()
    log.info('Sharing logs')
    snap = None 

    try:
        image_project = 'ubuntu-os-cloud'
        # Retrieve latest image from family
        ubuntu_image = gcp_svc.get_public_image()
        # Check to see if the bucket is available

        gcp_svc.check_bucket_name(values.bucket, values.project)

        # Check to see if the tar file is already in the bucket
        gcp_svc.check_bucket_file(values.bucket, values.path)
        
        # Create snapshot from root disk
        gcp_svc.create_snapshot(
            values.zone, values.instance, snapshot_name)

        # Wait for the snapshot to finish
        gcp_svc.wait_snapshot(snapshot_name)

        # Get snapshot object
        snap = gcp_svc.get_snapshot(snapshot_name)

        # Create disk from snapshot and wait for it to be ready
        gcp_svc.disk_from_snapshot(values.zone, snapshot_name, disk_name)

        # Wait for disk to initialize
        gcp_svc.wait_for_disk(values.zone, disk_name)

        # Split path name into path and file
        os.path.split(values.path)
        path = os.path.dirname(values.path)
        file = os.path.basename(values.path)

        # Commands to mount file system and compress into tar file
        cmds = '#!/bin/bash\n' + \
            'sudo mount -t ufs -o ro,ufstype=ufs2 /dev/sdb4 /mnt ||\n' + \
            'sudo mount -t ufs -o ro,ufstype=44bsd /dev/sdb5 /mnt\n' + \
            'sudo tar czvf /tmp/%s -C /mnt ./log ./crash\n' % (file) + \
            'sudo gsutil cp /tmp/%s gs://%s/%s/\n' % (file, values.bucket, path)
            
        metadata = {
            "items": [
                {
                    "key": "startup-script",
                    "value": cmds,
                },
            ]
        }

        # Attach new disk to instance
        disks = [
            {
                'boot': False,
                'autoDelete': False,
                'source': 'zones/%s/disks/%s' % (values.zone, disk_name),
            },
        ]

        # launch the instance and wait for it to initialize
        gcp_svc.run_instance(
            values.zone, instance_name, ubuntu_image, disks=disks,
            image_project=image_project, metadata=metadata, delete_boot=True)

        # Wait for tar file to upload to bucket
        if gcp_svc.wait_bucket_file(values.bucket, values.path):
            # Add email account to access control list
            try:
                gcp_svc.storage.objectAccessControls().insert(
                    bucket=values.bucket,
                    object=values.path,
                    body={'entity': 'user-%s' % (values.email),
                        'role': 'READER'}).execute()
                log.info('Updated file permissions')
            except Exception as e:
                log.error("Failed changing file permissions")
                raise util.BracketError("Failed changing file permissions: %s", e)
        else:
            log.error("Can't upload logs file")
            raise util.BracketError("Can't upload logs file")

        log.info(
            'Logs available at https://storage.cloud.google.com/%s/%s'
            % (values.bucket, values.path))
    finally:
        try:
            if snap:
                gcp_svc.delete_snapshot(snapshot_name)
            gcp_svc.cleanup(values.zone, None)
        except Exception as e:
            log.warn("Failed during cleanup: %s", e)
    return 0


class GCPSubcommand(Subcommand):

    def name(self):
        return 'gcp'

    def setup_config(self, config):
        config.register_option(
            '%s.project' % (self.name(),),
            'The GCP project metavisors will be launched into')
        config.register_option(
            '%s.network' % (self.name(),),
            'The GCP network metavisors will be launched into')
        config.register_option(
            '%s.subnetwork' % (self.name(),),
            'The GCP subnetwork metavisors will be launched into')
        config.register_option(
            '%s.zone' % (self.name(),),
            'The GCP zone metavisors will be launched into')

    def register(self, subparsers, parsed_config):
        self.config = parsed_config

        gcp_parser = subparsers.add_parser(
            self.name(),
            description='GCP operations',
            help='GCP operations'
        )

        gcp_subparsers = gcp_parser.add_subparsers(
            dest='gcp_subcommand'
        )

        encrypt_gcp_image_parser = gcp_subparsers.add_parser(
            'encrypt',
            description='Create an encrypted GCP image from an existing image',
            help='Encrypt a GCP image',
            formatter_class=brkt_cli.SortingHelpFormatter
        )
        encrypt_gcp_image_args.setup_encrypt_gcp_image_args(
            encrypt_gcp_image_parser, parsed_config)
        setup_instance_config_args(encrypt_gcp_image_parser, parsed_config)

        update_gcp_image_parser = gcp_subparsers.add_parser(
            'update',
            description=(
                'Update an encrypted GCP image with the latest Metavisor '
                'release'),
            help='Update an encrypted GCP image',
            formatter_class=brkt_cli.SortingHelpFormatter
        )
        update_encrypted_gcp_image_args.setup_update_gcp_image_args(
            update_gcp_image_parser, parsed_config)
        setup_instance_config_args(update_gcp_image_parser, parsed_config)

        launch_gcp_image_parser = gcp_subparsers.add_parser(
            'launch',
            description='Launch a GCP image',
            help='Launch a GCP image',
            formatter_class=brkt_cli.SortingHelpFormatter
        )
        launch_gcp_image_args.setup_launch_gcp_image_args(
            launch_gcp_image_parser, parsed_config)
        setup_instance_config_args(launch_gcp_image_parser, parsed_config,
                                   mode=INSTANCE_METAVISOR_MODE)

        wrap_image_parser = gcp_subparsers.add_parser(
            'wrap-guest-image',
            description=(
                'Launch guest image wrapped with Bracket Metavisor'
            ),
            help='Launch guest image wrapped with Bracket Metavisor',
            formatter_class=brkt_cli.SortingHelpFormatter
        )
        wrap_gcp_image_args.setup_wrap_gcp_image_args(
            wrap_image_parser, parsed_config)
        setup_instance_config_args(wrap_image_parser, parsed_config,
                                   mode=INSTANCE_METAVISOR_MODE)

        share_logs_parser = gcp_subparsers.add_parser(
            'share-logs',
            description='Creates a logs file of an instance and uploads it '
                        'to a google bucket',
            help='Upload logs file to google bucket',
            formatter_class=brkt_cli.SortingHelpFormatter
        )
        share_logs_gcp_args.setup_share_logs_gcp_args(share_logs_parser)
        setup_instance_config_args(
            share_logs_parser, parsed_config, mode=INSTANCE_METAVISOR_MODE)

    def debug_log_to_temp_file(self, values):
        return True

    def run(self, values):
        if values.gcp_subcommand == 'encrypt':
            return run_encrypt(values, self.config)
        if values.gcp_subcommand == 'update':
            return run_update(values, self.config)
        if values.gcp_subcommand == 'launch':
            return run_launch(values, self.config)
        if values.gcp_subcommand == 'share-logs':
            return run_sharelogs(values, self.config)
        if values.gcp_subcommand == 'wrap-guest-image':
            return run_wrap_image(values, self.config)


def get_subcommands():
    return [GCPSubcommand()]


def check_args(values, gcp_svc, cli_config):
    if values.encryptor_image:
        if values.bucket != 'prod':
            raise ValidationError(
                "Please provide either an encryptor image or an image bucket")
    # Verify we have a valid launch token
    instance_config_args.get_launch_token(values, cli_config)

    if values.validate:
        if not gcp_svc.project_exists(values.project):
            raise ValidationError(
                "Project provider either does not exist or you do not have access to it")
        if not gcp_svc.network_exists(values.network):
            raise ValidationError("Network provided does not exist")
        brkt_env = brkt_cli.brkt_env_from_values(values)
        if brkt_env is None:
            _, brkt_env = cli_config.get_current_env()


def validate_tags(tags):
    for tag in tags:
        gcp_service.validate_image_name(tag)
