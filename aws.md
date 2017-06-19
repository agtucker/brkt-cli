# AWS operations

The `aws` subcommand provides all AWS related operations for encrypting and updating images.

```
$ brkt aws --help
usage: brkt aws [-h] {encrypt,update,wrap-guest-image} ...

AWS operations

positional arguments:
  {encrypt,update,wrap-guest-image}
    encrypt             Encrypt an AWS image
    update              Update an encrypted AWS image
    wrap-guest-image    Launch guest image wrapped with Bracket Metavisor

optional arguments:
 -h, --help            show this help message and exit
```

## Encrypting images in AWS

The `aws encrypt` subcommand performs the following steps to create an
encrypted image:

1. Get the latest Metavisor AMI ID from `hvm_amis.json`, stored in S3.
1. Launch an instance based on an unencrypted AMI.  We call this
the guest instance.
1. Snapshot the root volume of the guest instance.
1. Launch a Bracket Encryptor instance.
1. Attach the unencrypted guest root volume to the Bracket Encryptor instance.
1. Copy the unencrypted root volume to a new, encrypted volume.
1. Create a new AMI based on the encrypted root volume and other volumes
required by the Metavisor at runtime.
1. Print the new AMI ID.

## Networking requirements

The following network connections are established during image encryption:

* **brkt-cli** downloads `https://solo-brkt-prod-net.s3.amazonaws.com/hvm_amis.json`.
* **brkt-cli** gets encryption status from the Encryptor instance on port 80.
The port number can be overridden with the --status-port flag.
* The Encryptor talks to the Bracket service at `yetiapi.mgmt.brkt.com`.  In
order to do this, port 443 must be accessible on the following hosts:
  * 52.32.38.106
  * 52.35.101.76
  * 52.88.55.6
* **brkt-cli** talks to `api.mgmt.brkt.com` on port 443.

## Encrypting an AMI

Run **brkt aws encrypt** to create a new encrypted AMI based on an existing
image:

```
$ brkt aws encrypt --region us-east-1 --brkt-tag env=prod ami-76e27e1e
15:28:37 Starting encryptor session caabe51a
15:28:38 Launching instance i-703f4c99 to snapshot root disk for ami-76e27e1e
...
15:57:11 Created encrypted AMI ami-07c2a262 based on ami-76e27e1e
15:57:11 Terminating encryptor instance i-753e4d9c
15:57:12 Deleting snapshot copy of original root volume snap-847da3e1
15:57:12 Done.
ami-07c2a262
```

When the process completes, the new AMI id is written to stdout.  Log
messages are written to stderr.

## Updating an encrypted AMI

Run **brkt aws update** to update an encrypted AMI based on an existing
encrypted image:

```
$ brkt aws update --region us-east-1 --brkt-tag env=prod ami-72094e18
13:38:14 Using zone us-east-1a
13:38:15 Updating ami-72094e18
13:38:15 Creating guest volume snapshot
...
13:39:25 Encrypted root drive created.
...
13:39:28 waiting for snapshot ready
13:39:48 metavisor updater snapshots ready
...
13:39:54 Created encrypted AMI ami-63733e09 based on ami-72094e18
13:39:54 Done.
ami-63733e09
```

When the process completes, the new AMI id is written to stdout.  Log
messages are written to stderr.

## Wrapping guest AMI

Run **brkt aws wrap-guest-image** to wrap a guest image with a Bracket
image. This generates an encrypted instance, without the guest root
volume being encrypted.

```
$ brkt aws wrap-guest-image --region us-east-1 --brkt-tag env=prod ami-72094e18
18:50:33 Created security group with id sg-d821d2a3
18:50:34 Launching wrapped guest instance i-039dba15316de37a0
18:50:53 Done.
Instance:i-039dba15316de37a0
```

When the process completes, it leaves a Bracket instance running with the
guest root image attached.

## Configuration

Before running the **brkt** command, make sure that you've set your AWS
environment variables:

```
$ export AWS_ACCESS_KEY_ID=<access key>
$ export AWS_SECRET_ACCESS_KEY=<secret key>
```

You'll also need to make sure that your AWS account has the required
permissions, such as running an instance, describing an image, and
creating snapshots.  See [brkt-cli-iam-permissions.json](https://github.com/brkt/brkt-cli/blob/master/reference_templates/brkt-cli-iam-permissions.json)
for the complete list of required permissions.

You can use the `--subnet` option to launch the Encryptor in a specific
VPC and subnet.  Without this option, the encryptor is launched in
your default VPC and subnet.

When launching the Encryptor or Updater instance, **brkt-cli** creates
a temporary security group that allows inbound access on port 80.
Alternately, you can use the `--security-group` option to specify one
or more existing security groups.

If both `--subnet` and `--security-group` are specified, they must
be in the same VPC.

## Usage
```
$ brkt aws encrypt --help
usage: brkt aws encrypt [-h] [--encrypted-ami-name NAME]
                        [--guest-instance-type TYPE] [--no-validate] --region
                        NAME [--security-group ID] [--subnet ID]
                        [--aws-tag KEY=VALUE] [--metavisor-version NAME]
                        [--ntp-server DNS_NAME]
                        [--proxy HOST:PORT | --proxy-config-file PATH]
                        [--status-port PORT] [--ca-cert PATH]
                        [--token TOKEN  | --brkt-tag NAME=VALUE]
                        ID

Create an encrypted AMI from an existing AMI.

positional arguments:
  ID                    The guest AMI that will be encrypted

optional arguments:
  --aws-tag KEY=VALUE   Set an AWS tag on resources created during encryption.
                        May be specified multiple times.
  --brkt-tag NAME=VALUE
                        Bracket tag which will be embedded in the JWT as a
                        claim. All characters must be alphanumeric or [-_.].
                        The tag name cannot be a JWT registered claim name
                        (see RFC 7519).
  --ca-cert PATH        Certificate that Metavisor uses to communicate with a
                        Customer Managed MCP.
  --encrypted-ami-name NAME
                        Specify the name of the generated encrypted AMI
  --guest-instance-type TYPE
                        The instance type to use when running the unencrypted
                        guest instance (default: m3.medium)
  --metavisor-version NAME
                        Metavisor version [e.g 1.2.12 ] (default: latest)
  --no-validate         Don't validate AMIs, subnet, and security groups
  --ntp-server DNS_NAME
                        NTP server to sync Metavisor clock. May be specified
                        multiple times.
  --proxy HOST:PORT     Proxy that Metavisor uses to talk to the Bracket
                        service
  --proxy-config-file PATH
                        proxy.yaml file that defines the proxy configuration
                        that metavisor uses to talk to the Bracket service
  --region NAME         The AWS region metavisors will be launched into
  --security-group ID   Use this security group when running the encryptor
                        instance. May be specified multiple times.
  --status-port PORT    Specify the port to receive http status of encryptor.
                        Any port in range 1-65535 can be used except for port
                        81. (default: 80)
  --subnet ID           Launch instances in this subnet
  --token TOKEN         Token (JWT) that Metavisor uses to authenticate with
                        the Bracket service. Use the make-token subcommand to
                        generate a token.
  -h, --help            show this help message and exit
```

The `aws update` subcommand updates an encrypted AMI with the latest
version of the Metavisor code.

```
usage: brkt aws update [-h] [--encrypted-ami-name NAME]
                       [--guest-instance-type TYPE]
                       [--updater-instance-type TYPE] [--no-validate] --region
                       NAME [--security-group ID] [--subnet ID]
                       [--aws-tag KEY=VALUE] [--metavisor-version NAME]
                       [--ntp-server DNS_NAME]
                       [--proxy HOST:PORT | --proxy-config-file PATH]
                       [--status-port PORT] [--ca-cert PATH]
                       [--token TOKEN | --brkt-tag NAME=VALUE]
                       ID

Update an encrypted AMI with the latest Metavisor release.

positional arguments:
  ID                    The encrypted AMI that will be updated

optional arguments:
  --aws-tag KEY=VALUE   Set an AWS tag on resources created during update. May
                        be specified multiple times.
  --brkt-tag NAME=VALUE
                        Bracket tag which will be embedded in the JWT as a
                        claim. All characters must be alphanumeric or [-_.].
                        The tag name cannot be a JWT registered claim name
                        (see RFC 7519).
  --ca-cert PATH        Certificate that Metavisor uses to communicate with a
                        Customer Managed MCP.
  --encrypted-ami-name NAME
                        Specify the name of the generated encrypted AMI
  --guest-instance-type TYPE
                        The instance type to use when running the encrypted
                        guest instance. Default: m3.medium (default:
                        m3.medium)
  --metavisor-version NAME
                        Metavisor version [e.g 1.2.12 ] (default: latest)
  --no-validate         Don't validate AMIs, subnet, and security groups
  --ntp-server DNS_NAME
                        NTP server to sync Metavisor clock. May be specified
                        multiple times.
  --proxy HOST:PORT     Proxy that Metavisor uses to talk to the Bracket
                        service
  --proxy-config-file PATH
                        proxy.yaml file that defines the proxy configuration
                        that metavisor uses to talk to the Bracket service
  --region NAME         The AWS region metavisors will be launched into
  --security-group ID   Use this security group when running the encryptor
                        instance. May be specified multiple times.
  --status-port PORT    Specify the port to receive http status of encryptor.
                        Any port in range 1-65535 can be used except for port
                        81. (default: 80)
  --subnet ID           Launch instances in this subnet
  --token TOKEN         Token (JWT) that Metavisor uses to authenticate with
                        the Bracket service. Use the make-token subcommand to
                        generate a token.
  --updater-instance-type TYPE
                        The instance type to use when running the updater
                        instance. Default: m3.medium (default: m3.medium)
  -h, --help            show this help message and exit
```

The `aws wrap-guest-image` subcommand creates an encrypted instance with the latest
version of the Metavisor code.

```
usage: brkt aws wrap-guest-image [-h] [--wrapped-instance-name NAME]
                                 [--instance-type TYPE] [--no-validate]
                                 --region NAME [--security-group ID]
                                 [--subnet ID] [--aws-tag KEY=VALUE]
                                 [--metavisor-version NAME] [--key NAME]
                                 [--iam ROLE] [--ntp-server DNS_NAME]
                                 [--proxy HOST:PORT | --proxy-config-file PATH]
                                 [--status-port PORT] [--ca-cert PATH]
                                 [--token TOKEN | --brkt-tag NAME=VALUE]
                                 ID

Launch guest image wrapped with Bracket Metavso

positional arguments:
  ID                    The guest AMI that will be launched as a wrapped
                        Bracket instance

optional arguments:
  -h, --help            show this help message and exit
  --wrapped-instance-name NAME
                        Specify the name of the wrapped Bracket instance
  --instance-type TYPE  The instance type to use when launching the wrapped
                        image
  --no-validate         Don't validate AMIs, snapshots, subnet, or security
                        groups
  --region NAME         The AWS region metavisors will be launched into
  --security-group ID   Use this security group when running the encryptor
                        instance. May be specified multiple times.
  --subnet ID           Launch instances in this subnet
  --aws-tag KEY=VALUE   Set an AWS tag on resources created during update. May
                        be specified multiple times.
  --metavisor-version NAME
                        Metavisor version [e.g 1.2.12 ] (default: latest)
  --key NAME            SSH key pair name
  --iam ROLE            The IAM role to use for the launched instance
  --ntp-server DNS_NAME
                        NTP server to sync Metavisor clock. May be specified
                        multiple times.
  --proxy HOST:PORT     Proxy that Metavisor uses to talk to the Bracket
                        service
  --proxy-config-file PATH
                        proxy.yaml file that defines the proxy configuration
                        that metavisor uses to talk to the Bracket service
  --status-port PORT    Specify the port to receive http status of encryptor.
                        Any port in range 1-65535 can be used except for port
                        81. (default: 80)
  --ca-cert PATH        Certificate that Metavisor uses to communicate with a
                        Customer Managed MCP.
  --token TOKEN         Token (JWT) that Metavisor uses to authenticate with
                        the Bracket service. Use the make-token subcommand to
                        generate a token.
  --brkt-tag NAME=VALUE
                        Bracket tag which will be embedded in the JWT as a
                        claim. All characters must be alphanumeric or [-_.].
                        The tag name cannot be a JWT registered claim name
                        (see RFC 7519).
```
