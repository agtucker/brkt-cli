#!/usr/bin/env python

import httplib
import logging
import socket

from brkt_cli.encryptor_service import (
    ENCRYPTOR_STATUS_PORT,
    wait_for_encryption,
    wait_for_encryptor_up
)
from brkt_cli.gcp.gcp_service import gcp_metadata_from_userdata
from brkt_cli.util import Deadline, retry, append_suffix
from googleapiclient import errors
from brkt_cli.util import CRYPTO_XTS


log = logging.getLogger(__name__)


def setup_encryption(gcp_svc,
                     image_id,
                     encrypted_image_disk,
                     instance_name,
                     zone,
                     image_project,
                     crypto_policy):
    ''' Add 6GB to the size of the guest root disk so that we can combine
        the metavisor disk contents with the guest disk contents and
        produce an "encapsulated" guest root disk. 6GB is the current
        metavisor disk size. It should not be hardcoded.
    '''
    try:
        # create disk from guest image
        gcp_svc.disk_from_image(zone, image_id, instance_name, image_project)
        log.info('Waiting for guest root disk to become ready')
        gcp_svc.wait_for_detach(zone, instance_name)

        guest_size = gcp_svc.get_disk_size(zone, instance_name)
        # create blank disk. the encrypted image will be
        # dd'd to this disk. Blank disk should be 2x the size
        # of the unencrypted guest root (GCM)
        log.info('Creating disk for encrypted image')
        if crypto_policy == CRYPTO_XTS:
            gcp_svc.create_disk(zone, encrypted_image_disk, guest_size + 6)
        else:
            gcp_svc.create_disk(zone, encrypted_image_disk, guest_size * 2 + 6)
        #
        # Amazing but true - just attach a big drive to get more IOPS
        # to be shared by all volumes attached to this VM. We don't
        # use the dummy-iops drive but get to use it's IOPS for other
        # drives.
        #
        log.info('Creating dummy IOPS disk')
        dummy_name = append_suffix(encrypted_image_disk, "-dummy-iops", 64)
        gcp_svc.create_disk(zone, dummy_name, 500)
    except:
        log.info('Encryption setup failed')
        raise


def do_encryption(gcp_svc,
                  enc_svc_cls,
                  zone,
                  encryptor,
                  encryptor_image,
                  instance_name,
                  instance_config,
                  encrypted_image_disk,
                  crypto_policy,
                  network,
                  subnetwork,
                  status_port=ENCRYPTOR_STATUS_PORT,
                  gcp_tags=None):
    instance_config.brkt_config['crypto_policy_type'] = crypto_policy
    metadata = gcp_metadata_from_userdata(instance_config.make_userdata())
    log.info('Launching encryptor instance')
    dummy_name = append_suffix(encrypted_image_disk, "-dummy-iops", 64)
    gcp_svc.run_instance(zone=zone,
                         name=encryptor,
                         image=encryptor_image,
                         network=network,
                         subnet=subnetwork,
                         disks=[gcp_svc.get_disk(zone, instance_name),
                                gcp_svc.get_disk(zone, encrypted_image_disk),
                                gcp_svc.get_disk(zone, dummy_name)],
                         delete_boot=False,
                         metadata=metadata,
                         tags=gcp_tags)

    try:
        host_ips = []
        ip = gcp_svc.get_instance_ip(encryptor, zone)
        if ip:
            host_ips.append(ip)
        pvt_ip = gcp_svc.get_private_ip(encryptor, zone)
        if pvt_ip:
            host_ips.append(pvt_ip)
        enc_svc = enc_svc_cls(host_ips, port=status_port)
        wait_for_encryptor_up(enc_svc, Deadline(600))
        log.info(
            'Waiting for encryption service on %s (%s:%s)',
            encryptor, ip, enc_svc.port
        )
        wait_for_encryption(enc_svc)
    except:
        f = gcp_svc.write_serial_console_file(zone, encryptor)
        if f:
            log.info('Encryption failed. Writing console to %s' % f)
        raise
    retry(function=gcp_svc.delete_instance, on=[httplib.BadStatusLine,
          socket.error, errors.HttpError])(zone, encryptor)


def create_image(gcp_svc, zone, encrypted_image_disk, encrypted_image_name,
                 encryptor):
    try:
        # create image from encrypted image disk
        gcp_svc.wait_for_detach(zone, encrypted_image_disk)
        log.info("Creating bracketized guest image")
        gcp_svc.create_gcp_image_from_disk(zone, encrypted_image_name,
                                           encrypted_image_disk)
        gcp_svc.wait_image(encrypted_image_name)
        log.info("Image %s successfully created!", encrypted_image_name)
    except:
        log.info('Image creation failed.')
        raise


def encrypt(gcp_svc, enc_svc_cls, image_id, encryptor_image,
            encrypted_image_name, zone, instance_config, crypto_policy,
            image_project=None, keep_encryptor=False, image_file=None,
            image_bucket=None, network=None, subnetwork=None,
            status_port=ENCRYPTOR_STATUS_PORT, cleanup=True, gcp_tags=None):
    try:
        # create metavisor image from file in GCS bucket
        log.info('Retrieving encryptor image from GCS bucket')
        if not encryptor_image:
            try:
                _ = gcp_svc.get_latest_encryptor_image(zone, image_bucket,
                                                       image_file=image_file)
                encryptor_image = _
            except errors.HttpError as e:
                encryptor_image = None
                log.exception('GCP API call to create image from file failed')
                return
        else:
            # Keep user provided encryptor image
            keep_encryptor = True

        # For GCP there is no way to identify the encryptor type
        # Default to XTS
        if crypto_policy is None:
            crypto_policy = CRYPTO_XTS

        instance_name = 'brkt-guest-' + gcp_svc.get_session_id()
        encryptor = instance_name + '-encryptor'
        encrypted_image_disk = 'encrypted-image-' + gcp_svc.get_session_id()

        # create guest root disk and blank disk to dd to
        setup_encryption(gcp_svc, image_id, encrypted_image_disk,
                         instance_name, zone, image_project, crypto_policy)

        # run encryptor instance with avatar_creator as root,
        # customer image and blank disk
        do_encryption(gcp_svc, enc_svc_cls, zone, encryptor, encryptor_image,
                      instance_name, instance_config, encrypted_image_disk,
                      crypto_policy, network, subnetwork,
                      status_port=status_port, gcp_tags=gcp_tags)

        # create image
        create_image(gcp_svc, zone, encrypted_image_disk, encrypted_image_name,
                     encryptor)

        return encrypted_image_name
    except errors.HttpError as e:
        log.exception('GCP API request failed: {}'.format(e.message))
    finally:
        if not cleanup:
            log.info("Not cleaning up")
            return
        log.info("Cleaning up")
        gcp_svc.cleanup(zone, encryptor_image, keep_encryptor)
