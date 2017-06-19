# GCP operations

The `gcp` subcommand provides all GCP related operations for encrypting, updating and launching images.

```
$ brkt gcp --help
usage: brkt gcp [-h] {encrypt,update,launch,wrap-guest-image,share-logs} ...

GCP operations

positional arguments:
  {encrypt,update,launch,wrap-guest-image,share-logs}
    encrypt             Encrypt a GCP image
    update              Update an encrypted GCP image
    launch              Launch a GCP image
    wrap-guest-image    Launch guest image wrapped with Bracket Metavisor
    share-logs          Upload logs file to google bucket

optional arguments:
  -h, --help            show this help message and exit
```

# Encrypting images in GCP

The `gcp encrypt` subcommand performs the following steps to create an
encrypted image:

1. Get the latest Metavisor image named `latest.image.tar.gz` from 
Google Cloud Storage
1. Create an encryptor image locally from the latest Metavisor image
1. Launch an instance based on the unencrypted image. We call this the
guest instance.
1. Snapshot the root volume of the guest instance.
1. Launch a Bracket Encryptor instance based on the locally created
encryptor image
1. Attach the unencrypted guest root volume to the Bracket Encryptor instance
1. Copy the unencrypted root volume to a new, encrypted volume
1. Create a new image based on the encryptor image
1. Create a snapshot of the encrypted guest root volume with the corresponding
name
1. Print the new encrypted image name

# Networking requirements

The following connections are established during image encryption:

* **brkt-cli** downloads `http://storage.googleapis.com/brkt-prod-images/latest.image.tar.gz`
* **brkt-cli** gets encryption status from the Encryptor instance on port 80. 
The port number can be overridden with the --status-port flag.
* The Encryptor talks to the Bracket service at `yetiapi.mgmt.brkt.com`. In 
order to do this, port 443 must be accessible on the following hosts:
  * 52.32.38.106
  * 52.35.101.76
  * 52.88.55.6
* brkt-cli talks to `api.mgmt.brkt.com` on port 443.

# Encrypting a GCP image

Run **gcp encrypt** to create an encrypted image based on an existing
image.

```
$ brkt gcp encrypt --help
usage: brkt gcp encrypt [-h] [--encrypted-image-name NAME] --zone ZONE
                        [--no-validate] --project PROJECT
                        [--image-project NAME] [--network NETWORK]
                        [--subnetwork SUBNETWORK] [--gcp-tag VALUE]
                        [--ntp-server DNS_NAME]
                        [--proxy HOST:PORT | --proxy-config-file PATH]
                        [--status-port PORT] [--ca-cert PATH]
                        [--token TOKEN | --brkt-tag NAME=VALUE]
                        ID

Create an encrypted GCP image from an existing image

positional arguments:
  ID                    The image that will be encrypted

optional arguments:
  --brkt-tag NAME=VALUE
                        Bracket tag which will be embedded in the JWT as a
                        claim. All characters must be alphanumeric or [-_.].
                        The tag name cannot be a JWT registered claim name
                        (see RFC 7519).
  --ca-cert PATH        Certificate that Metavisor uses to communicate with a
                        Customer Managed MCP.
  --encrypted-image-name NAME
                        Specify the name of the generated encrypted image
                        (default: None)
  --gcp-tag             Set a GCP tag on the encryptor instance. May be
                        specified multiple times.
  --image-project NAME  GCP project name which owns the image (e.g. centos-
                        cloud) (default: None)
  --network NETWORK
  --no-validate         Don't validate images or token (default: True)
  --ntp-server DNS_NAME
                        Optional NTP server to sync Metavisor clock. May be
                        specified multiple times. (default: None)
  --project PROJECT     GCP project name (default: None)
  --proxy HOST:PORT     Use this HTTPS proxy during encryption. May be
                        specified multiple times. (default: None)
  --proxy-config-file PATH
                        Path to proxy.yaml file that will be used during
                        encryption (default: None)
  --status-port PORT    Specify the port to receive http status of encryptor.
                        Any port in range 1-65535 can be used except for port
                        81. (default: 80)
  --subnetwork SUBNETWORK
  --token TOKEN         Token that the encrypted instance will use to
                        authenticate with the Bracket service. Use the make-
                        token subcommand to generate a token. (default: None)
  --zone ZONE           GCP zone to operate in (default: None)
  -h, --help            show this help message and exit
```

  The `gcp update` subcommand updates an encrypted
image with the latest version of the Metavisor code.

```
$ brkt gcp update --help
usage: brkt gcp update [-h] [--encrypted-image-name NAME] --zone ZONE
                       --project PROJECT [--no-validate] [--network NETWORK]
                       [--subnetwork SUBNETWORK] [--gcp-tag VALUE]
                       [--ntp-server DNS_NAME]
                       [--proxy HOST:PORT | --proxy-config-file PATH]
                       [--status-port PORT] [--ca-cert PATH]
                       [--token TOKEN | --brkt-tag NAME=VALUE]
                       ID

Update an encrypted GCP image with the latest Metavisor release

positional arguments:
  ID                    The image that will be updated

optional arguments:
  --brkt-tag NAME=VALUE
                        Bracket tag which will be embedded in the JWT as a
                        claim. All characters must be alphanumeric or [-_.].
                        The tag name cannot be a JWT registered claim name
                        (see RFC 7519).
  --ca-cert PATH        Certificate that Metavisor uses to communicate with a
                        Customer Managed MCP.
  --encrypted-image-name NAME
                        Specify the name of the generated encrypted Image
                        (default: None)
  --gcp-tag             Set a GCP tag on the updater instance. May be
                        specified multiple times.
  --network NETWORK
  --no-validate         Don't validate images or token (default: True)
  --ntp-server DNS Name
                        Optional NTP server to sync Metavisor clock. May be
                        specified multiple times. (default: None)
  --project PROJECT     GCP project name (default: None)
  --proxy HOST:PORT     Use this HTTPS proxy during encryption. May be
                        specified multiple times. (default: None)
  --proxy-config-file PATH
                        Path to proxy.yaml file that will be used during
                        encryption (default: None)
  --status-port PORT    Specify the port to receive http status of encryptor.
                        Any port in range 1-65535 can be used except for port
                        81. (default: 80)
  --subnetwork SUBNETWORK
  --token TOKEN         Token that the encrypted instance will use to
                        authenticate with the Bracket service. Use the make-
                        token subcommand to generate a token. (default: None)
  --zone ZONE           GCP zone to operate in (default: us-central1-a)
  -h, --help            show this help message and exit
```

The `gcp launch` subcommand launches an encrypted GCP image.

```
$ brkt gcp launch --help
usage: brkt gcp launch [-h] [--instance-name NAME]
                       [--instance-type INSTANCE_TYPE] [--zone ZONE]
                       [--no-delete-boot] --project PROJECT
                       [--network NETWORK] [--gcp-tag VALUE]
                       [--subnetwork NAME] [--ssd-scracth-disks N]
                       [--ntp-server DNS_NAME]
                       [--proxy HOST:PORT | --proxy-config-file PATH]
                       [--ca-cert PATH]
                       [--token TOKEN | --brkt-tag NAME=VALUE]
                       ID

Launch a GCP image

positional arguments:
  ID                    The image that will be launched

optional arguments:
  --brkt-tag NAME=VALUE
                        Bracket tag which will be embedded in the JWT as a
                        claim. All characters must be alphanumeric or [-_.].
                        The tag name cannot be a JWT registered claim name
                        (see RFC 7519).
  --ca-cert PATH        Certificate that Metavisor uses to communicate with a
                        Customer Managed MCP.
  --gcp-tag             Set a GCP tag on the encrypted instance being
                        launched. May be specified multiple times.
  --instance-name NAME  Name of the instance
  --instance-type INSTANCE_TYPE
                        Instance type (default: n1-standard-1)
  --network NETWORK
  --no-delete-boot      Delete boot disk when instance is deleted (default:
                        False)
  --ntp-server DNS_NAME
                        Optional NTP server to sync Metavisor clock. May be
                        specified multiple times. (default: None)
  --project PROJECT     GCP project name (default: None)
  --proxy HOST:PORT     Use this HTTPS proxy during encryption. May be
                        specified multiple times. (default: None)
  --proxy-config-file PATH
                        Path to proxy.yaml file that will be used during
                        encryption (default: None)
  --ssd-scratch-disks N
                        Number of SSD scratch disks to be attached (max. 8)
                        (default: 0)
  --subnetwork NAME     Launch instance in this subnetwork (default: None)
  --token TOKEN         Token that the encrypted instance will use to
                        authenticate with the Bracket service. Use the make-
                        token subcommand to generate a token. (default: None)
  --zone ZONE           GCP zone to operate in (default: us-central1-a)
  -h, --help            show this help message and exit
```

The `gcp wrap-guest-image` subcommand launches an encrypted GCP instance
without the guest root volume being encrypted.

```
$ brkt gcp launch --help
usage: brkt gcp launch [-h] [--instance-name NAME]
                       [--instance-type INSTANCE_TYPE] --zone ZONE
                       [--no-delete-boot] --project PROJECT
                       [--image-project NAME] [--network NETWORK]
                       [--gcp-tag VALUE] [--subnetwork NAME]
                       [--ssd-scracth-disks N]
                       [--ntp-server DNS_NAME]
                       [--proxy HOST:PORT | --proxy-config-file PATH]
                       [--ca-cert PATH]
                       [--token TOKEN | --brkt-tag NAME=VALUE]
                       ID

Launch guest image wrapped with Bracket Metavisor

positional arguments:
  ID                    The image that will be wrapped with the Bracket
                        Metavisor

optional arguments:
  --brkt-tag NAME=VALUE
                        Bracket tag which will be embedded in the JWT as a
                        claim. All characters must be alphanumeric or [-_.]
                        The tag name cannot be a JWT registered claim name
                        (see RFC 7519).
  --ca-cert PATH        Certificate that Metavisor uses to communicate with a
                        Customer Managed MCP.
  --gcp-tag             Set a GCP tag on the encrypted instance being
                        launched. May be specified multiple times.
  --image-project NAME  GCP project name which owns the image (e.g. centos-
                        cloud)
  --instance-name NAME  Name of the instance
  --instance-type INSTANCE_TYPE
                        Instance type (default: n1-standard-1)
  --network NETWORK
  --no-delete-boot      Delete boot disk when instance is deleted (default:
                        False)
  --ntp-server DNS_NAME
                        Optional NTP server to sync Metavisor clock. May be
                        specified multiple times. (default: None)
  --project PROJECT     GCP project name (default: None)
  --proxy HOST:PORT     Use this HTTPS proxy during encryption. May be
                        specified multiple times. (default: None)
  --proxy-config-file PATH
                        Path to proxy.yaml file that will be used during
                        encryption (default: None)
  --ssd-scratch-disks N
                        Number of SSD scratch disks to be attached (max. 8)
                        (default: 0)
  --subnetwork NAME     Launch instance in this subnetwork (default: None)
  --token TOKEN         Token that the encrypted instance will use to
                        authenticate with the Bracket service. Use the make-
                        token subcommand to generate a token. (default: None)
  --zone ZONE           GCP zone to operate in (default: us-central1-a)
  -h, --help            show this help message and exit
```

## Configuration

Before running the GCP commands in **brkt-cli**, you will  need to install
[gcloud](https://cloud.google.com/sdk/gcloud/) and configure it to work 
with your Google account and GCP project. Make sure that your Google 
account has `Editor` permissions within the selected Google project.

You can use the `--network` option to launch the encryptor instance in
a specific GCP network. Additionally if you created this network using
custom subnetworks, then you **must** specify the corresponding subnetwork
using the `--subnetwork` option. In the absence of the `--network` option,
the encryptor is launched in the `default` GCP network.

You will also need to add a firewall rule that allows inbound access
to the Encryptor or Updater instance on port **80**, or the port that
you specify with the **--status-port** option in the default network
(or in the network where you launch the encryptor instance).

## Encrypting an image

Run **gcp encrypt** to encrypt an image:

```
$ brkt gcp encrypt --zone us-central1-a --project brkt-dev --brkt-tag env=prod --image-project ubuntu-os-cloud ubuntu-1404-trusty-v20160627
...
14:30:23 Starting encryptor session 59e3b3a7
...
14:55:18 Encryption is 99% complete
14:55:28 Encrypted root drive created.
14:55:29 Creating snapshot of encrypted image disk
14:56:21 Disk detach successful
14:56:21 Creating metavisor image
14:58:25 Image ubuntu-1404-trusty-v20160627-encrypted-a1fe1069 successfully created!
14:58:25 Cleaning up
14:58:25 deleting disk brkt-guest-59e3b3a7
14:58:25 Disk detach successful
14:58:26 deleting disk encrypted-image-59e3b3a7
14:58:26 Disk detach successful
14:58:27 deleting disk brkt-guest-59e3b3a7-encryptor
14:58:27 Disk detach successful
14:58:28 Deleting encryptor image encryptor-59e3b3a7
ubuntu-1404-trusty-v20160627-encrypted-a1fe1069
```

## Updating an image

Run **gcp update** to update an encrypted image with the latest
Metavisor code:

```
$ brkt gcp update --zone us-central1-a --project brkt-dev --brkt-tag env=prod ubuntu-1404-trusty-v20160627-encrypted-ee521b31
...
15:50:04 Starting updater session 80985e58
...
15:55:02 Encrypted root drive created.
15:55:02 Deleting updater instance
15:55:54 Disk detach successful
15:55:54 Creating updated metavisor image
15:56:45 deleting disk brkt-updater-80985e58-guest
15:56:46 Disk detach successful
15:56:46 deleting disk brkt-updater-80985e58-metavisor
15:56:47 Disk detach successful
15:56:47 Deleting encryptor image encryptor-80985e58
ubuntu-1404-trusty-v20160627-encrypted-63e57e6e
```

## Launching an image

Run **gcp launch** to launch an encrypted GCP image

```
$ brkt gcp launch --instance-name brkt-test-instance --project <project> --brkt-tag env=prod --zone us-central1-c centos-6-v20160921-encrypted-30fccdeb
18:13:54 Creating guest root disk from snapshot
18:13:54 Attempting refresh to obtain initial access_token
18:13:54 Refreshing access_token
18:13:55 Waiting for disk to become ready
18:14:05 Waiting for disk to become ready
18:14:15 Waiting for disk to become ready
18:14:26 Waiting for disk to become ready
18:14:36 Waiting for disk to become ready
18:14:46 Waiting for disk to become ready
18:14:56 Waiting for disk to become ready
18:14:56 Starting instance
18:14:58 Waiting for brkt-test-instance to become ready
18:15:03 Waiting for brkt-test-instance to become ready
...
18:15:59 Waiting for brkt-test-instance to become ready
18:16:10 Instance brkt-test-instance (104.198.44.8) launched successfully
brkt-test-instance
```

## Launching a wrapped instance

Run **gcp wrap-guest-image** to launch a guest image wrapped with the Bracket
Metavisor

```
$ brkt gcp wrap-guest-image --project <project> --brkt-tag env=prod --zone us-central1-c centos-6-v20170327
19:44:47 Retrieving encryptor image from GCP bucket
19:44:47 Attempting refresh to obtain initial access_token
19:44:47 Refreshing access_token
19:46:40 Waiting for guest root disk to become ready
19:46:40 Disk detach successful
19:46:40 Launching wrapped guest image
19:46:46 Waiting for brkt-guest-e37b3894 to become ready
19:46:51 Waiting for brkt-guest-e37b3894 to become ready
19:46:57 Waiting for brkt-guest-e37b3894 to become ready
19:47:02 Waiting for brkt-guest-e37b3894 to become ready
19:47:07 Waiting for brkt-guest-e37b3894 to become ready
19:47:15 Waiting for brkt-guest-e37b3894 to become ready
19:47:20 Waiting for brkt-guest-e37b3894 to become ready
19:47:25 Waiting for brkt-guest-e37b3894 to become ready
19:47:31 Waiting for brkt-guest-e37b3894 to become ready
19:47:36 Waiting for brkt-guest-e37b3894 to become ready
19:47:42 Waiting for brkt-guest-e37b3894 to become ready
19:47:48 Waiting for brkt-guest-e37b3894 to become ready
19:48:00 Instance brkt-guest-e37b3894 (35.184.91.87) launched successfully
19:48:00 Deleting encryptor image encryptor-e37b3894
brkt-guest-e37b3894
```
